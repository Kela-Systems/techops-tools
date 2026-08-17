#!/usr/bin/env python3
"""
Shared Teltonika RutOS device client (REST API + SSH/UCI).

Device-agnostic: TeltonikaClient plus the helpers both the OTD500 and RUTM08
provisioning pipelines build on. The per-device pipelines live in their own
apps (otd_configure.configure_device, rutm_configure.configure_rutm).

A fresh RutOS device boots on 192.168.1.1 with a UNIQUE factory password printed
on the device label and forces a password change on first login. TeltonikaClient
exposes the building blocks the per-device pipelines compose (login,
set_admin_password, set_hostname, set_timezone, upgrade_firmware, enable_rms,
join_tailscale, verify_configuration, ssh_exec, ...).

Transport (the "both" model):
  * REST API   (https://<host>/api, firmware >= 07.06) is primary for auth,
    identity, firmware and reboot detection. Auth is a bearer token:
        POST /api/login {"username","password"} -> {"data":{"token": "..."}}
        Authorization: Bearer <token>   on every later call (token TTL ~300s).
  * SSH + UCI  is the workhorse for the configuration settings, because UCI is
    uniform across firmware where the per-feature REST endpoints are not:
        uci set system.@system[0].hostname='otd-haifa-port'; uci commit system

Both transports use the SAME credentials, so once set_admin_password() runs,
everything switches to the new password automatically.

Field names / UCI paths that vary by firmware are pulled out as the *_KEYS /
UCI_* constants near the top, and identity is discovered by searching the JSON
(same approach as magos_configure.py) — so you don't have to hard-code one
firmware's exact schema. Verify the marked-(*) paths against your devices via
the Teltonika dev portal (https://developers.teltonika-networks.com/).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

try:
    import requests
    import urllib3
    # Intentional (bench): RutOS ships a self-signed cert and the bench talks to
    # factory-default devices over a direct local link, so TLS verification is
    # off (see TeltonikaClient(verify=False)). Silence the resulting per-request
    # InsecureRequestWarning so it doesn't drown the step log.
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    sys.exit("This script needs 'requests'.  Install it with:  pip install requests")

try:
    import paramiko
except ImportError:
    paramiko = None  # SSH-backed steps will raise a clear error if used.


# All device-talking steps log through this so callers (CLI, web UI) can route
# them wherever they like. The CLI attaches a stdout handler in main().
log = logging.getLogger("teltonika")

# Every record is tagged with the current device's serial (once known) + a
# lowercased level, so handlers can render: [info] [OTD5-...-0042] Logged in.
_LOG_CTX = {"sn": "-"}


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.levelname_lc = record.levelname.lower()
        # Honour a per-record `sn` if a caller set one via logging `extra=`,
        # otherwise fall back to the current process-wide serial.
        if not hasattr(record, "sn"):
            record.sn = _LOG_CTX["sn"]
        return True


log.addFilter(_ContextFilter())
LOG_LINE_FORMAT = "[%(levelname_lc)s] [%(sn)s] %(message)s"


def set_log_serial(serial: Optional[str]) -> None:
    """Tag subsequent log lines with this device's serial (None resets to '-')."""
    _LOG_CTX["sn"] = serial or "-"


def install_log_context(logger: logging.Logger) -> None:
    """Attach the shared context filter to a per-tool logger so its records carry
    the %(levelname_lc)s and %(sn)s fields that LOG_LINE_FORMAT renders. Every
    tool's logger shares the one process-wide serial set via set_log_serial()."""
    logger.addFilter(_ContextFilter())


def load_settings(path: str) -> dict:
    """Load a tool's settings JSON, dropping `_`-prefixed comment keys."""
    with open(path, "r", encoding="utf-8") as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}


