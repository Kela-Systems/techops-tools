#!/usr/bin/env python3
"""
sim_audit.py — Cross-reference Teltonika RMS devices with Droam SIM inventory.

Generates a report showing:
  - Each Teltonika device and its SIM(s), flagged as known/unknown in Droam
  - All Droam SIMs not installed in any Teltonika device

Usage:
  cp .env.example .env       # fill in your credentials
  python3 sim_audit.py       # outputs report.json + report.md + report.html
  python3 sim_audit.py --rms-only     # skip Droam (useful while Droam creds aren't set)
  python3 sim_audit.py --no-rms-cmd   # skip RMS command-channel enrichment
  python3 sim_audit.py --no-ssh       # skip SSH fallback enrichment
  python3 sim_audit.py --no-html      # skip the interactive HTML report
  python3 sim_audit.py --no-droam-usage             # skip per-SIM usage detail calls
  python3 sim_audit.py --devices otd-kela-fob-05    # on-demand: scope to matching devices
  python3 sim_audit.py --iccids 8935201...,8935... # on-demand: scope by ICCID

Enrichment surfaces standby SIM slots RMS doesn't report as active and pairs each
device to its Tailscale node. Two paths, best-effort:

  1. RMS command channel (primary, on by default): relays `tailscale ip -4` +
     `uci show` to each device that is ONLINE in RMS, addressed by RMS device ID.
     Because the device is addressed by RMS identity, the tailscale IP it reports
     gives an authoritative RMS<->tailnet pairing (no name-guessing). Needs only
     RMS_API_TOKEN. Runs only for online devices to limit RMS API calls.

  2. SSH (fallback): for devices RMS could not verify, SSHes over Tailscale and
     reads the same data. Requires SSH_USER / SSH_PASS, `paramiko`, and the host
     being on the same tailnet as the devices.

Both resolve tailnet nodes via the local `tailscale` CLI. When a verified device's
tailnet hostname differs from its RMS name, the report flags it for a manual
rename so future runs match exactly.
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

SSH_USER = os.environ.get("SSH_USER", "")
SSH_PASS = os.environ.get("SSH_PASS", "")


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


def fetch_rms_rate_limit() -> dict | None:
    """
    Read the current RMS API quota from the X-RateLimit-* response headers.

    Costs one lightweight request; called at the end of a run so the numbers
    reflect everything the script just did. Best-effort — returns None on failure.
    """
    try:
        resp = requests.get(
            f"{RMS_API_BASE}/devices",
            headers=rms_headers(),
            params={"limit": 1},
            timeout=30,
        )
    except requests.RequestException:
        return None
    h = resp.headers
    try:
        limit = int(h["X-RateLimit-Limit"])
        remaining = int(h["X-RateLimit-Remaining"])
    except (KeyError, ValueError):
        return None
    reset_ts = None
    try:
        reset_ts = int(h["X-RateLimit-Reset"])
    except (KeyError, ValueError):
        pass
    info = {
        "limit": limit,
        "remaining": remaining,
        "used": limit - remaining,
        "reset_ts": reset_ts,
        "reset_at": (datetime.fromtimestamp(reset_ts, tz=timezone.utc).isoformat()
                     if reset_ts else None),
    }
    return info


LICENSE_EXPIRING_DAYS = 30  # window (days) for the "expiring soon" flag


def _parse_rms_dt(value: str | None) -> datetime | None:
    """Parse an RMS timestamp ('YYYY-MM-DD HH:MM:SS') to a datetime, or None."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def extract_license(dev: dict) -> dict:
    """
    Pull the RMS Management-credit (license) info from a raw device object and
    derive status signals. All fields come from the existing /devices response,
    so this adds no API calls.
    """
    activated = bool(dev.get("credit_activated"))
    enabled = bool(dev.get("credit_enabled"))
    expire_raw = dev.get("credit_expire_date")
    expire_dt = _parse_rms_dt(expire_raw)

    days_left = None
    expired = False
    expiring_soon = False
    if expire_dt is not None:
        days_left = (expire_dt - datetime.now()).days
        expired = days_left < 0
        expiring_soon = 0 <= days_left <= LICENSE_EXPIRING_DAYS

    return {
        "pack": dev.get("management_credit_type_name"),
        "type": dev.get("management_credit_type"),
        "type_id": dev.get("management_credit_type_id"),
        "data_pack": dev.get("data_credit_type_name"),
        "activated": activated,
        "enabled": enabled,
        "active": activated and enabled,
        "auto_extend": bool(dev.get("auto_extend_credit")),
        "expire_date": expire_raw,
        "days_left": days_left,
        "expired": expired,
        "expiring_soon": expiring_soon,
        "monitoring_enabled": bool(dev.get("monitoring_enabled")),
        "extended_warranty": dev.get("extended_warranty"),
    }


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
        sim1["active"] = True
        sim1["source"] = "RMS"
        sims.append(sim1)
    sim2 = sim_block("_2")
    if sim2:
        sim2["slot"] = 2
        sim2["active"] = True
        sim2["source"] = "RMS"
        sims.append(sim2)

    # SSH/UCI-discovered standby slots (populated by enrich_devices_via_ssh).
    known_iccids = {s["iccid"] for s in sims}
    next_slot = (max((s["slot"] for s in sims), default=0)) + 1
    for extra in (dev.get("_uci_sims") or []):
        iccid = (extra.get("iccid") or "").strip()
        if not iccid or iccid in known_iccids:
            continue
        known_iccids.add(iccid)
        sims.append({
            "iccid": iccid,
            "imsi": "",
            "operator": "",
            "operator_number": "",
            "sim_state": "",
            "connection_type": "",
            "connection_state": "",
            "network_state": "",
            "mobile_ip": "",
            "signal_dbm": None,
            "rsrp": None,
            "rsrq": None,
            "sinr": None,
            "slot": extra.get("slot") or next_slot,
            "active": False,
            "source": extra.get("source") or "Device UCI config (slot inactive / standby)",
        })
        next_slot += 1

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
        "tailscale": dev.get("_tailscale") or {"name": None, "ip": None, "online": False, "match": "none"},
        "license": extract_license(dev),
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


def fetch_droam_sims(with_usage: bool = True,
                     usage_iccids: set[str] | None = None) -> list[dict]:
    """Fetch all SIM records from Droam via its internal AJAX table endpoint.

    When with_usage is set, also enriches each SIM with authoritative used/limit
    from the per-SIM detail endpoint (one extra request per SIM). usage_iccids, if
    given, scopes that per-SIM fetch to just those ICCIDs (on-demand runs).
    """
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

    if with_usage and sims:
        enrich_droam_usage(s, sims, only_iccids=usage_iccids)

    return sims


def iccid_canonical(iccid: str | None) -> str:
    """
    Canonical ICCID key for matching across sources.

    RMS/UCI and Droam can report the same SIM with different lengths (18/19/20
    digits) and an optional trailing Luhn check digit or `F` padding. We reduce
    to digits-only; callers also compare on the 18-digit prefix to bridge those
    length differences. Returns '' for empty/garbage input.
    """
    digits = _re.sub(r"\D", "", iccid or "")
    return digits


def _iccid_keys(*iccids: str | None) -> set[str]:
    """Canonical match keys for one or more ICCIDs: full digits + 18-digit prefix."""
    keys: set[str] = set()
    for v in iccids:
        c = iccid_canonical(v)
        if c:
            keys.add(c)
            if len(c) >= 18:
                keys.add(c[:18])
    return keys


def filter_scope_devices(devices: list[dict], names: str, iccids: str) -> list[dict]:
    """
    Return the subset of raw RMS devices matching --devices (name substrings) or
    --iccids (active-slot ICCIDs). Empty filters → all devices unchanged.
    """
    name_terms = [t.strip().lower() for t in (names or "").split(",") if t.strip()]
    want_iccids: set[str] = set()
    for t in (iccids or "").split(","):
        want_iccids |= _iccid_keys(t)
    if not name_terms and not want_iccids:
        return devices

    out = []
    for d in devices:
        nm = (d.get("name") or "").lower()
        name_hit = any(term in nm for term in name_terms)
        icc_hit = bool(want_iccids & _iccid_keys(d.get("iccid"), d.get("iccid_2")))
        if name_hit or icc_hit:
            out.append(d)
    return out


def _to_float(text: str | None) -> float | None:
    """Parse a Droam numeric cell (e.g. '1,234.50', '-', '') to float or None."""
    if text is None:
        return None
    t = str(text).strip().replace(",", "")
    if t in ("", "-", "—", "N/A", "n/a"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def parse_plan_limit_mb(plan_text: str) -> float | None:
    """
    Best-effort extraction of a data cap (in MB) from a Droam plan name,
    e.g. 'Global 10GB Monthly' -> 10240, '500 MB' -> 500. None if not found.
    """
    if not plan_text:
        return None
    m = _re.search(r"(\d+(?:\.\d+)?)\s*(TB|GB|MB)\b", plan_text, _re.IGNORECASE)
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2).upper()
    return value * {"MB": 1, "GB": 1024, "TB": 1024 * 1024}[unit]


_DROAM_USAGE_PATH = "/data/sim_card/last_usages"


