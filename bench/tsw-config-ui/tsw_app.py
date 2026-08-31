#!/usr/bin/env python3
"""TSW202 configurator — bench Web UI (FastAPI).

Workflow (one switch at a time, nothing to type):
  1. Plug a TSW202 into the laptop (it boots at 192.168.1.2).
  2. The UI detects it, reads its LAN MAC over ARP, and asks for one thing —
     the factory label password, scanned off the sticker's QR code or typed.
  3. Submit -> full pipeline: set password -> firmware floor -> NTP
     192.168.88.10 -> timezone Asia/Jerusalem -> verify -> move the management
     IP to 192.168.88.2.
  4. Unplug it and plug in the next one.

Step 3's last move is the operator's to choose (TEC-848): the address picker
offers `fixed` (every switch to the same address — the long-standing
192.168.88.2), `manual` (type this one's) and `dhcp` (assign nothing and find
the switch again by MAC). No `cycle`: switches don't land several to a site.

The common bench-UI shell (run machinery, routes, WebSocket) lives in
bench_core.bench_ui; this file adds only the TSW202 specifics: the two-address
detection (probe the factory address AND the final management address, so an
already-provisioned switch can be re-run), a form with no site name (the switch
baseline does not name the device), and per-run JSON files named after the
serial rather than a hostname there isn't one of.
"""
from __future__ import annotations

import socket
from pathlib import Path
from typing import Optional, Union

from pydantic import BaseModel

from bench_core import DEFAULT_TIMEZONE, DEFAULT_USERNAME
from bench_core.bench_ui import (
    DETECT_TIMEOUT_SEC,
    TERMINAL_PHASES,
    BenchConfigurator,
    read_device_mac,
)
from bench_core.ip_mode import (
    MODE_DHCP,
    MODE_FIXED,
    MODE_MANUAL,
    IpModeError,
    IpModePolicy,
    split_ip,
)
from bench_core.run_record import build_run_entry

from tsw_configure import (
    DEFAULT_TSW_DHCP_SUBNETS,
    DEFAULT_TSW_GATEWAY,
    DEFAULT_TSW_HOST,
    DEFAULT_TSW_LAN_IP,
    DEFAULT_TSW_MIN_FIRMWARE,
    DEFAULT_TSW_NETMASK,
    DEFAULT_TSW_NTP_SERVER,
    EXPECTED_MODEL,
    TswClient,
    configure_tsw,
    verify_tsw,
)

BASE_DIR = Path(__file__).resolve().parent


class ConfigureBody(BaseModel):
    # No site name: nothing in the switch baseline is named after one, so
    # asking for it would be a field the operator fills in for nothing.
    initial_password: str = ""
    # Where this switch should end up. Blank `ip_mode` means "the mode the
    # picker is set to", which is what the page sends on the normal path;
    # `octet` carries a manual entry, as a last octet or a full address.
    ip_mode: str = ""
    octet: Optional[Union[int, str]] = None


