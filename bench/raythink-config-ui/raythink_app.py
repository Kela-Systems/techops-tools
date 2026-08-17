#!/usr/bin/env python3
"""Raythink camera configurator — bench Web UI (FastAPI).

Workflow (one camera at a time, no manifest):
  1. Connect a Raythink camera (it ships on the static IP 192.168.1.123,
     admin/admin) to the bench network.
  2. The UI detects it, reads its LAN MAC over ARP, and asks for the config
     profile (LAN or Cellular) and how to address it: a static-IP octet
     (manual, or auto-cycled 30 -> 31 -> ... -> 50 -> 30), or DHCP.
  3. Submit -> full pipeline: set password -> import profile -> NTP -> address
     the camera (static 192.168.88.XX, or DHCP) -> verify.
  4. The camera moves off 192.168.1.123; connect the next one.

The common bench-UI shell (run machinery, routes, WebSocket, step logging) lives
in bench_core.bench_ui; this file adds only the camera specifics: detection on
the factory IP, the profile/IP-mode form, the persisted cycle counter, and the
configure_camera pipeline call.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from bench_core import tcp_port_open
from bench_core.bench_ui import (
    DETECT_TIMEOUT_SEC,
    BenchConfigurator,
    read_device_mac,
)
from bench_core.run_record import build_run_entry

from raythink_camera import DEFAULT_HOST
from raythink_configure import (
    DEFAULT_LEASE_TIMEOUT,
    DEFAULT_OCTET_MAX,
    DEFAULT_OCTET_MIN,
    DEFAULT_SCAN_SUBNETS,
    configure_camera,
    device_name,
    resolve_profile,
    target_ip_for,
)

BASE_DIR = Path(__file__).resolve().parent
IP_STATE_PATH = BASE_DIR / "ip_state.json"

# How the camera gets its address: a static one whose last octet the operator
# types ("manual") or the tool auto-assigns ("cycle"), or the site's own DHCP
# server ("dhcp" — no octet to choose).
IP_MODES = ("manual", "cycle", "dhcp")


def norm_ip_mode(mode: str) -> str:
    """One of IP_MODES; anything unrecognised (including a stale value in
    ip_state.json) falls back to manual."""
    return mode if mode in IP_MODES else "manual"


class ConfigureBody(BaseModel):
    profile: str = ""              # profile key, e.g. "lan" | "cellular"
    ip_mode: str = ""              # one of IP_MODES; blank = use the saved mode
    octet: Optional[int] = None    # required for manual mode


class IpModeBody(BaseModel):
    mode: str                      # one of IP_MODES


class RaythinkConfigurator(BenchConfigurator):
    title = "Raythink Camera Configurator"
    port = 8005
    html_file = "raythink.html"
    config_filename = "config/raythink.config.json"
    log_filename = "raythink-config.log"
    logger_name = "raythink"
    tailscale_label = "raythink"
    history_limit = 30

    # ── helpers: octet range + persisted cycle counter ───────────────────────

    def _octet_range(self) -> tuple[int, int]:
        static = self.cfg.get("static", {}) or {}
        return (int(static.get("octet_min", DEFAULT_OCTET_MIN)),
                int(static.get("octet_max", DEFAULT_OCTET_MAX)))

    def _dhcp_subnets(self) -> list[str]:
        """Subnets swept for a camera's MAC after it is switched to DHCP."""
        return (self.cfg.get("dhcp", {}) or {}).get("scan_subnets", DEFAULT_SCAN_SUBNETS)

    def _load_ip_state(self) -> tuple[int, str]:
        """Read the persisted cycle counter + IP mode from ip_state.json, falling
        back to the config's range/default_mode."""
        lo, _hi = self._octet_range()
        default_mode = (self.cfg.get("static", {}) or {}).get("default_mode", "manual")
        try:
            data = json.loads(IP_STATE_PATH.read_text())
            val = int(data.get("cycle_next", lo))
            mode = data.get("ip_mode", default_mode)
        except (OSError, ValueError, TypeError):
            val, mode = lo, default_mode
        return self._clamp_octet(val), norm_ip_mode(mode)

    def _clamp_octet(self, octet: int) -> int:
        lo, hi = self._octet_range()
        if octet < lo or octet > hi:
            return lo
        return octet

    def _save_ip_state(self) -> None:
        try:
            IP_STATE_PATH.write_text(json.dumps(
                {"cycle_next": self.state["cycle_next"], "ip_mode": self.state["ip_mode"]}),
                encoding="utf-8")
        except OSError:
            self.logger.warning("Could not persist the IP state to %s.", IP_STATE_PATH)

    def _advance_cycle(self) -> None:
        lo, hi = self._octet_range()
        nxt = self.state["cycle_next"] + 1
        if nxt > hi:
            nxt = lo
        self.state["cycle_next"] = nxt
        self._save_ip_state()

    # ── state ────────────────────────────────────────────────────────────────

    def initial_state(self) -> dict:
        cycle_next, ip_mode = self._load_ip_state()
        return {
            "phase": "waiting",        # waiting|detected|configuring|configured|error
            "detected": False,
            "active_host": None,
            "active_mac": None,
            "busy": False,
            "ip_mode": ip_mode,        # one of IP_MODES
            "cycle_next": cycle_next,
            "message": "Connect the first camera (it ships on 192.168.1.123)…",
            "last_result": None,
            "history": [],
        }

    def extra_public_state(self) -> dict:
        lo, hi = self._octet_range()
        static = self.cfg.get("static", {}) or {}
        return {
            "profiles": list((self.cfg.get("profiles", {}) or {}).keys()),
            "octet_min": lo,
            "octet_max": hi,
            "subnet_prefix": static.get("subnet_prefix", "192.168.88"),
            "ntp_server": self.cfg.get("ntp_server", ""),
            "gateway": static.get("gateway", ""),
            "netmask": static.get("netmask", ""),
            "dhcp_subnets": self._dhcp_subnets(),
        }

    def reload(self) -> str:
        msg = super().reload()
        # Re-clamp the persisted counter AND refresh the saved IP mode, in case
        # either changed in the config/state file since startup.
        self.state["cycle_next"], self.state["ip_mode"] = self._load_ip_state()
        n = len(self.cfg.get("profiles", {}) or {})
        return f"{msg} {n} profile(s) configured."

    # ── pipeline hooks ────────────────────────────────────────────────────────

    def hostname_for(self, inputs: dict) -> str:
        return device_name(inputs.get("octet"))

    def client_host(self, inputs: dict) -> str:
        return inputs.get("host") or self.cfg.get("host", DEFAULT_HOST)

    def build_client(self, run_cfg: dict, host: str):
        from raythink_camera import DEFAULT_SCHEME, DEFAULT_USERNAME, RaythinkCameraClient
        return RaythinkCameraClient(
            host=host,
            username=run_cfg.get("username", DEFAULT_USERNAME),
            scheme=run_cfg.get("scheme", DEFAULT_SCHEME),
            verify=not run_cfg.get("insecure", True),
        )

    def run_pipeline(self, client, run_cfg: dict, inputs: dict) -> dict:
        return configure_camera(client, profile_name=inputs["profile"],
                                profile_path=inputs["profile_path"],
                                octet=inputs.get("octet"), settings=run_cfg,
                                mac=inputs.get("mac") or "")

    def build_entry(self, result: dict, inputs: dict, duration: int) -> dict:
        ident = result["identity"]
        return build_run_entry(
            tool="raythink",
            ok=result["ok"],
            error=result["error"],
            serial=ident.get("serial", "unknown"),
            mac=ident.get("mac") or inputs.get("mac") or "unknown",
            model=ident.get("model", "Raythink"),
            firmware=ident.get("firmware", "unknown"),
            duration_s=duration,
            verification=result.get("verification", []),
            warnings=result.get("warnings", []),
            steps=result["steps"],
            log=result["log"],
            device={
                "hostname": result["hostname"],
                "profile": inputs["profile"],
                # The address the bench assigned — empty on a DHCP run, where
                # the lease belongs to the DHCP server (it shows in the run's
                # verification rows instead).
                "ip": inputs["target_ip"],
                "ip_mode": result.get("ip_mode", "static"),
            },
        )

    def on_run_recorded(self, result: dict, inputs: dict, entry: dict) -> None:
        # Burn a cycle number only on a clean run, so a failed camera keeps its
        # slot for the retry.
        if result["ok"] and inputs.get("advance_cycle"):
            self._advance_cycle()

    def success_message(self, result: dict, entry: dict, took: str) -> str:
        dev = entry["device"]
        where = f"at {dev['ip']}" if dev.get("ip") else "on DHCP"
        return (f"Configured camera {where} (SN {entry['serial']}) via "
                f"'{dev['profile']}' in {took}. Connect the next camera.")

    def dismiss_message(self) -> str:
        return "Connect the next camera…"

    # ── detection loop ──────────────────────────────────────────────────────

    def _reachable(self, host: str) -> bool:
        port = 443 if self.cfg.get("scheme", "http") == "https" else 80
        return tcp_port_open(host, port, timeout=DETECT_TIMEOUT_SEC)

    async def poll_once(self, loop) -> None:
        if self.state["busy"]:
            return  # mid-run the camera reboots / changes IP — leave detection alone

        host = self.cfg.get("host", DEFAULT_HOST)
        reachable = await loop.run_in_executor(None, self._reachable, host)
        self.state["detected"] = reachable

        if reachable:
            self.state["active_host"] = host
            mac = await loop.run_in_executor(None, read_device_mac, host)
            self.state["active_mac"] = mac
            if self.state["phase"] in ("configured", "error"):
                return  # a run finished but the camera is still on the factory IP
            self.state["phase"] = "detected"
            self.state["message"] = (f"Camera detected on {host} (MAC {mac or 'unknown'}). "
                                     "Pick a profile and the IP, then Configure.")
        else:
            self.state["active_host"] = None
            self.state["active_mac"] = None
            if self.state["phase"] in ("detected", "configured", "error"):
                self.state["phase"] = "waiting"
                self.state["message"] = "Connect the next camera…"

    # ── routes ────────────────────────────────────────────────────────────────

    def register_routes(self, app) -> None:
        @app.post("/api/ip-mode")
        async def set_ip_mode(body: IpModeBody):
            """Set the addressing mode (persisted). Can be changed any time,
            including before a camera is connected."""
            mode = norm_ip_mode(body.mode)
            self.state["ip_mode"] = mode
            self._save_ip_state()
            prefix = (self.cfg.get("static", {}) or {}).get("subnet_prefix", "192.168.88")
            self.state["message"] = {
                "cycle": f"IP mode: cycle (next {prefix}.{self.state['cycle_next']}).",
                "dhcp": "IP mode: DHCP (the camera keeps the address its DHCP "
                        "server gives it).",
                "manual": "IP mode: manual (enter the last octet per camera).",
            }[mode]
            return self.public_state()

        @app.post("/api/configure")
        async def configure(body: ConfigureBody):
            profiles = self.cfg.get("profiles", {}) or {}
            if not profiles:
                return {"error": "No config profiles set — add them to raythink.config.json."}
            profile = (body.profile or "").strip() or next(iter(profiles))
            if profile not in profiles:
                return {"error": f"Unknown profile '{profile}'. Known: {', '.join(profiles)}."}
            try:
                profile_path = str(resolve_profile(self.cfg, profile))
            except Exception as e:  # noqa: BLE001 — surface a missing file cleanly
                return {"error": str(e)}

            if not self.state["detected"]:
                return {"error": "No camera is currently detected."}

            lo, hi = self._octet_range()
            mode = norm_ip_mode(body.ip_mode or self.state.get("ip_mode", "manual"))
            if mode != self.state.get("ip_mode"):
                self.state["ip_mode"] = mode
                self._save_ip_state()
            advance_cycle = False
            octet: Optional[int] = None      # None == leave the camera on DHCP
            if mode == "cycle":
                octet = self.state["cycle_next"]
                advance_cycle = True
            elif mode == "manual":
                if body.octet is None:
                    return {"error": f"Enter the last octet ({lo}-{hi})."}
                octet = int(body.octet)
                if not (lo <= octet <= hi):
                    return {"error": f"IP octet {octet} is out of range {lo}-{hi}."}
            elif not self.state["active_mac"]:
                # DHCP is verified by finding the camera again by MAC, so a
                # camera whose MAC we never read can't be checked afterwards.
                return {"error": "Could not read this camera's MAC over ARP, so it "
                                 "could not be found again after switching to DHCP. "
                                 "Check the cabling/adapter subnet, or assign a "
                                 "static IP instead."}

            target_ip = "" if octet is None else target_ip_for(self.cfg, octet)
            inputs = {
                "profile": profile, "profile_path": profile_path, "octet": octet,
                "ip_mode": mode, "target_ip": target_ip,
                "advance_cycle": advance_cycle,
                "host": self.state["active_host"] or self.cfg.get("host", DEFAULT_HOST),
                "mac": self.state["active_mac"],
            }
            label = f"{device_name(octet)} -> {target_ip or 'DHCP'} ('{profile}')"
            if not await self.execute_run(inputs, label):
                return {"error": "A configuration is already in progress."}
            return self.public_state()

    # ── CLI banner ──────────────────────────────────────────────────────────────

    def print_banner(self) -> None:
        lo, hi = self._octet_range()
        static = self.cfg.get("static", {}) or {}
        print(f"  camera       : {self.cfg.get('host', DEFAULT_HOST)} "
              "(laptop must be on 192.168.1.x)")
        print(f"  static IP    : {static.get('subnet_prefix', '192.168.88')}.{lo}-{hi} "
              f"(gw {static.get('gateway', '?')} / {static.get('netmask', '?')})")
        dhcp = self.cfg.get("dhcp", {}) or {}
        print(f"  or DHCP      : found again by MAC on "
              f"{', '.join(self._dhcp_subnets())} "
              f"(up to {dhcp.get('lease_timeout', DEFAULT_LEASE_TIMEOUT)}s)")
        print(f"  NTP          : {self.cfg.get('ntp_server', '?')}")
        profiles = self.cfg.get("profiles", {}) or {}
        print(f"  profiles     : {', '.join(profiles) if profiles else 'NONE — add them to the config'}")
        for key, rel in profiles.items():
            ok = (BASE_DIR / "config" / rel).is_file()
            print(f"    - {key:<9}: {rel} ({'found' if ok else 'MISSING'})")
        print(f"  config       : "
              f"{'config/raythink.config.json' if self.cfg else 'MISSING — copy the example'}")


configurator = RaythinkConfigurator(BASE_DIR)
app = configurator.build_app()


if __name__ == "__main__":
    configurator.run()
