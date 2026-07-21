#!/usr/bin/env python3
"""OTD500 configurator — bench Web UI (FastAPI).

Workflow (one device at a time, no manifest):
  1. Plug an OTD500 into the laptop (it boots at 192.168.1.1).
  2. The UI detects it, reads its LAN MAC over ARP, and asks for the site name
     and the factory label password.
  3. Submit -> full pipeline -> unplug -> repeat. Nothing is persisted beyond
     the session history.

The common bench-UI shell (detection loop, run machinery, routes, WebSocket)
lives in bench_core.bench_ui; this file adds only the OTD specifics: the
configure_device pipeline call.
"""
from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

from bench_core import (
    DEFAULT_HOST,
    DEFAULT_NAME_PREFIX,
    DEFAULT_USERNAME,
    TeltonikaClient,
    device_name,
    tcp_port_open,
)
from bench_core.bench_ui import (
    DETECT_TIMEOUT_SEC,
    BenchConfigurator,
    read_device_mac,
)
from bench_core.run_record import build_run_entry

from otd_configure import configure_device

BASE_DIR = Path(__file__).resolve().parent


class ConfigureBody(BaseModel):
    site_name: str
    label_password: str = ""


class OtdConfigurator(BenchConfigurator):
    title = "OTD500 Configurator"
    port = 8003
    html_file = "otd.html"
    config_filename = "config/site.config.json"
    log_filename = "otd-config.log"
    tailscale_label = "otd"
    history_limit = 20  # full step logs per entry — the JSON files are the archive

    # ── state ────────────────────────────────────────────────────────────────

    def initial_state(self) -> dict:
        return {
            "phase": "waiting",        # waiting|detected|configuring|configured|error
            "detected": False,
            "active_mac": None,
            "busy": False,
            "message": "Plug in an OTD500, then enter its site name + label password.",
            "last_result": None,
            "history": [],
        }

    def extra_public_state(self) -> dict:
        return {"name_prefix": self.cfg.get("name_prefix", DEFAULT_NAME_PREFIX)}

    # ── pipeline hooks ─────────────────────────────────────────────────────────

    def hostname_for(self, inputs: dict) -> str:
        return device_name(inputs["site_name"], self.cfg.get("name_prefix", DEFAULT_NAME_PREFIX))

    def client_host(self, inputs: dict) -> str:
        return self.cfg.get("host", DEFAULT_HOST)

    def build_client(self, run_cfg: dict, host: str):
        return TeltonikaClient(
            host=host,
            username=run_cfg.get("username", DEFAULT_USERNAME),
            scheme=run_cfg.get("scheme", "https"),
            verify=not run_cfg.get("insecure", True),
        )

    def run_pipeline(self, client, run_cfg: dict, inputs: dict) -> dict:
        return configure_device(
            client, label_password=inputs["label_password"],
            site_name=inputs["site_name"], settings=run_cfg,
            expected={"mac": inputs.get("mac") or ""},
        )

    def build_entry(self, result: dict, inputs: dict, duration: int) -> dict:
        ident = result["identity"]
        return build_run_entry(
            tool="otd",
            ok=result["ok"],
            error=result["error"],
            serial=ident.get("serial", "unknown"),
            mac=ident.get("mac", inputs.get("mac") or "unknown"),
            model=ident.get("model", "OTD500"),
            firmware=ident.get("firmware", "unknown"),
            duration_s=duration,
            verification=result.get("verification", []),
            warnings=result["warnings"],
            steps=result["steps"],
            log=result["log"],
            device={
                "hostname": result["hostname"],
                "site_name": inputs["site_name"],
                "imei": ident.get("imei", "unknown"),
            },
        )

    def success_message(self, result: dict, entry: dict, took: str) -> str:
        warn = f" ({len(result['warnings'])} verify warning(s))" if result["warnings"] else ""
        return (f"Configured {result['hostname']} (SN {entry['serial']}) in {took}{warn}. "
                "Unplug it and plug in the next one.")

    def dismiss_message(self) -> str:
        return "Plug in the next OTD500…"

    # ── detection loop ──────────────────────────────────────────────────────────

    def _reachable(self) -> bool:
        host = self.cfg.get("host", DEFAULT_HOST)
        port = 443 if self.cfg.get("scheme", "https") == "https" else 80
        return tcp_port_open(host, port, timeout=DETECT_TIMEOUT_SEC)

    async def poll_once(self, loop) -> None:
        if self.state["busy"]:
            return  # mid-run the device reboots/moves IP — leave detection alone

        reachable = await loop.run_in_executor(None, self._reachable)
        self.state["detected"] = reachable

        if not reachable:
            self.state["active_mac"] = None
            if self.state["phase"] in ("detected", "configured", "error"):
                self.state["phase"] = "waiting"
                self.state["message"] = "Plug in the next OTD500…"
            return

        mac = await loop.run_in_executor(
            None, read_device_mac, self.cfg.get("host", DEFAULT_HOST))
        self.state["active_mac"] = mac

        if self.state["phase"] in ("configured", "error"):
            return  # still plugged in after a run; wait for unplug
        self.state["phase"] = "detected"
        self.state["message"] = (f"Device detected (MAC {mac or 'unknown'}). Enter the "
                                 "site name and the label password, then Configure.")

    # ── routes ────────────────────────────────────────────────────────────────

    def register_routes(self, app) -> None:
        @app.post("/api/configure")
        async def configure(body: ConfigureBody):
            site = body.site_name.strip()
            if not site:
                return {"error": "Enter a site name."}
            if not re.search(r"[A-Za-z0-9]", site):
                return {"error": "The site name needs at least one letter or digit."}
            if not self.state["detected"]:
                return {"error": "No device is currently detected."}
            inputs = {"site_name": site,
                      "label_password": body.label_password.strip(),
                      "mac": self.state.get("active_mac")}
            label = f"{self.hostname_for(inputs)} ({self.cfg.get('host', DEFAULT_HOST)})"
            if not await self.execute_run(inputs, label):
                return {"error": "A configuration is already in progress."}
            return self.public_state()

    # ── CLI banner ──────────────────────────────────────────────────────────────

    def print_banner(self) -> None:
        print(f"  device       : {self.cfg.get('host', DEFAULT_HOST)} "
              "(laptop must be on 192.168.1.x)")
        print(f"  config       : "
              f"{'config/site.config.json' if self.cfg else 'MISSING — copy the example'}")
        fw_cfg = self.cfg.get("firmware", {}) or {}
        if fw_cfg.get("mode") == "local":
            fw_bin = self.base_dir / (fw_cfg.get("bin_path") or "")
            fw_state = "found" if fw_bin.is_file() else "MISSING — download it before configuring"
            print(f"  firmware     : local image {fw_bin.name} ({fw_state})")


configurator = OtdConfigurator(BASE_DIR)
app = configurator.build_app()


if __name__ == "__main__":
    configurator.run()
