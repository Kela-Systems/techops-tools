#!/usr/bin/env python3
"""OTD500 batch configurator — bench Web UI (FastAPI).

Two modes, switchable from the UI (defaults to ad-hoc):

  Ad-hoc (default)
    1. Plug an OTD500 into the laptop (it boots at 192.168.1.1).
    2. The UI detects it, reads its LAN MAC over ARP, and asks for the site name
       and the factory label password.
    3. Submit -> full pipeline -> unplug -> repeat. Nothing is persisted beyond
       the session history; no manifest needed.

  Batch (opt-in)
    A saved list (manifest.csv) you build and edit in the UI. A plugged-in device
    is matched to a row by its LAN MAC and (with Auto on) provisioned hands-free.
    Rows can be pre-seeded with just a site name and captured at the bench: an
    unmatched device's live MAC/serial are recorded into the row on its first run,
    so the batch can be built entirely by plugging devices in. The same manifest
    feeds rms_register.py for bulk RMS pre-registration.

The common bench-UI shell (detection loop, run machinery, routes, WebSocket)
lives in bench_core.bench_ui; this file adds only the OTD specifics: the two
modes, the manifest (load + UI editing), auto-on-match, and the
configure_device pipeline call.
"""
from __future__ import annotations

import csv
import io
import re
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

# The columns written back when the UI edits the batch. Kept in sync with
# manifest.example.csv and what rms_register.py reads (extra columns are ignored
# by csv.DictReader, so the format stays compatible).
MANIFEST_FIELDS = ["mac", "label_password", "site_name", "serial", "imei",
                   "esim_activation_code", "note"]
MANIFEST_HEADER = (
    "# OTD500 batch manifest — managed by the OTD500 Configurator (Batch mode).\n"
    "# One row per device. You can also edit it by hand; see manifest.example.csv\n"
    "# for the full field docs. Gitignored — it holds per-device label passwords.\n"
    "# A row may be 'planned' (site_name only): its mac/serial are filled in the\n"
    "# first time that device is configured at the bench.\n"
)


def _row_id(mac: str, site_name: str) -> str:
    """A stable id for a manifest row, used for UI edit/delete. Derived from the
    content (MAC if present, else the site name) so it survives a reload — no
    stale-id race after an add/edit/delete regenerates the in-memory rows."""
    return mac if mac else "site:" + (site_name or "").strip().lower()


class AutoBody(BaseModel):
    enabled: bool


class ModeBody(BaseModel):
    mode: str  # "adhoc" | "batch"


class ConfigureBody(BaseModel):
    # Ad-hoc: site_name + label_password are required. Batch capture: same fields
    # attach/append a row with the live MAC, then configure it. Batch manual
    # (matched row): send an empty body and the active MAC is used.
    site_name: str = ""
    label_password: str = ""


class RowBody(BaseModel):
    site_name: str
    label_password: str = ""
    mac: str = ""
    serial: str = ""
    imei: str = ""
    esim_activation_code: str = ""
    note: str = ""


class RowEditBody(RowBody):
    id: str  # label_password left blank = keep the existing one


