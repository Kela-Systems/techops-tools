#!/usr/bin/env python3
"""RUTM08 configurator — bench Web UI (FastAPI).

Workflow (one router at a time, no manifest):
  1. Plug a RUTM08 into the laptop (it boots at 192.168.1.1).
  2. The UI detects it, reads its LAN MAC over ARP, and asks for the site name
     and the factory label password.
  3. Submit -> full pipeline: set password -> hostname rut-<site> -> timezone
     -> firmware -> RMS -> Tailscale -> verify -> move LAN to 192.168.88.1.
  4. Unplug it and plug in the next one.

The common bench-UI shell (run machinery, routes, WebSocket) lives in
bench_core.bench_ui; this file adds only the RUTM specifics: the
two-address detection (probe the factory address AND the final LAN address, so
an already-moved router can be re-run) and the configure_rutm pipeline call.
"""
from __future__ import annotations

import re
import socket
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from bench_core import DEFAULT_HOST, DEFAULT_USERNAME, device_name
from bench_core.bench_ui import (
    DETECT_TIMEOUT_SEC,
    BenchConfigurator,
    read_device_mac,
)
from bench_core.run_record import build_run_entry

from rutm_configure import DEFAULT_RUTM_LAN_IP, DEFAULT_RUTM_PREFIX, RutmClient, configure_rutm

BASE_DIR = Path(__file__).resolve().parent


class ConfigureBody(BaseModel):
    site_name: str
    initial_password: str = ""


