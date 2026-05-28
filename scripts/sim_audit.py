#!/usr/bin/env python3
"""
sim_audit.py — Cross-reference Teltonika RMS devices with Droam SIM inventory.

Generates a report showing:
  - Each Teltonika device and its SIM(s), flagged as known/unknown in Droam
  - All Droam SIMs not installed in any Teltonika device

Usage:
  cp .env.example .env           # fill in your credentials
  python3 sim_audit.py           # RMS + Droam cross-reference
  python3 sim_audit.py --ssh     # also SSH into each online device to discover all SIM slots
  python3 sim_audit.py --rms-only  # skip Droam (useful while Droam creds aren't set)

SSH enrichment reads /etc/config/simcard from each online OTD500 via SSH, discovering
ICCIDs for inactive/standby SIM slots (not visible via the RMS API).
Credentials are read from SSH_USER / SSH_PASS in .env (defaults: root / no password).
"""

from __future__ import annotations

import argparse
import json
import os
import re as _re_top
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

SSH_USER = os.environ.get("SSH_USER", "root")
SSH_PASS = os.environ.get("SSH_PASS", "") or None  # None = use SSH agent / key


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
# SSH enrichment — read /etc/config/simcard from each device
# ---------------------------------------------------------------------------

def parse_uci_simcard(text: str) -> list[dict]:
    """
    Parse `uci show simcard` output into a list of SIM slot records.

    Each entry has: position (int slot), iccid (str), primary (bool).
    Slots with no iccid are omitted.
    """
    slots: dict[str, dict] = {}
    current: Optional[str] = None

    for line in text.splitlines():
        line = line.strip()
        # section header: simcard.@sim[0]=sim  OR  simcard.@sim[2]=
        # (the ']=' distinguishes it from key lines like simcard.@sim[0].key=val)
        m = _re_top.match(r"simcard\.@sim\[(\d+)\]=", line)
        if m:
            current = m.group(1)
            if current not in slots:
                slots[current] = {}
            continue
        if current is None:
            continue
        # key=value lines: simcard.@sim[0].iccid='...'
        m2 = _re_top.match(r"simcard\.@sim\[\d+\]\.(\w+)='?([^']*)'?", line)
        if m2:
            key, val = m2.group(1), m2.group(2).strip()
            slots[current][key] = val

    result = []
    for slot_data in slots.values():
        iccid = slot_data.get("iccid", "").strip()
        if not iccid:
            continue
        try:
            position = int(slot_data.get("position", 0))
        except ValueError:
            position = 0
        result.append({
            "slot": position,
            "iccid": iccid,
            "primary": slot_data.get("primary") == "1",
            "source": "ssh_uci",
        })

    return result


def normalize_iccid(iccid: str) -> str:
    """Strip trailing 'F' padding used in some ICCID encodings (ITU-T E.118)."""
    return iccid.upper().rstrip("F")


def fetch_device_slots_via_ssh(
    host: str,
    ssh_user: str,
    ssh_pass: Optional[str],
    ssh_key: Optional[str],
    timeout: int = 15,
) -> Optional[list[dict]]:
    """
    SSH into a device and read /etc/config/simcard to discover all SIM slot ICCIDs.

    Returns a list of slot dicts (same shape as parse_uci_simcard output),
    or None if the connection failed.
    """
    try:
        import paramiko  # type: ignore[import]
    except ImportError:
        print(
            "  WARNING: paramiko not installed — SSH enrichment unavailable.\n"
            "  Install with: pip install paramiko",
            file=sys.stderr,
        )
        return None

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        connect_kwargs: dict = {
            "hostname": host,
            "username": ssh_user,
            "timeout": timeout,
            "look_for_keys": ssh_key is None and ssh_pass is None,
            "allow_agent": ssh_key is None and ssh_pass is None,
        }
        if ssh_key:
            connect_kwargs["key_filename"] = ssh_key
        if ssh_pass:
            connect_kwargs["password"] = ssh_pass

        client.connect(**connect_kwargs)
        _, stdout, _ = client.exec_command("uci show simcard 2>/dev/null", timeout=timeout)
        text = stdout.read().decode("utf-8", errors="replace")
        client.close()
        return parse_uci_simcard(text)
    except Exception as exc:  # noqa: BLE001
        print(f"    SSH failed: {exc}", file=sys.stderr)
        try:
            client.close()
        except Exception:
            pass
        return None


