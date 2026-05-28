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
# HTML report
# ---------------------------------------------------------------------------

def write_html(report: dict, path: Path) -> None:  # noqa: C901
    """Generate a self-contained interactive HTML report."""
    s = report["summary"]
    ts = report["generated_at"]
    droam_queried = s["total_droam_sims"] is not None

    # ---- helpers ----
    def badge(text: str, colour: str) -> str:
        return f'<span class="badge bg-{colour} text-wrap">{text}</span>'

    def sim_droam_badge(sim: dict) -> str:
        v = sim.get("in_droam")
        if v is True:
            return badge("✔ Droam", "success")
        if v is False:
            return badge("✘ Not in Droam", "danger")
        return badge("—", "secondary")

    def sim_detail_rows(sim: dict) -> str:
        di = sim.get("droam_info") or {}
        rows = [
            ("ICCID", f"<code>{sim['iccid']}</code>"),
            ("IMSI", f"<code>{sim['imsi'] or '—'}</code>"),
            ("Operator", f"{sim['operator'] or '—'} ({sim['operator_number'] or '—'})"),
            ("SIM state", sim["sim_state"] or "—"),
            ("Connection", f"{sim['connection_state'] or '—'} / {sim['connection_type'] or '—'}"),
            ("Network", sim["network_state"] or "—"),
            ("Mobile IP", sim["mobile_ip"] or "—"),
            ("Signal", f"{sim['signal_dbm']} dBm &nbsp; RSRP {sim['rsrp']} &nbsp; RSRQ {sim['rsrq']} &nbsp; SINR {sim['sinr']}"),
        ]
        if di:
            tags_str = ", ".join(di.get("tags") or []) or "—"
            rows += [
                ("── Droam status", f"{di.get('status') or '—'} / session: {di.get('in_session') or '—'}"),
                ("── Droam plan", f"{di.get('plan') or '—'} ({di.get('sim_card_type') or '—'})"),
                ("── Droam IP / APN", f"{di.get('ip') or '—'} / {di.get('apn') or '—'}"),
                ("── Droam tags", tags_str),
                ("── Contract exp.", di.get("contract_expiration_at") or "—"),
            ]
        return "".join(
            f"<tr><th class='text-nowrap pe-3 fw-normal text-muted small'>{k}</th><td>{v}</td></tr>"
            for k, v in rows
        )

    # ---- build device rows ----
    device_rows_html = []
    for dev in report["devices"]:
        online = dev["status"] == "online"
        status_badge = badge("● online", "success") if online else badge("○ offline", "secondary")
        tags_html = " ".join(badge(t, "info") for t in dev["tags"]) if dev["tags"] else "—"

        sims = dev["sims"]
        # Droam match column: show worst case (any NOT in Droam → red)
        if not droam_queried or not sims:
            match_badge = badge("—", "secondary")
        elif all(s.get("in_droam") for s in sims):
            match_badge = badge(f"✔ {len(sims)}/{len(sims)}", "success")
        elif any(s.get("in_droam") for s in sims):
            matched = sum(1 for s in sims if s.get("in_droam"))
            match_badge = badge(f"⚠ {matched}/{len(sims)}", "warning")
        else:
            match_badge = badge(f"✘ 0/{len(sims)}", "danger")

        # per-slot SIM badges for the main row
        sim_badges = " ".join(
            f"{sim_droam_badge(s)}&nbsp;<code class='small'>{s['iccid'][:12]}…</code>"
            for s in sims
        ) if sims else "<span class='text-muted small'>no SIM data</span>"

        # expandable detail panel
        panel_id = f"dev-{dev['id']}"
        detail_table_rows = "".join(
            f"<tr><th class='text-nowrap pe-3 fw-normal text-muted small'>{k}</th><td>{v}</td></tr>"
            for k, v in [
                ("Model", dev["model"] or "—"),
                ("Serial", dev["serial"] or "—"),
                ("MAC", dev["mac"] or "—"),
                ("IMEI", dev["imei"] or "—"),
                ("Firmware", dev["firmware"] or "—"),
                ("WAN IP", dev["wan_ip"] or "—"),
                ("WAN state", dev["wan_state"] or "—"),
                ("Created", dev["created_at"] or "—"),
                ("Last seen", dev["last_connection_at"] or "—"),
            ]
        )
        sim_panels = "".join(
            f"""<div class='mb-2 p-2 border rounded'>
                  <strong class='small'>Slot {s['slot']}</strong> &nbsp; {sim_droam_badge(s)}
                  <table class='table table-sm mb-0 mt-1'>{sim_detail_rows(s)}</table>
                </div>"""
            for s in sims
        ) or "<p class='text-muted small mb-0'>No SIM data.</p>"

        detail_html = f"""
        <tr class='collapse' id='{panel_id}'>
          <td colspan='7' class='bg-light border-top-0 pt-0'>
            <div class='row g-3 p-2'>
              <div class='col-md-5'>
                <p class='fw-semibold mb-1 small text-uppercase text-muted'>Device</p>
                <table class='table table-sm mb-0'>{detail_table_rows}</table>
              </div>
              <div class='col-md-7'>
                <p class='fw-semibold mb-1 small text-uppercase text-muted'>SIMs</p>
                {sim_panels}
              </div>
            </div>
          </td>
        </tr>"""

        name_link = (
            f'<a class="text-decoration-none" data-bs-toggle="collapse" '
            f'href="#{panel_id}" role="button">'
            f'{dev["name"]}'
            f'</a>'
        )
        device_rows_html.append(
            f"<tr data-name='{dev['name'].lower()}' data-status='{dev['status']}'>"
            f"<td>{status_badge}</td>"
            f"<td>{name_link}</td>"
            f"<td class='small text-muted'>{dev['model'] or '—'}</td>"
            f"<td>{match_badge}</td>"
            f"<td class='small'>{sim_badges}</td>"
            f"<td class='small text-muted'>{dev['wan_ip'] or '—'}</td>"
            f"<td class='small'>{tags_html}</td>"
            f"</tr>"
            f"{detail_html}"
        )

    # ---- orphan Droam SIMs table ----
    orphans = report.get("droam_sims_not_in_any_teltonika", [])
    if orphans:
        orphan_rows = "".join(
            f"<tr>"
            f"<td><code class='small'>{o['iccid']}</code></td>"
            f"<td class='small'>{o.get('msisdn') or '—'}</td>"
            f"<td class='small'>{o.get('operator') or '—'}</td>"
            f"<td>{badge(o.get('status') or '—', 'secondary')}</td>"
            f"<td class='small'>{o.get('plan') or '—'}</td>"
            f"<td class='small'>{', '.join(o.get('tags') or []) or '—'}</td>"
            f"<td class='small'>{'Yes' if o.get('is_esim') else 'No'}</td>"
            f"</tr>"
            for o in orphans
        )
        orphan_section = f"""
        <h5 class='mt-4 mb-3'>
          Droam SIMs not installed in any Teltonika device
          <span class='badge bg-warning text-dark ms-2'>{len(orphans)}</span>
        </h5>
        <table class='table table-sm table-hover table-bordered' id='orphanTable'>
          <thead class='table-light'>
            <tr>
              <th>ICCID</th><th>MSISDN</th><th>Operator</th>
              <th>Status</th><th>Plan</th><th>Tags</th><th>eSIM</th>
            </tr>
          </thead>
          <tbody>{orphan_rows}</tbody>
        </table>"""
    else:
        orphan_section = "<p class='text-success mt-4'>All Droam SIMs are installed in a Teltonika device.</p>"

    # ---- summary cards ----
    def stat_card(label: str, value: str, colour: str = "primary") -> str:
        return (
            f"<div class='col'><div class='card text-center h-100 border-{colour}'>"
            f"<div class='card-body py-2'>"
            f"<div class='display-6 fw-bold text-{colour}'>{value}</div>"
            f"<div class='small text-muted'>{label}</div>"
            f"</div></div></div>"
        )

    cards = [
        stat_card("RMS devices", str(s["total_rms_devices"])),
        stat_card("SIM slots", str(s["total_rms_sims"])),
    ]
    if droam_queried:
        cards += [
            stat_card("Droam SIMs", str(s["total_droam_sims"])),
            stat_card("Matched", str(s["droam_sims_found_in_rms"]), "success"),
            stat_card("RMS not in Droam", str(s["rms_sims_not_in_droam"]),
                      "danger" if s["rms_sims_not_in_droam"] else "success"),
            stat_card("Droam unassigned", str(s["droam_sims_not_in_any_device"]),
                      "warning" if s["droam_sims_not_in_any_device"] else "success"),
        ]
    cards_html = "".join(cards)

    # ---- full page ----
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SIM Audit Report</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
  <style>
    body {{ font-family: system-ui, sans-serif; background: #f8f9fa; }}
    .collapse.show td {{ border-top: none; }}
    #deviceTable th {{ cursor: pointer; user-select: none; white-space: nowrap; }}
    #deviceTable th::after {{ content: " ↕"; color: #aaa; font-size: .7em; }}
    #deviceTable th.asc::after {{ content: " ↑"; color: #333; }}
    #deviceTable th.desc::after {{ content: " ↓"; color: #333; }}
    code {{ font-size: .85em; }}
    .badge {{ font-size: .75em; }}
  </style>
</head>
<body>
<div class="container-fluid py-4">

  <div class="d-flex justify-content-between align-items-center mb-3">
    <h3 class="mb-0">📡 SIM Audit Report</h3>
    <span class="text-muted small">Generated: {ts}</span>
  </div>

  <div class="row row-cols-2 row-cols-md-3 row-cols-lg-6 g-2 mb-4">
    {cards_html}
  </div>

  <div class="card mb-4">
    <div class="card-header d-flex justify-content-between align-items-center flex-wrap gap-2">
      <span class="fw-semibold">Teltonika Devices</span>
      <div class="d-flex gap-2 flex-wrap">
        <input id="searchBox" class="form-control form-control-sm" style="width:220px"
               placeholder="Search name / ICCID…" oninput="filterTable()">
        <select id="statusFilter" class="form-select form-select-sm" style="width:130px"
                onchange="filterTable()">
          <option value="">All status</option>
          <option value="online">Online only</option>
          <option value="offline">Offline only</option>
        </select>
      </div>
    </div>
    <div class="card-body p-0">
      <div class="table-responsive">
        <table class="table table-hover mb-0" id="deviceTable">
          <thead class="table-light">
            <tr>
              <th onclick="sortTable(0)">Status</th>
              <th onclick="sortTable(1)">Name</th>
              <th onclick="sortTable(2)">Model</th>
              <th onclick="sortTable(3)">Droam</th>
              <th>SIMs</th>
              <th onclick="sortTable(5)">WAN IP</th>
              <th>Tags</th>
            </tr>
          </thead>
          <tbody id="deviceBody">
            {"".join(device_rows_html)}
          </tbody>
        </table>
      </div>
    </div>
  </div>

  {orphan_section}

</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
  // Simple client-side sort
  let sortCol = -1, sortDir = 1;
  function sortTable(col) {{
    const tbody = document.getElementById('deviceBody');
    const ths = document.querySelectorAll('#deviceTable th');
    ths.forEach((th, i) => th.classList.remove('asc', 'desc'));
    if (sortCol === col) {{ sortDir *= -1; }} else {{ sortCol = col; sortDir = 1; }}
    ths[col].classList.add(sortDir === 1 ? 'asc' : 'desc');

    // collect main rows (non-collapse) and their following collapse rows
    const rows = [...tbody.querySelectorAll('tr:not(.collapse)')];
    rows.sort((a, b) => {{
      const at = (a.cells[col]?.innerText || '').trim().toLowerCase();
      const bt = (b.cells[col]?.innerText || '').trim().toLowerCase();
      return at < bt ? -sortDir : at > bt ? sortDir : 0;
    }});
    rows.forEach(r => {{
      tbody.appendChild(r);
      const next = document.getElementById(r.querySelector('a[href]')?.getAttribute('href')?.slice(1))
                    ?.closest('tr');
      if (next) tbody.appendChild(next);
    }});
  }}

  function filterTable() {{
    const q = document.getElementById('searchBox').value.toLowerCase();
    const st = document.getElementById('statusFilter').value;
    document.querySelectorAll('#deviceBody tr:not(.collapse)').forEach(row => {{
      const name = row.dataset.name || '';
      const status = row.dataset.status || '';
      const text = row.innerText.toLowerCase();
      const vis = (!q || name.includes(q) || text.includes(q)) && (!st || status === st);
      row.style.display = vis ? '' : 'none';
      // also hide the paired collapse row when filtering
      const collapseId = row.querySelector('a[href]')?.getAttribute('href')?.slice(1);
      const collapseRow = collapseId && document.getElementById(collapseId)?.closest('tr');
      if (collapseRow) collapseRow.style.display = vis ? '' : 'none';
    }});
  }}
</script>
</body>
</html>"""

    path.write_text(html, encoding="utf-8")
    print(f"HTML report   → {path}")


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

    html_path = output_dir / "report.html"
    write_html(report, html_path)

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
