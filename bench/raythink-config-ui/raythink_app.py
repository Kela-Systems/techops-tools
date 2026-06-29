#!/usr/bin/env python3
"""Raythink camera configurator — bench Web UI (FastAPI).

Workflow (one camera at a time, no manifest):
  1. Connect a Raythink camera (it ships on the static IP 192.168.1.123,
     admin/admin) to the bench network.
  2. The UI detects it, reads its LAN MAC over ARP, and asks for the config
     profile (LAN or Cellular) and the static-IP octet (manual, or auto-cycled
     30 -> 31 -> ... -> 50 -> 30).
  3. Submit -> full pipeline: set password -> import profile -> NTP -> static IP
     (192.168.88.XX) -> verify.
  4. The camera moves off 192.168.1.123; connect the next one.

The common bench-UI shell (run machinery, routes, WebSocket, step logging) lives
in bench_core.bench_ui; this file adds only the camera specifics: detection on
the factory IP, the profile/IP-mode form, the persisted cycle counter, and the
configure_camera pipeline call.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from bench_core import tcp_port_open
from bench_core.bench_ui import (
    DETECT_TIMEOUT_SEC,
    BenchConfigurator,
    read_device_mac,
)

from raythink_camera import DEFAULT_HOST
from raythink_configure import (
    DEFAULT_OCTET_MAX,
    DEFAULT_OCTET_MIN,
    configure_camera,
    device_name,
    resolve_profile,
    target_ip_for,
)

BASE_DIR = Path(__file__).resolve().parent
IP_STATE_PATH = BASE_DIR / "ip_state.json"


class ConfigureBody(BaseModel):
    profile: str = ""              # profile key, e.g. "lan" | "cellular"
    ip_mode: str = ""              # "manual" | "cycle"; blank = use the saved mode
    octet: Optional[int] = None    # required for manual mode


class IpModeBody(BaseModel):
    mode: str                      # "manual" | "cycle"


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
        mode = "cycle" if mode == "cycle" else "manual"
        return self._clamp_octet(val), mode

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
            "ip_mode": ip_mode,        # "manual" | "cycle"
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
        return device_name(inputs["octet"])

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
                                octet=inputs["octet"], settings=run_cfg)

    def build_entry(self, result: dict, inputs: dict, duration: int) -> dict:
        ident = result["identity"]
        return {
            "hostname": result["hostname"],
            "profile": inputs["profile"],
            "ip": inputs["target_ip"],
            "mac": ident.get("mac") or inputs.get("mac") or "unknown",
            "serial": ident.get("serial", "unknown"),
            "model": ident.get("model", "Raythink"),
            "firmware": ident.get("firmware", "unknown"),
            "status": "ok" if result["ok"] else "error",
            "error": result["error"],
            "steps": result["steps"],
            "verification": result.get("verification", []),
            "log": result["log"],
            "duration_s": duration,
            "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        }

    def on_run_recorded(self, result: dict, inputs: dict, entry: dict) -> None:
        # Burn a cycle number only on a clean run, so a failed camera keeps its
        # slot for the retry.
        if result["ok"] and inputs.get("advance_cycle"):
            self._advance_cycle()

    def success_message(self, result: dict, entry: dict, took: str) -> str:
        return (f"Configured camera at {entry['ip']} (SN {entry['serial']}) via "
                f"'{entry['profile']}' in {took}. Connect the next camera.")

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
            """Set the static-IP assignment mode (persisted). Can be changed any
            time, including before a camera is connected."""
            mode = "cycle" if body.mode == "cycle" else "manual"
            self.state["ip_mode"] = mode
            self._save_ip_state()
            self.state["message"] = (
                f"IP mode: cycle (next {self.cfg.get('static', {}).get('subnet_prefix', '192.168.88')}."
                f"{self.state['cycle_next']})." if mode == "cycle"
                else "IP mode: manual (enter the last octet per camera).")
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
            requested = body.ip_mode or self.state.get("ip_mode", "manual")
            mode = "cycle" if requested == "cycle" else "manual"
            if mode != self.state.get("ip_mode"):
                self.state["ip_mode"] = mode
                self._save_ip_state()
            advance_cycle = False
            if mode == "cycle":
                octet = self.state["cycle_next"]
                advance_cycle = True
            else:
                if body.octet is None:
                    return {"error": f"Enter the last octet ({lo}-{hi})."}
                octet = int(body.octet)
                if not (lo <= octet <= hi):
                    return {"error": f"IP octet {octet} is out of range {lo}-{hi}."}

            target_ip = target_ip_for(self.cfg, octet)
            inputs = {
                "profile": profile, "profile_path": profile_path, "octet": octet,
                "target_ip": target_ip, "advance_cycle": advance_cycle,
                "host": self.state["active_host"] or self.cfg.get("host", DEFAULT_HOST),
                "mac": self.state["active_mac"],
            }
            label = f"{device_name(octet)} -> {target_ip} ('{profile}')"
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