def enrich_devices_with_ssh(
    devices: list[dict],
    ssh_user: str,
    ssh_pass: Optional[str],
    ssh_key: Optional[str],
    max_workers: int = 20,
) -> None:
    """
    For each online device, SSH in (in parallel) and merge per-slot ICCIDs from UCI config.
    Updates devices in-place: adds 'ssh_slots' list (None if SSH failed or device offline).
    """
    import threading

    online = [d for d in devices if d.get("status") == "online"]
    offline_count = len(devices) - len(online)
    print(
        f"SSH enrichment: {len(online)} online device(s) to probe"
        f"{f', {offline_count} offline skipped' if offline_count else ''}"
        f" (up to {max_workers} parallel connections)…",
        flush=True,
    )

    # Mark offline devices immediately
    for dev in devices:
        if dev.get("status") != "online":
            dev["ssh_slots"] = None

    results: dict[int, Optional[list[dict]]] = {}
    lock = threading.Lock()

    def probe_one(dev: dict) -> None:
        host = dev.get("name") or dev.get("wan_ip") or ""
        if not host:
            with lock:
                results[dev["id"]] = None
            return
        slots = fetch_device_slots_via_ssh(host, ssh_user, ssh_pass, ssh_key)
        with lock:
            results[dev["id"]] = slots
            count = len(results)
            total = len(online)
            status = f"✔ {len(slots)} slot(s)" if slots is not None else "✗ SSH failed"
            print(f"  [{count:>3}/{total}] {host}: {status}", flush=True)

    threads = []
    semaphore = threading.Semaphore(max_workers)

    def run_with_semaphore(dev: dict) -> None:
        with semaphore:
            probe_one(dev)

    for dev in online:
        t = threading.Thread(target=run_with_semaphore, args=(dev,), daemon=True)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # Write results back to devices
    for dev in online:
        dev["ssh_slots"] = results.get(dev["id"])

    succeeded = sum(1 for v in results.values() if v is not None)
    print(f"  → SSH enrichment complete: {succeeded}/{len(online)} succeeded\n", flush=True)


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
    # Build Droam lookup by ICCID. Also index by normalised (F-stripped) ICCID
    # so we match regardless of trailing-F encoding differences.
    droam_by_iccid: dict[str, dict] = {}
    if droam_sims is not None:
        for sim in droam_sims:
            n = normalise_droam_sim(sim)
            raw_iccid = n.get("iccid", "")
            if not raw_iccid:
                continue
            droam_by_iccid[raw_iccid] = n
            norm = normalize_iccid(raw_iccid)
            if norm != raw_iccid:
                droam_by_iccid[norm] = n

    def droam_lookup(iccid: str) -> Optional[dict]:
        """Try raw ICCID then normalised (F-stripped) variant."""
        return droam_by_iccid.get(iccid) or droam_by_iccid.get(normalize_iccid(iccid))

    # Collect all ICCIDs seen across RMS + SSH so we can find orphan Droam SIMs
    seen_iccids: set[str] = set()

    device_rows = []
    for dev in devices:
        rec = extract_device_record(dev)

        # Merge SSH-discovered slots into rec["sims"]
        ssh_slots: Optional[list[dict]] = dev.get("ssh_slots")
        rec["ssh_enriched"] = ssh_slots is not None
        if ssh_slots:
            rms_iccids = {s["iccid"] for s in rec["sims"]}
            for slot in ssh_slots:
                slot_iccid = slot["iccid"]
                # Avoid duplicating the active SIM already from RMS
                if slot_iccid in rms_iccids or normalize_iccid(slot_iccid) in {
                    normalize_iccid(i) for i in rms_iccids
                }:
                    continue
                rec["sims"].append({
                    "iccid": slot_iccid,
                    "imsi": "",
                    "operator": "",
                    "operator_number": "",
                    "sim_state": "inactive",
                    "connection_type": "",
                    "connection_state": "inactive",
                    "network_state": "",
                    "mobile_ip": "",
                    "signal_dbm": None,
                    "rsrp": None,
                    "rsrq": None,
                    "sinr": None,
                    "slot": slot["slot"],
                    "source": "ssh_uci",
                })

        for sim in rec["sims"]:
            iccid = sim["iccid"]
            seen_iccids.add(iccid)
            seen_iccids.add(normalize_iccid(iccid))
            if droam_sims is not None:
                droam_match = droam_lookup(iccid)
                sim["in_droam"] = droam_match is not None
                sim["droam_info"] = droam_match
            else:
                sim["in_droam"] = None  # Droam not queried
                sim["droam_info"] = None
        device_rows.append(rec)

    orphan_droam_sims: list[dict] = []
    if droam_sims is not None:
        seen_for_orphan = seen_iccids | {normalize_iccid(i) for i in seen_iccids}
        for iccid, sim_rec in droam_by_iccid.items():
            if iccid not in seen_for_orphan and normalize_iccid(iccid) not in seen_for_orphan:
                orphan_droam_sims.append(sim_rec)
        # deduplicate (we may have indexed both raw and norm)
        seen_orphan: set[str] = set()
        unique_orphans = []
        for o in orphan_droam_sims:
            k = o.get("iccid", "")
            if k not in seen_orphan:
                seen_orphan.add(k)
                unique_orphans.append(o)
        orphan_droam_sims = unique_orphans

    total_sims = sum(len(d["sims"]) for d in device_rows)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_rms_devices": len(device_rows),
            "total_rms_sims": total_sims,
            "total_droam_sims": len({v["iccid"] for v in droam_by_iccid.values()}) if droam_sims is not None else None,
            "droam_sims_found_in_rms": sum(
                1 for d in device_rows for s in d["sims"] if s.get("in_droam") is True
            ) if droam_sims is not None else None,
            "rms_sims_not_in_droam": sum(
                1 for d in device_rows for s in d["sims"] if s.get("in_droam") is False
            ) if droam_sims is not None else None,
            "droam_sims_not_in_any_device": len(orphan_droam_sims) if droam_sims is not None else None,
            "ssh_enriched": any(d.get("ssh_enriched") for d in device_rows),
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
        return f'<span class="badge bg-{colour}">{text}</span>'

    def sim_droam_badge(sim: dict) -> str:
        v = sim.get("in_droam")
        inactive = sim.get("source") == "ssh_uci"
        if v is True:
            colour = "success"
            label = "✔ Droam"
        elif v is False:
            colour = "danger"
            label = "✘ Not in Droam"
        else:
            return badge("—", "secondary")
        if inactive:
            label += " (inactive slot)"
        return badge(label, colour)

    def sim_slot_label(sim: dict) -> str:
        inactive = sim.get("source") == "ssh_uci"
        slot = sim.get("slot", "?")
        if inactive:
            return f"Slot {slot} <span class='text-muted fw-normal'>(inactive — from device config)</span>"
        return f"Slot {slot}"

    def sim_detail_rows(sim: dict) -> str:
        di = sim.get("droam_info") or {}
        inactive = sim.get("source") == "ssh_uci"
        rows: list[tuple[str, str]] = [
            ("ICCID", f"<code>{sim['iccid']}</code>"),
        ]
        if not inactive:
            rows += [
                ("IMSI", f"<code>{sim.get('imsi') or '—'}</code>"),
                ("Operator", f"{sim.get('operator') or '—'} ({sim.get('operator_number') or '—'})"),
                ("SIM state", sim.get("sim_state") or "—"),
                ("Connection", f"{sim.get('connection_state') or '—'} / {sim.get('connection_type') or '—'}"),
                ("Network", sim.get("network_state") or "—"),
                ("Mobile IP", sim.get("mobile_ip") or "—"),
                ("Signal", f"{sim.get('signal_dbm')} dBm &nbsp;&nbsp; RSRP {sim.get('rsrp')} &nbsp;&nbsp; RSRQ {sim.get('rsrq')} &nbsp;&nbsp; SINR {sim.get('sinr')}"),
            ]
        else:
            rows.append(("Source", "Device UCI config (slot inactive / standby)"))
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
        # sort key: "0online" / "1offline" so online sorts first
        status_sort = "0online" if online else "1offline"
        tags_html = " ".join(badge(t, "light text-dark border") for t in dev["tags"]) if dev["tags"] else "—"

        sims = dev["sims"]
        all_iccids = " ".join(s["iccid"] for s in sims)  # for full-ICCID search

        # Droam match column: colour by worst-case
        if not droam_queried or not sims:
            match_badge = badge("—", "secondary")
            match_sort = "3"
        elif all(s.get("in_droam") for s in sims):
            match_badge = badge(f"✔ {len(sims)}/{len(sims)}", "success")
            match_sort = "0"
        elif any(s.get("in_droam") for s in sims):
            matched = sum(1 for s in sims if s.get("in_droam"))
            match_badge = badge(f"⚠ {matched}/{len(sims)}", "warning")
            match_sort = "1"
        else:
            match_badge = badge(f"✘ 0/{len(sims)}", "danger")
            match_sort = "2"

        # SIM summary for main row (truncated ICCID for display)
        sim_badges = " ".join(
            f"{sim_droam_badge(s)}&thinsp;<code class='small'>{s['iccid']}</code>"
            for s in sims
        ) if sims else "<span class='text-muted small'>no SIM data</span>"

        # Detail panel (uses plain JS toggle, not Bootstrap collapse — avoids display:block on <tr>)
        panel_id = f"d{dev['id']}"
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
            f"""<div class='mb-2 p-2 border rounded{"" if s.get("source") != "ssh_uci" else " border-secondary opacity-75"}'>
                  <strong class='small'>{sim_slot_label(s)}</strong> &nbsp; {sim_droam_badge(s)}
                  <table class='table table-sm mb-0 mt-1'>{sim_detail_rows(s)}</table>
                </div>"""
            for s in sims
        ) or "<p class='text-muted small mb-0'>No SIM data.</p>"

        detail_html = (
            f"<tr class='detail-row' id='{panel_id}'>"
            f"<td colspan='7' class='bg-light p-0'>"
            f"<div class='row g-0 p-3'>"
            f"<div class='col-md-5 pe-md-3'>"
            f"<p class='fw-semibold mb-1 small text-uppercase text-muted'>Device</p>"
            f"<table class='table table-sm mb-0'>{detail_table_rows}</table>"
            f"</div>"
            f"<div class='col-md-7 mt-3 mt-md-0'>"
            f"<p class='fw-semibold mb-1 small text-uppercase text-muted'>SIMs</p>"
            f"{sim_panels}"
            f"</div></div></td></tr>"
        )

        name_link = (
            f'<a class="dev-toggle text-decoration-none" href="#" '
            f'data-target="{panel_id}" onclick="toggleDetail(this);return false;">'
            f'<span class="toggle-arrow me-1">▶</span>{dev["name"]}'
            f'</a>'
        )
        device_rows_html.append(
            f"<tr class='device-row' "
            f"data-name='{dev['name'].lower()}' "
            f"data-status='{dev['status']}' "
            f"data-iccids='{all_iccids}' "
            f"data-detail='{panel_id}'>"
            f"<td data-sort='{status_sort}'>{status_badge}</td>"
            f"<td>{name_link}</td>"
            f"<td class='small text-muted'>{dev['model'] or '—'}</td>"
            f"<td data-sort='{match_sort}'>{match_badge}</td>"
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
        orphan_section = (
            f"<div class='card mt-4'>"
            f"<div class='card-header fw-semibold'>"
            f"Droam SIMs not installed in any Teltonika device "
            f"<span class='badge bg-warning text-dark ms-1'>{len(orphans)}</span>"
            f"</div>"
            f"<div class='card-body p-0'>"
            f"<div class='table-responsive'>"
            f"<table class='table table-sm table-hover mb-0'>"
            f"<thead class='table-light'><tr>"
            f"<th>ICCID</th><th>MSISDN</th><th>Operator</th>"
            f"<th>Status</th><th>Plan</th><th>Tags</th><th>eSIM</th>"
            f"</tr></thead>"
            f"<tbody>{orphan_rows}</tbody>"
            f"</table></div></div></div>"
        )
    else:
        orphan_section = (
            "<div class='alert alert-success mt-4 mb-0'>"
            "✔ All Droam SIMs are accounted for in a Teltonika device."
            "</div>"
        )

    # ---- summary cards ----
    def stat_card(label: str, value: str, colour: str = "primary") -> str:
        return (
            f"<div class='col'><div class='card text-center h-100 border-{colour}'>"
            f"<div class='card-body py-2 px-1'>"
            f"<div class='h2 fw-bold text-{colour} mb-0'>{value}</div>"
            f"<div class='small text-muted'>{label}</div>"
            f"</div></div></div>"
        )

    cards = [
        stat_card("Devices", str(s["total_rms_devices"])),
        stat_card("SIM slots", str(s["total_rms_sims"])),
    ]
    if droam_queried:
        cards += [
            stat_card("Droam SIMs", str(s["total_droam_sims"])),
            stat_card("Matched", str(s["droam_sims_found_in_rms"]), "success"),
            stat_card("RMS gaps", str(s["rms_sims_not_in_droam"]),
                      "danger" if s["rms_sims_not_in_droam"] else "success"),
            stat_card("Droam unassigned", str(s["droam_sims_not_in_any_device"]),
                      "warning" if s["droam_sims_not_in_any_device"] else "success"),
        ]
    cards_html = "".join(cards)

    model_filter = report.get("model_filter", "")
    subtitle = "OTD500 devices only" if model_filter == "OTD500" else (f"model filter: {model_filter}" if model_filter else "all device models")
    if s.get("ssh_enriched"):
        subtitle += " · SSH-enriched (all SIM slots)"

    # ---- full page ----
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SIM Audit — {ts[:10]}</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
  <style>
    body {{ font-family: system-ui, sans-serif; background: #f8f9fa; }}

    /* Detail rows: hidden by default, shown via JS (avoids Bootstrap setting display:block on <tr>) */
    .detail-row {{ display: none; }}
    .detail-row.open {{ display: table-row; }}
    .detail-row td {{ border-top: none !important; }}

    /* Sortable column headers */
    .sortable {{ cursor: pointer; user-select: none; white-space: nowrap; }}
    .sortable::after {{ content: " ↕"; color: #bbb; font-size: .7em; }}
    .sortable.asc::after {{ content: " ↑"; color: #333; font-size: .8em; }}
    .sortable.desc::after {{ content: " ↓"; color: #333; font-size: .8em; }}

    .toggle-arrow {{ font-size: .7em; transition: transform .15s; display: inline-block; }}
    .open-arrow .toggle-arrow {{ transform: rotate(90deg); }}

    code {{ font-size: .82em; word-break: break-all; }}
    .badge {{ font-size: .72em; }}
    #deviceTable td {{ vertical-align: middle; }}
  </style>
</head>
<body>
<div class="container-fluid py-4" style="max-width:1600px">

  <div class="d-flex justify-content-between align-items-start mb-3 flex-wrap gap-2">
    <div>
      <h3 class="mb-0">📡 SIM Audit Report</h3>
      <div class="text-muted small">{subtitle} &middot; generated {ts[:19].replace("T"," ")} UTC</div>
    </div>
  </div>

  <div class="row row-cols-2 row-cols-sm-3 row-cols-lg-6 g-2 mb-4">
    {cards_html}
  </div>

  <div class="card mb-2">
    <div class="card-header d-flex justify-content-between align-items-center flex-wrap gap-2 py-2">
      <span class="fw-semibold">Teltonika Devices</span>
      <div class="d-flex gap-2 flex-wrap align-items-center">
        <input id="searchBox" class="form-control form-control-sm" style="width:240px"
               placeholder="Search name or full ICCID…" oninput="applyFilters()">
        <select id="statusFilter" class="form-select form-select-sm" style="width:140px"
                onchange="applyFilters()">
          <option value="">All status</option>
          <option value="online">Online only</option>
          <option value="offline">Offline only</option>
        </select>
        <select id="droamFilter" class="form-select form-select-sm" style="width:160px"
                onchange="applyFilters()">
          <option value="">All Droam states</option>
          <option value="0">Fully matched</option>
          <option value="1">Partially matched</option>
          <option value="2">Not in Droam</option>
        </select>
        <button class="btn btn-sm btn-outline-secondary" onclick="clearFilters()">Clear</button>
        <span id="visCount" class="text-muted small"></span>
      </div>
    </div>
    <div class="card-body p-0">
      <div class="table-responsive">
        <table class="table table-hover mb-0" id="deviceTable">
          <thead class="table-light">
            <tr>
              <th class="sortable" data-col="0">Status</th>
              <th class="sortable" data-col="1">Name</th>
              <th class="sortable" data-col="2">Model</th>
              <th class="sortable" data-col="3">Droam</th>
              <th>SIMs</th>
              <th class="sortable" data-col="5">WAN IP</th>
              <th>Tags</th>
            </tr>
          </thead>
          <tbody id="deviceBody">
            {"".join(device_rows_html)}
          </tbody>
        </table>
      </div>
      <div id="emptyMsg" class="text-center text-muted py-4" style="display:none">No devices match the current filters.</div>
    </div>
  </div>

  {orphan_section}

</div>
<script>
  // ---- expand / collapse detail rows ----
  function toggleDetail(link) {{
    const id = link.dataset.target;
    const row = document.getElementById(id);
    const open = row.classList.toggle('open');
    link.classList.toggle('open-arrow', open);
  }}

  // ---- sort ----
  let sortCol = -1, sortDir = 1;
  document.querySelectorAll('#deviceTable th.sortable').forEach(th => {{
    th.addEventListener('click', () => {{
      const col = +th.dataset.col;
      document.querySelectorAll('#deviceTable th.sortable').forEach(h => h.classList.remove('asc','desc'));
      sortDir = (sortCol === col) ? -sortDir : 1;
      sortCol = col;
      th.classList.add(sortDir === 1 ? 'asc' : 'desc');

      const tbody = document.getElementById('deviceBody');
      const rows = [...tbody.querySelectorAll('tr.device-row')];
      rows.sort((a, b) => {{
        // prefer data-sort attribute, fall back to cell text
        const cellA = a.cells[col];
        const cellB = b.cells[col];
        const at = (cellA?.dataset.sort ?? cellA?.innerText ?? '').trim().toLowerCase();
        const bt = (cellB?.dataset.sort ?? cellB?.innerText ?? '').trim().toLowerCase();
        return at < bt ? -sortDir : at > bt ? sortDir : 0;
      }});
      rows.forEach(r => {{
        tbody.appendChild(r);
        const detailRow = document.getElementById(r.dataset.detail);
        if (detailRow) tbody.appendChild(detailRow);
      }});
      applyFilters(); // re-apply visibility after sort
    }});
  }});

  // ---- filter ----
  function applyFilters() {{
    const q = document.getElementById('searchBox').value.trim().toLowerCase();
    const st = document.getElementById('statusFilter').value;
    const dm = document.getElementById('droamFilter').value;
    let vis = 0;
    document.querySelectorAll('#deviceBody tr.device-row').forEach(row => {{
      const nameMatch = !q || row.dataset.name.includes(q) || row.dataset.iccids.toLowerCase().includes(q);
      const statusMatch = !st || row.dataset.status === st;
      const droamMatch = !dm || (row.querySelector('td[data-sort]')?.dataset.sort ?? '') === dm ||
                         (row.cells[3]?.dataset.sort ?? '') === dm;
      const show = nameMatch && statusMatch && droamMatch;
      row.style.display = show ? '' : 'none';
      const detailRow = document.getElementById(row.dataset.detail);
      if (detailRow) {{
        if (!show) detailRow.classList.remove('open'); // collapse if hidden
        detailRow.style.display = show ? '' : 'none';
      }}
      if (show) vis++;
    }});
    document.getElementById('visCount').textContent =
      vis + ' of {s["total_rms_devices"]} device' + ({s["total_rms_devices"]} !== 1 ? 's' : '');
    document.getElementById('emptyMsg').style.display = vis === 0 ? '' : 'none';
  }}

  function clearFilters() {{
    document.getElementById('searchBox').value = '';
    document.getElementById('statusFilter').value = '';
    document.getElementById('droamFilter').value = '';
    applyFilters();
  }}

  // init count on load
  applyFilters();
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
    parser.add_argument(
        "--model",
        default="OTD500",
        help=(
            "Only include devices whose model starts with this prefix "
            "(default: OTD500). Pass an empty string to include all models."
        ),
    )
    parser.add_argument(
        "--ssh",
        action="store_true",
        help=(
            "SSH into each online device to read /etc/config/simcard, discovering ICCIDs "
            "for all SIM slots (not just the active one). "
            "Credentials come from SSH_USER / SSH_PASS in .env (defaults: root / SSH agent). "
            "Requires paramiko (pip install paramiko)."
        ),
    )
    args = parser.parse_args()

    if not RMS_TOKEN:
        print("ERROR: RMS_API_TOKEN is not set. Copy .env.example to .env and fill it in.", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- RMS ---
    rms_devices = fetch_rms_devices()
    if args.model:
        before = len(rms_devices)
        rms_devices = [d for d in rms_devices if (d.get("model") or "").startswith(args.model)]
        print(f"  Model filter '{args.model}': {len(rms_devices)} of {before} devices kept\n", flush=True)

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

    # --- SSH enrichment ---
    if args.ssh:
        enrich_devices_with_ssh(
            rms_devices,
            ssh_user=SSH_USER,
            ssh_pass=SSH_PASS,
            ssh_key=None,
        )

    # --- Report ---
    report = build_report(rms_devices, droam_sims)
    report["model_filter"] = args.model

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