def make_step_runner(logger: logging.Logger, catch: type = SystemExit):
    """Return `(failures, step)` where `step(label, fn)` runs `fn()` and records
    `label: <err>` in `failures` (instead of aborting) when it raises `catch`.

    Used by the per-tool provisioning pipelines so a non-critical step's failure
    is reported in the run record rather than silently completing the run."""
    failures: list[str] = []

    def step(label: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except catch as e:
            failures.append(f"{label}: {e}")
            logger.error("Step '%s' FAILED: %s", label, e)

    return failures, step


def tcp_port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """True if a TCP connection to host:port succeeds within `timeout` seconds.
    Used by the detection loops to tell whether a device is plugged in."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --- OTD500 factory defaults ------------------------------------------------
DEFAULT_HOST = "192.168.1.1"
DEFAULT_USERNAME = "admin"
DEFAULT_SCHEME = "https"          # RutOS REST API is HTTPS (self-signed by default)
# Intentional (bench): a shared post-provisioning password the operator can
# override in the per-tool config. It is only a *fallback* default for the
# controlled bench network, not a secret — real deployments set their own.
DEFAULT_NEW_PASSWORD = "Kelasys123!"
DEFAULT_TIMEZONE = "Asia/Jerusalem"
DEFAULT_NAME_PREFIX = "otd-"

# RutOS stores BOTH an IANA zone name and the matching POSIX TZ string. Map the
# zones we actually use; extend as needed.
POSIX_TZ = {
    "Asia/Jerusalem": "IST-2IDT,M3.4.4/26,M10.5.0",
    "UTC": "UTC0",
    "Europe/Vilnius": "EET-2EEST,M3.5.0/3,M10.5.0/4",
}

# Identity field names (normalised: lowercased, non-alphanumerics stripped) to
# search for in whatever JSON the device returns.
SERIAL_KEYS = ("serial", "serialnumber", "serialno", "sn", "deviceserial", "mnf_serial")
MAC_KEYS = ("mac", "macaddress", "macaddr", "hwaddr", "ethmac", "lanmac", "mnf_mac")
MODEL_KEYS = ("model", "modelname", "productname", "product", "devicemodel", "name", "board")
FW_KEYS = ("firmware", "fwversion", "version", "current_version", "firmwareversion")
IMEI_KEYS = ("imei",)

# --- UCI paths (verified against OTD5_R_00.07.20.3) -------------------------
UCI_HOSTNAME = "system.system.hostname"          # device name == hostname for RMS
UCI_TIMEZONE = "system.system.timezone"          # POSIX TZ string
UCI_ZONENAME = "system.ntp.zoneName"             # IANA zone name (note capital N)
# RMS lives in the `rms_mqtt` package; the connect daemon's enable flag is
# `1` by default, so "connect to RMS" is really: ensure enabled + force connect.
UCI_RMS_ENABLED = "rms_mqtt.rms_connect_mqtt.enable"
# SIM "Preferred network type" is per-SIM in `simcard`: service='lte' == 4G only
# (other values: auto, lte_nr5g, nr5g, umts, gsm ...).
SIM_SERVICE_4G = "lte"
# Tailscale is an add-on package (opkg-installed). Config lives under the
# `tailscale` package's `settings` section (verified on OTD5_R_00.07.20.3).
TS_PACKAGE = "tailscale"
UCI_TS_ENABLED = "tailscale.settings.enabled"

# Teltonika RMS cloud API — used to REGISTER the device in your account (separate
# from enabling the on-device client). https://developers.rms.teltonika-networks.com/
RMS_API_BASE = "https://api.rms.teltonika-networks.com"


def register_in_rms(api_token: str, company_id: str, *, name: str, serial: str,
                    mac: str, device_password: str, device_series: str = "otd",
                    wait: int = 180) -> None:
    """Register the device in the Teltonika RMS cloud by serial+MAC so it actually
    shows up / connects in your account. This is a HOST-side API call, but the
    machine running this tool usually reaches the internet *through the OTD
    device* — and the device's modem resets during provisioning (4G-only switch,
    FOTA). So on a connection error we RETRY for up to `wait` seconds until the
    uplink recovers, rather than failing on the first timeout. Idempotent: an
    'already exists' response counts as success. Raises SystemExit on real
    failure so the pipeline records it.

    Payload follows the current RMS v3 schema (data is a LIST; fields are
    device_series / mac / serial / name / auto_credit_enable / password_confirmation).
    """
    if not api_token or not company_id:
        raise SystemExit("RMS registration needs rms.api_token + rms.company_id in config.")
    if not serial or serial == "unknown" or not mac or mac == "unknown":
        raise SystemExit(f"RMS registration needs a real serial+MAC "
                         f"(got serial={serial!r} mac={mac!r}).")
    device = {
        "company_id": int(company_id) if str(company_id).strip().isdigit() else company_id,
        "device_series": device_series,
        "mac": mac_with_colons(mac),
        "serial": serial,
        "name": name,
        "auto_credit_enable": True,   # consume a credit / start trial so it can connect
    }
    if device_password:
        device["password_confirmation"] = device_password  # required by RMS since 2023
    headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
    log.info("Registering %s in RMS (series=%s serial=%s MAC=%s) ...",
             name, device_series, serial, device["mac"])

    deadline = time.time() + max(wait, 0)
    attempt = 0
    while True:
        attempt += 1
        try:
            r = requests.post(f"{RMS_API_BASE}/devices", headers=headers,
                              json={"data": [device]}, timeout=30)
        except requests.exceptions.RequestException as e:
            # Connectivity problem (uplink down while the modem re-attaches) — wait
            # and retry until the deadline. Anything else is a hard failure.
            if time.time() < deadline:
                log.info("RMS API not reachable yet (attempt %d) — uplink likely still "
                         "recovering; retrying in 10s ...", attempt)
                time.sleep(10)
                continue
            raise SystemExit(f"RMS API unreachable after ~{wait}s "
                             f"(device uplink never recovered): {e}")
        # We got an HTTP response, so the network is fine — decide on the body.
        if r.status_code in (200, 201):
            log.info("Registered in RMS: %s.", name)
            return
        body = r.text[:300]
        # Treat only an explicit "already exists / already registered" as a
        # benign duplicate — a bare "registered" elsewhere in an error body is
        # too loose and could mask a real failure.
        if r.status_code in (409, 422) and ("already" in body.lower() or "exist" in body.lower()):
            log.info("Device already in RMS (%s) — OK.", serial)
            return
        if r.status_code in (401, 403):
            raise SystemExit(
                f"RMS rejected the token (HTTP {r.status_code}: {body}). The personal "
                f"access token needs the 'devices:write' scope (and 'devices:read'), "
                f"the account must have 2FA enabled, and company_id ({company_id}) must "
                f"belong to that token. Regenerate the token in RMS with those scopes.")
        raise SystemExit(f"RMS registration failed: HTTP {r.status_code}: {body}")


def rms_find_device(api_token: str, serial: str, timeout: int = 20) -> Optional[dict]:
    """The RMS cloud record for the device with `serial` (device id, connection
    state, management pack fields, ...). None when the lookup could not answer
    (no token, API error) or the device is not registered."""
    if not api_token or not serial or serial == "unknown":
        return None
    try:
        r = requests.get(f"{RMS_API_BASE}/devices",
                         headers={"Authorization": f"Bearer {api_token}"},
                         params={"serial": serial, "limit": 100}, timeout=timeout)
        if r.status_code != 200:
            return None
        payload = r.json()
    except (requests.exceptions.RequestException, ValueError):
        return None
    for dev in payload.get("data") or []:
        if isinstance(dev, dict) and str(dev.get("serial", "")).strip() == str(serial).strip():
            return dev
    return None


def rms_cloud_device_status(api_token: str, serial: str, timeout: int = 20) -> Optional[bool]:
    """Authoritative connectivity check: ask the RMS cloud whether the device
    with `serial` shows as connected. Returns True/False, or None when the
    lookup could not answer (no token, API error, device not found)."""
    dev = rms_find_device(api_token, serial, timeout)
    return rms_status_connected(json.dumps(dev)) if dev is not None else None


def _pack_matches(pack: str, credit_type_id, credit_type_name: str) -> bool:
    """True when the configured `pack` selects this credit type. Accepts a
    numeric credit_type_id, the RMS short type name ('management_3y'), or the
    display name ('Paid Management pack 3y.'). Names are compared as token
    sets — the smaller set must be contained in the larger — so
    'management_3y' == {management,3y} matches the display name's
    {paid,management,pack,3y}, while 'management_1y' can never match
    'management_10y' (the duration token differs)."""
    want = pack.strip().lower()
    if not want:
        return False
    if want.isdigit():
        return str(credit_type_id) == want
    a = set(re.findall(r"[a-z0-9]+", want))
    b = set(re.findall(r"[a-z0-9]+", (credit_type_name or "").lower()))
    if not a or not b:
        return False
    return a <= b or b <= a


def assign_rms_pack(api_token: str, company_id: str, *, serial: str, pack: str,
                    wait: int = 180) -> None:
    """Assign a Management/Data pack to the device — the RMS UI's
    Actions -> Device -> "Set pack" action, done via the API:

        GET /devices?serial=...    -> device id (+ current pack, for idempotency)
        GET /credits?company_id=.. -> credit_type_id of the wanted pack
        PUT /devices/credit        -> {"data":[{device_id, credit_type_id,
                                                credit_enabled: 1}]}

    `pack` selects the pack type: numeric credit_type_id, the RMS short name
    ('management_3y'), or a fragment of the display name ('Paid Management
    pack 3y.'). Idempotent: a device that already carries a matching pack is
    left alone — RMS packs CANNOT be revoked, so a blind re-assign on a re-run
    would burn a second pack. Like register_in_rms, connection errors are
    retried for up to `wait` seconds (the bench usually reaches the internet
    through the device being provisioned). Raises SystemExit on real failure."""
    if not api_token:
        raise SystemExit("RMS pack assignment needs rms.api_token in config.")
    if not serial or serial == "unknown":
        raise SystemExit(f"RMS pack assignment needs a real serial (got {serial!r}).")
    headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
    deadline = time.time() + max(wait, 0)

    def _get(url: str, **params) -> dict:
        """GET with the same wait-out-the-uplink retry loop as register_in_rms."""
        while True:
            try:
                r = requests.get(url, headers=headers, params=params or None, timeout=30)
            except requests.exceptions.RequestException as e:
                if time.time() < deadline:
                    log.info("RMS API not reachable yet — uplink likely still "
                             "recovering; retrying in 10s ...")
                    time.sleep(10)
                    continue
                raise SystemExit(f"RMS API unreachable after ~{wait}s: {e}")
            if r.status_code in (401, 403):
                raise SystemExit(
                    f"RMS rejected the token (HTTP {r.status_code}: {r.text[:200]}). "
                    f"Pack assignment needs the 'devices:read', 'credits:read' and "
                    f"'device_credits:write' scopes on the personal access token.")
            if r.status_code != 200:
                raise SystemExit(f"RMS API {url} failed: HTTP {r.status_code}: {r.text[:300]}")
            try:
                return r.json()
            except ValueError:
                raise SystemExit(f"RMS API {url} returned non-JSON: {r.text[:200]}")

    # 1) Device id — the unit was usually registered moments ago, so give the
    # cloud a little time to show it before declaring failure.
    dev = None
    while dev is None:
        payload = _get(f"{RMS_API_BASE}/devices", serial=serial, limit=100)
        dev = next((d for d in payload.get("data") or []
                    if isinstance(d, dict)
                    and str(d.get("serial", "")).strip() == str(serial).strip()), None)
        if dev is None:
            if time.time() >= deadline:
                raise SystemExit(f"Device {serial} not found in RMS — register it "
                                 "first (rms-register step) before assigning a pack.")
            log.info("Device %s not visible in RMS yet — retrying in 10s ...", serial)
            time.sleep(10)
    dev_id = dev.get("id")

    # Idempotency: a matching pack already on the device means a re-run — skip
    # (packs cannot be revoked; re-assigning would consume another one).
    cur_name = dev.get("management_credit_type_name") or dev.get("management_credit_type") or ""
    if _pack_matches(pack, dev.get("management_credit_type_id"), cur_name) or \
       _pack_matches(pack, None, dev.get("management_credit_type") or ""):
        log.info("Device already has pack '%s' — skipping (packs are not revocable).",
                 cur_name or pack)
        return

    # 2) Which credit_type_id is the wanted pack, and does the company still
    # have one left to assign?
    params = {"limit": 100}
    if company_id:
        params["company_id"] = int(company_id) if str(company_id).strip().isdigit() else company_id
    credits = (_get(f"{RMS_API_BASE}/credits", **params).get("data") or [])
    matching = [c for c in credits if isinstance(c, dict)
                and _pack_matches(pack, c.get("credit_type_id"), c.get("credit_type_name"))]
    available = [c for c in matching if (c.get("credit_left") or 0) > 0]
    if not available:
        have = ", ".join(sorted({f"{c.get('credit_type_name')} (left={c.get('credit_left')})"
                                 for c in credits if isinstance(c, dict)})) or "(none)"
        detail = "no packs of that type left" if matching else "no such pack type"
        raise SystemExit(f"RMS pack '{pack}': {detail} in the company pool. "
                         f"Company credits: {have}")
    credit_type_id = available[0]["credit_type_id"]
    log.info("Assigning pack '%s' (credit_type_id=%s, %s left) to %s (device id %s) ...",
             available[0].get("credit_type_name") or pack, credit_type_id,
             available[0].get("credit_left"), serial, dev_id)

    # 3) The actual "Set pack" save.
    body = {"data": [{"device_id": dev_id, "credit_type_id": credit_type_id,
                      "credit_enabled": 1}]}
    while True:
        try:
            r = requests.put(f"{RMS_API_BASE}/devices/credit", headers=headers,
                             json=body, timeout=30)
        except requests.exceptions.RequestException as e:
            if time.time() < deadline:
                log.info("RMS API not reachable yet — retrying the pack assignment in 10s ...")
                time.sleep(10)
                continue
            raise SystemExit(f"RMS API unreachable after ~{wait}s: {e}")
        break
    if r.status_code in (401, 403):
        raise SystemExit(
            f"RMS rejected the pack assignment (HTTP {r.status_code}: {r.text[:200]}). "
            f"The personal access token is missing the 'device_credits:write' scope "
            f"(the PUT /devices/credit endpoint) — regenerate it with that scope added.")
    if r.status_code not in (200, 201, 202):
        raise SystemExit(f"RMS pack assignment failed: HTTP {r.status_code}: {r.text[:300]}")

    # 4) The PUT is asynchronous (status-channel based) — confirm the pack
    # actually landed on the device record before reporting success.
    confirm_deadline = time.time() + 90
    while time.time() < confirm_deadline:
        dev = rms_find_device(api_token, serial)
        if dev and str(dev.get("management_credit_type_id")) == str(credit_type_id):
            log.info("Pack assigned: %s now has '%s'.",
                     serial, dev.get("management_credit_type_name") or pack)
            return
        time.sleep(5)
    raise SystemExit(f"RMS accepted the pack assignment (HTTP {r.status_code}) but the "
                     f"device record does not show the pack after 90s — check the RMS UI "
                     f"(Actions -> Device -> Set pack) for {serial}.")


def _norm_key(k: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


def _find_field(data, candidate_keys) -> Optional[str]:
    """Recursively search dict/list `data` for the first value whose key matches
    one of `candidate_keys` (compared by normalised name)."""
    wanted = {_norm_key(c) for c in candidate_keys}
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for key, val in cur.items():
                if isinstance(val, (dict, list)):
                    stack.append(val)
                elif val not in (None, "") and _norm_key(key) in wanted:
                    return str(val)
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def normalize_mac(mac: Optional[str]) -> str:
    """Lowercase, strip separators — so 00:1E:42.. and 00-1e-42.. compare equal."""
    return re.sub(r"[^0-9a-f]", "", (mac or "").lower())


def mac_with_colons(mac: Optional[str]) -> str:
    """'2097272FDFF0' -> '20:97:27:2F:DF:F0' (RMS wants the colon form)."""
    h = normalize_mac(mac).upper()
    if len(h) != 12:
        return (mac or "").strip()
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


def canonical_mac(mac: str) -> str:
    """Zero-pad each octet then strip separators, so macOS's '0:1e:42:aa:bb:1',
    Windows's '20-97-27-2f-df-f0' and a manifest's '00:1E:42:AA:BB:01' all
    compare equal."""
    parts = re.split(r"[:-]", mac)
    if len(parts) == 6:
        mac = ":".join(p.zfill(2) for p in parts)
    return normalize_mac(mac)


_MAC_RE = re.compile(r"([0-9a-fA-F]{1,2}(?:[:-][0-9a-fA-F]{1,2}){5})")

# One "<ip> ... <mac>" row of an ARP/neighbour listing. The gap between the two
# is deliberately loose (but never crosses a line): the three formats we read
# put different words in it — macOS '? (10.0.0.5) at 0:1e:42:aa:bb:1 on en0',
# Windows '10.0.0.5   00-1e-42-aa-bb-01  dynamic', and Linux's `ip neigh`
# '10.0.0.5 dev eth0 lladdr 00:1e:42:aa:bb:01 REACHABLE' (whose 'eth0' is why
# the gap can't be restricted to non-digits).
_ARP_ROW_RE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})[^\n]*?"
                         r"([0-9a-fA-F]{1,2}(?:[:-][0-9a-fA-F]{1,2}){5})")

# MACs that mean "nothing answered", not a device.
_NON_MACS = ("000000000000", "ffffffffffff")


def mac_from_arp_output(text: str) -> Optional[str]:
    """First usable MAC in arp/ip-neigh output. Skips entries that mean 'no
    answer' rather than a device: all-zero (unresolved) and broadcast."""
    for m in _MAC_RE.finditer(text or ""):
        mac = canonical_mac(m.group(1))
        if mac not in _NON_MACS:
            return mac
    return None


def rms_status_connected(raw: str) -> bool:
    """True if a `ubus call rms* status` payload indicates the device is connected
    to RMS. The exact schema varies by firmware, so we (1) check common string
    signals and (2) walk the JSON for any connection-ish field set to a truthy /
    'connected' value."""
    if not raw:
        return False
    low = raw.lower()
    for sig in ('"connected": true', '"connected":true', '"status": "connected"',
                '"connection_state": "connected"', '"connection_status": "connected"',
                'connected to server', '"mqtt": "connected"'):
        if sig in low:
            return True
    try:
        data = json.loads(raw)
    except ValueError:
        return False
    truthy = {"1", "true", "connected", "online", "up", "active"}
    hit = [False]

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                kl = str(k).lower()
                # "connect" must not match "disconnect"/"disconnect_reason" etc.
                connect_key = "connect" in kl and "disconnect" not in kl
                if (connect_key or kl in ("status", "state", "mqtt")) and \
                        not isinstance(v, (dict, list)):
                    if v is True or str(v).strip().lower() in truthy:
                        hit[0] = True
                walk(v)
        elif isinstance(node, list):
            for it in node:
                walk(it)

    walk(data)
    return hit[0]


def _version_digits(s: str) -> tuple:
    """The dotted numeric version embedded in `s` (e.g. 'OTD5_R_00.07.23.4' ->
    (0, 7, 23, 4)), or () if there is no dotted version."""
    m = re.search(r"\d+(?:\.\d+)+", s or "")
    return tuple(int(x) for x in m.group(0).split(".")) if m else ()


def fw_versions_match(a: str, b: str) -> bool:
    """Firmware-version compare tolerant of prefix/separator differences (the
    release name 'OTD5_R_00.07.23.4' vs whatever /etc/version reports) but NOT
    of differing version numbers. A plain substring test used to mis-match
    '00.07.23' against '00.07.23.4' and skip a needed upgrade — so compare the
    extracted dotted version numbers for equality instead, falling back to a
    strict normalised-equality only when there is no dotted version to parse."""
    va, vb = _version_digits(a), _version_digits(b)
    if va and vb:
        return va == vb
    na = re.sub(r"[^0-9a-z]", "", (a or "").lower())
    nb = re.sub(r"[^0-9a-z]", "", (b or "").lower())
    return bool(na) and bool(nb) and na == nb


def device_name(site_name: str, prefix: str = DEFAULT_NAME_PREFIX) -> str:
    """otd-<site_name>, sanitised to a valid hostname label."""
    slug = re.sub(r"[^A-Za-z0-9-]", "-", site_name.strip().lower()).strip("-")
    return f"{prefix}{slug}"


def assert_device_model(identity: dict, expected_prefix: str, tool_name: str) -> None:
    """Abort the run unless the connected device is the model this pipeline is for.

    Every RutOS family (OTD500, RUTM08, …) boots on the SAME factory IP
    (192.168.1.1), so the bench's TCP/HTTP detection can't tell them apart — an
    operator on the wrong tab would otherwise mis-name the unit (e.g. label a
    RUTM08 `otd-<site>`), register it in RMS under the wrong series, and skip the
    steps that don't apply.     `get_identity()` reads the authoritative model off
    the device's manufacturer-info block; this is the one place we assert it.

    Raises SystemExit (a hard failure) when the model doesn't match — including
    when it couldn't be read at all ('unknown'), so a device we know nothing about
    (the "behaving oddly" case, where a mix-up is most likely) is refused rather
    than rubber-stamped.
    """
    model = (identity.get("model") or "").strip()
    if not model or model.lower() == "unknown":
        raise SystemExit(
            f"Could not read the device model for the {tool_name} — refusing to "
            "guess. Check the device is powered, cabled, and reachable, then retry.")
    if not model.upper().startswith(expected_prefix.upper()):
        raise SystemExit(
            f"Wrong device for the {tool_name}: the connected unit reports "
            f"model '{model}', but this tool provisions {expected_prefix}* "
            "devices. Move to the matching configurator tab for this device "
            "before running it.")


# --- host-side network helpers -----------------------------------------------
# When the OTD reboots for a firmware flash, the bench computer's Ethernet link
# drops, and some hosts take a long time to re-acquire a 192.168.1.x lease once
# it returns. Actively renewing the host's DHCP lease speeds the reconnect up.
# Purely best-effort: on macOS/Linux this needs root (we also try passwordless
# sudo); if unavailable we note it once and stop trying.
_DHCP_RENEW_UNAVAILABLE = False


def host_iface_for(target_ip: str) -> Optional[str]:
    """Name of the host interface that routes to `target_ip` (None if unknown).
    Must be resolved while the device is still up — mid-reboot the lookup can
    fall through to the default (internet) interface, which we must not touch."""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["route", "-n", "get", target_ip], capture_output=True,
                                 text=True, timeout=5).stdout
            m = re.search(r"interface:\s*(\S+)", out)
            return m.group(1) if m else None
        if sys.platform.startswith("linux"):
            out = subprocess.run(["ip", "route", "get", target_ip], capture_output=True,
                                 text=True, timeout=5).stdout
            m = re.search(r"\bdev\s+(\S+)", out)
            return m.group(1) if m else None
    except Exception:  # noqa: BLE001
        return None
    return None