class RowIdBody(BaseModel):
    id: str


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
            "mode": "adhoc",           # adhoc (default) | batch
            "phase": "waiting",        # waiting|detected|unmatched|configuring|configured|error
            "detected": False,
            "active_mac": None,
            "matched_site": None,
            "busy": False,
            "auto": True,              # batch hands-free: configure each matched device on detect
            "message": "Ad-hoc mode — plug in an OTD500, then enter its site name + label password.",
            "last_result": None,
            "history": [],
            "manifest_problems": self.manifest_problems,
        }

    def _new_row(self, *, site_name: str, label_password: str = "", mac: str = "",
                 serial: str = "", imei: str = "", esim_activation_code: str = "",
                 note: str = "") -> dict:
        """A fully-shaped manifest row (with a UI-stable id) from raw fields."""
        mac_display = mac.strip()
        mac_canon = canonical_mac(mac_display) if mac_display else ""
        return {
            "id": _row_id(mac_canon, site_name),
            "mac": mac_canon,
            "mac_display": mac_display,
            "label_password": label_password,
            "site_name": site_name,
            "serial": serial,
            "imei": imei,
            "esim_activation_code": esim_activation_code,
            "note": note,
            "status": "pending",     # pending | done | error
            "result": None,
        }

    def load_manifest(self) -> tuple[list[dict], list[str]]:
        """Read manifest.csv into rows keyed for matching. Skips comment lines (#).

        Also lints the manifest. site_name is the only required field; a row may
        be 'planned' with no MAC yet (matched/filled at the bench) and the label
        password may be empty (the pipeline falls back to the shared password).
        Rows with an invalid or duplicate MAC, or no site_name, are skipped and
        reported. Duplicate site names are kept but flagged (they collide on the
        hostname / Tailscale node name)."""
        if not MANIFEST_PATH.exists():
            return [], []
        rows: list[dict] = []
        problems: list[str] = []
        seen_macs: set[str] = set()
        seen_sites: set[str] = set()
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            lines = [ln for ln in f if not ln.lstrip().startswith("#")]
        for raw in csv.DictReader(lines):
            mac_display = (raw.get("mac") or "").strip()
            label_password = (raw.get("label_password") or "").strip()
            site_name = (raw.get("site_name") or "").strip()
            serial = (raw.get("serial") or "").strip()
            imei = (raw.get("imei") or "").strip()
            # Skip a wholly blank line (e.g. a trailing newline row).
            if not any([mac_display, label_password, site_name, serial, imei]):
                continue
            if not site_name:
                problems.append(f"{mac_display or '(no mac)'}: empty site_name — row skipped")
                continue
            mac = ""
            if mac_display:
                mac = canonical_mac(mac_display)
                if len(mac) != 12:
                    problems.append(f"{mac_display}: not a valid MAC (need 12 hex digits, "
                                    "with or without ':'/'-') — row skipped")
                    continue
                if mac in seen_macs:
                    problems.append(f"{mac_display}: duplicate mac — row skipped")
                    continue
                seen_macs.add(mac)
            if site_name.lower() in seen_sites:
                problems.append(f"{mac_display or site_name}: duplicate site_name "
                                f"'{site_name}' — hostname and Tailscale name will collide")
            seen_sites.add(site_name.lower())
            rows.append({
                "id": _row_id(mac, site_name),
                "mac": mac,
                "mac_display": mac_display,
                "label_password": label_password,
                "site_name": site_name,
                "serial": serial,
                "imei": imei,
                "esim_activation_code": (raw.get("esim_activation_code") or "").strip(),
                "note": (raw.get("note") or "").strip(),
                "status": "pending",
                "result": None,
            })
        for p in problems:
            self.logger.warning("manifest: %s", p)
        return rows, problems

    def save_manifest(self) -> None:
        """Rewrite manifest.csv from the in-memory rows (UI edits + captured
        identity). Writes a fixed header + the data columns; the field docs live
        in manifest.example.csv."""
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        for r in self.manifest:
            writer.writerow({
                "mac": r.get("mac_display") or r.get("mac", ""),
                "label_password": r.get("label_password", ""),
                "site_name": r.get("site_name", ""),
                "serial": r.get("serial", ""),
                "imei": r.get("imei", ""),
                "esim_activation_code": r.get("esim_activation_code", ""),
                "note": r.get("note", ""),
            })
        MANIFEST_PATH.write_text(MANIFEST_HEADER + buf.getvalue(), encoding="utf-8")

    def _persist_and_reload(self) -> None:
        self.save_manifest()
        self.manifest, self.manifest_problems = self.load_manifest()
        self.state["manifest_problems"] = self.manifest_problems

    def _find_row(self, mac: Optional[str]) -> Optional[dict]:
        if not mac:
            return None
        for row in self.manifest:
            if row["mac"] and row["mac"] == mac:
                return row
        return None

    def _row_by_id(self, row_id: str) -> Optional[dict]:
        return next((r for r in self.manifest if r["id"] == row_id), None)

    def _manifest_public(self) -> list[dict]:
        """Manifest rows for the browser — the label password is never sent, only
        a `has_password` flag so the UI can show whether one is set."""
        out = []
        for r in self.manifest:
            d = {k: v for k, v in r.items() if k != "label_password"}
            d["has_password"] = bool(r.get("label_password"))
            out.append(d)
        return out

    def counts(self) -> dict:
        if self.state.get("mode") == "batch":
            c = {"total": len(self.manifest), "done": 0, "error": 0, "pending": 0}
            for r in self.manifest:
                c[r["status"]] = c.get(r["status"], 0) + 1
            return c
        done = sum(1 for h in self.state["history"] if h["status"] == "ok")
        return {"done": done, "error": len(self.state["history"]) - done}

    def extra_public_state(self) -> dict:
        return {"manifest": self._manifest_public(),
                "name_prefix": self.cfg.get("name_prefix", "otd-")}

    def reload(self) -> str:
        self.cfg = self.load_config()
        self.manifest, self.manifest_problems = self.load_manifest()
        self.state["config_loaded"] = bool(self.cfg)
        self.state["manifest_problems"] = self.manifest_problems
        msg = (f"Loaded {len(self.manifest)} manifest row(s)." if self.manifest
               else "No manifest.csv yet — add rows in Batch mode or copy manifest.example.csv.")
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
            "mac": row["mac_display"] or ident.get("mac", "unknown"),
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
        # If this row lives in the batch, record the identity we just learned
        # (MAC/serial/IMEI) back into the manifest so a planned/captured row
        # becomes a full, RMS-registerable record.
        if any(r is row for r in self.manifest):
            ident = result.get("identity", {}) or {}
            changed = False
            if not row.get("mac") and entry.get("mac") not in (None, "", "unknown"):
                row["mac_display"] = entry["mac"]
                row["mac"] = canonical_mac(entry["mac"])
                changed = True
            for key in ("serial", "imei"):
                val = ident.get(key)
                if val and val != "unknown" and not row.get(key):
                    row[key] = val
                    changed = True
            if changed:
                self.save_manifest()

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
            return
        if not reachable:
            self.state["active_mac"] = None
            self.state["matched_site"] = None
            if self.state["phase"] in ("detected", "unmatched", "configured", "error"):
                self.state["phase"] = "waiting"
                self.state["message"] = self._waiting_message()
            return

        mac = await loop.run_in_executor(
            None, read_device_mac, self.cfg.get("host", DEFAULT_HOST))
        self.state["active_mac"] = mac

        if self.state["mode"] == "adhoc":
            self.state["matched_site"] = None
            if self.state["phase"] in ("configured", "error"):
                return  # still plugged in after a run; wait for unplug
            self.state["phase"] = "detected"
            self.state["message"] = (f"Device detected (MAC {mac or 'unknown'}). Enter the "
                                     "site name and the label password, then Configure.")
            return

        # batch mode
        row = self._find_row(mac)
        self.state["matched_site"] = row["site_name"] if row else None
        if self.state["phase"] in ("configured", "error"):
            return  # still plugged in after a run; wait for unplug
        if row is None:
            self.state["phase"] = "unmatched"
            self.state["message"] = (f"Device detected (MAC {mac or 'unknown'}) — not in the "
                                     "batch yet. Add it below (it'll keep its MAC), or switch "
                                     "to Ad-hoc.")
        elif row["status"] == "done":
            self.state["phase"] = "detected"
            self.state["message"] = (f"{device_name(row['site_name'])} is already "
                                     "provisioned. Unplug it, or click Run again.")
        elif self.state["auto"]:
            await self._run_row(row)
        else:
            self.state["phase"] = "detected"
            self.state["message"] = (f"Matched {device_name(row['site_name'])} "
                                     f"({row['mac_display']}). Click Configure.")

    def _waiting_message(self) -> str:
        if self.state["mode"] == "adhoc":
            return "Plug in the next OTD500…"
        return ("Plug in the next OTD500…" if self.counts()["pending"]
                else "All batch rows done. Plug one in to re-check.")

    async def _run_row(self, row: dict) -> bool:
        label = f"{device_name(row['site_name'])} ({row['mac_display'] or 'no mac'})"
        return await self.execute_run({"row": row}, label)

    # ── batch capture / ad-hoc helpers ──────────────────────────────────────────

    def _capture_row(self, site_name: str, label_password: str) -> Optional[dict]:
        """Batch capture: attach the live MAC to a planned (mac-less) row with the
        same site name, or to a row already on this MAC, else append a new row.
        Persists immediately and returns the (live, in-manifest) row."""
        mac_display = self.state.get("active_mac") or ""
        mac = canonical_mac(mac_display) if mac_display else ""
        target = self._find_row(mac)
        if target is None:
            target = next((r for r in self.manifest
                           if not r.get("mac") and r["site_name"].lower() == site_name.lower()),
                          None)
        if target is None:
            target = self._new_row(site_name=site_name, label_password=label_password,
                                   mac=mac_display)
            self.manifest.append(target)
        target["site_name"] = site_name
        if mac:
            target["mac"] = mac
            target["mac_display"] = mac_display
        if label_password:
            target["label_password"] = label_password
        self.save_manifest()
        return target

    # ── routes ────────────────────────────────────────────────────────────────

    def register_routes(self, app) -> None:
        @app.post("/api/mode")
        async def set_mode(body: ModeBody):
            mode = "batch" if body.mode == "batch" else "adhoc"
            self.state["mode"] = mode
            self.state["phase"] = "waiting"
            self.state["message"] = (
                "Batch mode — plug a device in to match it against the batch, or add rows below."
                if mode == "batch" else
                "Ad-hoc mode — plug in an OTD500, then enter its site name + label password.")
            return self.public_state()

        @app.post("/api/auto")
        async def set_auto(body: AutoBody):
            self.state["auto"] = body.enabled
            self.state["message"] = ("Auto mode ON — matched devices configure on detect."
                                     if body.enabled
                                     else "Auto mode off — click Configure per device.")
            return self.public_state()

        @app.post("/api/configure")
        async def configure(body: ConfigureBody):
            """Ad-hoc Configure, batch capture ("Save & configure"), and batch
            manual run (empty body -> the currently matched row)."""
            site = body.site_name.strip()
            pw = body.label_password.strip()

            if self.state["mode"] == "adhoc":
                if not site:
                    return {"error": "Enter a site name."}
                if not re.search(r"[A-Za-z0-9]", site):
                    return {"error": "The site name needs at least one letter or digit."}
                if not self.state["detected"]:
                    return {"error": "No device is currently detected."}
                row = self._new_row(site_name=site, label_password=pw,
                                    mac=self.state.get("active_mac") or "")
                if not await self.execute_run({"row": row}, f"{device_name(site)} (ad-hoc)"):
                    return {"error": "A configuration is already in progress."}
                return self.public_state()

            # batch mode
            if site:  # capture-at-bench
                if not re.search(r"[A-Za-z0-9]", site):
                    return {"error": "The site name needs at least one letter or digit."}
                if not self.state["detected"]:
                    return {"error": "No device is currently detected."}
                row = self._capture_row(site, pw)
            else:  # matched manual run / retry
                row = self._find_row(self.state["active_mac"])
                if not row:
                    return {"error": "No matched device is currently detected."}
            if not await self._run_row(row):
                return {"error": "A configuration is already in progress."}
            return self.public_state()

        @app.post("/api/batch/add")
        async def batch_add(body: RowBody):
            site = body.site_name.strip()
            if not site:
                return {"error": "Enter a site name."}
            if self.state["busy"]:
                return {"error": "Busy configuring — try again in a moment."}
            self.manifest.append(self._new_row(
                site_name=site, label_password=body.label_password.strip(),
                mac=body.mac.strip(), serial=body.serial.strip(), imei=body.imei.strip(),
                esim_activation_code=body.esim_activation_code.strip(), note=body.note.strip()))
            self._persist_and_reload()
            self.state["message"] = f"Added {device_name(site)} to the batch."
            return self.public_state()

        @app.post("/api/batch/edit")
        async def batch_edit(body: RowEditBody):
            if self.state["busy"]:
                return {"error": "Busy configuring — try again in a moment."}
            row = self._row_by_id(body.id)
            if not row:
                return {"error": "Row not found (it may have been reloaded — try again)."}
            site = body.site_name.strip()
            if not site:
                return {"error": "Enter a site name."}
            row["site_name"] = site
            row["mac_display"] = body.mac.strip()
            row["serial"] = body.serial.strip()
            row["imei"] = body.imei.strip()
            row["esim_activation_code"] = body.esim_activation_code.strip()
            row["note"] = body.note.strip()
            if body.label_password.strip():  # blank = keep existing
                row["label_password"] = body.label_password.strip()
            self._persist_and_reload()
            self.state["message"] = f"Updated {device_name(site)}."
            return self.public_state()

        @app.post("/api/batch/delete")
        async def batch_delete(body: RowIdBody):
            if self.state["busy"]:
                return {"error": "Busy configuring — try again in a moment."}
            before = len(self.manifest)
            self.manifest = [r for r in self.manifest if r["id"] != body.id]
            if len(self.manifest) == before:
                return {"error": "Row not found (it may have been reloaded — try again)."}
            self._persist_and_reload()
            self.state["message"] = "Removed a row from the batch."
            return self.public_state()

    # ── CLI banner ──────────────────────────────────────────────────────────────

    def print_banner(self) -> None:
        print(f"  device       : {self.cfg.get('host', DEFAULT_HOST)} "
              "(laptop must be on 192.168.1.x)")
        print("  mode         : ad-hoc by default (switch to Batch in the UI)")
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