def _droam_num(text: str | None) -> float | None:
    """
    Parse a Droam-formatted number to float. Droam uses space (or NBSP) thousands
    separators and a '.' decimal, e.g. '299 500.00' -> 299500.0, '1 048 576' -> 1048576.0.
    """
    if text is None:
        return None
    cleaned = str(text).replace("\u00a0", "").replace(" ", "").replace(",", "").strip()
    cleaned = _re.sub(r"[^\d.]", "", cleaned)
    if not cleaned or cleaned == ".":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_droam_usage_html(html: str) -> dict:
    """
    Parse the `html` from POST /data/sim_card/last_usages/{id} into usage numbers.

    Returns {used_mb, limit_mb, daily_mb, day_labels}. Fields are None/[] when absent.
    'This month' -> used, 'Init balance' -> plan data cap (limit).
    """
    html = html or ""

    def after_label(label: str) -> float | None:
        m = _re.search(_re.escape(label) + r"(.*?)MB", html, _re.S)
        if not m:
            return None
        # last number-ish run before the MB unit
        nums = _re.findall(r"[\d\u00a0 ,.]+", m.group(1))
        return _droam_num(nums[-1]) if nums else None

    used_mb = after_label("This month")
    limit_mb = after_label("Init balance")

    daily_mb: list[float] = []
    m = _re.search(r"barData\s*=\s*\[([^\]]*)\]", html)
    if m:
        daily_mb = [float(x) for x in _re.findall(r"-?\d+(?:\.\d+)?", m.group(1))]

    day_labels: list[str] = []
    m = _re.search(r"days\s*=\s*\[([^\]]*)\]", html)
    if m:
        day_labels = _re.findall(r'"([^"]*)"', m.group(1))

    return {"used_mb": used_mb, "limit_mb": limit_mb,
            "daily_mb": daily_mb, "day_labels": day_labels}


def fetch_droam_usage_one(session: requests.Session, sim_id, retries: int = 2) -> dict | None:
    """Fetch + parse per-SIM usage detail. None on failure (after retries).

    Retries with a short backoff to ride out transient throttling — this runs
    per-SIM across the whole fleet, so a small failure rate compounds otherwise.
    """
    base = DROAM_URL.rstrip("/")
    url = f"{base}{_DROAM_USAGE_PATH}/{sim_id}"
    for attempt in range(retries + 1):
        try:
            r = session.post(url, headers=_droam_ajax_headers(session), timeout=30)
            if r.status_code == 200:
                body = r.json()
                if body.get("ack") == "success":
                    return parse_droam_usage_html(body.get("html", ""))
        except (requests.RequestException, ValueError):
            pass
        if attempt < retries:
            _time.sleep(0.75 * (attempt + 1))
    return None