def renew_host_dhcp(iface: Optional[str]) -> None:
    """Renew the HOST's DHCP lease on `iface` (all adapters on Windows)."""
    global _DHCP_RENEW_UNAVAILABLE
    if _DHCP_RENEW_UNAVAILABLE:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["ipconfig", "/renew"], capture_output=True, timeout=30)
            log.info("Renewed the host's DHCP lease (ipconfig /renew).")
            return
        if not iface:
            return
        if sys.platform == "darwin":
            for cmd in (["ipconfig", "set", iface, "DHCP"],
                        ["sudo", "-n", "ipconfig", "set", iface, "DHCP"]):
                if subprocess.run(cmd, capture_output=True, timeout=10).returncode == 0:
                    log.info("Renewed the host's DHCP lease on %s.", iface)
                    return
            _DHCP_RENEW_UNAVAILABLE = True
            log.info("Can't renew the host's DHCP lease (needs root) — relying on the "
                     "OS to reconnect. Run the app with sudo to enable active renews.")
            return
        # Linux best-effort (passwordless sudo only).
        if subprocess.run(["sudo", "-n", "dhclient", iface],
                          capture_output=True, timeout=30).returncode == 0:
            log.info("Renewed the host's DHCP lease on %s.", iface)
        else:
            _DHCP_RENEW_UNAVAILABLE = True
    except Exception:  # noqa: BLE001 — never let a host-side nicety kill a run
        _DHCP_RENEW_UNAVAILABLE = True


