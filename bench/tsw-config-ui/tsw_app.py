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
from typing import Optional

from pydantic import BaseModel

from bench_core import DEFAULT_TIMEZONE, DEFAULT_USERNAME
from bench_core.bench_ui import (
    DETECT_TIMEOUT_SEC,
    BenchConfigurator,
    read_device_mac,
    save_run_record,
)
from bench_core.run_record import build_run_entry

from tsw_configure import (
    DEFAULT_TSW_GATEWAY,
    DEFAULT_TSW_HOST,
    DEFAULT_TSW_LAN_IP,
    DEFAULT_TSW_MIN_FIRMWARE,
    DEFAULT_TSW_NETMASK,
    DEFAULT_TSW_NTP_SERVER,
    EXPECTED_MODEL,
    TswClient,
    configure_tsw,
)

BASE_DIR = Path(__file__).resolve().parent


class ConfigureBody(BaseModel):
    # The only input. No site name: nothing in the switch baseline is named
    # after one, so asking for it would be a field the operator fills in for
    # nothing.
    initial_password: str = ""


class TswConfigurator(BenchConfigurator):
    title = "TSW202 Configurator"
    port = 8007
    html_file = "tsw.html"
    config_filename = "config/tsw.config.json"
    log_filename = "tsw-config.log"
    tailscale_label = "tsw"   # unused — the switch baseline has no Tailscale step
    history_limit = 30  # full step logs per entry — the JSON files are the archive
    label_scan_enabled = True  # read the factory password off the QR label (TEC-349)

    # ── state ────────────────────────────────────────────────────────────────

    def initial_state(self) -> dict:
        return {
            "phase": "waiting",        # waiting|detected|configuring|configured|error
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
                "firmware_found": self._firmware_image_found()}

    # ── config helpers ────────────────────────────────────────────────────────

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
                             settings=run_cfg)

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
                "ip": result.get("ip") or self._lan_ip(),
                "password_source": self.password_source(inputs, "initial_password"),
                "firmware_note": result.get("firmware_note", ""),
            },
        )

    def _save_log(self, entry: dict) -> Optional[str]:
        """Name the per-run JSON after the serial. The shared writer defaults to
        `device.hostname`, which this tool doesn't have — and the serial is the
        better handle anyway: it's what's on the sticker someone reads back."""
        return save_run_record(self.log_dir, entry, name_stem=entry.get("serial"),
                               logger=self.logger)

    def success_message(self, result: dict, entry: dict, took: str) -> str:
        return (f"Configured TSW202 SN {entry['serial']} at "
                f"{entry['device']['ip']} in {took}. "
                "Unplug it and plug in the next one.")

    def dismiss_message(self) -> str:
        return "Plug in the next TSW202…"

    # ── detection loop ──────────────────────────────────────────────────────────

    def _detect_host(self) -> Optional[str]:
        """First address a switch answers on: the factory IP (192.168.1.2 — the
        .2, not the routers' .1), then the final management IP (an
        already-provisioned unit plugged back in). None if neither."""
        port = 443 if self.cfg.get("scheme", "https") == "https" else 80
        hosts = [self._factory_host()]
        lan_ip = self._lan_ip()
        if lan_ip and lan_ip not in hosts:
            hosts.append(lan_ip)
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
            self.state["at_final_lan"] = (host == self._lan_ip()
                                          and host != self._factory_host())
            mac = await loop.run_in_executor(None, read_device_mac, host)
            self.state["active_mac"] = mac

            if self.state["phase"] in ("configured", "error"):
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
            if self.state["phase"] in ("detected", "configured", "error"):
                self.state["phase"] = "waiting"
                self.state["message"] = "Plug in the next TSW202…"

    # ── routes ────────────────────────────────────────────────────────────────

    def register_routes(self, app) -> None:
        @app.post("/api/configure")
        async def configure(body: ConfigureBody):
            if not self.state["detected"]:
                return {"error": "No switch is currently detected."}
            host = self.state["active_host"] or self._factory_host()
            password, source = self.resolve_label_password(body.initial_password)
            inputs = {"initial_password": password,
                      "password_source": source,
                      "host": host, "mac": self.state["active_mac"]}
            label = f"{self.hostname_for(inputs)} ({host})"
            if not await self.execute_run(inputs, label):
                return {"error": "A configuration is already in progress."}
            return self.public_state()

    # ── CLI banner ──────────────────────────────────────────────────────────────

    def print_banner(self) -> None:
        print(f"  device       : {self._factory_host()} "
              "(laptop must be on 192.168.1.x)")
        print(f"  management IP: {self._lan_ip()} (applied as the last step; "
              f"gw {self.cfg.get('gateway', DEFAULT_TSW_GATEWAY)} / "
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
        print(f"  note         : this station needs an address on "
              f"{self._lan_ip().rsplit('.', 1)[0]}.x to confirm the final move")


configurator = TswConfigurator(BASE_DIR)
app = configurator.build_app()


if __name__ == "__main__":
    configurator.run()