def enrich_droam_usage(session: requests.Session, raw_sims: list[dict],
                       max_workers: int = 6, only_iccids: set[str] | None = None) -> int:
    """
    Attach `_usage` (authoritative used/limit) to each raw Droam SIM via the
    per-SIM detail endpoint. Mutates in place; returns how many were enriched.
    Parallel but modest, since Droam is session-sensitive.

    When only_iccids is given (canonical keys incl. 18-digit prefixes), only SIMs
    whose ICCID matches are fetched — used to scope on-demand runs to a few devices.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    targets = []
    for r in raw_sims:
        if not r.get("id"):
            continue
        if only_iccids is not None:
            if not (_iccid_keys(normalise_droam_sim(r).get("iccid")) & only_iccids):
                continue
        targets.append(r)
    scope_note = f" scoped to {len(targets)} of {len(raw_sims)}" if only_iccids is not None else ""
    print(f"Fetching per-SIM usage from Droam ({len(targets)} SIMs{scope_note})...", flush=True)
    done = 0

    def _one(raw: dict):
        return raw, fetch_droam_usage_one(session, raw["id"])

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_one, r) for r in targets]
        for i, fut in enumerate(as_completed(futures), 1):
            raw, usage = fut.result()
            if usage:
                raw["_usage"] = usage
                done += 1
            if i % 100 == 0:
                print(f"  ...{i}/{len(targets)}", flush=True)
    print(f"  → usage enriched for {done}/{len(targets)} SIMs\n", flush=True)
    return done


def normalise_droam_sim(raw: dict) -> dict:
    """Extract clean plain-text fields from a raw Droam SIM record (which contains HTML)."""

    def plain(field: str) -> str:
        val = raw.get(field) or ""
        return _strip_html(str(val)) if "<" in str(val) else str(val).strip()

    # Droam renders these as icon/badge spans where the label lives in a
    # data-tooltip-text attribute or a bg-*/text-* CSS class rather than as text,
    # so strip-tags alone yields ''. Parse the attribute first, then fall back.
    def _badge(field: str) -> str:
        html = str(raw.get(field) or "")
        m = _re.search(r'data-tooltip-text="([^"]+)"', html)
        if m:
            return m.group(1).strip()
        return _strip_html(html)

    # status: "Active", "Inactive", "Suspended", etc.
    status_text = _badge("status")
    status_raw = str(raw.get("status") or "")
    if not status_text:
        if "bg-active" in status_raw:
            status_text = "Active"
        elif "bg-inactive" in status_raw:
            status_text = "Inactive"
        elif "suspend" in status_raw.lower():
            status_text = "Suspended"
    low = status_text.lower()
    if "inactive" in low:
        status_text = "Inactive"
    elif "active" in low:
        status_text = "Active"

    # in_session badge: "Online" / "Offline"
    session_text = _badge("in_session")
    if not session_text:
        sess_raw = str(raw.get("in_session") or "")
        if "not-available" in sess_raw:
            session_text = "Offline"
        elif "available" in sess_raw:
            session_text = "Online"

    # plan text (embedded in HTML div)
    plan_text = plain("plan")

    # tags: badge labels (tooltip attr, else the visible text of each badge)
    tags_html = str(raw.get("tags") or "")
    tag_texts = [t.strip() for t in _re.findall(r'data-tooltip-text="([^"]+)"', tags_html) if t.strip()]
    if not tag_texts:
        tag_texts = [t.strip() for t in _re.findall(r">\s*([^<>]+?)\s*<", tags_html) if t.strip()]

    iccid = plain("iccid") or (raw.get("nickname") or "").strip()

    # --- usage ---
    # List endpoint gives `this_month_mb`; the per-SIM detail endpoint (attached
    # as `_usage` by enrich_droam_usage) gives authoritative used + plan limit
    # ("Init balance"), which the list view lacks.
    usage = raw.get("_usage") or {}
    used_mb = usage.get("used_mb")
    if used_mb is None:
        used_mb = _to_float(plain("this_month_mb"))
    available_mb = _to_float(plain("available_mb"))   # remaining; often '-' in list view
    days_left = _to_float(plain("days_left"))          # often '-' in list view
    daily_mb = usage.get("daily_mb") or []

    # Plan limit: authoritative "Init balance" > used+remaining > parsed plan name.
    limit_mb = usage.get("limit_mb")
    if limit_mb is None and used_mb is not None and available_mb is not None:
        limit_mb = used_mb + available_mb
    if limit_mb is None:
        limit_mb = parse_plan_limit_mb(plan_text)
    # Remaining, if not given directly, from limit - used.
    if available_mb is None and limit_mb is not None and used_mb is not None:
        available_mb = round(limit_mb - used_mb, 2)

    pct_used = None
    if limit_mb and limit_mb > 0 and used_mb is not None:
        pct_used = round(used_mb / limit_mb * 100, 1)

    status_active = status_text.lower() == "active"
    at_risk = pct_used is not None and pct_used >= 80.0
    # Idle = active SIM with effectively no usage this period.
    idle = status_active and used_mb is not None and used_mb < 1.0

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
        "throttle": plain("throttle"),
        # usage / signals
        "data_used_mb": used_mb,
        "data_available_mb": available_mb,
        "data_limit_mb": limit_mb,
        "data_used_pct": pct_used,
        "data_daily_mb": daily_mb,
        "days_left": days_left,
        "at_risk": at_risk,
        "idle": idle,
        "tags": tag_texts,
        "is_esim": bool(raw.get("eid") and raw.get("eid") != "-"),
        "_raw_id": raw.get("id"),
    }


# ---------------------------------------------------------------------------
# SSH / UCI enrichment
# ---------------------------------------------------------------------------

_ICCID_RE = _re.compile(r"\b(\d{18,20}[0-9Ff]?)\b")


def _clean_host(wan_ip: str | None) -> str:
    """Strip CIDR mask / whitespace from an RMS wan_ip to get a bare host."""
    if not wan_ip:
        return ""
    return wan_ip.split("/")[0].strip().strip('"')


def _parse_uci_iccids(uci_text: str) -> list[dict]:
    """
    Parse ICCID values out of a device's UCI dump.

    Teltonika RUTOS/OTDOS stores per-SIM settings under `simcard` (and related
    configs); when a SIM is configured for a slot its ICCID often appears as
    `...iccid='<value>'`. We pull every such value and try to associate it with
    a slot number inferred from the option path (sim1 → 1, sim2 → 2, ...).
    """
    found: dict[str, int | None] = {}
    for line in uci_text.splitlines():
        line = line.strip()
        if "iccid" not in line.lower():
            continue
        m = _re.search(r"=['\"]?([0-9A-Fa-f]{18,21})['\"]?\s*$", line)
        if not m:
            continue
        iccid = m.group(1).strip()
        slot: int | None = None
        # RUTOS/OTDOS uses `simcard.@sim[0]` (0-based) → slot 1; some configs use `sim1` (1-based).
        bracket = _re.search(r"@?sim\[(\d+)\]", line, _re.IGNORECASE)
        if bracket:
            slot = int(bracket.group(1)) + 1
        else:
            plain = _re.search(r"sim[_ ]?(\d)", line, _re.IGNORECASE)
            if plain:
                slot = int(plain.group(1))
        found.setdefault(iccid, slot)
    return [{"iccid": ic, "slot": sl, "source": "Device UCI config (slot inactive / standby)"}
            for ic, sl in found.items()]


def _ssh_probe(host: str, timeout: int = 8) -> dict:
    """
    SSH into a device and return {"iccids": [...], "ts_ip": "100.x" | ""}.

    `ts_ip` is the device's own Tailscale IPv4 (`tailscale ip -4`), which lets us
    match it to the correct tailnet node authoritatively — regardless of how its
    RMS name compares to its tailnet hostname.
    """
    import paramiko  # imported lazily so --no-ssh works without the dependency

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            host,
            username=SSH_USER,
            password=SSH_PASS,
            timeout=timeout,
            banner_timeout=timeout,
            auth_timeout=timeout,
            look_for_keys=False,
            allow_agent=False,
        )

        ts_ip = ""
        try:
            _in, out, _err = client.exec_command("tailscale ip -4 2>/dev/null", timeout=timeout)
            for line in out.read().decode("utf-8", "replace").splitlines():
                line = line.strip()
                if line.startswith("100."):
                    ts_ip = line
                    break
        except Exception:
            pass

        # `uci show` dumps the full merged config; grep keeps output small.
        _in, out, _err = client.exec_command(
            "uci show 2>/dev/null | grep -i iccid", timeout=timeout
        )
        iccids = _parse_uci_iccids(out.read().decode("utf-8", "replace"))
        return {"iccids": iccids, "ts_ip": ts_ip}
    finally:
        try:
            client.close()
        except Exception:
            pass


def _tailscale_nodes() -> dict:
    """
    Return {hostname_lower: {"ip": "100.x", "online": bool}} from `tailscale status`.

    Teltonika devices join the tailnet under a MagicDNS hostname that matches
    their RMS device name, so this lets us reach them at their 100.x address
    instead of the (non-routable) carrier/LAN wan_ip RMS reports. Returns {} if
    the tailscale CLI is unavailable.
    """
    import subprocess

    for cli in ("tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
        try:
            proc = subprocess.run(
                [cli, "status", "--json"], capture_output=True, text=True, timeout=25
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0 or not proc.stdout:
            continue
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            continue

        nodes: dict[str, dict] = {}
        for section in ("Self", "Peer"):
            entries = data.get(section) or {}
            if section == "Self":
                entries = {"self": entries}
            for peer in entries.values():
                host = (peer.get("HostName") or "").strip()
                ips = peer.get("TailscaleIPs") or []
                ip = next((a for a in ips if a.startswith("100.")), ips[0] if ips else None)
                if host and ip:
                    nodes[host.lower()] = {
                        "name": host,
                        "ip": ip,
                        "online": bool(peer.get("Online")),
                    }
        return nodes
    return {}


_DEVTYPE_PREFIX = _re.compile(
    r"^(otd500|otd|rutm08|rutm52|rutm|rutx50|rutx12|rutx\d*|rutc50|rutc\d*|rut)[-_]",
    _re.IGNORECASE,
)


def _norm_device_name(name: str | None) -> str:
    """Normalise a device/tailnet name for matching: drop the otd-/rut- device-type
    prefix and a trailing ' (old)' so `otd-kela-fob-27` == tailnet `kela-fob-27`."""
    n = (name or "").strip().lower()
    n = _re.sub(r"\s*\(old\)$", "", n)
    n = _DEVTYPE_PREFIX.sub("", n)
    return n.strip("-_ ")


def match_devices_to_tailnet(devices: list[dict], nodes: dict) -> dict:
    """
    Set dev['_tailscale'] = {name, ip, online, match} on every device.

    match is 'exact' (name identical), 'normalized' (matched after stripping the
    device-type prefix), or 'none' (no tailnet node → a pairing gap). Returns
    stats counts. Ambiguous normalized matches prefer a candidate that shares the
    device's prefix, then a bare (prefix-less) node, then an online one.
    """
    from collections import defaultdict

    norm_index: dict[str, list[str]] = defaultdict(list)
    for host_lower in nodes:
        norm_index[_norm_device_name(host_lower)].append(host_lower)

    stats = {"exact": 0, "normalized": 0, "none": 0}
    for dev in devices:
        name = (dev.get("name") or "").strip()
        nl = name.lower()
        match_key: str | None = None
        kind = "none"

        if nl in nodes:
            match_key, kind = nl, "exact"
        else:
            cands = norm_index.get(_norm_device_name(name), [])
            if cands:
                pref_m = _DEVTYPE_PREFIX.match(nl)
                pref = pref_m.group(1) if pref_m else ""

                def _score(h: str) -> tuple:
                    h_pref_m = _DEVTYPE_PREFIX.match(h)
                    h_pref = h_pref_m.group(1) if h_pref_m else ""
                    return (
                        1 if pref and h_pref == pref else 0,   # same device-type prefix
                        1 if not h_pref_m else 0,               # bare (no prefix) node
                        1 if nodes[h]["online"] else 0,         # online
                    )

                match_key = sorted(cands, key=_score, reverse=True)[0]
                kind = "normalized"

        if match_key:
            dev["_tailscale"] = {
                "name": nodes[match_key]["name"],
                "ip": nodes[match_key]["ip"],
                "online": nodes[match_key]["online"],
                "match": kind,
            }
            stats[kind] += 1
        else:
            dev["_tailscale"] = {"name": None, "ip": None, "online": False, "match": "none"}
            stats["none"] += 1
    return stats


# ---------------------------------------------------------------------------
# RMS command channel enrichment (authoritative, identity-anchored by RMS ID)
# ---------------------------------------------------------------------------
#
# RMS can relay a shell command to a device addressed purely by its RMS device
# ID (POST /devices/{id}/command), then stream the result back over a status
# channel. Because the device is addressed by RMS identity, the tailscale IP it
# reports is provably from *that* device — giving an authoritative RMS<->tailnet
# pairing with no name-guessing and no wrong-box risk. Works for any device that
# is online in RMS, including ones with no tailnet name match at all.

_RMS_STATUS_BASE = RMS_API_BASE.rsplit("/api", 1)[0] + "/status/channel"
_RMS_PROBE_CMD = "tailscale ip -4 2>/dev/null; echo '===UCI==='; uci show 2>/dev/null | grep -i iccid"


def rms_run_command(device_id: int, command: str,
                    poll_interval: float = 3.0, timeout: float = 45.0) -> tuple[str, str]:
    """
    Fire a command at a device via RMS and poll for its output.

    Returns (status, value) where status is 'completed' | 'error' | 'timeout'.
    Best-effort: any transport hiccup is reported as ('error', <reason>).
    """
    import time

    try:
        resp = requests.post(
            f"{RMS_API_BASE}/devices/{device_id}/command",
            headers={**rms_headers(), "Content-Type": "application/json"},
            json={"command": command},
            timeout=30,
        )
    except requests.RequestException as exc:
        return "error", f"post {type(exc).__name__}"
    if resp.status_code != 200:
        return "error", f"HTTP {resp.status_code}"
    channel = (resp.json().get("meta") or {}).get("channel")
    if not channel:
        return "error", "no channel"

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll_interval)
        try:
            s = requests.get(f"{_RMS_STATUS_BASE}/{channel}", headers=rms_headers(), timeout=30)
        except requests.RequestException:
            continue
        if s.status_code != 200:
            continue
        events = (s.json().get("data") or {}).get(str(device_id)) or []
        if events and events[-1].get("status") in ("completed", "error"):
            return events[-1]["status"], events[-1].get("value", "") or ""
    return "timeout", ""


def _parse_rms_probe(value: str) -> tuple[str, list[dict]]:
    """Split combined probe output into (tailscale_ipv4, uci_iccids)."""
    head, _, tail = value.partition("===UCI===")
    ts_ip = ""
    for line in head.splitlines():
        line = line.strip()
        if line.startswith("100."):
            ts_ip = line
            break
    return ts_ip, _parse_uci_iccids(tail)


def enrich_devices_via_rms(devices: list[dict], nodes: dict, max_workers: int = 8) -> dict:
    """
    Enrich devices through the RMS command channel (only devices online in RMS).

    Same mutations as the SSH path (`_uci_sims`, verified `_tailscale`), but
    authoritative: the pairing is anchored to the RMS device ID rather than a
    guessed tailnet address.
    """
    if not RMS_TOKEN:
        print("RMS_API_TOKEN not set — skipping RMS command enrichment.\n", file=sys.stderr)
        return {"attempted": 0, "enriched": 0, "extra_slots": 0, "failed": 0,
                "skipped": 0, "verified": 0, "renames": 0}

    from concurrent.futures import ThreadPoolExecutor, as_completed

    ip_to_node = {v["ip"]: v for v in nodes.values()}
    # Raw RMS status: 1 == online (see extract_device_record).
    online = [d for d in devices if d.get("status") == 1 and d.get("id")]
    stats = {"attempted": len(online), "enriched": 0, "extra_slots": 0, "failed": 0,
             "skipped": len(devices) - len(online), "verified": 0, "renames": 0}

    print(f"Enriching via RMS command channel ({len(online)} online devices, "
          f"{stats['skipped']} offline skipped)...", flush=True)

    def _probe(dev: dict):
        status, value = rms_run_command(dev["id"], _RMS_PROBE_CMD)
        if status == "timeout":  # RMS command relay is occasionally flaky — retry once
            status, value = rms_run_command(dev["id"], _RMS_PROBE_CMD)
        return dev, status, value

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_probe, dev): dev for dev in online}
        for fut in as_completed(futures):
            dev = futures[fut]
            try:
                dev, status, value = fut.result()
            except Exception as exc:  # noqa: BLE001
                stats["failed"] += 1
                print(f"  {dev.get('name')} (#{dev.get('id')}): rms cmd skip — {type(exc).__name__}", flush=True)
                continue
            if status != "completed":
                stats["failed"] += 1
                print(f"  {dev.get('name')} (#{dev.get('id')}): rms cmd {status}", flush=True)
                continue

            ts_ip, iccids = _parse_rms_probe(value)

            node = ip_to_node.get(ts_ip) if ts_ip else None
            if node:
                rms_name = (dev.get("name") or "").strip()
                mismatch = node["name"].strip().lower() != rms_name.lower()
                dev["_tailscale"] = {
                    "name": node["name"], "ip": node["ip"], "online": True,
                    "match": "verified", "reported_ip": ts_ip, "name_mismatch": mismatch,
                }
                stats["verified"] += 1
                if mismatch:
                    stats["renames"] += 1
                    print(f"  {dev.get('name')} (#{dev.get('id')}): tailnet node is "
                          f"'{node['name']}' — RMS name differs (rename?)", flush=True)

            active = {(dev.get("iccid") or "").strip(), (dev.get("iccid_2") or "").strip()}
            extras = [e for e in iccids if e["iccid"] and e["iccid"] not in active]
            if extras:
                dev["_uci_sims"] = extras
                stats["enriched"] += 1
                stats["extra_slots"] += len(extras)
                print(f"  {dev.get('name')} (#{dev.get('id')}): +{len(extras)} standby slot(s)", flush=True)

    print(
        f"  → RMS command: {stats['enriched']}/{stats['attempted']} enriched "
        f"({stats['extra_slots']} extra slots), {stats['verified']} tailnet-verified, "
        f"{stats['renames']} name mismatches, {stats['failed']} failed, "
        f"{stats['skipped']} offline skipped\n",
        flush=True,
    )
    return stats


def _resolve_ssh_target(dev: dict, wanip_fallback: bool) -> str | None:
    """
    Pick the host to SSH to, using the tailnet match recorded on the device.

    Returns None when the device should be skipped (offline tailnet node, or no
    routable address). The RMS wan_ip is only used when wanip_fallback is set,
    since those carrier/LAN IPs are almost never routable and just cause slow
    timeouts.
    """
    ts = dev.get("_tailscale") or {}
    if ts.get("ip"):
        return ts["ip"] if ts.get("online") else None
    if wanip_fallback:
        return _clean_host(dev.get("wan_ip")) or None
    return None


def enrich_devices_via_ssh(devices: list[dict], nodes: dict, max_workers: int = 12,
                           wanip_fallback: bool = False, skip_verified: bool = False) -> dict:
    """
    Mutates each raw RMS device dict in-place:
      - `_uci_sims`: ICCIDs from UCI config that RMS didn't report (standby slots)
      - `_tailscale`: upgraded to match='verified' using the device's own
        `tailscale ip -4`, matched to the tailnet node by IP. When the verified
        tailnet hostname differs from the RMS name, sets name_mismatch so the
        report can flag it for a manual rename.

    Reaches devices over Tailscale and runs the SSH probes in parallel. Failures
    per device are swallowed so the audit still completes.

    When skip_verified is set (SSH acting as a fallback behind the RMS command
    channel), devices already verified by RMS are left alone so we don't SSH twice.
    """
    if not SSH_USER or not SSH_PASS:
        print(
            "SSH_USER / SSH_PASS not set in .env — skipping SSH/UCI enrichment.\n",
            file=sys.stderr,
        )
        return {"attempted": 0, "enriched": 0, "extra_slots": 0, "failed": 0,
                "skipped": 0, "verified": 0, "renames": 0}

    try:
        import paramiko  # noqa: F401
    except ImportError:
        print(
            "paramiko is not installed — skipping SSH/UCI enrichment.\n"
            "  pip install paramiko   (or run with --no-ssh)\n",
            file=sys.stderr,
        )
        return {"attempted": 0, "enriched": 0, "extra_slots": 0, "failed": 0,
                "skipped": 0, "verified": 0, "renames": 0}

    from concurrent.futures import ThreadPoolExecutor, as_completed

    print("Enriching devices via SSH (standby SIM slots + tailnet IP verification)...", flush=True)
    ip_to_node = {v["ip"]: v for v in nodes.values()}

    # Resolve targets first so we only spawn SSH work for reachable devices.
    targets: list[tuple[dict, str]] = []
    stats = {"attempted": 0, "enriched": 0, "extra_slots": 0, "failed": 0,
             "skipped": 0, "verified": 0, "renames": 0}
    for dev in devices:
        if skip_verified and (dev.get("_tailscale") or {}).get("match") == "verified":
            stats["skipped"] += 1
            continue
        host = _resolve_ssh_target(dev, wanip_fallback)
        if not host:
            stats["skipped"] += 1
            continue
        targets.append((dev, host))
    stats["attempted"] = len(targets)

    def _probe(dev: dict, host: str):
        active = {
            (dev.get("iccid") or "").strip(),
            (dev.get("iccid_2") or "").strip(),
        }
        result = _ssh_probe(host)
        extras = [e for e in result["iccids"] if e["iccid"] and e["iccid"] not in active]
        return dev, host, extras, result["ts_ip"]

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_probe, dev, host): (dev, host) for dev, host in targets}
        for fut in as_completed(futures):
            dev, host = futures[fut]
            try:
                dev, host, new_extras, ts_ip = fut.result()
            except Exception as exc:  # noqa: BLE001 — best-effort, keep going
                stats["failed"] += 1
                print(f"  {dev.get('name')} ({host}): ssh skip — {type(exc).__name__}", flush=True)
                continue

            # Authoritative tailnet pairing via the device's own tailscale IP.
            node = ip_to_node.get(ts_ip) if ts_ip else None
            if node:
                rms_name = (dev.get("name") or "").strip()
                mismatch = node["name"].strip().lower() != rms_name.lower()
                dev["_tailscale"] = {
                    "name": node["name"], "ip": node["ip"], "online": True,
                    "match": "verified", "reported_ip": ts_ip, "name_mismatch": mismatch,
                }
                stats["verified"] += 1
                if mismatch:
                    stats["renames"] += 1
                    print(f"  {dev.get('name')} ({host}): tailnet node is "
                          f"'{node['name']}' — RMS name differs (rename?)", flush=True)

            if new_extras:
                dev["_uci_sims"] = new_extras
                stats["enriched"] += 1
                stats["extra_slots"] += len(new_extras)
                print(f"  {dev.get('name')} ({host}): +{len(new_extras)} standby slot(s)", flush=True)

    print(
        f"  → SSH: {stats['enriched']}/{stats['attempted']} enriched "
        f"({stats['extra_slots']} extra slots), {stats['verified']} tailnet-verified, "
        f"{stats['renames']} name mismatches, {stats['failed']} failed, "
        f"{stats['skipped']} skipped\n",
        flush=True,
    )
    return stats


# ---------------------------------------------------------------------------
# Cross-reference & report
# ---------------------------------------------------------------------------

def build_report(devices: list[dict], droam_sims: list[dict] | None,
                 rms_api: dict | None = None,
                 droam_usage_fetched_at: str | None = None,
                 perf: dict | None = None,
                 scoped: bool = False) -> dict:
    # Index Droam SIMs by canonical ICCID (+ 18-digit prefix) for robust matching
    # across RMS/UCI/Droam length & check-digit differences.
    droam_norm: list[tuple[str, dict]] = []
    droam_by_canon: dict[str, dict] = {}
    droam_by_p18: dict[str, dict] = {}
    if droam_sims is not None:
        for sim in droam_sims:
            n = normalise_droam_sim(sim)
            canon = iccid_canonical(n.get("iccid"))
            if not canon:
                continue
            droam_norm.append((canon, n))
            droam_by_canon.setdefault(canon, n)
            if len(canon) >= 18:
                droam_by_p18.setdefault(canon[:18], n)

    def _match_droam(iccid: str) -> dict | None:
        c = iccid_canonical(iccid)
        if not c:
            return None
        m = droam_by_canon.get(c)
        if m is None and len(c) >= 18:
            m = droam_by_p18.get(c[:18])
        return m

    matched_canons: set[str] = set()
    device_rows = []
    for dev in devices:
        rec = extract_device_record(dev)
        for sim in rec["sims"]:
            if droam_sims is not None:
                droam_match = _match_droam(sim["iccid"])
                sim["in_droam"] = droam_match is not None
                sim["droam_info"] = droam_match
                if droam_match is not None:
                    matched_canons.add(iccid_canonical(droam_match.get("iccid")))
            else:
                sim["in_droam"] = None  # Droam not queried
                sim["droam_info"] = None
        device_rows.append(rec)

    # In scoped mode only a subset of devices is loaded, so "not in any device"
    # can't be computed meaningfully — skip the orphan list.
    orphan_droam_sims: list[dict] = []
    if droam_sims is not None and not scoped:
        for canon, sim_rec in droam_norm:
            if canon not in matched_canons:
                orphan_droam_sims.append(sim_rec)

    def _count_signal(flag: str) -> int | None:
        if droam_sims is None:
            return None
        return sum(1 for d in device_rows for s in d["sims"]
                   if s.get("droam_info") and s["droam_info"].get(flag))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "droam_usage_fetched_at": droam_usage_fetched_at,
        "perf": perf,
        "summary": {
            "total_rms_devices": len(device_rows),
            "total_rms_sims": sum(len(d["sims"]) for d in device_rows),
            "total_droam_sims": len(droam_by_canon) if droam_sims is not None else None,
            "droam_sims_found_in_rms": len(matched_canons) if droam_sims is not None else None,
            "rms_sims_not_in_droam": sum(
                1 for d in device_rows for s in d["sims"] if s["in_droam"] is False
            ) if droam_sims is not None else None,
            "droam_sims_not_in_any_device": (len(orphan_droam_sims)
                                             if (droam_sims is not None and not scoped) else None),
            "sims_at_risk": _count_signal("at_risk"),
            "sims_idle": _count_signal("idle"),
            "licenses_expired": sum(1 for d in device_rows if (d.get("license") or {}).get("expired")),
            "licenses_expiring_soon": sum(1 for d in device_rows if (d.get("license") or {}).get("expiring_soon")),
        },
        "rms_api": rms_api,
        "devices": device_rows,
        "droam_sims_not_in_any_teltonika": orphan_droam_sims,
    }


def _fmt_rms_api(rms_api: dict | None) -> str | None:
    """One-line human summary of RMS API quota, or None if unavailable."""
    if not rms_api:
        return None
    used = rms_api.get("used")
    limit = rms_api.get("limit")
    remaining = rms_api.get("remaining")
    reset_ts = rms_api.get("reset_ts")
    parts = f"{used:,} / {limit:,} used ({remaining:,} remaining)"
    if reset_ts:
        # Show in the local timezone the machine is running in.
        reset_local = datetime.fromtimestamp(reset_ts).astimezone()
        now = datetime.now(reset_local.tzinfo)
        days = (reset_local - now).days
        parts += f" · cycle resets {reset_local:%Y-%m-%d %H:%M %Z} (~{days}d)"
    return parts


def _fmt_license_md(lic: dict | None) -> str:
    """One-line license/credit summary for Markdown."""
    lic = lic or {}
    pack = lic.get("pack") or "—"
    if not lic.get("expire_date"):
        return pack
    status = ("EXPIRED" if lic.get("expired")
              else "expiring soon" if lic.get("expiring_soon")
              else "active" if lic.get("active") else "inactive")
    days = f", {lic['days_left']}d left" if lic.get("days_left") is not None else ""
    extend = " · auto-extend" if lic.get("auto_extend") else ""
    return f"{pack} — {status} (expires {lic['expire_date'][:10]}{days}){extend}"


def write_markdown(report: dict, path: Path) -> None:
    lines = []
    ts = report["generated_at"]
    s = report["summary"]

    lines += [
        "# SIM Audit Report",
        "",
        f"Generated: {ts}",
    ]
    if report.get("droam_usage_fetched_at"):
        lines.append(f"Droam usage details fetched: {report['droam_usage_fetched_at']}")
    _perf = report.get("perf") or {}
    if _perf:
        _ac = _perf.get("api_calls", {})
        lines.append(
            f"Run: {_perf.get('total_seconds')}s · {_perf.get('api_calls_total')} HTTP calls "
            f"(RMS {_ac.get('rms', 0)}, Droam {_ac.get('droam', 0)})"
        )
    lines += [
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| RMS devices | {s['total_rms_devices']} |",
        f"| SIM slots in RMS | {s['total_rms_sims']} |",
        f"| RMS licenses expired | {s.get('licenses_expired', 0)} |",
        f"| RMS licenses expiring ≤{LICENSE_EXPIRING_DAYS}d | {s.get('licenses_expiring_soon', 0)} |",
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

    _rms_api_line = _fmt_rms_api(report.get("rms_api"))
    if _rms_api_line:
        lines.append(f"| RMS API quota | {_rms_api_line} |")

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
            f"| RMS license | {_fmt_license_md(dev.get('license'))} |",
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
                    used_txt = _fmt_mb(droam_info.get("data_used_mb"))
                    limit_txt = _fmt_mb(droam_info.get("data_limit_mb"))
                    pct = droam_info.get("data_used_pct")
                    usage_txt = f"{used_txt} / {limit_txt}" + (f" ({pct:.0f}%)" if pct is not None else "")
                    flags = []
                    if droam_info.get("at_risk"):
                        flags.append("AT RISK")
                    if droam_info.get("idle"):
                        flags.append("IDLE")
                    if flags:
                        usage_txt += "  [" + ", ".join(flags) + "]"
                    sim_lines += [
                        f"  - Droam status: {droam_info.get('status') or '—'} / Session: {droam_info.get('in_session') or '—'}",
                        f"  - Droam plan: {droam_info.get('plan') or '—'} ({droam_info.get('sim_card_type') or '—'})",
                        f"  - Data usage: {usage_txt}",
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
# Interactive HTML report
# ---------------------------------------------------------------------------

import html as _htmllib


def _e(val) -> str:
    """HTML-escape any value, mapping None/empty to an em dash."""
    if val is None or val == "":
        return "—"
    return _htmllib.escape(str(val))


def _device_droam_state(sims: list[dict]) -> tuple[int, str, str]:
    """Return (sort_code, badge_class, label) for a device's overall Droam match."""
    rated = [s for s in sims if s.get("in_droam") is not None]
    if not rated:
        return 3, "bg-secondary", "— no data"
    matched = sum(1 for s in rated if s.get("in_droam"))
    total = len(rated)
    if matched == total:
        return 0, "bg-success", f"✔ {matched}/{total}"
    if matched == 0:
        return 2, "bg-danger", f"✘ 0/{total}"
    return 1, "bg-warning text-dark", f"▲ {matched}/{total}"


