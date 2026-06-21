#!/usr/bin/env python3
"""OTD500 batch configurator — bench Web UI (FastAPI).

Workflow:
  1. Import a manifest CSV (mac, label_password, site_name [, serial, imei, esim]).
  2. Plug an OTD500 into the laptop (it boots at 192.168.1.1).
  3. The UI reads its LAN MAC over ARP (no login needed), matches the manifest
     row, logs in with that row's label password, and runs the full pipeline:
       set password -> firmware -> name/hostname -> timezone -> 4G-only
       -> RMS -> Tailscale -> [optional] eSIM.
  4. The row is checked off; unplug and plug in the next one. Hands-free.

Devices are configured one at a time (all share 192.168.1.1). The common bench-UI
shell (detection loop, run machinery, routes, WebSocket) lives in
bench_core.bench_ui; this file adds only the OTD specifics: the manifest,
auto-on-match, and the configure_device pipeline call.
"""
from __future__ import annotations

import csv
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from bench_core import (
    DEFAULT_HOST,
    DEFAULT_USERNAME,
    TeltonikaClient,
    device_name,
)
from bench_core.bench_ui import (
    DETECT_TIMEOUT_SEC,
    BenchConfigurator,
    canonical_mac,
    read_device_mac,
)

from otd_configure import configure_device

BASE_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = BASE_DIR / "manifest.csv"


class AutoBody(BaseModel):
    enabled: bool


