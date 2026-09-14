#!/usr/bin/env python3
"""PLANET IGS-4215-8UP2T2S PoE switch configurator — bench Web UI (FastAPI).

Workflow (one switch at a time, nothing to type):
  1. Plug an IGS-4215 into the laptop (it boots at 192.168.0.100).
  2. The UI detects it over HTTP and reads its MAC.
  3. Press Configure -> full pipeline: firmware floor -> password -> port
     names -> NTP 192.168.88.10 -> timezone -> verify -> disable telnet
     -> move the management IP to 192.168.88.3. (PoE is left to the switch
     since 2026-09-14 — see `planet_configure.poe_is_managed`.)
  4. Unplug it and plug in the next one.

Three differences from the Teltonika tools this UI sits beside:

* **Nothing is scanned or typed.** PLANET's factory password is per-unit but
  *derivable* — `sw` + the last 6 hex digits of the MAC — and the detection loop
  already reads that MAC off ARP, so the tool works it out itself and
  `label_scan_enabled` stays off. The only choice the operator makes is the
  address. (If the MAC cannot be read, the run falls back to the shared
  password alone and says so, rather than guessing into the switch's lockout.)
* **Detection is over HTTP, not SSH.** On the firmware this tool exists to
  replace, the SSH server answers on port 22 and then refuses to give anyone a
  CLI — so probing 22 would report a healthy switch that cannot be configured.
  The web UI works on every build, which makes port 80 the honest probe.
* **The firmware step is on by default and is load-bearing.** Below
  v1.305b260324 the CLI is unusable; the flash is what makes the run possible
  at all, so the page shows the floor and whether the image is on disk, and a
  missing image is called out before the operator presses Configure.

The common bench-UI shell (run machinery, routes, WebSocket) lives in
bench_core.bench_ui; this file adds only the IGS-4215 specifics.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from pydantic import BaseModel

from bench_core import DEFAULT_USERNAME, tcp_port_open
from bench_core.bench_ui import (
    DETECT_TIMEOUT_SEC,
    TERMINAL_PHASES,
    BenchConfigurator,
    read_device_mac,
)
from bench_core.ip_mode import (
    MODE_FIXED,
    MODE_MANUAL,
    IpModeError,
    IpModePolicy,
    split_ip,
)
from bench_core.run_record import build_run_entry

from planet_configure import (
    DEFAULT_PLANET_HOST,
    DEFAULT_PLANET_LAN_IP,
    DEFAULT_PLANET_MIN_FIRMWARE,
    DEFAULT_PLANET_NETMASK,
    DEFAULT_PLANET_NTP_SERVER,
    DEFAULT_PLANET_TZ_ACRONYM,
    DEFAULT_PLANET_TZ_OFFSET,
    EXPECTED_MODEL,
    PSE_PORT_COUNT,
    SINGLE_SUPPLY_BUDGET_W,
    PlanetClient,
    configure_planet,
    poe_is_managed,
    ports_of,
    verify_planet,
)

BASE_DIR = Path(__file__).resolve().parent


class ConfigureBody(BaseModel):
    # No site name and no label password: nothing in the switch baseline is
    # named after a site, and PLANET has no per-unit factory password.
    ip_mode: str = ""
    octet: Optional[Union[int, str]] = None
    # Per-run escape hatch for the one case where skipping the firmware step is
    # right: a switch already known-good that the operator is re-running.
    skip_firmware: bool = False


class PlanetConfigurator(BenchConfigurator):
    title = "PLANET IGS-4215 PoE Switch Configurator"
    port = 8008
    html_file = "planet.html"
    config_filename = "config/planet.config.json"
    log_filename = "planet-config.log"
    history_limit = 30
    label_scan_enabled = False   # no per-unit factory password to scan
    verify_supported = True
    record_tool = "planet"
    ip_modes_enabled = True

    # ── state ────────────────────────────────────────────────────────────────

    def initial_state(self) -> dict:
        return {
            "phase": "waiting",
            "detected": False,
            "active_host": None,
            "active_mac": None,
            "at_final_lan": False,
            "busy": False,
            "message": "Plug in the first IGS-4215…",
            "last_result": None,
            "history": [],
        }

    def extra_public_state(self) -> dict:
        """What the page needs to show the plan before anything is pressed.

        The PoE table is the point of this tool, so it is published as state
        rather than baked into the HTML: an operator who edits the config sees
        the new plan on reload without anyone touching the page.
        """
        return {
            "ntp_server": self.cfg.get("ntp_server", DEFAULT_PLANET_NTP_SERVER),
            "timezone": f"{self._tz_acronym()} (UTC+{self._tz_offset()})",
            "min_firmware": self._min_firmware(),
            "firmware_enabled": self._firmware_enabled(),
            "firmware_found": self._firmware_image_found(),
            "poe_managed": self._poe_managed(),
            "poe_plan": self._poe_plan(),
            "poe_budget_w": self._poe_budget(),
            "poe_allocated_w": self._poe_allocated(),
            "disable_telnet": bool(self.cfg.get("disable_telnet", True)),
        }

    # ── address modes ─────────────────────────────────────────────────────────

    def ip_mode_policy(self) -> IpModePolicy:
        """fixed / manual, on the subnet the config's `lan_ip` names.

        No `cycle` (a site takes one switch) and no `dhcp`: this switch is
        configured with no default gateway, so a unit that wandered off to a
        lease would be reachable only by whoever guessed the subnet — and it
        serves no DHCP itself to help anyone find it.
        """
        prefix, octet = split_ip(self._lan_ip() or DEFAULT_PLANET_LAN_IP)
        return IpModePolicy(
            modes=(MODE_FIXED, MODE_MANUAL),
            prefix=prefix or "192.168.88",
            default_fixed_octet=octet or 3,
            default_mode=self.cfg.get("default_ip_mode", MODE_FIXED),
        )

    # ── config helpers ────────────────────────────────────────────────────────

    def _fw_cfg(self) -> dict:
        return self.cfg.get("firmware", {}) or {}

    def _firmware_enabled(self) -> bool:
        return bool(self._fw_cfg().get("enabled", True))

    def _min_firmware(self) -> str:
        return self._fw_cfg().get("minimum_version") or DEFAULT_PLANET_MIN_FIRMWARE

    def _firmware_bin(self) -> Optional[Path]:
        rel = self._fw_cfg().get("bix_path") or ""
        return self.base_dir / rel if rel else None

    def _firmware_image_found(self) -> bool:
        """Whether the local .bix is on disk.

        Unlike the Teltonika tools this is not merely a nicety: a switch that
        arrives below the floor has an unusable SSH server, so a missing image
        means the run cannot complete. The page warns loudly rather than
        blocking Configure — a switch that arrives already-new needs no image.
        """
        image = self._firmware_bin()
        return bool(image and image.is_file())

    def _tz_acronym(self) -> str:
        return str(self.cfg.get("timezone_acronym", DEFAULT_PLANET_TZ_ACRONYM))[:4]

    def _tz_offset(self):
        return self.cfg.get("timezone_offset", DEFAULT_PLANET_TZ_OFFSET)

    def _poe_cfg(self) -> dict:
        return self.cfg.get("poe", {}) or {}

    def _poe_budget(self) -> int:
        return int(self._poe_cfg().get("budget_w", SINGLE_SUPPLY_BUDGET_W))

    def _poe_allocated(self) -> float:
        return sum(float(spec.get("limit_w", 0))
                   for spec in ports_of(self._poe_cfg()).values()
                   if spec.get("enabled"))

    def _poe_managed(self) -> bool:
        """Whether the bench sets PoE at all — off since 2026-09-14. The plan
        below is still published, because it is what the ports are FOR even
        when the switch decides their power itself."""
        return poe_is_managed(self._poe_cfg())

    def _poe_plan(self) -> list[dict]:
        """The per-port plan, flattened for the page."""
        return [{"port": port,
                 "poe": port <= PSE_PORT_COUNT,
                 "enabled": bool(spec.get("enabled")),
                 "limit_w": float(spec.get("limit_w", 0)),
                 "priority": spec.get("priority", "low"),
                 "description": spec.get("description", "")}
                for port, spec in sorted(ports_of(self._poe_cfg()).items())]

    def _port_map_rows(self) -> list[list[str]]:
        """The printed port map: [port, what is plugged into it].

        The wattage is appended from `limit_w` rather than written into the
        text, so the label and the switch can never disagree about it. Plain
        port numbers, not `gi1` — the label is read against the numbers
        silkscreened on the metal.
        """
        rows = []
        managed = self._poe_managed()
        for port, spec in sorted(ports_of(self._poe_cfg()).items()):
            text = (spec.get("short") or spec.get("description") or "").strip()
            if not text:
                continue
            # Only when the bench actually applies that limit. With PoE left
            # to the switch, a label reading "RADAR 1 45W" would be claiming a
            # cap nobody set.
            if managed and spec.get("enabled"):
                text += f" {float(spec.get('limit_w', 0)):g}W"
            rows.append([str(port), text])
        return rows

    def _lan_ip(self) -> str:
        return self.cfg.get("lan_ip", DEFAULT_PLANET_LAN_IP)

    def _factory_host(self) -> str:
        return self.cfg.get("host", DEFAULT_PLANET_HOST)

    # ── pipeline hooks ─────────────────────────────────────────────────────────

    def hostname_for(self, inputs: dict) -> str:
        """A label for this run — NOT a hostname written to the device.

        The baseline doesn't name the switch, and PLANET exposes no serial over
        any interface, so the MAC is the only identifier available.
        """
        mac = (inputs.get("mac") or "").replace(":", "").replace("-", "")
        return f"planet-{mac[-4:].lower()}" if len(mac) >= 4 else "planet-unknown"

    def client_host(self, inputs: dict) -> str:
        return inputs["host"]

    def build_client(self, run_cfg: dict, host: str):
        return PlanetClient(host=host,
                            username=run_cfg.get("username", DEFAULT_USERNAME),
                            password=run_cfg.get("new_password", ""))

    def run_pipeline(self, client, run_cfg: dict, inputs: dict) -> dict:
        # A leaked CLI session holds one of the switch's few slots until it
        # times out, and the next run is then refused with a countdown.
        try:
            return self._run_pipeline(client, run_cfg, inputs)
        finally:
            client.close()

    def _run_pipeline(self, client, run_cfg: dict, inputs: dict) -> dict:
        if inputs.get("skip_firmware"):
            run_cfg = {**run_cfg,
                       "firmware": {**(run_cfg.get("firmware") or {}), "enabled": False}}
        return configure_planet(client,
                                initial_password=inputs.get("initial_password", ""),
                                settings=run_cfg,
                                target_ip=inputs.get("target_ip"),
                                ip_mode=inputs.get("ip_mode", ""),
                                mac=inputs.get("mac") or "")

    def verify_pipeline(self, client, run_cfg: dict, inputs: dict) -> dict:
        try:
            return self._verify_pipeline(client, run_cfg, inputs)
        finally:
            client.close()

    def _verify_pipeline(self, client, run_cfg: dict, inputs: dict) -> dict:
        return verify_planet(client, settings=run_cfg,
                             resolve=self.verify_resolver(inputs),
                             target_ip=inputs.get("target_ip"),
                             ip_mode=inputs.get("ip_mode", ""))

    def build_entry(self, result: dict, inputs: dict, duration: int) -> dict:
        ident = result["identity"]
        return build_run_entry(
            tool="planet",
            ok=result["ok"],
            error=result["error"],
            # No serial exists on this device; the MAC is the per-unit handle
            # and what a QA label would carry.
            serial=ident.get("serial") or (inputs.get("mac") or "unknown"),
            mac=ident.get("mac", inputs.get("mac") or "unknown"),
            model=ident.get("model", EXPECTED_MODEL),
            firmware=ident.get("firmware", "unknown"),
            duration_s=duration,
            verification=result.get("verification", []),
            warnings=result.get("warnings", []),
            steps=result["steps"],
            log=result["log"],
            device={
                "ip": result.get("ip", ""),
                "ip_mode": result.get("ip_mode") or MODE_FIXED,
                "reached_at": result.get("reached_at", ""),
                "firmware_note": result.get("firmware_note", ""),
                # What the bench did about power. The budget and the
                # allocation are recorded only when the bench actually applied
                # them — on a switch left to negotiate its own, "200 W
                # allocated" would be a claim about a configuration nobody
                # wrote, and the next person asking "why did a radar drop?"
                # would chase it.
                "poe_managed": self._poe_managed(),
                **({"poe_budget_w": self._poe_budget(),
                    "poe_allocated_w": self._poe_allocated()}
                   if self._poe_managed() else {}),
                # What the printed PORT MAP label says. Recorded with the run
                # so a re-print months later cannot drift from what the
                # installer actually stuck on the switch.
                "port_map": self._port_map_rows(),
                "dual_power": ident.get("dual_power", False),
            },
        )

    def log_name_stem(self, entry: dict) -> Optional[str]:
        """Name the per-run JSON after the MAC — there is no serial to use."""
        return entry.get("serial")

    def success_message(self, result: dict, entry: dict, took: str) -> str:
        where = result.get("reached_at") or result.get("ip") or "its factory address"
        return (f"Configured {EXPECTED_MODEL} {entry['serial']} at {where} in "
                f"{took}. Unplug it and plug in the next one.")

    def verify_message(self, result: dict, entry: dict, took: str) -> str:
        return (f"{EXPECTED_MODEL} {entry['serial']} PASSED verification in "
                f"{took} — nothing was changed. Unplug it and plug in the next one.")

    def dismiss_message(self) -> str:
        return "Plug in the next IGS-4215…"

    # ── detection loop ──────────────────────────────────────────────────────────

    def _detect_host(self) -> Optional[str]:
        """First address a switch answers on: the factory IP (192.168.0.100),
        then the management address an already-provisioned unit would be on.

        Port 80, not 22, on purpose. The firmware this tool exists to replace
        answers on 22 and then refuses to open a CLI, so an SSH probe would
        report a healthy switch that cannot actually be configured. The web UI
        answers on every build.
        """
        hosts = [self._factory_host()]
        for candidate in (self.ip_modes.policy.ip_for(self.ip_modes.fixed_octet),
                          self._lan_ip()):
            if candidate and candidate not in hosts:
                hosts.append(candidate)
        return next((host for host in hosts
                     if tcp_port_open(host, 80, timeout=DETECT_TIMEOUT_SEC)), None)

    async def poll_once(self, loop) -> None:
        if self.state["busy"]:
            return  # mid-run the switch reboots and moves IP — leave detection alone

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
                    self.state["message"] = (
                        f"Switch detected on {host} (MAC {mac or 'unknown'}) — "
                        "already on the final management address, so it was "
                        "likely provisioned before. Configure re-runs it safely.")
                else:
                    self.state["message"] = (
                        f"New switch detected on {host} (MAC {mac or 'unknown'}). "
                        "Press Configure.")
        else:
            self.state["active_host"] = None
            self.state["active_mac"] = None
            self.state["at_final_lan"] = False
            if self.state["phase"] in ("detected", *TERMINAL_PHASES):
                self.state["phase"] = "waiting"
                self.state["message"] = "Plug in the next IGS-4215…"

    # ── routes ────────────────────────────────────────────────────────────────

    def register_routes(self, app) -> None:
        @app.post("/api/configure")
        async def configure(body: ConfigureBody):
            if not self.state["detected"]:
                return {"error": "No switch is currently detected."}
            host = self.state["active_host"] or self._factory_host()
            try:
                assign = self.resolve_ip_mode(body.ip_mode, body.octet)
            except IpModeError as e:
                return {"error": str(e)}
            inputs = {"host": host, "mac": self.state["active_mac"],
                      "target_ip": assign.ip, "ip_mode": assign.mode,
                      "advance_cycle": assign.advance_cycle,
                      "skip_firmware": bool(body.skip_firmware),
                      "initial_password": ""}
            label = f"{self.hostname_for(inputs)} ({host})"
            if not await self.execute_run(inputs, label):
                return {"error": "A configuration is already in progress."}
            return self.public_state()

    # ── CLI banner ──────────────────────────────────────────────────────────────

    def print_banner(self) -> None:
        print(f"  device       : {self._factory_host()} "
              "(laptop must be on 192.168.0.x)")
        print(f"  {self.ip_modes.describe()}")
        next_ip = self.ip_modes.next_ip()
        print(f"  management IP: {next_ip} (applied as the last step; "
              f"{self.cfg.get('netmask', DEFAULT_PLANET_NETMASK)}, no gateway)")
        print(f"  NTP / TZ     : "
              f"{self.cfg.get('ntp_server', DEFAULT_PLANET_NTP_SERVER)} / "
              f"{self._tz_acronym()} (UTC+{self._tz_offset()})")
        image = self._firmware_bin()
        if not self._firmware_enabled():
            print("  firmware     : step DISABLED in config — a switch below "
                  f"{self._min_firmware()} will fail at SSH login")
        else:
            found = "found" if self._firmware_image_found() else \
                "MISSING — a switch below the floor cannot be configured without it"
            print(f"  firmware     : floor {self._min_firmware()}, image "
                  f"{image.name if image else '(none configured)'} ({found})")
        allocated, budget = self._poe_allocated(), self._poe_budget()
        over = "  ** OVER BUDGET **" if allocated > budget else ""
        print(f"  PoE plan     : {allocated:.0f} W allocated of {budget} W{over}")
        for row in self._poe_plan():
            state = (f"on  {row['limit_w']:>5.1f} W {row['priority']}"
                     if row["enabled"] else "off")
            print(f"    gi{row['port']}  {state:<22} {row['description']}")
        print(f"  config       : "
              f"{'config/planet.config.json' if self.cfg else 'MISSING — copy the example'}")
        if next_ip:
            print(f"  note         : this station needs an address on "
                  f"{next_ip.rsplit('.', 1)[0]}.x to confirm the final move")


configurator = PlanetConfigurator(BASE_DIR)
app = configurator.build_app()


if __name__ == "__main__":
    configurator.run()