def _fmt_mb(mb: float | None) -> str:
    """Human-friendly data size from MB."""
    if mb is None:
        return "—"
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    return f"{mb:.0f} MB"


def _fmt_usage_html(droam: dict) -> str:
    """Usage cell: used / limit (pct) + at-risk/idle badges + days left."""
    used = droam.get("data_used_mb")
    limit = droam.get("data_limit_mb")
    pct = droam.get("data_used_pct")
    parts = [f"{_fmt_mb(used)} used"]
    if limit:
        parts[0] = f"{_fmt_mb(used)} / {_fmt_mb(limit)}"
        if pct is not None:
            parts.append(f"({pct:.0f}%)")
    days = droam.get("days_left")
    if days is not None:
        parts.append(f"· {days:.0f}d left")
    daily = droam.get("data_daily_mb") or []
    if daily:
        parts.append(f"· 7d {_fmt_mb(sum(daily))}")
    badges = ""
    if droam.get("at_risk"):
        badges += " <span class='badge bg-danger'>at risk</span>"
    if droam.get("idle"):
        badges += " <span class='badge bg-warning text-dark'>idle</span>"
    return _e(" ".join(parts)) + badges


def _sim_detail_html(sim: dict) -> str:
    """Build the inner detail table for a single SIM slot."""
    droam = sim.get("droam_info")
    if sim.get("in_droam") is True:
        flag = '<span class="badge bg-success">✔ Droam</span>'
    elif sim.get("in_droam") is False:
        flag = '<span class="badge bg-danger">✘ Not in Droam</span>'
    else:
        flag = '<span class="badge bg-secondary">— (Droam not queried)</span>'

    def row(label: str, value: str) -> str:
        return (f"<tr><th class='text-nowrap pe-3 fw-normal text-muted small'>{label}</th>"
                f"<td>{value}</td></tr>")

    if sim.get("active", True):
        header = (f"<strong class='small'>Slot {_e(sim.get('slot'))}</strong> &nbsp; {flag}")
        wrapper_cls = "mb-2 p-2 border rounded"
        detail_rows = [
            row("ICCID", f"<code>{_e(sim.get('iccid'))}</code>"),
            row("IMSI", f"<code>{_e(sim.get('imsi'))}</code>"),
            row("Operator", f"{_e(sim.get('operator'))} ({_e(sim.get('operator_number'))})"),
            row("SIM state", _e(sim.get("sim_state"))),
            row("Connection", f"{_e(sim.get('connection_state'))} / {_e(sim.get('connection_type'))}"),
            row("Network", _e(sim.get("network_state"))),
            row("Mobile IP", _e(sim.get("mobile_ip"))),
            row("Signal", f"{_e(sim.get('signal_dbm'))} dBm &nbsp;&nbsp; RSRP {_e(sim.get('rsrp'))} "
                          f"&nbsp;&nbsp; RSRQ {_e(sim.get('rsrq'))} &nbsp;&nbsp; SINR {_e(sim.get('sinr'))}"),
        ]
    else:
        flag = flag.replace("✔ Droam", "✔ Droam (inactive slot)")
        header = (f"<strong class='small'>Slot {_e(sim.get('slot'))} "
                  f"<span class='text-muted fw-normal'>(inactive — from device config)</span></strong> "
                  f"&nbsp; {flag}")
        wrapper_cls = "mb-2 p-2 border rounded border-secondary opacity-75"
        detail_rows = [
            row("ICCID", f"<code>{_e(sim.get('iccid'))}</code>"),
            row("Source", _e(sim.get("source"))),
        ]

    if droam:
        tags = ", ".join(droam.get("tags") or []) or "—"
        detail_rows += [
            row("── Droam status", f"{_e(droam.get('status'))} / session: {_e(droam.get('in_session'))}"),
            row("── Droam plan", f"{_e(droam.get('plan'))} ({_e(droam.get('sim_card_type'))})"),
            row("── Data usage", _fmt_usage_html(droam)),
            row("── Droam IP / APN", f"{_e(droam.get('ip'))} / {_e(droam.get('apn'))}"),
            row("── Droam tags", _e(tags)),
            row("── Contract exp.", _e(droam.get("contract_expiration_at"))),
        ]

    return (f"<div class='{wrapper_cls}'>\n  {header}\n"
            f"  <table class='table table-sm mb-0 mt-1'>{''.join(detail_rows)}</table>\n</div>")