class OtdConfigurator(BenchConfigurator):
    title = "OTD500 Configurator"
    port = 8003
    html_file = "otd.html"
    config_filename = "site.config.json"
    log_filename = "otd-config.log"
    tailscale_label = "otd"
    history_limit = 20  # full step logs per entry — the JSON files are the archive

    # ── config + state ───────────────────────────────────────────────────────

    def initial_state(self) -> dict:
        self.manifest, self.manifest_problems = self.load_manifest()
        return {
            "phase": "waiting",        # waiting|detected|unmatched|configuring|configured|error
            "detected": False,
            "active_mac": None,
            "matched_site": None,
            "busy": False,
            "auto": True,              # hands-free: configure each matched device on detect
            "message": "Load a manifest, then plug in the first OTD500.",
            "last_result": None,
            "history": [],
            "manifest_problems": self.manifest_problems,
        }

    def load_manifest(self) -> tuple[list[dict], list[str]]:
        """Read manifest.csv into rows keyed for matching. Skips comment lines (#).

        Also lints the manifest: rows with a duplicate MAC or an empty
        label_password / site_name can't provision correctly, so they are skipped
        and reported. Duplicate site names are kept but flagged — they produce
        colliding hostnames and Tailscale node names."""
        if not MANIFEST_PATH.exists():
            return [], []
        rows: list[dict] = []
        problems: list[str] = []
        seen_macs: set[str] = set()
        seen_sites: set[str] = set()
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            lines = [ln for ln in f if not ln.lstrip().startswith("#")]
        for raw in csv.DictReader(lines):
            if not raw.get("mac"):
                continue
            mac = canonical_mac(raw["mac"])
            mac_display = raw["mac"].strip()
            label_password = (raw.get("label_password") or "").strip()
            site_name = (raw.get("site_name") or "").strip()
            if len(mac) != 12:
                problems.append(f"{mac_display}: not a valid MAC (need 12 hex digits, "
                                "with or without ':'/'-') — row skipped")
                continue
            if mac in seen_macs:
                problems.append(f"{mac_display}: duplicate mac — row skipped")
                continue
            seen_macs.add(mac)
            if not label_password:
                problems.append(f"{mac_display}: empty label_password — row skipped")
                continue
            if not site_name:
                problems.append(f"{mac_display}: empty site_name — row skipped")
                continue
            if site_name.lower() in seen_sites:
                problems.append(f"{mac_display}: duplicate site_name '{site_name}' — "
                                "hostname and Tailscale name will collide")
            seen_sites.add(site_name.lower())
            rows.append({
                "mac": mac,
                "mac_display": mac_display,
                "label_password": label_password,
                "site_name": site_name,
                "serial": (raw.get("serial") or "").strip(),
                "imei": (raw.get("imei") or "").strip(),
                "esim_activation_code": (raw.get("esim_activation_code") or "").strip(),
                "note": (raw.get("note") or "").strip(),
                "status": "pending",     # pending | done | error
                "result": None,
            })
        for p in problems:
            self.logger.warning("manifest: %s", p)
        return rows, problems

    def _find_row(self, mac: Optional[str]) -> Optional[dict]:
        if not mac:
            return None
        for row in self.manifest:
            if row["mac"] == mac:
                return row
        return None

    def _manifest_public(self) -> list[dict]:
        return [{k: v for k, v in r.items() if k != "label_password"} for r in self.manifest]

    def counts(self) -> dict:
        c = {"total": len(self.manifest), "done": 0, "error": 0, "pending": 0}
        for r in self.manifest:
            c[r["status"]] = c.get(r["status"], 0) + 1
        return c

    def extra_public_state(self) -> dict:
        return {"manifest": self._manifest_public()}

    def reload(self) -> str:
        self.cfg = self.load_config()
        self.manifest, self.manifest_problems = self.load_manifest()
        self.state["config_loaded"] = bool(self.cfg)
        self.state["manifest_problems"] = self.manifest_problems
        msg = (f"Loaded {len(self.manifest)} manifest row(s)." if self.manifest
               else "No manifest.csv found — copy manifest.example.csv.")
        if self.manifest_problems:
            shown = "; ".join(self.manifest_problems[:3])
            more = (f" (+{len(self.manifest_problems) - 3} more)"
                    if len(self.manifest_problems) > 3 else "")
            msg += f" {len(self.manifest_problems)} manifest problem(s): {shown}{more}"
        return msg

    # ── pipeline hooks ─────────────────────────────────────────────────────────

    def hostname_for(self, inputs: dict) -> str:
        return device_name(inputs["row"]["site_name"], self.cfg.get("name_prefix", "otd-"))

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
        row = inputs["row"]
        return configure_device(
            client, label_password=row["label_password"], site_name=row["site_name"],
            settings=run_cfg,
            expected={"serial": row["serial"], "imei": row["imei"], "mac": row["mac"],
                      "esim_activation_code": row["esim_activation_code"]},
        )

    def build_entry(self, result: dict, inputs: dict, duration: int) -> dict:
        row = inputs["row"]
        ident = result["identity"]
        return {
            "hostname": result["hostname"],
            "site_name": row["site_name"],
            "mac": row["mac_display"],
            "serial": ident.get("serial", "unknown"),
            "model": ident.get("model", "OTD500"),
            "firmware": ident.get("firmware", "unknown"),
            "imei": ident.get("imei", "unknown"),
            "warnings": result["warnings"],
            "status": "ok" if result["ok"] else "error",
            "error": result["error"],
            "steps": result["steps"],
            "verification": result.get("verification", []),
            "log": result["log"],
            "duration_s": duration,
            "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        }

    def on_run_recorded(self, result: dict, inputs: dict, entry: dict) -> None:
        row = inputs["row"]
        row["status"] = "done" if result["ok"] else "error"
        row["result"] = {k: entry[k] for k in ("status", "serial", "firmware", "warnings", "time")}

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
        try:
            with socket.create_connection((host, port), timeout=DETECT_TIMEOUT_SEC):
                return True
        except OSError:
            return False

    async def poll_once(self, loop) -> None:
        reachable = await loop.run_in_executor(None, self._reachable)
        self.state["detected"] = reachable

        if self.state["busy"]:
            pass
        elif reachable:
            mac = await loop.run_in_executor(
                None, read_device_mac, self.cfg.get("host", DEFAULT_HOST))
            self.state["active_mac"] = mac
            row = self._find_row(mac)
            self.state["matched_site"] = row["site_name"] if row else None

            if self.state["phase"] in ("configured", "error"):
                pass  # still plugged in after a run; wait for unplug
            elif row is None:
                self.state["phase"] = "unmatched"
                self.state["message"] = (f"Device detected (MAC {mac or 'unknown'}) but no "
                                         "matching manifest row. Add it to manifest.csv and reload.")
            elif row["status"] == "done":
                self.state["phase"] = "detected"
                self.state["message"] = (f"{device_name(row['site_name'])} is already "
                                         "provisioned. Unplug it, or retry from history.")
            elif self.state["auto"]:
                await self._run_row(row)
            else:
                self.state["phase"] = "detected"
                self.state["message"] = (f"Matched {device_name(row['site_name'])} "
                                         f"({row['mac_display']}). Click Configure.")
        else:
            self.state["active_mac"] = None
            self.state["matched_site"] = None
            if self.state["phase"] in ("detected", "unmatched", "configured", "error"):
                self.state["phase"] = "waiting"
                self.state["message"] = ("Plug in the next OTD500…" if self.counts()["pending"]
                                         else "All manifest rows done. Plug one in to re-check.")

    async def _run_row(self, row: dict) -> bool:
        label = f"{device_name(row['site_name'])} ({row['mac_display']})"
        return await self.execute_run({"row": row}, label)

    # ── routes ────────────────────────────────────────────────────────────────

    def register_routes(self, app) -> None:
        @app.post("/api/auto")
        async def set_auto(body: AutoBody):
            self.state["auto"] = body.enabled
            self.state["message"] = ("Auto mode ON — matched devices configure on detect."
                                     if body.enabled
                                     else "Auto mode off — click Configure per device.")
            return self.public_state()

        @app.post("/api/configure")
        async def configure():
            """Manual trigger (when auto is off, or to re-run a matched device)."""
            row = self._find_row(self.state["active_mac"])
            if not row:
                return {"error": "No matched device is currently detected."}
            if not await self._run_row(row):
                return {"error": "A configuration is already in progress."}
            return self.public_state()

    # ── CLI banner ──────────────────────────────────────────────────────────────

    def print_banner(self) -> None:
        print(f"  device       : {self.cfg.get('host', DEFAULT_HOST)} "
              "(laptop must be on 192.168.1.x)")
        print(f"  manifest     : {len(self.manifest)} row(s) from manifest.csv")
        for p in self.manifest_problems:
            print(f"  MANIFEST !!  : {p}")
        print(f"  config       : "
              f"{'site.config.json' if self.cfg else 'MISSING — copy the example'}")
        fw_cfg = self.cfg.get("firmware", {}) or {}
        if fw_cfg.get("mode") == "local":
            fw_bin = self.base_dir / (fw_cfg.get("bin_path") or "")
            fw_state = "found" if fw_bin.is_file() else "MISSING — download it before configuring"
            print(f"  firmware     : local image {fw_bin.name} ({fw_state})")


configurator = OtdConfigurator(BASE_DIR)
app = configurator.build_app()


if __name__ == "__main__":
    configurator.run()