class TswConfigurator(BenchConfigurator):
    title = "TSW202 Configurator"
    port = 8007
    html_file = "tsw.html"
    config_filename = "config/tsw.config.json"
    log_filename = "tsw-config.log"
    tailscale_label = "tsw"   # unused — the switch baseline has no Tailscale step
    history_limit = 30  # full step logs per entry — the JSON files are the archive
    label_scan_enabled = True  # read the factory password off the QR label (TEC-349)
    retain_label_password = True  # and keep it on bench-central (TEC-845)
    verify_supported = True    # mutation-free re-check of a finished switch (TEC-348)
    record_tool = "tsw"
    ip_modes_enabled = True    # fixed / manual / DHCP address picker (TEC-848)

    # ── state ────────────────────────────────────────────────────────────────

    def initial_state(self) -> dict:
        return {
            "phase": "waiting",        # waiting|detected|configuring|configured|
                                       # verifying|verified|error
            "detected": False,
            "active_host": None,       # the address the switch answered on
            "active_mac": None,
            "at_final_lan": False,     # detected on lan_ip -> probably already provisioned
            "busy": False,
            "message": "Plug in the first TSW202…",
            "last_result": None,
            "history": [],
        }

    def extra_public_state(self) -> dict:
        return {"ntp_server": self.cfg.get("ntp_server", DEFAULT_TSW_NTP_SERVER),
                "min_firmware": self._min_firmware(),
                "firmware_found": self._firmware_image_found(),
                "dhcp_subnets": self._dhcp_subnets()}

    # ── address modes (TEC-848) ───────────────────────────────────────────────

    def ip_mode_policy(self) -> IpModePolicy:
        """fixed / manual / DHCP, on the subnet the config's `lan_ip` names.

        No `cycle`: a site takes one switch, so there is no second address for a
        counter to move to, and offering the mode would mean nothing for the
        device in front of the operator.

        The range deliberately stays the whole /24 rather than something
        narrower around .2. The bench has no register of which addresses on a
        site's management subnet are free, so any bound this tool invented
        would be a guess that rejects legitimate entries — the checkable
        mistake is the wrong SUBNET, and `parse_octet` already catches that.
        """
        prefix, octet = split_ip(self._lan_ip() or DEFAULT_TSW_LAN_IP)
        return IpModePolicy(
            modes=(MODE_FIXED, MODE_MANUAL, MODE_DHCP),
            prefix=prefix or "192.168.88",
            default_fixed_octet=octet or 2,
            default_mode=self.cfg.get("default_ip_mode", MODE_FIXED),
        )

    def ip_mode_message(self) -> str:
        if self.ip_modes.mode == MODE_DHCP:
            return ("Switches will be left on DHCP — nothing is assigned, and "
                    "each one is found again by its MAC on "
                    f"{', '.join(self._dhcp_subnets())}.")
        return self.ip_modes.describe()

    # ── config helpers ────────────────────────────────────────────────────────

    def _dhcp_subnets(self) -> list[str]:
        return self.cfg.get("dhcp_subnets", DEFAULT_TSW_DHCP_SUBNETS)

    def _min_firmware(self) -> str:
        return ((self.cfg.get("firmware", {}) or {}).get("minimum_version")
                or DEFAULT_TSW_MIN_FIRMWARE)

    def _firmware_bin(self) -> Optional[Path]:
        rel = (self.cfg.get("firmware", {}) or {}).get("bin_path") or ""
        return self.base_dir / rel if rel else None

    def _firmware_image_found(self) -> bool:
        """Whether the local image is on disk. Only a switch that arrives BELOW
        the floor needs it, so a missing image is reported, not fatal — the page
        warns rather than blocking Configure."""
        image = self._firmware_bin()
        return bool(image and image.is_file())

    def _lan_ip(self) -> str:
        return self.cfg.get("lan_ip", DEFAULT_TSW_LAN_IP)

    def _factory_host(self) -> str:
        return self.cfg.get("host", DEFAULT_TSW_HOST)

    # ── pipeline hooks ─────────────────────────────────────────────────────────

    def hostname_for(self, inputs: dict) -> str:
        """A label for this run — NOT a hostname written to the device.

        The baseline doesn't name the switch, but the base class needs something
        to put in "Configuring …" before login, when the serial isn't known yet.
        The ARP MAC is the only identifier available that early.
        """
        mac = (inputs.get("mac") or "").replace(":", "").replace("-", "")
        return f"tsw-{mac[-4:].lower()}" if len(mac) >= 4 else "tsw-unknown"

    def client_host(self, inputs: dict) -> str:
        return inputs["host"]

    def build_client(self, run_cfg: dict, host: str):
        return TswClient(
            host=host,
            username=run_cfg.get("username", DEFAULT_USERNAME),
            scheme=run_cfg.get("scheme", "https"),
            verify=not run_cfg.get("insecure", True),
        )

    def run_pipeline(self, client, run_cfg: dict, inputs: dict) -> dict:
        return configure_tsw(client, initial_password=inputs["initial_password"],
                             settings=run_cfg,
                             target_ip=inputs.get("target_ip"),
                             ip_mode=inputs.get("ip_mode", ""),
                             mac=inputs.get("mac") or "")

    def verify_pipeline(self, client, run_cfg: dict, inputs: dict) -> dict:
        """Mutation-free re-check of a finished switch (TEC-348).

        The history lookup answers two things: whether this switch has a
        configure record at all — a unit nobody provisioned must not verify
        green — and, since TEC-848, which address that run chose for it, the
        one part of the baseline that is now per-unit.
        """
        return verify_tsw(client, settings=run_cfg,
                          resolve=self.verify_resolver(inputs),
                          target_ip=inputs.get("target_ip"),
                          ip_mode=inputs.get("ip_mode", ""))

    def build_entry(self, result: dict, inputs: dict, duration: int) -> dict:
        ident = result["identity"]
        return build_run_entry(
            tool="tsw",
            ok=result["ok"],
            error=result["error"],
            serial=ident.get("serial", "unknown"),
            mac=ident.get("mac", inputs.get("mac") or "unknown"),
            model=ident.get("model", EXPECTED_MODEL),
            firmware=ident.get("firmware", "unknown"),
            duration_s=duration,
            verification=result.get("verification", []),
            warnings=result.get("warnings", []),
            steps=result["steps"],
            log=result["log"],
            device={
                # No hostname: nothing in the pipeline sets one.
                # `ip` is what the bench ASSIGNED, so it is empty on a DHCP run
                # — the lease belongs to the site and recording it as ours
                # would be a claim the next reader (a QA label, bench-central)
                # would act on. `reached_at` carries where the switch actually
                # answered, and `ip_mode` says which of the two to trust.
                "ip": result.get("ip", ""),
                "ip_mode": result.get("ip_mode") or MODE_FIXED,
                "reached_at": result.get("reached_at", ""),
                "password_source": self.password_source(inputs, "initial_password"),
                "firmware_note": result.get("firmware_note", ""),
            },
        )

    def log_name_stem(self, entry: dict) -> Optional[str]:
        """Name the per-run JSON after the serial. The shared writer defaults to
        `device.hostname`, which this tool doesn't have — and the serial is the
        better handle anyway: it's what's on the sticker someone reads back."""
        return entry.get("serial")

    @staticmethod
    def _where(entry: dict) -> str:
        """How to say where a switch ended up, in one phrase. Under DHCP there
        is no assigned address to name, so the lease it was found on is the
        only useful answer — and it is labelled as a finding, not a promise."""
        device = entry.get("device", {})
        if device.get("ip"):
            return f"at {device['ip']}"
        found = device.get("reached_at")
        return f"on DHCP, currently at {found}" if found else "on DHCP"

    def success_message(self, result: dict, entry: dict, took: str) -> str:
        return (f"Configured TSW202 SN {entry['serial']} "
                f"{self._where(entry)} in {took}. "
                "Unplug it and plug in the next one.")

    def verify_message(self, result: dict, entry: dict, took: str) -> str:
        return (f"TSW202 SN {entry['serial']} {self._where(entry)} PASSED "
                f"verification in {took} — nothing was changed. "
                "Unplug it and plug in the next one.")

    def dismiss_message(self) -> str:
        return "Plug in the next TSW202…"

    # ── detection loop ──────────────────────────────────────────────────────────

    def _detect_host(self) -> Optional[str]:
        """First address a switch answers on: the factory IP (192.168.1.2 — the
        .2, not the routers' .1), then the management addresses an
        already-provisioned unit plugged back in might be on. None if neither.

        The picker's fixed address is probed as well as the config's `lan_ip`,
        since an operator who moved the fixed one is telling us where their
        finished switches now live. A unit left on DHCP or sent somewhere
        manual is not found here — there is nothing to guess — and is plugged
        in and re-run from its factory address like any other.
        """
        port = 443 if self.cfg.get("scheme", "https") == "https" else 80
        hosts = [self._factory_host()]
        for candidate in (self.ip_modes.policy.ip_for(self.ip_modes.fixed_octet),
                          self._lan_ip()):
            if candidate and candidate not in hosts:
                hosts.append(candidate)
        for host in hosts:
            try:
                with socket.create_connection((host, port), timeout=DETECT_TIMEOUT_SEC):
                    return host
            except OSError:
                continue
        return None

    async def poll_once(self, loop) -> None:
        if self.state["busy"]:
            return  # mid-run the switch reboots/moves IP — leave detection alone

        host = await loop.run_in_executor(None, self._detect_host)
        self.state["detected"] = bool(host)

        if host:
            self.state["active_host"] = host
            self.state["at_final_lan"] = host != self._factory_host()
            mac = await loop.run_in_executor(None, read_device_mac, host)
            self.state["active_mac"] = mac

            if self.state["phase"] in TERMINAL_PHASES:
                pass  # still plugged in after a run; wait for unplug
            else:
                self.state["phase"] = "detected"
                if self.state["at_final_lan"]:
                    self.state["message"] = (f"Switch detected on {host} (MAC "
                                             f"{mac or 'unknown'}) — already on the "
                                             "final management address, so it was "
                                             "likely provisioned before. To re-run "
                                             "it, leave the label password empty.")
                else:
                    self.state["message"] = (f"New switch detected on {host} "
                                             f"(MAC {mac or 'unknown'}). Scan or type "
                                             "the label password, then Configure.")
        else:
            self.state["active_host"] = None
            self.state["active_mac"] = None
            self.state["at_final_lan"] = False
            if self.state["phase"] in ("detected", *TERMINAL_PHASES):
                self.state["phase"] = "waiting"
                self.state["message"] = "Plug in the next TSW202…"

    # ── routes ────────────────────────────────────────────────────────────────

    def register_routes(self, app) -> None:
        @app.post("/api/configure")
        async def configure(body: ConfigureBody):
            if not self.state["detected"]:
                return {"error": "No switch is currently detected."}
            host = self.state["active_host"] or self._factory_host()
            mac = self.state["active_mac"]
            try:
                assign = self.resolve_ip_mode(body.ip_mode, body.octet)
            except IpModeError as e:
                return {"error": str(e)}
            # A switch left on DHCP is found again by MAC and by nothing else,
            # so without one the run would provision it and then lose it. Better
            # to refuse before touching the device than to strand it.
            if assign.dhcp and not mac:
                return {"error": "This switch's MAC could not be read, and it is "
                                 "the only way to find one again after it moves "
                                 "to DHCP. Re-plug it, or pick an address."}
            inputs = {"host": host, "mac": mac,
                      "target_ip": assign.ip, "ip_mode": assign.mode,
                      "advance_cycle": assign.advance_cycle,
                      **self.label_password_inputs(body.initial_password,
                                                   key="initial_password")}
            label = f"{self.hostname_for(inputs)} ({host})"
            if not await self.execute_run(inputs, label):
                return {"error": "A configuration is already in progress."}
            return self.public_state()

    # ── CLI banner ──────────────────────────────────────────────────────────────

    def print_banner(self) -> None:
        print(f"  device       : {self._factory_host()} "
              "(laptop must be on 192.168.1.x)")
        print(f"  {self.ip_modes.describe()}")
        next_ip = self.ip_modes.next_ip()
        print(f"  management IP: {next_ip or 'left to DHCP'} (applied as the "
              f"last step; gw {self.cfg.get('gateway', DEFAULT_TSW_GATEWAY)} / "
              f"{self.cfg.get('netmask', DEFAULT_TSW_NETMASK)})")
        print(f"  NTP / TZ     : {self.cfg.get('ntp_server', DEFAULT_TSW_NTP_SERVER)}"
              f" / {self.cfg.get('timezone', DEFAULT_TIMEZONE)}")
        image = self._firmware_bin()
        found = "found" if self._firmware_image_found() else \
            "MISSING — needed only for a switch that arrives below the floor"
        print(f"  firmware     : floor {self._min_firmware()}, "
              f"image {image.name if image else '(none configured)'} ({found})")
        print(f"  config       : "
              f"{'config/tsw.config.json' if self.cfg else 'MISSING — copy the example'}")
        # Confirming the management move needs an address on the target subnet,
        # and a switch serves no DHCP to hand us one.
        if next_ip:
            print(f"  note         : this station needs an address on "
                  f"{next_ip.rsplit('.', 1)[0]}.x to confirm the final move")
        else:
            print(f"  note         : switches are left on DHCP and found again "
                  f"by MAC on {', '.join(self._dhcp_subnets())}")


configurator = TswConfigurator(BASE_DIR)
app = configurator.build_app()


if __name__ == "__main__":
    configurator.run()