def write_html(report: dict, path: Path) -> None:
    s = report["summary"]
    devices = report["devices"]
    droam_queried = s["total_droam_sims"] is not None
    total_devices = len(devices)

    # ---- summary stat cards ----
    # `action` (optional) makes a tile a clickable filter shortcut; the JS reads
    # data-tile and applies the matching filter to the device list below.
    def card(value, label, border, action=None) -> str:
        cls = f"card text-center h-100 border-{border}"
        attrs = ""
        if action:
            cls += " tile-clickable"
            attrs = (f" role='button' tabindex='0' data-tile='{action}' "
                     f"title='Filter list by: {_e(label)}'")
        return (f"<div class='col'><div class='{cls}'{attrs}>"
                f"<div class='card-body py-2 px-1'>"
                f"<div class='h2 fw-bold text-{border} mb-0'>{value}</div>"
                f"<div class='small text-muted'>{label}</div></div></div></div>")

    ts_matched = sum(1 for d in devices if (d.get("tailscale") or {}).get("name"))
    ts_renames = sum(1 for d in devices if (d.get("tailscale") or {}).get("name_mismatch"))
    ts_available = ts_matched > 0

    cards = [
        card(s["total_rms_devices"], "Devices", "primary", action="clear"),
        card(s["total_rms_sims"], "SIM slots", "primary"),
    ]
    if ts_available:
        cards.append(card(f"{ts_matched}/{total_devices}", "Tailnet paired", "info", action="tailnet:match"))
        cards.append(card(total_devices - ts_matched, "Tailnet gaps", "secondary", action="tailnet:none"))
        cards.append(card(ts_renames, "Name mismatches", "warning", action="tailnet:mismatch"))

    lic_expired = sum(1 for d in devices if (d.get("license") or {}).get("expired"))
    lic_soon = sum(1 for d in devices if (d.get("license") or {}).get("expiring_soon"))
    cards.append(card(lic_expired, "License expired", "danger", action="signal:licexpired"))
    cards.append(card(lic_soon, "License expiring", "warning", action="signal:licsoon"))

    if droam_queried:
        cards += [
            card(s["total_droam_sims"], "Droam SIMs", "primary"),
            card(s["droam_sims_found_in_rms"], "Matched", "success", action="droam:0"),
            card(s["rms_sims_not_in_droam"], "RMS gaps", "danger", action="droam:2"),
        ]
        if s.get("droam_sims_not_in_any_device") is not None:
            cards.append(card(s["droam_sims_not_in_any_device"], "Droam unassigned",
                              "warning", action="orphans"))
        if s.get("sims_at_risk") is not None:
            cards.append(card(s["sims_at_risk"], "SIMs at risk", "danger", action="signal:atrisk"))
        if s.get("sims_idle") is not None:
            cards.append(card(s["sims_idle"], "Idle SIMs", "warning", action="signal:idle"))

    # ---- device rows ----
    rows: list[str] = []
    for i, dev in enumerate(devices):
        sims = dev["sims"]
        detail_id = f"d{dev.get('id') or i}"
        d_sort, d_badge, d_label = _device_droam_state(sims)
        status = dev["status"]
        status_badge = ('<span class="badge bg-success">● online</span>' if status == "online"
                        else '<span class="badge bg-secondary">○ offline</span>')
        status_sort = ("0online" if status == "online" else "1offline")

        iccids = " ".join(sim["iccid"] for sim in sims if sim.get("iccid"))
        tags_html = (" ".join(f"<span class=\"badge bg-light text-dark border\">{_e(t)}</span>"
                              for t in dev["tags"]) if dev["tags"] else "—")

        sim_cell_parts = []
        for sim in sims:
            if sim.get("in_droam") is True:
                b = ('<span class="badge bg-success">✔ Droam (inactive slot)</span>'
                     if not sim.get("active", True) else '<span class="badge bg-success">✔ Droam</span>')
            elif sim.get("in_droam") is False:
                b = '<span class="badge bg-danger">✘ Not in Droam</span>'
            else:
                b = '<span class="badge bg-secondary">—</span>'
            di = sim.get("droam_info") or {}
            usage_badge = ""
            if di.get("at_risk"):
                usage_badge = " <span class='badge bg-danger' title='SIM at/near plan limit'>at risk</span>"
            elif di.get("idle"):
                usage_badge = " <span class='badge bg-warning text-dark' title='active SIM with ~no usage this period'>idle</span>"
            sim_cell_parts.append(f"{b}&thinsp;<code class='small'>{_e(sim.get('iccid'))}</code>{usage_badge}")
        sim_cell = " ".join(sim_cell_parts) if sim_cell_parts else "—"
        dev_atrisk = any((sim.get("droam_info") or {}).get("at_risk") for sim in sims)
        dev_idle = any((sim.get("droam_info") or {}).get("idle") for sim in sims)

        # Tailscale pairing cell / gap indicator
        ts = dev.get("tailscale") or {}
        ts_name, ts_online, ts_match = ts.get("name"), ts.get("online"), ts.get("match")
        ts_mismatch = bool(ts.get("name_mismatch"))
        if ts_name:
            ts_state = "online" if ts_online else "offline"
            dot = ("<span class='text-success'>●</span>" if ts_online
                   else "<span class='text-secondary'>○</span>")
            if ts_match == "verified":
                marker = " <span class='badge bg-success' title='verified via device tailscale ip'>✓</span>"
            elif ts_match == "normalized":
                marker = " <span class='badge bg-light text-dark border' title='matched by normalized name (unverified)'>~</span>"
            else:
                marker = ""
            rename = (" <span class='badge bg-warning text-dark' title='RMS name differs from tailnet hostname — rename to align'>rename?</span>"
                      if ts_mismatch else "")
            ts_cell = f"{dot} <span class='small'>{_e(ts_name)}</span>{marker}{rename}"
            ts_sort = "0" + ts_name.lower()
        else:
            ts_state = "none"
            ts_cell = '<span class="badge bg-danger">✘ no tailnet</span>'
            ts_sort = "zzz"

        # License / RMS management credit cell + state
        lic = dev.get("license") or {}
        lic_exp = lic.get("expire_date") or ""
        if not lic.get("pack") and not lic_exp:
            lic_state, lic_badge = "none", "<span class='badge bg-secondary'>—</span>"
        elif lic.get("expired"):
            lic_state, lic_badge = "expired", "<span class='badge bg-danger'>expired</span>"
        elif lic.get("expiring_soon"):
            lic_state, lic_badge = "soon", "<span class='badge bg-warning text-dark'>expiring</span>"
        elif lic.get("active"):
            lic_state, lic_badge = "active", "<span class='badge bg-success'>active</span>"
        else:
            lic_state, lic_badge = "inactive", "<span class='badge bg-secondary'>inactive</span>"
        lic_days = (f" <span class='small text-muted'>({lic['days_left']}d)</span>"
                    if lic.get("days_left") is not None else "")
        lic_cell = (f"<div class='small'>{_e(lic.get('pack') or '—')}</div>"
                    f"{lic_badge} <span class='small text-muted'>{_e(lic_exp[:10])}</span>{lic_days}")
        lic_sort = lic_exp or "zzzz"

        device_facts = [
            ("Model", _e(dev["model"])), ("Serial", _e(dev["serial"])),
            ("MAC", _e(dev["mac"])), ("IMEI", _e(dev["imei"])),
            ("Firmware", _e(dev["firmware"])), ("WAN IP", _e(dev["wan_ip"])),
            ("WAN state", _e(dev["wan_state"])), ("Created", _e(dev["created_at"])),
            ("Last seen", _e(dev["last_connection_at"])),
            ("Tailscale", (f"{_e(ts_name)} ({ts_state}, {ts_match})"
                           + (" — RMS name differs, rename to align" if ts_mismatch else "")
                           if ts_name else "— no matching tailnet node")),
            ("Tailscale IP", _e(ts.get("ip"))),
            ("Tailscale IP (device-reported)", _e(ts.get("reported_ip"))),
            ("License pack", _e(lic.get("pack") or "—")),
            ("License status", (("active" if lic.get("active") else "inactive")
                                + (" · EXPIRED" if lic.get("expired") else
                                   " · expiring soon" if lic.get("expiring_soon") else ""))),
            ("License expires", (_e(lic_exp) + (f" ({lic['days_left']}d left)"
                                                if lic.get("days_left") is not None else ""))
                                 if lic_exp else "—"),
            ("Auto-extend credit", "Yes" if lic.get("auto_extend") else "No"),
            ("Data/Connect pack", _e(lic.get("data_pack") or "—")),
        ]
        device_table = "".join(
            f"<tr><th class='text-nowrap pe-3 fw-normal text-muted small'>{k}</th><td>{v}</td></tr>"
            for k, v in device_facts
        )
        sims_html = ("".join(_sim_detail_html(sim) for sim in sims)
                     if sims else "<div class='text-muted small'>No SIM data.</div>")

        rows.append(
            f"<tr class='device-row' data-name='{_e(dev['name']).lower()}' data-status='{status}' "
            f"data-model='{_e((dev.get('model') or '').lower())}' "
            f"data-ts='{ts_state}' data-tsname='{_e((ts_name or '')).lower()}' "
            f"data-mismatch='{1 if ts_mismatch else 0}' "
            f"data-atrisk='{1 if dev_atrisk else 0}' data-idle='{1 if dev_idle else 0}' "
            f"data-license='{lic_state}' "
            f"data-iccids='{_e(iccids)}' data-detail='{detail_id}'>"
            f"<td data-sort='{status_sort}'>{status_badge}</td>"
            f"<td><a class=\"dev-toggle text-decoration-none\" href=\"#\" data-target=\"{detail_id}\" "
            f"onclick=\"toggleDetail(this);return false;\"><span class=\"toggle-arrow me-1\">▶</span>"
            f"{_e(dev['name'])}</a></td>"
            f"<td class='small text-muted'>{_e(dev['model'])}</td>"
            f"<td data-sort='{_e(lic_sort)}'>{lic_cell}</td>"
            f"<td data-sort='{_e(ts_sort)}'>{ts_cell}</td>"
            f"<td data-sort='{d_sort}'><span class=\"badge {d_badge}\">{d_label}</span></td>"
            f"<td class='small'>{sim_cell}</td>"
            f"<td class='small text-muted'>{_e(dev['wan_ip'])}</td>"
            f"<td class='small'>{tags_html}</td></tr>"
            f"<tr class='detail-row' id='{detail_id}'><td colspan='9' class='bg-light p-0'>"
            f"<div class='row g-0 p-3'>"
            f"<div class='col-md-5 pe-md-3'><p class='fw-semibold mb-1 small text-uppercase text-muted'>Device</p>"
            f"<table class='table table-sm mb-0'>{device_table}</table></div>"
            f"<div class='col-md-7 mt-3 mt-md-0'><p class='fw-semibold mb-1 small text-uppercase text-muted'>SIMs</p>"
            f"{sims_html}</div></div></td></tr>"
        )

    # ---- orphan Droam SIMs ----
    orphan_section = ""
    if droam_queried and s.get("droam_sims_not_in_any_device") is not None:
        orphans = report.get("droam_sims_not_in_any_teltonika", [])
        orphan_rows = "".join(
            f"<tr><td><code class='small'>{_e(o.get('iccid'))}</code></td>"
            f"<td class='small'>{_e(o.get('msisdn'))}</td>"
            f"<td class='small'>{_e(o.get('operator'))}</td>"
            f"<td><span class=\"badge bg-secondary\">{_e(o.get('status'))}</span></td>"
            f"<td class='small'>{_e(o.get('plan'))}</td>"
            f"<td class='small'>{_fmt_usage_html(o)}</td>"
            f"<td class='small'>{_e(', '.join(o.get('tags') or []) or '')}</td>"
            f"<td class='small'>{'Yes' if o.get('is_esim') else 'No'}</td></tr>"
            for o in orphans
        )
        orphan_section = (
            f"<div class='card mt-4' id='orphanSection'><div class='card-header fw-semibold'>"
            f"Droam SIMs not installed in any Teltonika device "
            f"<span class='badge bg-warning text-dark ms-1'>{len(orphans)}</span></div>"
            f"<div class='card-body p-0'><div class='table-responsive'>"
            f"<table class='table table-sm table-hover mb-0'><thead class='table-light'><tr>"
            f"<th>ICCID</th><th>MSISDN</th><th>Operator</th><th>Status</th>"
            f"<th>Plan</th><th>Data usage</th><th>Tags</th><th>eSIM</th></tr></thead>"
            f"<tbody>{orphan_rows}</tbody></table></div></div></div>"
        )

    droam_filter = ("""        <select id="droamFilter" class="form-select form-select-sm" style="width:160px" onchange="applyFilters()">
          <option value="">All Droam states</option>
          <option value="0">Fully matched</option>
          <option value="1">Partially matched</option>
          <option value="2">Not in Droam</option>
        </select>
""" if droam_queried else "")

    tailnet_filter = ("""        <select id="tailnetFilter" class="form-select form-select-sm" style="width:170px" onchange="applyFilters()">
          <option value="">All tailnet</option>
          <option value="match">Paired</option>
          <option value="none">Unpaired (gaps)</option>
          <option value="offline">Paired but offline</option>
          <option value="mismatch">Name mismatch (rename?)</option>
        </select>
""" if ts_available else "")

    models = sorted({(dev.get("model") or "").strip() for dev in devices if (dev.get("model") or "").strip()})
    model_options = "".join(
        f'          <option value="{_e(m.lower())}">{_e(m)}</option>\n' for m in models
    )
    model_filter = (f"""        <select id="modelFilter" class="form-select form-select-sm" style="width:150px" onchange="applyFilters()">
          <option value="">All models</option>
{model_options}        </select>
""" if models else "")

    generated = report["generated_at"]
    subtitle = f"generated {generated}"
    if report.get("droam_usage_fetched_at"):
        subtitle += f" · Droam usage as of {report['droam_usage_fetched_at']}"
    _perf = report.get("perf") or {}
    if _perf:
        _ac = _perf.get("api_calls", {})
        subtitle += (f" · run {_perf.get('total_seconds')}s, "
                     f"{_perf.get('api_calls_total')} HTTP calls "
                     f"(RMS {_ac.get('rms', 0)}, Droam {_ac.get('droam', 0)})")

    # RMS API quota widget (top-right of header).
    rms_api = report.get("rms_api") or {}
    rms_api_html = ""
    if rms_api.get("limit"):
        used = rms_api["used"]; limit = rms_api["limit"]; remaining = rms_api["remaining"]
        pct = (used / limit * 100) if limit else 0
        bar_color = "bg-success" if pct < 70 else "bg-warning" if pct < 90 else "bg-danger"
        reset_txt = ""
        if rms_api.get("reset_ts"):
            reset_local = datetime.fromtimestamp(rms_api["reset_ts"]).astimezone()
            now = datetime.now(reset_local.tzinfo)
            days = (reset_local - now).days
            reset_txt = f"resets {reset_local:%Y-%m-%d %H:%M %Z} (~{days}d)"
        rms_api_html = (
            "<div class='card border' style='min-width:240px'>"
            "<div class='card-body py-2 px-3'>"
            "<div class='d-flex justify-content-between small'>"
            "<span class='fw-semibold'>RMS API usage</span>"
            f"<span class='text-muted'>{pct:.1f}%</span></div>"
            "<div class='progress my-1' style='height:6px'>"
            f"<div class='progress-bar {bar_color}' style='width:{pct:.1f}%'></div></div>"
            f"<div class='small text-muted'>{used:,} / {limit:,} used · {remaining:,} left</div>"
            f"<div class='small text-muted'>{_e(reset_txt)}</div>"
            "</div></div>"
        )

    html_doc = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SIM Audit</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
  <style>
    body {{ font-family: system-ui, sans-serif; background: #f8f9fa; }}
    .detail-row {{ display: none; }}
    .detail-row.open {{ display: table-row; }}
    .detail-row td {{ border-top: none !important; }}
    .sortable {{ cursor: pointer; user-select: none; white-space: nowrap; }}
    .sortable::after {{ content: " ↕"; color: #bbb; font-size: .7em; }}
    .sortable.asc::after {{ content: " ↑"; color: #333; font-size: .8em; }}
    .sortable.desc::after {{ content: " ↓"; color: #333; font-size: .8em; }}
    .tile-clickable {{ cursor: pointer; transition: transform .1s, box-shadow .1s; }}
    .tile-clickable:hover {{ transform: translateY(-2px); box-shadow: 0 .25rem .6rem rgba(0,0,0,.12); }}
    .tile-clickable.tile-active {{ box-shadow: 0 0 0 .18rem rgba(13,110,253,.35); }}
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
      <div class="text-muted small">{_e(subtitle)}</div>
    </div>
    {rms_api_html}
  </div>

  <div class="row row-cols-2 row-cols-sm-3 row-cols-lg-6 g-2 mb-4">
    {''.join(cards)}
  </div>

  <div class="card mb-2">
    <div class="card-header d-flex justify-content-between align-items-center flex-wrap gap-2 py-2">
      <span class="fw-semibold">Teltonika Devices</span>
      <div class="d-flex gap-2 flex-wrap align-items-center">
        <input id="searchBox" class="form-control form-control-sm" style="width:240px"
               placeholder="Search name or full ICCID…" oninput="applyFilters()">
        <select id="statusFilter" class="form-select form-select-sm" style="width:140px" onchange="applyFilters()">
          <option value="">All status</option>
          <option value="online">Online only</option>
          <option value="offline">Offline only</option>
        </select>
{model_filter}{tailnet_filter}{droam_filter}        <button class="btn btn-sm btn-outline-secondary" onclick="clearFilters()">Clear</button>
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
              <th class="sortable" data-col="3">License</th>
              <th class="sortable" data-col="4">Tailscale</th>
              <th class="sortable" data-col="5">Droam</th>
              <th>SIMs</th>
              <th class="sortable" data-col="7">WAN IP</th>
              <th>Tags</th>
            </tr>
          </thead>
          <tbody id="deviceBody">
            {''.join(rows)}
          </tbody>
        </table>
      </div>
      <div id="emptyMsg" class="text-center text-muted py-4" style="display:none">No devices match the current filters.</div>
    </div>
  </div>

  {orphan_section}

</div>
<script>
  function toggleDetail(link) {{
    const id = link.dataset.target;
    const row = document.getElementById(id);
    const open = row.classList.toggle('open');
    link.classList.toggle('open-arrow', open);
  }}

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
      applyFilters();
    }});
  }});

  let signalFilter = '';  // '', 'atrisk', 'idle' — driven by the summary tiles

  function applyFilters() {{
    const q = document.getElementById('searchBox').value.trim().toLowerCase();
    const st = document.getElementById('statusFilter').value;
    const mdEl = document.getElementById('modelFilter');
    const md = mdEl ? mdEl.value : '';
    const dmEl = document.getElementById('droamFilter');
    const dm = dmEl ? dmEl.value : '';
    const tnEl = document.getElementById('tailnetFilter');
    const tn = tnEl ? tnEl.value : '';
    const sig = signalFilter;
    let vis = 0;
    document.querySelectorAll('#deviceBody tr.device-row').forEach(row => {{
      const nameMatch = !q || row.dataset.name.includes(q) ||
                        (row.dataset.tsname || '').includes(q) ||
                        row.dataset.iccids.toLowerCase().includes(q);
      const statusMatch = !st || row.dataset.status === st;
      const modelMatch = !md || (row.dataset.model || '') === md;
      const droamMatch = !dm || (row.cells[5]?.dataset.sort ?? '') === dm;
      const ts = row.dataset.ts || 'none';
      const mismatch = row.dataset.mismatch === '1';
      const tnMatch = !tn || (tn === 'match' ? (ts === 'online' || ts === 'offline')
                              : tn === 'none' ? (ts === 'none')
                              : tn === 'offline' ? (ts === 'offline')
                              : tn === 'mismatch' ? mismatch : true);
      const signalMatch = !sig || (sig === 'atrisk' ? row.dataset.atrisk === '1'
                                   : sig === 'idle' ? row.dataset.idle === '1'
                                   : sig === 'licexpired' ? row.dataset.license === 'expired'
                                   : sig === 'licsoon' ? row.dataset.license === 'soon' : true);
      const show = nameMatch && statusMatch && modelMatch && droamMatch && tnMatch && signalMatch;
      row.style.display = show ? '' : 'none';
      const detailRow = document.getElementById(row.dataset.detail);
      if (detailRow) {{
        if (!show) detailRow.classList.remove('open');
        detailRow.style.display = show ? '' : 'none';
      }}
      if (show) vis++;
    }});
    document.getElementById('visCount').textContent =
      vis + ' of {total_devices} device' + ({total_devices} !== 1 ? 's' : '');
    document.getElementById('emptyMsg').style.display = vis === 0 ? '' : 'none';
  }}

  function resetControls() {{
    document.getElementById('searchBox').value = '';
    document.getElementById('statusFilter').value = '';
    const mdEl = document.getElementById('modelFilter');
    if (mdEl) mdEl.value = '';
    const dmEl = document.getElementById('droamFilter');
    if (dmEl) dmEl.value = '';
    const tnEl = document.getElementById('tailnetFilter');
    if (tnEl) tnEl.value = '';
    signalFilter = '';
  }}

  function clearFilters() {{
    resetControls();
    document.querySelectorAll('[data-tile]').forEach(t => t.classList.remove('tile-active'));
    applyFilters();
  }}

  // Summary tiles → filter shortcuts.
  function applyTile(el) {{
    const t = el.dataset.tile;
    document.querySelectorAll('[data-tile]').forEach(x => x.classList.remove('tile-active'));
    if (t === 'orphans') {{
      document.getElementById('orphanSection')?.scrollIntoView({{behavior: 'smooth', block: 'start'}});
      return;
    }}
    resetControls();
    if (t !== 'clear') {{
      const [k, v] = t.split(':');
      if (k === 'status') document.getElementById('statusFilter').value = v;
      else if (k === 'model') {{ const e = document.getElementById('modelFilter'); if (e) e.value = v; }}
      else if (k === 'tailnet') {{ const e = document.getElementById('tailnetFilter'); if (e) e.value = v; }}
      else if (k === 'droam') {{ const e = document.getElementById('droamFilter'); if (e) e.value = v; }}
      else if (k === 'signal') signalFilter = v;
      el.classList.add('tile-active');
    }}
    applyFilters();
    document.getElementById('deviceTable').scrollIntoView({{behavior: 'smooth', block: 'start'}});
  }}

  document.querySelectorAll('[data-tile]').forEach(el => {{
    el.addEventListener('click', () => applyTile(el));
    el.addEventListener('keydown', e => {{
      if (e.key === 'Enter' || e.key === ' ') {{ e.preventDefault(); applyTile(el); }}
    }});
  }});

  applyFilters();
</script>
</body>
</html>"""

    path.write_text(html_doc, encoding="utf-8")
    print(f"HTML report   → {path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

class ApiCallCounter:
    """
    Count outbound HTTP calls by destination for performance reporting.

    Patches requests.Session.request — every requests.* helper (get/post and any
    Session) routes through it — so both the RMS calls (module-level requests.*)
    and the Droam calls (session-based) are captured. Tailscale runs via the CLI
    (subprocess), so it isn't counted here.
    """

    def __init__(self) -> None:
        self.counts = {"rms": 0, "droam": 0, "other": 0}

    @staticmethod
    def _classify(url: str) -> str:
        u = url or ""
        if "droam" in u:
            return "droam"
        if "teltonika" in u:
            return "rms"
        return "other"

    def __enter__(self):
        import requests.sessions as _rs
        self._orig = _rs.Session.request
        counts = self.counts
        classify = self._classify
        orig = self._orig

        def _patched(inner_self, method, url, *args, **kwargs):
            counts[classify(url)] += 1
            return orig(inner_self, method, url, *args, **kwargs)

        _rs.Session.request = _patched
        return self

    def __exit__(self, *exc):
        import requests.sessions as _rs
        _rs.Session.request = self._orig
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit SIMs across RMS and Droam")
    parser.add_argument(
        "--rms-only",
        action="store_true",
        help="Skip Droam (useful when Droam credentials aren't configured yet)",
    )
    parser.add_argument(
        "--no-rms-cmd",
        action="store_true",
        help="Skip RMS command-channel enrichment (authoritative tailnet pairing + "
             "standby slots for devices online in RMS)",
    )
    parser.add_argument(
        "--no-ssh",
        action="store_true",
        help="Skip SSH/UCI enrichment. By default SSH runs only as a fallback for "
             "devices the RMS command channel could not verify",
    )
    parser.add_argument(
        "--ssh-wanip-fallback",
        action="store_true",
        help="For devices with no matching tailnet node, also try their RMS wan_ip "
             "(usually not routable; slow due to timeouts)",
    )
    parser.add_argument(
        "--no-droam-usage",
        action="store_true",
        help="Skip the per-SIM Droam usage detail fetch (one request per SIM; "
             "plan limit / %-used come only from the plan name without it)",
    )
    parser.add_argument(
        "--no-html",
        action="store_true",
        help="Skip writing the interactive report.html",
    )
    parser.add_argument(
        "--devices",
        default="",
        help="Scope the run to specific devices (comma-separated, case-insensitive "
             "substring match on device name). Enrichment + per-SIM Droam usage are "
             "limited to these devices, saving calls for on-demand checks.",
    )
    parser.add_argument(
        "--iccids",
        default="",
        help="Scope the run to devices holding these ICCIDs (comma-separated).",
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

    import time
    timings: dict[str, float] = {}

    def _timed(name: str):
        class _T:
            def __enter__(self_):
                self_.t0 = time.perf_counter()
                return self_
            def __exit__(self_, *exc):
                timings[name] = round(time.perf_counter() - self_.t0, 1)
                return False
        return _T()

    droam_usage_fetched_at: str | None = None
    run_start = time.perf_counter()

    scoped = bool(args.devices.strip() or args.iccids.strip())

    with ApiCallCounter() as counter:
        # --- RMS ---
        with _timed("rms_fetch"):
            rms_devices = fetch_rms_devices()

        if scoped:
            before = len(rms_devices)
            rms_devices = filter_scope_devices(rms_devices, args.devices, args.iccids)
            print(f"Scoped run: {len(rms_devices)} of {before} devices match "
                  f"--devices/--iccids "
                  f"({', '.join(d.get('name', '?') for d in rms_devices) or 'none'})\n",
                  flush=True)
            if not rms_devices:
                print("No devices matched the scope filter — nothing to do.", file=sys.stderr)
                sys.exit(1)

        # --- Tailscale pairing (always, so the report shows the tailnet column + gaps) ---
        with _timed("tailscale"):
            ts_nodes = _tailscale_nodes()
            if ts_nodes:
                m = match_devices_to_tailnet(rms_devices, ts_nodes)
                print(
                    f"Tailscale pairing: {m['exact'] + m['normalized']}/{len(rms_devices)} devices matched "
                    f"({m['exact']} exact, {m['normalized']} normalized), {m['none']} gaps "
                    f"(of {len(ts_nodes)} tailnet nodes)\n",
                    flush=True,
                )
            else:
                print("tailscale CLI unavailable — skipping tailnet pairing/enrichment.\n", file=sys.stderr)

        # --- Enrichment: standby SIM slots + authoritative tailnet pairing ---
        # RMS command channel is the primary, identity-anchored path. SSH then runs
        # as a fallback only for devices RMS could not verify (offline in RMS, or
        # command relay failed) — so we don't probe the same device twice.
        rms_cmd_ran = False
        if not args.no_rms_cmd:
            with _timed("rms_cmd_enrich"):
                enrich_devices_via_rms(rms_devices, ts_nodes)
            rms_cmd_ran = True

        if not args.no_ssh:
            with _timed("ssh_enrich"):
                enrich_devices_via_ssh(
                    rms_devices, ts_nodes,
                    wanip_fallback=args.ssh_wanip_fallback,
                    skip_verified=rms_cmd_ran,
                )

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
                # In scoped mode, only fetch per-SIM usage for the in-scope devices'
                # ICCIDs (active slots + any UCI-discovered standby slots).
                usage_iccids: set[str] | None = None
                if scoped:
                    usage_iccids = set()
                    for d in rms_devices:
                        usage_iccids |= _iccid_keys(d.get("iccid"), d.get("iccid_2"))
                        for extra in (d.get("_uci_sims") or []):
                            usage_iccids |= _iccid_keys(extra.get("iccid"))
                with _timed("droam_fetch"):
                    droam_sims = fetch_droam_sims(with_usage=not args.no_droam_usage,
                                                  usage_iccids=usage_iccids)
                if not args.no_droam_usage:
                    droam_usage_fetched_at = datetime.now(timezone.utc).isoformat()

        # --- RMS API quota snapshot (after all RMS work, so it's current) ---
        rms_api = fetch_rms_rate_limit()

    total_seconds = round(time.perf_counter() - run_start, 1)
    perf = {
        "total_seconds": total_seconds,
        "api_calls": dict(counter.counts),
        "api_calls_total": sum(counter.counts.values()),
        "phase_seconds": timings,
    }

    print(f"\nPerformance: {total_seconds}s total · "
          f"{perf['api_calls_total']} HTTP calls "
          f"(RMS {counter.counts['rms']}, Droam {counter.counts['droam']}, "
          f"other {counter.counts['other']})", flush=True)
    for name, secs in timings.items():
        print(f"  {name:<16} {secs:>6.1f}s", flush=True)

    # --- Report ---
    report = build_report(rms_devices, droam_sims, rms_api=rms_api,
                          droam_usage_fetched_at=droam_usage_fetched_at, perf=perf,
                          scoped=scoped)

    json_path = output_dir / "report.json"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"JSON report   → {json_path}")

    md_path = output_dir / "report.md"
    write_markdown(report, md_path)

    if not args.no_html:
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