class RutmConfigurator(BenchConfigurator):
    title = "RUTM08 Configurator"
    port = 8004
    html_file = "rutm.html"
    config_filename = "config/rutm.config.json"
    log_filename = "rutm-config.log"
    tailscale_label = "rutm"
    history_limit = 30  # full step logs per entry — the JSON files are the archive
    label_scan_enabled = True  # read the factory password off the QR label (TEC-349)

    # ── state ────────────────────────────────────────────────────────────────

    def initial_state(self) -> dict:
        return {
            "phase": "waiting",        # waiting|detected|configuring|configured|error
            "detected": False,
            "active_host": None,       # the address the router answered on
            "active_mac": None,
            "at_final_lan": False,     # detected on lan_ip -> probably already provisioned
            "busy": False,
            "message": "Plug in the first RUTM08…",
            "last_result": None,
            "history": [],
        }

    def extra_public_state(self) -> dict:
        return {"name_prefix": self.cfg.get("name_prefix", DEFAULT_RUTM_PREFIX)}

    # ── pipeline hooks ─────────────────────────────────────────────────────────

    def hostname_for(self, inputs: dict) -> str:
        return device_name(inputs["site_name"], self.cfg.get("name_prefix", DEFAULT_RUTM_PREFIX))

    def client_host(self, inputs: dict) -> str:
        return inputs["host"]

    def build_client(self, run_cfg: dict, host: str):
        return RutmClient(
            host=host,
            username=run_cfg.get("username", DEFAULT_USERNAME),
            scheme=run_cfg.get("scheme", "https"),
            verify=not run_cfg.get("insecure", True),
        )

    def run_pipeline(self, client, run_cfg: dict, inputs: dict) -> dict:
        return configure_rutm(client, site_name=inputs["site_name"],
                              initial_password=inputs["initial_password"], settings=run_cfg)

    def build_entry(self, result: dict, inputs: dict, duration: int) -> dict:
        ident = result["identity"]
        return build_run_entry(
            tool="rutm",
            ok=result["ok"],
            error=result["error"],
            serial=ident.get("serial", "unknown"),
            mac=ident.get("mac", inputs.get("mac") or "unknown"),
            model=ident.get("model", "RUTM08"),
            firmware=ident.get("firmware", "unknown"),
            duration_s=duration,
            verification=result.get("verification", []),
            warnings=result.get("warnings", []),
            steps=result["steps"],
            log=result["log"],
            device={
                "hostname": result["hostname"],
                "site_name": inputs["site_name"],
                "password_source": self.password_source(inputs, "initial_password"),
            },
        )

    def dismiss_message(self) -> str:
        return "Plug in the next RUTM08…"

    # ── detection loop ──────────────────────────────────────────────────────────

    def _detect_host(self) -> Optional[str]:
        """First address a router answers on: the factory IP, then the final LAN IP
        (an already-provisioned unit plugged back in). Returns None if neither."""
        port = 443 if self.cfg.get("scheme", "https") == "https" else 80
        hosts = [self.cfg.get("host", DEFAULT_HOST)]
        lan_ip = self.cfg.get("lan_ip", DEFAULT_RUTM_LAN_IP)
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
            return  # mid-run the device reboots/moves IP — leave detection alone

        host = await loop.run_in_executor(None, self._detect_host)
        self.state["detected"] = bool(host)

        if host:
            self.state["active_host"] = host
            self.state["at_final_lan"] = (host == self.cfg.get("lan_ip", DEFAULT_RUTM_LAN_IP)
                                          and host != self.cfg.get("host", DEFAULT_HOST))
            mac = await loop.run_in_executor(None, read_device_mac, host)
            self.state["active_mac"] = mac

            if self.state["phase"] in ("configured", "error"):
                pass  # still plugged in after a run; wait for unplug
            else:
                self.state["phase"] = "detected"
                if self.state["at_final_lan"]:
                    self.state["message"] = (f"Router detected on {host} (MAC {mac or 'unknown'}) "
                                             "— already moved to the final LAN, so it was likely "
                                             "provisioned before. To re-run it, leave the label "
                                             "password empty.")
                else:
                    self.state["message"] = (f"New router detected on {host} "
                                             f"(MAC {mac or 'unknown'}). Enter the site name and "
                                             "the label password, then Configure.")
        else:
            self.state["active_host"] = None
            self.state["active_mac"] = None
            self.state["at_final_lan"] = False
            if self.state["phase"] in ("detected", "configured", "error"):
                self.state["phase"] = "waiting"
                self.state["message"] = "Plug in the next RUTM08…"

    # ── routes ────────────────────────────────────────────────────────────────

    def register_routes(self, app) -> None:
        @app.post("/api/configure")
        async def configure(body: ConfigureBody):
            site_name = body.site_name.strip()
            if not site_name:
                return {"error": "Enter a site name."}
            if not re.search(r"[A-Za-z0-9]", site_name):
                return {"error": "The site name needs at least one letter or digit."}
            if not self.state["detected"]:
                return {"error": "No router is currently detected."}
            host = self.state["active_host"] or self.cfg.get("host", DEFAULT_HOST)
            password, source = self.resolve_label_password(body.initial_password)
            inputs = {"site_name": site_name,
                      "initial_password": password,
                      "password_source": source,
                      "host": host, "mac": self.state["active_mac"]}
            label = f"{self.hostname_for(inputs)} ({host})"
            if not await self.execute_run(inputs, label):
                return {"error": "A configuration is already in progress."}
            return self.public_state()

    # ── CLI banner ──────────────────────────────────────────────────────────────

    def print_banner(self) -> None:
        print(f"  device       : {self.cfg.get('host', DEFAULT_HOST)} "
              "(laptop must be on 192.168.1.x)")
        print(f"  final LAN    : {self.cfg.get('lan_ip', DEFAULT_RUTM_LAN_IP)} "
              "(applied as the last step)")
        print(f"  config       : "
              f"{'config/rutm.config.json' if self.cfg else 'MISSING — copy the example'}")
        fw_cfg = self.cfg.get("firmware", {}) or {}
        if fw_cfg.get("mode") == "local":
            fw_bin = self.base_dir / (fw_cfg.get("bin_path") or "")
            fw_state = "found" if fw_bin.is_file() else "MISSING — download it before configuring"
            print(f"  firmware     : local image {fw_bin.name} ({fw_state})")


configurator = RutmConfigurator(BASE_DIR)
app = configurator.build_app()


if __name__ == "__main__":
    configurator.run()