# How wide/fast to sweep a bench subnet looking for one device. A /24 at these
# settings costs ~1s, so a caller can afford to retry the whole sweep while a
# device boots.
SCAN_WORKERS = 128
SCAN_TCP_TIMEOUT = 0.3


def arp_table() -> dict[str, str]:
    """{canonical MAC: IP} for every resolved entry in the host's ARP cache.

    One listing covers the whole cache, so a caller looking for one device among
    many candidate addresses reads it once instead of shelling out per address.
    Best-effort: an empty dict when no listing command works."""
    cmds = ([["arp", "-a"]] if sys.platform == "win32"
            else [["arp", "-an"], ["ip", "neigh", "show"]])
    for cmd in cmds:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
        except Exception:  # noqa: BLE001 — try the next command / give up quietly
            continue
        table: dict[str, str] = {}
        for ip, mac in _ARP_ROW_RE.findall(out or ""):
            canon = canonical_mac(mac)
            # First entry wins: a MAC listed on several interfaces (e.g. an
            # alias route) resolves to the address the OS lists first.
            if canon not in _NON_MACS:
                table.setdefault(canon, ip)
        if table:
            return table
    return {}


def find_ip_by_mac(mac: str, subnets: list[str], *, port: int,
                   timeout: float = SCAN_TCP_TIMEOUT,
                   workers: int = SCAN_WORKERS) -> Optional[str]:
    """The address of the host with `mac` on `subnets`, or None.

    For a device that has moved somewhere we can't predict — it was just
    switched to DHCP, say — so we have to find it by the one identifier that
    doesn't change. TCP-probes every candidate address in parallel (which both
    finds the live hosts and populates the host's ARP cache), then reads the
    cache once and matches the MAC.

    A hit must ALSO be answering on `port`, which is what makes this safe to
    call repeatedly while a device boots: a stale cache entry still pointing at
    the device's old address can't be mistaken for the new one."""
    want = canonical_mac(mac or "")
    if not want or want in _NON_MACS:
        return None
    candidates: list[str] = []
    for subnet in subnets or []:
        try:
            candidates += [str(h) for h in
                           ipaddress.ip_network(subnet, strict=False).hosts()]
        except ValueError:
            log.warning("Skipping invalid scan subnet %r.", subnet)
    if not candidates:
        return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(tcp_port_open, ip, port, timeout): ip
                   for ip in candidates}
        open_hosts = {futures[f] for f in as_completed(futures) if f.result()}
    if not open_hosts:
        return None
    found = arp_table().get(want)
    return found if found in open_hosts else None


