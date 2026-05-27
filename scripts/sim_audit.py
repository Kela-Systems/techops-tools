#!/usr/bin/env python3
"""
sim_audit.py — Cross-reference Teltonika RMS devices with Droam SIM inventory.

Generates a report showing:
  - Each Teltonika device and its SIM(s), flagged as known/unknown in Droam
  - All Droam SIMs not installed in any Teltonika device

Usage:
  cp .env.example .env       # fill in your credentials
  python3 sim_audit.py       # outputs report.json + report.md
  python3 sim_audit.py --rms-only  # skip Droam (useful while Droam creds aren't set)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

RMS_API_BASE = "https://rms.teltonika-networks.com/api"
RMS_TOKEN = os.environ.get("RMS_API_TOKEN", "")

DROAM_URL = os.environ.get("DROAM_URL", "")
DROAM_USERNAME = os.environ.get("DROAM_USERNAME", "")
DROAM_PASSWORD = os.environ.get("DROAM_PASSWORD", "")


# ---------------------------------------------------------------------------
# RMS
# ---------------------------------------------------------------------------

def rms_headers() -> dict:
    return {
        "Authorization": f"Bearer {RMS_TOKEN}",
        "Accept": "application/json",
    }


def fetch_rms_devices() -> list[dict]:
    """Fetch all devices from RMS, handling pagination."""
    devices: list[dict] = []
    offset = 0
    limit = 100

    print("Fetching devices from RMS...", flush=True)
    while True:
        resp = requests.get(
            f"{RMS_API_BASE}/devices",
            headers=rms_headers(),
            params={"limit": limit, "offset": offset},
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()

        batch = body.get("data", [])
        total = body.get("meta", {}).get("total", 0)
        devices.extend(batch)
        print(f"  offset {offset}: fetched {len(batch)} devices (total={total}, so far={len(devices)})", flush=True)

        if not batch or len(devices) >= total:
            break
        offset += limit

    print(f"  → {len(devices)} devices total\n", flush=True)
    return devices


def extract_device_record(dev: dict) -> dict:
    """Normalise a raw RMS device into a clean record with all relevant fields."""

    def sim_block(suffix: str) -> dict | None:
        iccid = (dev.get(f"iccid{suffix}") or "").strip()
        if not iccid:
            return None
        return {
            "iccid": iccid,
            "imsi": (dev.get(f"imsi{suffix}") or "").strip(),
            "operator": (dev.get(f"operator{suffix}") or "").strip(),
            "operator_number": (dev.get(f"operator_number{suffix}") or "").strip(),
            "sim_state": (dev.get(f"sim_state{suffix}") or "").strip(),
            "connection_type": (dev.get(f"connection_type{suffix}") or "").strip(),
            "connection_state": (dev.get(f"connection_state{suffix}") or "").strip(),
            "network_state": (dev.get(f"network_state{suffix}") or "").strip(),
            "mobile_ip": (dev.get(f"mobile_ip{suffix}") or "").strip(),
            "signal_dbm": dev.get(f"signal{suffix}"),
            "rsrp": dev.get(f"rsrp{suffix}"),
            "rsrq": dev.get(f"rsrq{suffix}"),
            "sinr": dev.get(f"sinr{suffix}"),
        }

    sims = []
    sim1 = sim_block("")
    if sim1:
        sim1["slot"] = 1
        sims.append(sim1)
    sim2 = sim_block("_2")
    if sim2:
        sim2["slot"] = 2
        sims.append(sim2)

    return {
        "id": dev.get("id"),
        "name": dev.get("name"),
        "model": dev.get("model"),
        "serial": dev.get("serial"),
        "mac": dev.get("mac"),
        "wlan_mac": dev.get("wlan_mac"),
        "imei": dev.get("imei") or "",
        "imei_2": dev.get("imei_2") or "",
        "firmware": dev.get("firmware"),
        "hardware_revision": dev.get("hardware_revision"),
        "created_at": dev.get("created_at"),
        "last_connection_at": dev.get("last_connection_at"),
        "status": "online" if dev.get("status") == 1 else "offline",
        "wan_ip": dev.get("wan_ip"),
        "wan_state": dev.get("wan_state"),
        "router_uptime_s": dev.get("router_uptime"),
        "temperature_c": dev.get("temperature"),
        "is_esim": dev.get("esim", False),
        "tags": [t.get("name") for t in (dev.get("tags") or [])],
        "sims": sims,
    }


# ---------------------------------------------------------------------------
# Droam
# ---------------------------------------------------------------------------

import re as _re
import time as _time
import urllib.parse as _urlparse


def _droam_session() -> requests.Session:
    """
    Return an authenticated Droam requests.Session.

    Droam (platform.droam.com) is a Laravel server-rendered app with no
    public REST API. We log in via the HTML form, then call internal AJAX
    endpoints the browser uses.

    IMPORTANT: Droam enforces a single-active-session policy.  If the user
    is already logged in via a browser, their session will be invalidated
    when we log in here (and restored when they next log in).  The script
    handles this by:
      1. Login  →  2. Logout (kills all competing sessions)  →  3. Login again
    This ensures we have a clean, uncontested session for the API calls.
    """
    base = DROAM_URL.rstrip("/")
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    })

    def _csrf() -> str:
        r = s.get(f"{base}/login", timeout=30)
        m = _re.search(r'name="_token" value="([^"]+)"', r.text)
        return m.group(1) if m else ""

    def _login() -> None:
        token = _csrf()
        s.post(f"{base}/login", allow_redirects=False, timeout=30,
               data={"_token": token, "email": DROAM_USERNAME, "password": DROAM_PASSWORD})

    def _logout() -> None:
        xsrf = _urlparse.unquote(s.cookies.get("XSRF-TOKEN", ""))
        s.post(f"{base}/logout", allow_redirects=False, timeout=30,
               headers={"X-XSRF-TOKEN": xsrf})

    # Login → logout (clears all other active sessions) → login fresh
    _login()
    _logout()
    _time.sleep(1)
    _login()

    # Warm up the sim_cards page so the server establishes table state
    s.get(f"{base}/sim_cards", timeout=30)
    return s


def _droam_ajax_headers(s: requests.Session) -> dict:
    xsrf = _urlparse.unquote(s.cookies.get("XSRF-TOKEN", ""))
    return {
        "X-Requested-With": "XMLHttpRequest",
        "X-XSRF-TOKEN": xsrf,
        "Referer": f"{DROAM_URL.rstrip('/')}/sim_cards",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }


def _strip_html(text: str) -> str:
    return _re.sub(r"<[^>]+>", "", text).strip()


def fetch_droam_sims() -> list[dict]:
    """Fetch all SIM records from Droam via its internal AJAX table endpoint."""
    if not DROAM_URL:
        raise ValueError("DROAM_URL not set in .env")
    if not DROAM_USERNAME or not DROAM_PASSWORD:
        raise ValueError("DROAM_USERNAME / DROAM_PASSWORD not set in .env")

    base = DROAM_URL.rstrip("/")
    print("Logging in to Droam (logout+login to clear stale sessions)...", flush=True)
    s = _droam_session()

    sims: list[dict] = []
    page = 1
    page_size = 100

    print("Fetching SIMs from Droam...", flush=True)
    while True:
        hdrs = _droam_ajax_headers(s)
        resp = s.post(
            f"{base}/page/sim_cards",
            headers=hdrs,
            data={
                "pagerDropdownActive": "false",
                "current_page": page,
                "page_size": page_size,
                "sort_field": "iccid",
                "sort_order": "asc",
                "search": "",
            },
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()

        if body.get("ack") == "reload":
            raise RuntimeError(
                "Droam returned ack:reload — session may be contested. "
                "If a browser tab is open on Droam, close it and retry."
            )

        batch = body.get("data", [])
        total = body.get("total", 0)
        sims.extend(batch)
        print(f"  page {page}: fetched {len(batch)} SIMs (total={total}, so far={len(sims)})", flush=True)

        if not batch or len(sims) >= total:
            break
        page += 1

    print(f"  → {len(sims)} Droam SIMs total\n", flush=True)
    return sims


def normalise_droam_sim(raw: dict) -> dict:
    """Extract clean plain-text fields from a raw Droam SIM record (which contains HTML)."""

    def plain(field: str) -> str:
        val = raw.get(field) or ""
        return _strip_html(str(val)) if "<" in str(val) else str(val).strip()

    # status badge text: "Active", "Inactive", "Suspended", etc.
    status_text = plain("status")
    if "active" in status_text.lower():
        status_text = "Active"
    elif "inactive" in status_text.lower():
        status_text = "Inactive"

    # in_session badge: "Online" / "Offline"
    session_text = plain("in_session")

    # plan text (embedded in HTML div)
    plan_text = plain("plan")

    # tags: comma-separated badge texts
    tags_html = raw.get("tags") or ""
    tag_texts = [t.strip() for t in _re.findall(r'js-badged-text[^>]*>\s*([^<]+)', str(tags_html))]

    iccid = plain("iccid") or (raw.get("nickname") or "").strip()

    return {
        "iccid": iccid,
        "msisdn": (raw.get("msisdn") or "").strip(),
        "operator": (raw.get("operator") or "").strip(),
        "sim_card_type": (raw.get("sim_card_type") or "").strip(),
        "plan": plan_text,
        "status": status_text,
        "in_session": session_text,
        "ip": (raw.get("ip") or "").strip(),
        "apn": (raw.get("apn") or "").strip(),
        "eid": (raw.get("eid") or "").strip(),
        "network_type": (raw.get("network_type") or "").strip(),
        "contract_expiration_at": (raw.get("contract_expiration_at") or "").strip(),
        "plan_assigned_at": (raw.get("plan_assigned_at") or "").strip(),
        "last_usage_updated_at": (raw.get("last_usage_updated_at") or "").strip(),
        "tags": tag_texts,
        "is_esim": bool(raw.get("eid") and raw.get("eid") != "-"),
        "_raw_id": raw.get("id"),
    }


# ---------------------------------------------------------------------------
# Cross-reference & report
# ---------------------------------------------------------------------------

def build_report(devices: list[dict], droam_sims: list[dict] | None) -> dict:
    droam_by_iccid: dict[str, dict] = {}
    if droam_sims is not None:
        for sim in droam_sims:
            n = normalise_droam_sim(sim)
            if n.get("iccid"):
                droam_by_iccid[n["iccid"]] = n

    # Collect all ICCIDs seen in Teltonika devices
    seen_iccids: set[str] = set()

    device_rows = []
    for dev in devices:
        rec = extract_device_record(dev)
        for sim in rec["sims"]:
            iccid = sim["iccid"]
            seen_iccids.add(iccid)
            if droam_sims is not None:
                droam_match = droam_by_iccid.get(iccid)
                sim["in_droam"] = droam_match is not None
                sim["droam_info"] = droam_match
            else:
                sim["in_droam"] = None  # Droam not queried
                sim["droam_info"] = None
        device_rows.append(rec)

    orphan_droam_sims: list[dict] = []
    if droam_sims is not None:
        for iccid, sim_rec in droam_by_iccid.items():
            if iccid not in seen_iccids:
                orphan_droam_sims.append(sim_rec)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_rms_devices": len(device_rows),
            "total_rms_sims": sum(len(d["sims"]) for d in device_rows),
            "total_droam_sims": len(droam_by_iccid) if droam_sims is not None else None,
            "droam_sims_found_in_rms": len(seen_iccids & droam_by_iccid.keys()) if droam_sims is not None else None,
            "rms_sims_not_in_droam": sum(
                1 for d in device_rows for s in d["sims"] if s["in_droam"] is False
            ) if droam_sims is not None else None,
            "droam_sims_not_in_any_device": len(orphan_droam_sims) if droam_sims is not None else None,
        },
        "devices": device_rows,
        "droam_sims_not_in_any_teltonika": orphan_droam_sims,
    }


def write_markdown(report: dict, path: Path) -> None:
    lines = []
    ts = report["generated_at"]
    s = report["summary"]

    lines += [
        "# SIM Audit Report",
        "",
        f"Generated: {ts}",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| RMS devices | {s['total_rms_devices']} |",
        f"| SIM slots in RMS | {s['total_rms_sims']} |",
    ]
    if s["total_droam_sims"] is not None:
        lines += [
            f"| Droam SIMs total | {s['total_droam_sims']} |",
            f"| Droam SIMs matched in RMS | {s['droam_sims_found_in_rms']} |",
            f"| RMS SIMs not in Droam | {s['rms_sims_not_in_droam']} |",
            f"| Droam SIMs not in any Teltonika | {s['droam_sims_not_in_any_device']} |",
        ]
    else:
        lines.append("| Droam | not queried (--rms-only) |")

    lines += ["", "---", "", "## Teltonika Devices", ""]

    for dev in report["devices"]:
        online = "🟢" if dev["status"] == "online" else "🔴"
        lines += [
            f"### {online} {dev['name']}",
            "",
            "| Field | Value |",
            "|-------|-------|",
            f"| Model | {dev['model']} |",
            f"| Serial | {dev['serial']} |",
            f"| MAC | {dev['mac']} |",
            f"| IMEI | {dev['imei'] or '—'} |",
            f"| WAN IP | {dev['wan_ip'] or '—'} |",
            f"| WAN type | {dev['wan_state'] or '—'} |",
            f"| Firmware | {dev['firmware'] or '—'} |",
            f"| Created | {dev['created_at']} |",
            f"| Last seen | {dev['last_connection_at'] or '—'} |",
            f"| Tags | {', '.join(dev['tags']) if dev['tags'] else '—'} |",
            "",
        ]

        if dev["sims"]:
            lines.append("**SIMs:**")
            lines.append("")
            for sim in dev["sims"]:
                if sim.get("in_droam") is True:
                    droam_flag = "✅ in Droam"
                elif sim.get("in_droam") is False:
                    droam_flag = "❌ NOT in Droam"
                else:
                    droam_flag = "— (Droam not queried)"

                sim_lines = [
                    f"- **Slot {sim['slot']}** — ICCID: `{sim['iccid']}` — {droam_flag}",
                    f"  - IMSI: `{sim['imsi'] or '—'}`",
                    f"  - Operator: {sim['operator'] or '—'} ({sim['operator_number'] or '—'})",
                    f"  - State: {sim['sim_state'] or '—'} / Connection: {sim['connection_state'] or '—'}",
                    f"  - Network: {sim['connection_type'] or '—'} — {sim['network_state'] or '—'}",
                    f"  - Mobile IP: {sim['mobile_ip'] or '—'}",
                    f"  - Signal: {sim['signal_dbm']} dBm, RSRP: {sim['rsrp']}, RSRQ: {sim['rsrq']}, SINR: {sim['sinr']}",
                ]
                droam_info = sim.get("droam_info")
                if droam_info:
                    tags = ", ".join(droam_info.get("tags") or []) or "—"
                    sim_lines += [
                        f"  - Droam status: {droam_info.get('status') or '—'} / Session: {droam_info.get('in_session') or '—'}",
                        f"  - Droam plan: {droam_info.get('plan') or '—'} ({droam_info.get('sim_card_type') or '—'})",
                        f"  - Droam IP: {droam_info.get('ip') or '—'} / APN: {droam_info.get('apn') or '—'}",
                        f"  - Droam tags: {tags}",
                        f"  - Contract expires: {droam_info.get('contract_expiration_at') or '—'}",
                    ]
                lines += sim_lines
        else:
            lines.append("_No SIM data (device may be offline or wired)._")

        lines.append("")

    # Orphan Droam SIMs
    orphans = report.get("droam_sims_not_in_any_teltonika", [])
    if orphans:
        lines += [
            "---",
            "",
            "## Droam SIMs Not Installed in Any Teltonika Device",
            "",
            f"Found {len(orphans)} SIM(s) in Droam that don't appear in any Teltonika device.",
            "",
            "| ICCID | MSISDN | Operator | Status | Plan | Tags | eSIM |",
            "|-------|--------|----------|--------|------|------|------|",
        ]
        for sim in orphans:
            tags = ", ".join(sim.get("tags") or []) or "—"
            lines.append(
                f"| `{sim['iccid']}` | {sim.get('msisdn') or '—'} "
                f"| {sim.get('operator') or '—'} | {sim.get('status') or '—'} "
                f"| {sim.get('plan') or '—'} | {tags} | {'Yes' if sim.get('is_esim') else 'No'} |"
            )
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Markdown report → {path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Audit SIMs across RMS and Droam")
    parser.add_argument(
        "--rms-only",
        action="store_true",
        help="Skip Droam (useful when Droam credentials aren't configured yet)",
    )
    parser.add_argument(
        "--output-dir",
        default=str(Path(__file__).parent),
        help="Directory to write report files into (default: script directory)",
    )
    args = parser.parse_args()

    if not RMS_TOKEN:
        print("ERROR: RMS_API_TOKEN is not set. Copy .env.example to .env and fill it in.", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- RMS ---
    rms_devices = fetch_rms_devices()

    # --- Droam ---
    droam_sims: list[dict] | None = None
    if not args.rms_only:
        if not DROAM_URL:
            print(
                "DROAM_URL is not set — running in RMS-only mode.\n"
                "Set DROAM_URL, DROAM_USERNAME, DROAM_PASSWORD in .env to enable Droam cross-reference.\n",
                file=sys.stderr,
            )
        else:
            droam_sims = fetch_droam_sims()

    # --- Report ---
    report = build_report(rms_devices, droam_sims)

    json_path = output_dir / "report.json"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"JSON report   → {json_path}")

    md_path = output_dir / "report.md"
    write_markdown(report, md_path)

    # Quick summary to stdout
    s = report["summary"]
    print(f"\n{'='*50}")
    print(f"RMS devices:   {s['total_rms_devices']}")
    print(f"RMS SIM slots: {s['total_rms_sims']}")
    if s["total_droam_sims"] is not None:
        print(f"Droam SIMs:   {s['total_droam_sims']}")
        print(f"  matched:    {s['droam_sims_found_in_rms']}")
        print(f"  RMS not in Droam:        {s['rms_sims_not_in_droam']}")
        print(f"  Droam not in any device: {s['droam_sims_not_in_any_device']}")


if __name__ == "__main__":
    main()