class TeltonikaClient:
    """One OTD500 over REST (primary) + SSH/UCI (config). Never opens SSH until
    a step actually needs it."""

    # verify=False is intentional for the bench: devices use a self-signed cert
    # on a direct local link, so there is no CA to validate against (see the
    # urllib3.disable_warnings note at import time).
    def __init__(self, host: str = DEFAULT_HOST, username: str = DEFAULT_USERNAME,
                 scheme: str = DEFAULT_SCHEME, verify: bool = False, timeout: int = 20,
                 ssh_username: str = "root"):
        self.host = host
        self.username = username          # REST/WebUI user (admin)
        self.ssh_username = ssh_username  # SSH user (root) — different from REST!
        self.scheme = scheme
        self.timeout = timeout
        self.base = f"{scheme}://{host}/api"
        self.s = requests.Session()
        self.s.verify = verify
        self.s.headers.update({"Content-Type": "application/json"})
        self.password: Optional[str] = None
        self.token: Optional[str] = None
        self._ssh: "Optional[paramiko.SSHClient]" = None
        self._online: Optional[bool] = None  # cached internet-readiness result
        self.fw_target: str = ""   # version FOTA reports as available (target)
        self.fw_version: str = ""  # version actually running after an upgrade
        # Extra SSH passwords to try (used to self-heal a half-changed device
        # where root already has the new password but admin/REST does not).
        self._ssh_alt_passwords: list[str] = []

    # --- auth ---------------------------------------------------------------
    def login(self, password: str) -> None:
        """REST login; stores the bearer token + the working password (reused for
        SSH). Raises SystemExit on failure."""
        r = self.s.post(
            f"{self.base}/login",
            json={"username": self.username, "password": password},
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise SystemExit(f"Login failed (HTTP {r.status_code}): {r.text[:200]}")
        try:
            payload = r.json()
        except ValueError:
            raise SystemExit(f"Login returned a non-JSON body: {r.text[:200]}")
        token = _find_field(payload, ("token",))
        if not token:
            raise SystemExit(f"Login returned no token: {r.text[:200]}")
        self.token = token
        self.password = password
        self.s.headers["Authorization"] = f"Bearer {token}"
        log.info("Logged in as '%s'.", self.username)

    def _ssh_client(self) -> "paramiko.SSHClient":
        if paramiko is None:
            raise SystemExit("This step needs 'paramiko'.  Install it: pip install paramiko")
        if self._ssh is not None:
            return self._ssh
        candidates = [self.password] + [p for p in self._ssh_alt_passwords if p]
        last_auth_err = None
        for pw in candidates:
            cli = paramiko.SSHClient()
            # AutoAddPolicy: the bench talks to factory-default devices on a
            # direct/local link where there is no stable known-hosts identity to
            # pin — host-key TOFU adds no security here and would just break the
            # plug-in-anything workflow.
            cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                cli.connect(self.host, username=self.ssh_username, password=pw,
                            timeout=self.timeout, allow_agent=False, look_for_keys=False)
            except paramiko.AuthenticationException as e:
                last_auth_err = e
                cli.close()   # don't leak the socket/transport on a bad password
                continue
            except Exception as e:  # noqa: BLE001
                cli.close()
                raise SystemExit(f"SSH connect to {self.host} failed: {e}")
            self._ssh = cli
            return cli
        raise SystemExit(f"SSH auth failed for {self.ssh_username}@{self.host}: {last_auth_err}")

    def ssh_exec(self, command: str, check: bool = True,
                 exec_timeout: Optional[int] = None) -> str:
        """Run a shell command over SSH; return stdout. Raises on non-zero exit.

        `exec_timeout` bounds the WHOLE command: paramiko's timeout= only covers
        channel reads, and recv_exit_status() blocks forever if the device drops
        mid-command (e.g. reboots, modem reset) — which would leave the bench
        stuck on 'busy' until someone restarts the app."""
        cli = self._ssh_client()
        _in, out, err = cli.exec_command(command, timeout=self.timeout)
        deadline = time.time() + (exec_timeout or 120)
        while not out.channel.exit_status_ready():
            if time.time() > deadline:
                # The device went away mid-command (reboot/modem reset). Tear the
                # whole client down, not just the channel, so the next step opens
                # a fresh SSH session instead of reusing a dead one.
                out.channel.close()
                self.close()
                raise SystemExit(f"SSH command timed out after {exec_timeout or 120}s: {command}")
            time.sleep(0.2)
        rc = out.channel.recv_exit_status()
        stdout = out.read().decode("utf-8", "replace").strip()
        stderr = err.read().decode("utf-8", "replace").strip()
        if check and rc != 0:
            raise SystemExit(f"SSH command failed (rc={rc}): {command}\n{stderr or stdout}")
        return stdout

    def _uci(self, *sets: str, package: str) -> None:
        """uci set ...; uci commit <package> over SSH."""
        cmd = " && ".join([f"uci set {s}" for s in sets] + [f"uci commit {package}"])
        self.ssh_exec(cmd)

    def close(self) -> None:
        if self._ssh is not None:
            self._ssh.close()
            self._ssh = None
        # Release the pooled HTTP connection(s) too, so callers that create many
        # short-lived clients (the bench loop) don't leak sockets.
        try:
            self.s.close()
        except Exception:  # noqa: BLE001 — close must never raise
            pass

    # --- identity -----------------------------------------------------------
    def get_identity(self) -> dict:
        """serial / MAC / model from the manufacturer info block (authoritative),
        firmware from the REST device status, IMEI from gsmctl. We deliberately do
        NOT scrape /interfaces/status — its interface `name`/`mac` fields produce
        garbage like model='mob1s3a1' or a zero MAC. Returns the raw payloads."""
        raw: dict = {}

        # Manufacturer info: the reliable source of serial + LAN MAC + product name.
        try:
            mnf = self.ssh_exec("ubus call mnfinfo get 2>/dev/null", check=False)
            if mnf:
                raw["mnfinfo"] = json.loads(mnf)
        except (ValueError, SystemExit):
            pass
        mnf = raw.get("mnfinfo", {})

        # REST device status: firmware version (and a backup for serial/model).
        try:
            r = self.s.get(f"{self.base}/system/device/status", timeout=self.timeout)
            if r.status_code == 200:
                raw["device_status"] = r.json()
        except (requests.exceptions.RequestException, ValueError):
            pass
        dev = raw.get("device_status", {})

        identity = {
            "serial": _find_field(mnf, SERIAL_KEYS) or _find_field(dev, SERIAL_KEYS) or "unknown",
            "mac": _find_field(mnf, MAC_KEYS) or "unknown",
            # "unknown" (not a family default) when neither source actually reported
            # a model, so assert_device_model refuses to guess instead of rubber-
            # stamping a device it couldn't read. Cosmetic OTD500/RUTM08 fallbacks
            # for display live in each app's build_entry.
            "model": (_find_field(mnf, ("name",) + MODEL_KEYS)
                      or _find_field(dev, MODEL_KEYS) or "unknown"),
            "firmware": _find_field(dev, FW_KEYS) or "unknown",
            "imei": "unknown",
            "raw": raw,
        }
        # gsmctl -i prints the IMEI; pick the first line that actually looks like
        # one (14-16 digits) rather than blindly taking the last line, which can
        # be a trailing blank or a stray notice.
        imei_lines = [ln.strip() for ln in
                      self.ssh_exec("gsmctl -i 2>/dev/null", check=False).splitlines()
                      if ln.strip()]
        imei = next((ln for ln in imei_lines if re.fullmatch(r"\d{14,16}", ln)),
                    imei_lines[-1] if imei_lines else None)
        if imei:
            identity["imei"] = imei
        set_log_serial(identity["serial"])
        log.info("Identity: model=%s serial=%s MAC=%s fw=%s imei=%s",
                 identity["model"], identity["serial"], identity["mac"],
                 identity["firmware"], identity["imei"])
        return identity

    def verify_identity(self, identity: dict, expected: dict) -> list[str]:
        """Compare discovered serial/imei/mac against the expected identity (e.g.
        the live MAC read off the device). Returns a list of human-readable
        mismatch warnings (empty == all good)."""
        warnings = []
        for field, keys in (("serial", "serial"), ("imei", "imei"), ("mac", "mac")):
            want = (expected.get(keys) or "").strip()
            got = identity.get(field) or ""
            if not want or got in ("", "unknown"):
                continue
            same = (normalize_mac(want) == normalize_mac(got)) if field == "mac" \
                else (want.lower() == got.lower())
            if not same:
                warnings.append(f"{field} mismatch: expected={want} device={got}")
        if warnings:
            for w in warnings:
                log.warning("VERIFY: %s", w)
        else:
            log.info("Identity verified against the expected values.")
        return warnings

    # --- password (first-boot change) --------------------------------------
    def set_admin_password(self, new_password: str) -> None:
        """Change the admin password from the per-device label password to the
        shared default. Idempotent: if the device already accepts new_password we
        skip. Sets it for the WebUI/REST user (admin) and root (SSH)."""
        if self.password == new_password:
            log.info("Password already set to the shared default; skipping.")
            return
        log.info("Changing admin password to the shared default ...")

        # Allow the SSH connection to fall back to the new password too — so a
        # device left half-changed by an earlier failed run (root already on the
        # new password) still heals instead of erroring.
        self._ssh_alt_passwords = [new_password]

        # 1) SSH user (root) via chpasswd — guarantees root ends on the new pw.
        # shlex.quote: a password containing a quote must not break the shell
        # (worst case is failing MID password change).
        self.ssh_exec(f"echo {shlex.quote(f'{self.ssh_username}:{new_password}')} | chpasswd")

        # 2) WebUI/REST user (admin) via the first-login endpoint. The API wants
        #    password + password_confirm (and rejects current_password).
        try:
            r = self.s.post(
                f"{self.base}/system/actions/change_password_firstlogin",
                json={"data": {"password": new_password, "password_confirm": new_password}},
                timeout=self.timeout,
            )
            if r.status_code in (200, 201):
                log.info("WebUI/REST password changed via API.")
            else:
                log.warning("First-login password API returned HTTP %s: %s",
                            r.status_code, r.text[:200])
        except requests.exceptions.RequestException as e:
            log.warning("First-login password API call failed: %s", e)

        # Switch both transports to the new password.
        self.password = new_password
        self._ssh_alt_passwords = []
        self.close()                      # next SSH reconnects with the new password
        self.login(new_password)          # fresh REST token under the new password
        log.info("Password changed.")

    # --- firmware -----------------------------------------------------------
    def upgrade_firmware(self, *, bin_path: Optional[str] = None, fota: bool = False,
                         keep_settings: bool = True, net_wait: int = 180,
                         wait: bool = True, reboot_timeout: int = 420,
                         skip_if_version: str = "") -> None:
        """Upgrade to latest-stable. `bin_path` uploads a pinned local image;
        `fota=True` pulls latest-stable from Teltonika. Reboots the device — the
        connection WILL drop (expected). If wait, block until 192.168.1.1 is back.
        `skip_if_version`: skip the (local) flash when the device already runs
        this version — makes a retry of a half-provisioned device a no-op here."""
        if bin_path:
            if not os.path.exists(bin_path):
                raise SystemExit(f"Firmware image not found: {bin_path} — download it "
                                 "from Teltonika and check firmware.bin_path in the config.")
            if skip_if_version:
                cur = self.ssh_exec("cat /etc/version 2>/dev/null", check=False).strip()
                if fw_versions_match(cur, skip_if_version):
                    log.info("Firmware already at %s — skipping the flash.", cur)
                    return
            keep = "" if keep_settings else "-n "
            log.info("Uploading firmware %s (keep_settings=%s) ...", bin_path, keep_settings)
            sftp = self._ssh_client().open_sftp()
            sftp.put(bin_path, "/tmp/firmware.bin")
            sftp.close()
            log.info("Starting sysupgrade — device will reboot (connection drops).")
            self._fire_and_forget(f"sysupgrade {keep}/tmp/firmware.bin")
            self.close()
            if wait:
                self._wait_for_reboot(reboot_timeout)
        elif fota:
            self._fota_upgrade(keep_settings=keep_settings, wait=wait,
                               reboot_timeout=reboot_timeout, net_wait=net_wait)
        else:
            log.info("No firmware source given; skipping upgrade.")

    def _fota_upgrade(self, *, keep_settings: bool, wait: bool, reboot_timeout: int,
                      net_wait: int = 180, download_timeout: int = 600) -> None:
        """FOTA via the REST API: check for an update, download it, then upgrade.
        Endpoints verified from the RutOS Web API (firmware >= 07.06).

        Intentional bench heuristics (kept on purpose, see inline notes): the
        progress endpoint's schema is unreliable on this hardware, so we treat
        an explicit "completed" status, a download that goes quiet after showing
        activity, OR no progress at all after a short grace as "image ready"; and
        a connection dropped right after the upgrade request is taken as "the
        flash started". The real reboot is always confirmed afterwards."""
        # 1) Is an update available? Re-running an up-to-date device (e.g. a retry
        # after a later step failed) must be a no-op here, NOT a hard failure —
        # so an explicit "no update" answer skips the whole step.
        try:
            up = self.s.get(f"{self.base}/firmware/device/updates/status", timeout=self.timeout)
            if up.status_code == 200:
                payload = up.json()
                avail = _find_field(payload, ("new_available", "update_available",
                                              "updates_available", "available"))
                if avail is not None and str(avail).strip().lower() in ("0", "false", "no"):
                    log.info("FOTA: no update available — already on the latest "
                             "firmware; skipping the upgrade step.")
                    return
                ver = _find_field(payload, FW_KEYS + ("latest",))
                if ver:
                    self.fw_target = ver
                log.info("FOTA update status: %s", ver or up.text[:160])
        except requests.exceptions.RequestException:
            pass

        # 2) Start the download from Teltonika's FOTA server. The 4G-only switch
        # bounces the modem just before this, so the device may briefly report
        # "No internet connection" — re-wait for a stable link and retry rather
        # than aborting the whole run on a transient.
        log.info("Requesting FOTA download (needs internet) ...")
        deadline_net = time.time() + max(net_wait, 0)
        while True:
            r = self.s.post(f"{self.base}/firmware/actions/fota_download", timeout=self.timeout)
            if r.status_code in (200, 201, 202):
                break
            body = r.text or ""
            # Same idempotency rule at the download stage, in case the status
            # endpoint didn't say so explicitly.
            if any(s in body.lower() for s in ("no new", "up to date", "no update")):
                log.info("FOTA: device reports nothing to download — already on the "
                         "latest firmware; skipping the upgrade step.")
                return
            no_net = ("no internet" in body.lower() or '"code":15' in body.replace(" ", ""))
            if no_net and time.time() < deadline_net:
                log.info("FOTA: device reports no internet yet — letting the mobile "
                         "link settle, then retrying ...")
                self._online = None
                self.wait_for_internet(min(60, max(int(deadline_net - time.time()), 10)))
                time.sleep(5)
                continue
            raise SystemExit(f"FOTA download request failed (HTTP {r.status_code}): {body[:200]}")

        # 3) Poll download progress until it's ready. The progress endpoint's
        # schema varies (and on this unit it returns nothing parseable), so we
        # also log the RAW body once to learn it, and don't blind-wait the whole
        # window: the OTD image downloads in <1 min, so if no progress is ever
        # reported we proceed after a short safe grace; if progress IS reported we
        # track it to completion.
        deadline = time.time() + download_timeout
        start = time.time()
        last = None
        raw_logged = False
        saw_activity = False
        idle_polls = 0
        no_progress_grace = 75  # seconds to allow a silent (<1 min) download
        while time.time() < deadline:
            try:
                p = self.s.get(f"{self.base}/firmware/device/progress/status", timeout=self.timeout)
                raw = p.text if p.status_code == 200 else f"HTTP {p.status_code}"
                body = p.json() if p.status_code == 200 else {}
            except (requests.exceptions.RequestException, ValueError):
                raw, body = "", {}
            if not raw_logged:
                log.info("FOTA progress raw response: %s", (raw[:300] or "(empty)"))
                raw_logged = True
            # RutOS returns {"data":{"percents":"0","process":"started"}} — read
            # those exact fields, with the generic search as a fallback.
            data = body.get("data", {}) if isinstance(body, dict) else {}
            pct = str(data.get("percents") or _find_field(body, ("percents", "progress",
                      "percent", "percentage")) or "").strip()
            status = str(data.get("process") or _find_field(body, ("process", "status",
                         "state")) or "").strip()
            note = f"{status or '?'} {pct + '%' if pct else ''}".strip()
            if note and note != last:
                log.info("FOTA download: %s", note)
                last = note
            # Explicit completion signals.
            if (pct and pct.rstrip("%") in ("100", "100.0")) or \
               (status.lower() in ("downloaded", "ready", "idle", "success", "done",
                                   "completed", "finished")):
                log.info("FOTA download complete.")
                break
            in_progress = (status.lower() in ("started", "downloading", "in_progress",
                                              "busy", "pending", "running")
                           or (pct and pct.rstrip("%") not in ("", "0")))
            if in_progress:
                saw_activity = True
                idle_polls = 0
            else:
                idle_polls += 1
            # If we saw a download running and it then goes quiet, it's finished.
            if saw_activity and idle_polls >= 2:
                log.info("FOTA: download activity ended — image ready.")
                break
            # If progress is never reported at all, proceed after a safe grace
            # (the download completes in well under a minute on this hardware).
            if not saw_activity and time.time() - start > no_progress_grace:
                log.info("FOTA: no progress reported in %ds — image downloads quickly, "
                         "proceeding to flash.", no_progress_grace)
                break
            time.sleep(5)

        # 4) Flash + reboot. Read the version BEFORE so we can prove it changed.
        pre_ver = self.ssh_exec("cat /etc/version 2>/dev/null", check=False).strip()
        log.info("Starting FOTA upgrade (keep_settings=%s, from %s) — device reboots.",
                 keep_settings, pre_ver or "?")
        accepted = False
        try:
            # RutOS wants keep_settings as the string "1"/"0", NOT a JSON boolean.
            ru = self.s.post(f"{self.base}/firmware/actions/upgrade",
                             json={"data": {"keep_settings": "1" if keep_settings else "0"}},
                             timeout=self.timeout)
            log.info("FOTA upgrade request -> HTTP %s: %s", ru.status_code, (ru.text or "")[:200])
            accepted = ru.status_code in (200, 201, 202)
        except requests.exceptions.RequestException as e:
            # A dropped connection usually means the flash/reboot already started.
            log.info("Connection dropped after upgrade request (likely started): %s", e)
            accepted = True

        if not wait:
            self.close()
            return

        # The REST upgrade sometimes returns OK but does NOT actually flash, so we
        # confirm a real reboot. If the device never goes down, the REST path
        # didn't start a flash — fall back to sysupgrade of the downloaded image.
        if not self._wait_for_reboot(reboot_timeout, require_down=True):
            log.info("Device never went offline — REST upgrade did not start a flash. "
                     "Falling back to SSH sysupgrade of the downloaded image ...")
            self._ssh_sysupgrade_fota(keep_settings)
            self.close()
            self._wait_for_reboot(reboot_timeout, require_down=True)

        # Verify the firmware actually changed — never report a silent no-op.
        post_ver = self.ssh_exec("cat /etc/version 2>/dev/null", check=False).strip()
        self.fw_version = post_ver
        if pre_ver and post_ver and post_ver == pre_ver:
            raise SystemExit(f"Firmware did NOT change (still {post_ver}) — the upgrade "
                             f"did not take. accepted={accepted}.")
        log.info("Firmware upgraded: %s -> %s.", pre_ver or "?", post_ver or "?")

    def _ssh_sysupgrade_fota(self, keep_settings: bool) -> None:
        """Flash the FOTA-downloaded image via sysupgrade over SSH (fallback when
        the REST upgrade action doesn't actually start a flash)."""
        keep = "" if keep_settings else "-n "
        fw = self.ssh_exec(
            "ls -t /tmp/*.img /tmp/*.bin /tmp/fw/*.img /tmp/fw/*.bin 2>/dev/null | head -1",
            check=False).strip()
        if not fw:
            listing = self.ssh_exec("ls -la /tmp 2>/dev/null", check=False)
            raise SystemExit("FOTA: could not locate the downloaded firmware image in /tmp "
                             f"to sysupgrade. /tmp contents:\n{listing[:400]}")
        log.info("Flashing FOTA image via sysupgrade: %s", fw)
        self._fire_and_forget(f"sysupgrade {keep}{fw}")

    def _fire_and_forget(self, command: str) -> None:
        """Kick off a command that reboots the box; ignore the dropped channel."""
        try:
            cli = self._ssh_client()
            cli.exec_command(f"({command}) >/dev/null 2>&1 &", timeout=self.timeout)
        except Exception as e:  # noqa: BLE001 — reboot kills the socket
            log.info("Channel dropped after firmware command (expected): %s", e)

    def _port_open(self, port: Optional[int] = None) -> bool:
        import socket
        if port is None:                       # probe the actual web port, not always 443
            port = 443 if self.scheme == "https" else 80
        try:
            with socket.create_connection((self.host, port), timeout=3):
                return True
        except OSError:
            return False

    def _wait_for_reboot(self, timeout: int, require_down: bool = True,
                         down_wait: int = 75) -> bool:
        """Wait for the device to reboot and come back at self.host.

        If require_down, first confirm the device actually goes OFFLINE (so we
        don't false-positive when no flash/reboot happened). Returns True once it
        is back online; returns False if require_down and it never went down
        within down_wait (caller can then fall back). Raises if it goes down but
        never returns within `timeout`."""
        host_if = host_iface_for(self.host)  # resolve now, while the route is live
        if require_down:
            log.info("Waiting for the device to go down (confirming a real reboot) ...")
            down_deadline = time.time() + down_wait
            while time.time() < down_deadline:
                if not self._port_open():
                    log.info("Device went offline — rebooting.")
                    break
                time.sleep(2)
            else:
                return False
        else:
            time.sleep(20)

        log.info("Waiting for the device to come back at %s ...", self.host)
        deadline = time.time() + timeout
        next_renew = time.time() + 30  # give the OS a chance to reconnect by itself
        while time.time() < deadline:
            if self._port_open():
                time.sleep(10)  # give the web stack a moment after the port opens
                log.info("Device is back online.")
                self._online = None  # data link may need to re-attach after reboot
                # The pre-reboot SSH transport is dead now — drop it so the next
                # ssh_exec opens a fresh connection (else: "SSH session not active").
                self.close()
                if self.password:
                    self.login(self.password)
                return True
            if time.time() >= next_renew:
                renew_host_dhcp(host_if)
                next_renew = time.time() + 45
            time.sleep(5)
        raise SystemExit(f"Device did not come back within {timeout}s after firmware upgrade.")

    # --- connectivity (mobile data) ----------------------------------------
    def wait_for_internet(self, timeout: int = 180, target: str = "8.8.8.8") -> bool:
        """Block until the device's SIM has attached and has working data, or
        `timeout` elapses. A fresh SIM can take a minute+ to register, so this is
        what makes FOTA / Tailscale / eSIM reliable. Logs signal + registration
        as feedback. Returns True if online. Cached on self._online."""
        log.info("Waiting for mobile data (a fresh SIM can take a minute to attach) ...")
        # ICMP can be blocked on some APNs, so also try an HTTP fetch.
        probe = (f"(ping -c1 -W3 {target} >/dev/null 2>&1 || "
                 "wget -q -T5 -O /dev/null http://detectportal.firefox.com/success.txt "
                 ">/dev/null 2>&1) && echo OK || echo NO")
        # Require a few CONSECUTIVE successes so a flapping link (e.g. right after
        # the 4G-only modem re-attach) doesn't read as "online" prematurely.
        needed_streak = 3
        deadline = time.time() + timeout
        last_note = None
        streak = 0
        while time.time() < deadline:
            if self.ssh_exec(probe, check=False).strip().endswith("OK"):
                streak += 1
                if streak >= needed_streak:
                    sig = self.ssh_exec("gsmctl -q 2>/dev/null", check=False)
                    log.info("Internet is up and stable (signal=%s dBm).", sig or "?")
                    self._online = True
                    return True
                time.sleep(3)
                continue
            streak = 0
            sig = self.ssh_exec("gsmctl -q 2>/dev/null", check=False)
            reg = self.ssh_exec("gsmctl -j 2>/dev/null", check=False)
            note = f"signal={sig or '?'} reg={reg or '?'}"
            if note != last_note:
                log.info("...still waiting for data (%s)", note)
                last_note = note
            time.sleep(5)
        log.warning("No stable internet after %ds — SIM not attached or no coverage.", timeout)
        self._online = False
        return False

    def ensure_online(self, timeout: int = 180) -> bool:
        """wait_for_internet(), skipping the probe only once we've confirmed the
        link is up. A previous *failure* is not cached — a later step (after the
        SIM finally attaches) re-probes instead of being stuck offline forever."""
        if not self._online:
            self.wait_for_internet(timeout)
        return bool(self._online)

    # --- naming / timezone --------------------------------------------------
    def set_hostname(self, name: str) -> None:
        log.info("Setting hostname / device name to '%s' ...", name)
        qname = shlex.quote(name)   # never let an odd name break the UCI/echo shell line
        self._uci(f"{UCI_HOSTNAME}={qname}", package="system")
        self.ssh_exec(f"echo {qname} > /proc/sys/kernel/hostname", check=False)
        log.info("Hostname set.")

    def set_timezone(self, zonename: str) -> None:
        posix = POSIX_TZ.get(zonename)
        if not posix:
            raise SystemExit(f"No POSIX TZ mapping for '{zonename}'; add it to POSIX_TZ.")
        log.info("Setting timezone to %s ...", zonename)
        self._uci(f"{UCI_ZONENAME}='{zonename}'", f"{UCI_TIMEZONE}='{posix}'",
                  package="system")
        self.ssh_exec("/etc/init.d/sysntpd restart", check=False)
        log.info("Timezone set.")

    # --- SIM / mobile -------------------------------------------------------
    def set_sims_4g_only(self) -> None:
        """Force every SIM's preferred network type to LTE (4G) only via
        simcard.@sim[N].service='lte'. Covers all SIM slots present."""
        log.info("Setting all SIMs to 4G (LTE) only ...")
        indices = self._sim_indices()
        if not indices:
            log.warning("No simcard.@sim[N] sections found; skipping 4G-only.")
            return
        for i in indices:
            self.ssh_exec(f"uci set simcard.@sim[{i}].service='{SIM_SERVICE_4G}'")
        self.ssh_exec("uci commit simcard")
        # Re-attach the modem so the new preferred type takes effect. NOTE: this
        # bounces the cellular link (AT+CFUN=1,1 resets the module), so any data
        # connection drops for ~30-60s while it re-registers.
        self.ssh_exec("gsmctl -A 'AT+CFUN=1,1' >/dev/null 2>&1 || "
                      "/etc/init.d/network restart >/dev/null 2>&1 || true", check=False)
        # The link just went down — invalidate the cached online state so the next
        # internet-dependent step waits for a *fresh*, stable connection.
        self._online = None
        log.info("Set %d SIM slot(s) to 4G only (modem re-attaching — data drops briefly).",
                 len(indices))

    def _sim_indices(self) -> list[int]:
        """SIM section indices, e.g. [0,1,2] from simcard.@sim[0]=sim ..."""
        out = self.ssh_exec(
            "uci show simcard 2>/dev/null | "
            "sed -n 's/^simcard\\.@sim\\[\\([0-9]*\\)\\]=sim$/\\1/p'", check=False)
        return [int(x) for x in out.split() if x.strip().isdigit()]

    # --- RMS ----------------------------------------------------------------
    def enable_rms(self, auth_code: str = "") -> None:
        """Ensure RMS is enabled and force a connect attempt. On this firmware the
        rms_mqtt connect daemon is enabled by default; the device shows up in RMS
        once it has internet and is registered there by serial+MAC (the pipeline
        does this via register_in_rms when rms.api_token + rms.company_id are
        set). An auth code, if used, is entered on the RMS side."""
        log.info("Enabling RMS + forcing a connect attempt ...")
        if auth_code:
            log.info("(an RMS auth code is entered on the RMS side, not stored on-device)")
        # uci set/commit (raises on failure), then confirm the flag actually reads 1.
        self._uci(f"{UCI_RMS_ENABLED}='1'", package="rms_mqtt")
        val = self.ssh_exec(f"uci get {UCI_RMS_ENABLED} 2>/dev/null", check=False).strip()
        if val != "1":
            raise SystemExit(f"RMS enable flag did not stick (uci get returned '{val}').")
        # The RMS init-script name varies by firmware (here the ubus object is
        # just `rms`); restart whichever exists and force a connect via ubus.
        self.ssh_exec("for s in rms_mqtt rms rms_connect_mqtt rms_connect; do "
                      "[ -x /etc/init.d/$s ] && /etc/init.d/$s restart && break; done", check=False)
        self.ssh_exec("ubus call rms connect 2>/dev/null || rms_connect 2>/dev/null || true",
                      check=False)
        # IMPORTANT: enabling on-device is necessary but NOT sufficient — the unit
        # only appears/connects in RMS after it's registered there by serial+MAC.
        log.info("RMS enabled on-device. It will connect to RMS only once it is "
                 "registered there (serial+MAC) — done by register_in_rms when "
                 "rms.api_token + rms.company_id are configured.")

    # --- Tailscale ----------------------------------------------------------
    HAVE_TS = ("command -v tailscale >/dev/null && command -v tailscaled >/dev/null "
               "&& [ -f /etc/init.d/tailscale ] && echo __HAVE__ || echo __MISS__")

    def ensure_tailscale_installed(self) -> None:
        """Tailscale is an add-on package, absent on a fresh OTD500. Install it via
        opkg (needs internet) and VERIFY the binary + RutOS service wrapper are
        actually present — raising with the opkg output if not."""
        if "__HAVE__" in self.ssh_exec(self.HAVE_TS, check=False):
            log.info("Tailscale already installed.")
            return
        log.info("Installing the Tailscale package (opkg) ...")
        # opkg pulls over the 4G link — allow well beyond the default bound.
        self.ssh_exec("opkg update", check=False, exec_timeout=180)
        out = self.ssh_exec("opkg install tailscale 2>&1", check=False, exec_timeout=300)
        if "__HAVE__" not in self.ssh_exec(self.HAVE_TS, check=False):
            raise SystemExit(
                "Tailscale install incomplete (tailscale/tailscaled binary or "
                "/etc/init.d/tailscale missing). opkg said:\n" + out[-500:])
        log.info("Tailscale package installed.")

    # How many times to (re-)run `tailscale up` before giving up. A fresh SIM
    # right after the firmware reboot often loses the first control-plane race;
    # re-running is what makes it stick (mirrors a manual UI retry).
    TS_JOIN_ATTEMPTS = 3

    @staticmethod
    def _ts_needs_login(status: str) -> bool:
        """True when tailscaled has dropped into the interactive-login fallback —
        i.e. the authkey wasn't accepted in time and it's now waiting on a human
        ('Logged out. Log in at: <URL>'). Waiting longer won't help; re-run up."""
        s = status.lower()
        return ("logged out" in s or "log in at" in s
                or "to authenticate" in s or "needslogin" in s)

    def _tailscale_up_once(self, up: str, attempt: int, attempts: int) -> tuple[str, str, str]:
        """Run `tailscale up` once in the background and poll for a 100.x address.
        Returns (node_ip, up_output, status); node_ip is '' if it didn't join."""
        # Run `tailscale up` in the BACKGROUND on the device: with no internet it
        # blocks forever retrying the control server, and SSH's recv_exit_status()
        # would then hang the whole run. Redirecting fd 1/2 to a file lets the SSH
        # call return immediately; we poll for the 100.x address ourselves.
        log.info("Running tailscale up (attempt %d/%d, background, polling up to ~60s) ...",
                 attempt, attempts)
        self.ssh_exec("rm -f /tmp/ts_up.log", check=False)
        self.ssh_exec(f"({up}; echo __done__) >/tmp/ts_up.log 2>&1 </dev/null &", check=False)

        node_ip, status = "", ""
        for _ in range(30):
            ip = self.ssh_exec("tailscale ip -4 2>/dev/null", check=False).strip().splitlines()
            node_ip = ip[0].strip() if ip else ""
            if node_ip.startswith("100."):
                break
            # Fail fast: if `tailscale up` already exited with a real error, stop
            # waiting the full 60s for an address that will never appear. Match an
            # actual error line ("Error: ...", "failed", ...) rather than any
            # stray "Error" substring (which can appear in benign output/URLs).
            partial = self.ssh_exec("cat /tmp/ts_up.log 2>/dev/null", check=False)
            if "__done__" in partial and re.search(r"error:|failed|invalid", partial, re.I):
                break
            # Fail fast: if tailscaled has fallen back to interactive login the
            # authkey isn't going to take on this attempt — bail so we can re-run.
            status = self.ssh_exec("tailscale status 2>&1", check=False).strip()
            if self._ts_needs_login(status):
                break
            time.sleep(2)

        out = self.ssh_exec("cat /tmp/ts_up.log 2>/dev/null", check=False).strip()
        out = out.replace("__done__", "").strip()
        self.ssh_exec("rm -f /tmp/ts_up.log", check=False)  # don't leave run logs on the device
        if not status:
            status = self.ssh_exec("tailscale status 2>&1", check=False).strip()
        if out:
            log.info("tailscale up: %s", out[:300])
        return node_ip, out, status

    def join_tailscale(self, auth_key: str, hostname: str, login_server: str = "") -> None:
        if not auth_key:
            raise SystemExit("join_tailscale needs an auth key.")
        self.ensure_tailscale_installed()
        log.info("Joining Tailscale as '%s' ...", hostname)
        # Enable via the REAL uci path (tailscale.settings.enabled) so the init
        # script actually starts tailscaled.
        self.ssh_exec(f"uci set {UCI_TS_ENABLED}='1'; uci commit {TS_PACKAGE}", check=False)
        self.ssh_exec("/etc/init.d/tailscale enable >/dev/null 2>&1; "
                      "/etc/init.d/tailscale restart", check=False)

        # Wait for the daemon to actually be up before `tailscale up`.
        for _ in range(15):
            s = self.ssh_exec("tailscale status 2>&1", check=False)
            if "doesn't appear to be running" not in s and \
               "failed to connect to local tailscaled" not in s:
                break
            time.sleep(2)
        else:
            raise SystemExit("tailscaled did not start after enabling the service.")

        # --reset clears any leftover state from a prior partial `tailscale up`
        # (otherwise it errors: "changing settings ... requires mentioning all
        # non-default flags") and applies exactly the flags below.
        up = (f"tailscale up --reset --authkey={shlex.quote(auth_key)} "
              f"--hostname={shlex.quote(hostname)} --accept-routes")
        if login_server:
            up += f" --login-server={shlex.quote(login_server)}"

        # Right after a firmware reboot the SIM has only just re-attached, so the
        # first contact with Tailscale's control plane often loses a race: the
        # authkey handshake doesn't finish in time and tailscaled drops into the
        # interactive-login fallback. Waiting longer doesn't help once it's there
        # (it's waiting on a human) — so RE-RUN `tailscale up`, which is exactly
        # what a manual retry does and what makes it stick on a now-warm link.
        node_ip, out, status = "", "", ""
        for attempt in range(1, self.TS_JOIN_ATTEMPTS + 1):
            node_ip, out, status = self._tailscale_up_once(up, attempt, self.TS_JOIN_ATTEMPTS)
            if node_ip.startswith("100."):
                log.info("Tailscale up — node IP %s.", node_ip)
                return
            if attempt < self.TS_JOIN_ATTEMPTS:
                reason = ("dropped into interactive login (control plane not reachable yet)"
                          if self._ts_needs_login(status) else "no node IP yet")
                log.warning("Tailscale didn't join on attempt %d/%d (%s) — re-running "
                            "tailscale up ...", attempt, self.TS_JOIN_ATTEMPTS, reason)
                time.sleep(5)

        # Still not joined after every retry — give the operator a precise reason.
        if self._ts_needs_login(status):
            raise SystemExit(
                f"Tailscale did not join after {self.TS_JOIN_ATTEMPTS} attempts: the node "
                f"keeps falling back to interactive login (the auth key never gets accepted "
                f"in time — usually a fresh SIM still settling its data link, or an "
                f"expired/already-used key). status='{status[:200]}'")
        raise SystemExit(
            f"Tailscale did not come up after {self.TS_JOIN_ATTEMPTS} attempts (~60s each). "
            f"up='{out[:200]}' status='{status[:200]}'")

    # --- eSIM (optional) ----------------------------------------------------
    def load_esim(self, activation_code: str) -> None:
        """Download an eSIM profile via the LPA activation code. Reports the raw
        tool output rather than claiming success (eSIM CLI varies by firmware)."""
        if not activation_code:
            log.info("No eSIM activation code; skipping.")
            return
        log.info("Loading eSIM profile via activation code ...")
        out = self.ssh_exec(f"gsmctl --esim-download {shlex.quote(activation_code)} 2>&1",
                            check=False).strip()
        log.info("eSIM download output: %s", out[:300] or "(no output — verify on device)")

    # --- final verification -------------------------------------------------
    def verify_configuration(self, *, hostname: str, zonename: str, new_password: str,
                             sim_4g: bool, rms: bool, tailscale: bool,
                             esim: bool = False, expected_firmware: str = "",
                             rms_api_token: str = "", serial: str = "") -> list[dict]:
        """Re-read every setting back off the device and confirm it actually took.
        Returns a list of checks: {item, expected, actual, ok} where ok is True
        (passed), False (failed) or None (not in scope / skipped)."""
        checks: list[dict] = []

        def add(item, expected, actual, ok):
            checks.append({"item": item, "expected": expected, "actual": actual, "ok": ok})

        # Password: we are still authenticated, and SSH answers, on new_password.
        add("admin/root password", new_password,
            "in use" if self.password == new_password else self.password,
            self.password == new_password)

        hn = self.ssh_exec("uci get system.system.hostname 2>/dev/null", check=False).strip()
        add("hostname", hostname, hn, hn == hostname)

        zn = self.ssh_exec(f"uci get {UCI_ZONENAME} 2>/dev/null", check=False).strip()
        tz = self.ssh_exec(f"uci get {UCI_TIMEZONE} 2>/dev/null", check=False).strip()
        add("timezone", zonename, f"{zn} ({tz})", zn == zonename)

        if sim_4g:
            idxs = self._sim_indices()
            vals = [self.ssh_exec(f"uci get simcard.@sim[{i}].service 2>/dev/null",
                                  check=False).strip() for i in idxs]
            add("SIM 4G-only", f"lte x{len(idxs)}", ",".join(vals) or "(none)",
                bool(idxs) and all(v == SIM_SERVICE_4G for v in vals))
        else:
            add("SIM 4G-only", "(skipped)", "-", None)

        if rms:
            en = self.ssh_exec(f"uci get {UCI_RMS_ENABLED} 2>/dev/null", check=False).strip()
            # On-device status: the ubus object name varies by firmware, so ask
            # ubus what rms objects exist and try each (status, then get_status).
            objs = self.ssh_exec("ubus list 2>/dev/null | grep -i rms", check=False).split()
            conn = ""
            for obj in objs or ["rms", "rms_connect_mqtt", "rms_mqtt"]:
                for verb in ("status", "get_status"):
                    conn = self.ssh_exec(f"ubus call {obj} {verb} 2>/dev/null",
                                         check=False).strip()
                    if conn:
                        break
                if conn:
                    break
            connected, via = rms_status_connected(conn), "device"
            if not connected and rms_api_token:
                # The device-side schema misses on some firmware — ask the RMS
                # cloud itself (authoritative) before reporting "not connected".
                # It can take ~30s after registration to show connected there.
                for attempt in range(3):
                    cloud = rms_cloud_device_status(rms_api_token, serial)
                    if cloud is None:
                        break  # lookup unavailable — keep the on-device answer
                    if cloud:
                        connected, via = True, "RMS cloud"
                        break
                    if attempt < 2:
                        time.sleep(10)
            if connected:
                actual = f"enable={en}, connected (per {via})"
            else:
                # Surface the raw status so a missed schema is debuggable, not silent.
                snippet = " ".join(conn.split())[:80] if conn else "no status output"
                actual = f"enable={en}, not connected yet [{snippet}]"
            # PASS requires BOTH the on-device enable flag AND an actual connection
            # — enable=1 alone doesn't mean the unit reached RMS.
            add("RMS", "enable=1 + connected", actual, en == "1" and connected)
        else:
            add("RMS", "(skipped)", "-", None)

        if tailscale:
            ip = self.ssh_exec("tailscale ip -4 2>/dev/null", check=False).strip().splitlines()
            en = self.ssh_exec(f"uci get {UCI_TS_ENABLED} 2>/dev/null", check=False).strip()
            node_ip = ip[0].strip() if ip else ""
            add("Tailscale", "joined (100.x)",
                f"{node_ip or '(not joined)'} (enabled={en})", node_ip.startswith("100."))
        else:
            add("Tailscale", "(skipped)", "-", None)

        if esim:
            prof = self.ssh_exec("gsmctl --esim-list 2>/dev/null || gsmctl -A 'AT+ESIM?' 2>/dev/null",
                                 check=False).strip()
            add("eSIM profile", "loaded", prof[:80] or "(unknown — verify on device)",
                bool(prof) or None)

        fw = self.ssh_exec("cat /etc/version 2>/dev/null", check=False).strip()
        if expected_firmware:
            add("firmware", expected_firmware, fw or "unknown",
                fw_versions_match(fw, expected_firmware))
        else:
            add("firmware", "(any)", fw or "unknown", None)

        return checks


def format_verification(checks: list[dict]) -> str:
    """Render the verification checks as an aligned text report."""
    mark = {True: "PASS", False: "FAIL", None: "skip"}
    width = max((len(c["item"]) for c in checks), default=0)
    lines = ["── Verification ──"]
    for c in checks:
        lines.append(f"  [{mark[c['ok']]}] {c['item']:<{width}}  {c['actual']}")
    return "\n".join(lines)
