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
join_tailscale, configure_sim_switch, verify_configuration, ssh_exec, ...).

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
import posixpath
import re
import shlex
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
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
    Used by the detection loops to tell whether a device is plugged in.

    Goes through `lan_relay`, which answers directly on every station where
    that works and falls back to a permitted binary on a managed Mac that
    refuses this interpreter local-network access — where a plain connect
    reports every device as absent. See `bench_core.lan_relay`.
    """
    from bench_core.lan_relay import can_connect      # local: stdlib-only module
    return can_connect(host, port, timeout)


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
UCI_TIMEZONE = "system.system.timezone"          # POSIX TZ string — libc reads this
# Two IANA zone names (note the capital N in both), read by different consumers.
# The WebUI's Date & Time dropdown owns the one in the `system` section: a
# TSW202 with only the timeserver copy set showed a correct clock as "UTC" in
# the UI, which one Save & Apply would have written back over the clock. Proven
# by setting the zone in the UI and diffing `uci show system`.
UCI_ZONENAME = "system.system.zoneName"          # what the WebUI renders
UCI_ZONENAME_NTP = "system.ntp.zoneName"         # the timeserver section's copy
# RutOS carries TWO independent time subsystems and a device can run with them
# disagreeing: `system.ntp` (the OpenWrt timeserver section — enable flags and a
# zoneName of its own) and the `ntpclient` package (the client a RUTM08/OTD500
# actually polls with, with a THIRD zoneName). The demo pair in TEC-846 had
# system.ntp disabled on UTC while ntpclient said Asia/Jerusalem, which is how
# log timestamps end up disagreeing across one device's own subsystems.
NTP_CLIENT_PACKAGE = "ntpclient"
# The section types the servers are kept under. RutOS uses `ntpserver` (seen on
# an OTD500 running OTD5_R_00.07.22.3); upstream OpenWrt's package uses `server`.
NTP_SERVER_TYPES = ("ntpserver", "server")
# Read once at daemon startup, which is why `ntp_daemon_check` compares its
# mtime against the process: RutOS runs `ntpclient -s -l` with no server on the
# command line, so this file is the only place the running client's servers are.
NTP_CLIENT_CONFIG = "/etc/config/ntpclient"
UCI_NTP_SECTION = "system.ntp"
UCI_NTP_SERVER = "system.ntp.server"
UCI_NTP_ENABLED = "system.ntp.enabled"
# Poll seconds for the RutOS NTP client. Stock ships 86400 — one probe per day
# is not sync, it is a daily chance to notice the clock is wrong.
#
# Set to the build's validated FLOOR, because on this fleet the interval is not
# really a sync-accuracy knob, it is a RETRY LATENCY. Every unit is provisioned
# against a time server it cannot reach from the bench, so its early polls all
# fail; the interval is then how long a device sits on the firmware image's build
# date after it finally reaches a server. It also buys attempts against the
# TEC-846 rejection — samples that do reach the server are discarded by the
# RFC-4330 cross-checks, so 60 tries an hour is 60 chances for one to pass where
# there was 1. And it costs nothing in traffic: both targets are on the local
# network (the OTD500 reaches the same server through the router's UDP 123
# forward), so no polling crosses the cellular link.
NTP_CLIENT_INTERVAL = 60
# The floor is not ours: the binary validates `interval` to 60..2147483647 and
# SILENTLY substitutes 600 for anything it rejects (docs/verification-rows.md).
# So a config asking for 30 gets a TEN TIMES SLOWER poll than the default it was
# trying to beat, with nothing on the device saying so — which is why
# set_ntp_client clamps rather than passing the request through.
NTP_CLIENT_INTERVAL_MIN = 60
# The firewall redirect that carries an upstream OTD500's NTP through the RUTM08
# (TEC-857). Named, so a re-run updates the rule instead of stacking duplicates.
NTP_FORWARD_NAME = "kela-ntp"
NTP_PORT = 123
# RMS lives in the `rms_mqtt` package; the connect daemon's enable flag is
# `1` by default, so "connect to RMS" is really: ensure enabled + force connect.
UCI_RMS_ENABLED = "rms_mqtt.rms_connect_mqtt.enable"
# SIM "Preferred network type" is per-SIM in `simcard`: service='lte' == 4G only
# (other values: auto, lte_nr5g, nr5g, umts, gsm ...).
SIM_SERVICE_4G = "lte"
# What the modem reports it is ATTACHED on, as the <AcT> field of `AT+COPS?`
# (3GPP TS 27.007 §7.3). The config option above is the intent; this is the
# effect, and the two disagreeing is the whole reason the 4G row exists.
ACCESS_TECH = {
    0: "GSM (2G)",
    1: "GSM Compact (2G)",
    2: "UTRAN (3G)",
    3: "GSM/EGPRS (2G)",
    4: "UTRAN/HSDPA (3G)",
    5: "UTRAN/HSUPA (3G)",
    6: "UTRAN/HSPA+ (3G)",
    7: "E-UTRAN (4G)",
    8: "EC-GSM-IoT (2G)",
    9: "E-UTRAN NB-S1 (4G)",
    10: "E-UTRA on a 5G core",
    11: "NR on a 5G core (5G)",
    12: "NG-RAN (5G)",
    13: "E-UTRA/NR dual connectivity (5G)",
}
# The codes that mean "this is a 4G attach". 10 is deliberately excluded: it is
# an LTE radio bearer on a 5G core, which a device restricted to 4G should not
# have ended up on, and calling it 4G would hide a misconfigured preference.
FOURG_ACCESS_TECH = frozenset({7, 9})
# Tailscale is an add-on package (opkg-installed). Config lives under the
# `tailscale` package's `settings` section (verified on OTD5_R_00.07.20.3).
TS_PACKAGE = "tailscale"
UCI_TS_ENABLED = "tailscale.settings.enabled"

# --- SIM switch + per-operator data limits (TEC-359) ------------------------
# The `sim_switch` and `quota_limit` packages are NOT in Teltonika's public UCI
# documentation: every option name below was read off a real OTD500 running
# OTD5_R_00.07.22.3 (`uci export sim_switch` / `uci export quota_limit`), and
# the policy was validated on that device. configure_sim_switch() therefore
# warns when it meets a different firmware instead of trusting the names.
VERIFIED_SIM_SWITCH_FW = "07.22.3"
SIM_SWITCH_PACKAGE = "sim_switch"
# The physical SIM slots we provision. Slot 3 is the eSIM: it has a section in
# the config too, which we keep explicitly disabled.
SIM_SLOTS = (1, 2)
ESIM_SLOT = 3
# `sim_switch`/`quota_limit` sections name the modem they belong to. '2-1' is
# the OTD500's only modem — a last resort for a device whose own config names
# none (we read it from the existing sections / `simcard` first).
DEFAULT_MODEM_ID = "2-1"
# data_fail: 2 == the ICMP (ping) check method, the one we use.
SIM_SWITCH_DATA_FAIL_ICMP = "2"
SIM_SWITCH_DATA_FAIL_TIMEOUT = "3"
# quota_limit period: 3 == month.
QUOTA_PERIOD_MONTH = "3"
# The conditions that are policy, not per-site tuning (TEC-359): failover is
# sticky (enable_back off — stay on whichever SIM works) and identical on both
# slots. Only the four keys in SIM_SWITCH_TUNABLES come from the config.
SIM_SWITCH_TUNABLES = {"check_interval": 30, "check_count": 5,
                       "weak_signal_dbm": -105, "icmp_host": "8.8.8.8"}

# The on-device operator→quota script (Phase 1b) and how it is scheduled.
# NOT /usr/bin: the OTD500's rootfs is a read-only squashfs and the writable
# UBI volume is overlaid onto /etc and /usr/local only, so /usr/bin can't take
# a file at all ("Read-only file system"). /usr/local/bin is the writable,
# FHS-correct home for a locally installed executable.
QUOTA_SYNC_PATH = "/usr/local/bin/kela-quota-sync"
QUOTA_SYNC_INIT_PATH = "/etc/init.d/kela-quota-sync"
# Every line we add to a shared on-device file (the crontab, the upgrade keep
# list) carries this name, and the "drop our old lines first" seds match on it —
# so a re-run replaces our block instead of appending a second copy. A line we
# write WITHOUT the name in it would never be cleaned up again.
QUOTA_SYNC_NAME = posixpath.basename(QUOTA_SYNC_PATH)
QUOTA_SYNC_CRON = f"*/10 * * * * {QUOTA_SYNC_PATH} >/dev/null 2>&1"
CRONTAB_PATH = "/etc/crontabs/root"
# Last in the boot order: nothing waits on us, and the modem is up by then.
QUOTA_SYNC_START = "99"
QUOTA_SYNC_RC_LINK = (f"/etc/rc.d/S{QUOTA_SYNC_START}"
                      f"{posixpath.basename(QUOTA_SYNC_INIT_PATH)}")
# A keep-settings firmware upgrade restores only what RutOS collects from
# /etc/sysupgrade.conf + /lib/upgrade/keep.d/* (see add_uci_conffiles in
# /usr/sbin/profile.sh). That covers /etc/config and /etc/crontabs, so the cron
# entry would outlive the script it calls — the limits would silently freeze at
# their last values. Listing our three files fixes that, and the list names
# sysupgrade.conf ITSELF: it isn't in keep.d either, so without that line the
# block would survive one upgrade and be gone for the next.
SYSUPGRADE_CONF = "/etc/sysupgrade.conf"
QUOTA_SYNC_KEEP = (SYSUPGRADE_CONF, QUOTA_SYNC_PATH, QUOTA_SYNC_INIT_PATH,
                   QUOTA_SYNC_RC_LINK)
# An unrecognised SIM (or an empty slot) gets the smallest plan's limit, so a
# SIM we can't identify can never run up the biggest plan's worth of data.
# NOTE: both site.config JSONs (example + live) carry the same values under
# sim_switch.unknown_operator; this is only the fallback when that key is
# absent — keep the three in step.
QUOTA_UNKNOWN_OPERATOR = {"data_limit_mb": 1330000, "reset_day": 1, "enabled": True}

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


def expected_utc_offset(zonename: str) -> str:
    """The UTC offset `zonename` is in RIGHT NOW, e.g. '+0300' — or "" when this
    machine has no tz database to answer with.

    Paired with `TeltonikaClient.effective_utc_offset` to check a timezone by
    its effect on the device clock rather than by reading back the UCI option we
    just wrote. It has to be computed per-call, not tabled: Asia/Jerusalem is
    +0200 in winter and +0300 under IDT, so a fixed expectation would fail for
    half the year.

    Windows has no system tz database, which is why `tzdata` is in
    requirements.txt — the bench stations are exactly where this must not
    silently degrade.
    """
    from datetime import datetime, timezone
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError:      # pragma: no cover - Python < 3.9
        return ""
    try:
        return datetime.now(timezone.utc).astimezone(
            ZoneInfo(zonename)).strftime("%z")
    except (ZoneInfoNotFoundError, ValueError, KeyError, ModuleNotFoundError):
        log.warning("No tz database entry for '%s' on this machine, so the "
                    "timezone check can only confirm the clock left UTC "
                    "(pip install tzdata).", zonename)
        return ""


def fw_version_at_least(device_fw: str, minimum: str) -> bool:
    """True when `device_fw` is `minimum` or newer.

    The ordered counterpart to fw_versions_match, for a tool whose firmware
    setting is a FLOOR rather than a pin (the TSW202: flash a unit that is
    older, leave a newer one alone). Ordering the numeric tuples rather than
    the strings is what makes that safe across the three shapes a real compare
    hits: a release name against a bare version ('TSW2_R_00.01.10' vs
    '00.01.10'), differing segment counts ('00.01.07' predates '00.01.07.1'),
    and a segment that outgrows its zero padding ('00.01.100' follows
    '00.01.99'). Get it wrong and the step flashes a switch backwards.

    False when either side carries no dotted version: "we could not tell" must
    not read as "new enough" and silently skip an upgrade.
    """
    a, b = _version_digits(device_fw), _version_digits(minimum)
    return bool(a) and bool(b) and a >= b


def fw_carries_version(device_fw: str, version: str) -> bool:
    """True when `device_fw` is the release `version`, comparing only the parts
    `version` actually names.

    Unlike fw_versions_match (which demands the SAME dotted version), this
    answers "is this the firmware feature X was verified on?" for a short
    marker like '07.22.3' against what the device reports
    ('OTD5_R_00.07.22.3' / '00.07.22.3') — the leading '00.' is release
    packaging, not a version difference. A differing digit never matches.
    """
    a, b = _version_digits(device_fw), _version_digits(version)
    if not a or not b:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return longer[-len(shorter):] == shorter


_HOSTNAME_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME_RE = re.compile(rf"{_HOSTNAME_LABEL}(?:\.{_HOSTNAME_LABEL})*")


def icmp_host(cfg: dict) -> str:
    """`sim_switch.icmp_host` as an IP address or hostname, or SystemExit.

    This ends up in `data_fail_host`, the address the device pings to decide the
    active SIM has no working data. A typo there is invisible on the device — the
    check simply never succeeds, which on our rules means the SIM keeps being
    counted as failed — so a malformed value is a hard error, raised by
    validate_sim_switch_config() before anything is written.
    """
    raw = str(cfg.get("icmp_host") or SIM_SWITCH_TUNABLES["icmp_host"]).strip()
    if re.fullmatch(r"[0-9.]+", raw) or ":" in raw:
        # Meant as an IP literal, so require it to parse as one: '8.8.8',
        # '999.1.1.1' and a trailing '.' or ',' would all otherwise be accepted
        # as perfectly good hostnames.
        try:
            ipaddress.ip_address(raw)
            return raw
        except ValueError:
            pass
    elif len(raw) <= 253 and _HOSTNAME_RE.fullmatch(raw):
        return raw
    raise SystemExit("sim_switch.icmp_host must be an IP address or hostname "
                     f"(got {raw!r}).")


def sim_switch_options(position: int, *, cfg: Optional[dict] = None,
                       modem: str = "", enabled: bool = True) -> list[tuple[str, str]]:
    """The `config sim` options for ONE sim_switch slot, in the order the device
    itself writes them (TEC-359, verified on FW 07.22.3).

    `enabled=False` renders the minimal "slot exists but never switched to"
    section used for the eSIM slot. `modem=''` omits the modem option — for
    building the expected values in verification, where only the conditions
    matter. Everything except the four SIM_SWITCH_TUNABLES is fixed policy:
    sticky failover (enable_back off), symmetric on both slots.
    """
    cfg = cfg or {}
    opts: list[tuple[str, str]] = []
    if modem:
        opts.append(("modem", modem))
    opts += [("position", str(position)),
             # Priority is the slot number: slot 1 is tried first.
             ("order", str(position)),
             ("enabled", "1" if enabled else "0")]
    if not enabled:
        return opts

    def tunable(key: str) -> str:
        """A configured whole number, or the policy default when unset."""
        raw = cfg.get(key)
        if raw is None or str(raw).strip() == "":
            raw = SIM_SWITCH_TUNABLES[key]
        try:
            return str(int(str(raw).strip()))
        except ValueError:
            raise SystemExit(f"sim_switch.{key} must be a whole number (got {raw!r}).")

    host = icmp_host(cfg)
    return opts + [
        ("interval", tunable("check_interval")),           # check interval, seconds
        ("retry_count", tunable("check_count")),           # consecutive checks
        ("on_signal", "1"),                                # switch on weak signal
        ("weak_signal", tunable("weak_signal_dbm")),       # RSSI dBm threshold
        ("data_limit", "1"),                               # switch on data limit
        ("sms_limit", "0"),
        ("roaming", "0"),
        ("no_network", "1"),
        ("denied", "1"),                                   # network denied (barred SIM)
        ("sim_not_ready", "1"),                            # SIM not inserted
        ("data_fail", SIM_SWITCH_DATA_FAIL_ICMP),
        ("data_fail_host", host),
        ("data_fail_timeout", SIM_SWITCH_DATA_FAIL_TIMEOUT),
        ("enable_back", "0"),                              # sticky: never switch back
        # The WebUI writes fail_flag='1' on every save. Its purpose is not
        # documented, so we mirror what the WebUI does rather than guess.
        ("fail_flag", "1"),
    ]


def _whole_number(value, what: str) -> int:
    """`value` as an int, or SystemExit naming the config key — NOT ValueError,
    which would escape the step runner (it catches SystemExit only) and take
    down the whole run instead of failing the one step."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise SystemExit(f"{what} must be a whole number (got {value!r}).")


def quota_operators(cfg: dict) -> list[dict]:
    """The operator table from a `sim_switch` config block, normalised to
    {name, iccid_prefixes, mccmnc, data_limit_mb, reset_day, enabled}.

    Config typos here become wrong data limits on field SIMs, so they are hard
    errors rather than skipped rows: a non-numeric ICCID prefix (or an operator
    with none at all), a non-numeric limit, or an enabled operator without a
    positive data_limit_mb — which would write a 0 MB limit to the device and
    cut the SIM's data on the first sync.
    """
    operators = []
    for raw in cfg.get("operators") or []:
        name = re.sub(r"[^a-z0-9_-]", "", str(raw.get("name", "")).lower()) or "operator"
        prefixes = [str(p).strip() for p in (raw.get("iccid_prefixes") or [])]
        if not prefixes or not all(p.isdigit() for p in prefixes):
            raise SystemExit(f"sim_switch.operators['{name}']: iccid_prefixes must be "
                             f"a list of digit strings (got {prefixes!r}).")
        enabled = bool(raw.get("enabled", True))
        limit = _whole_number(raw.get("data_limit_mb", 0),
                              f"sim_switch.operators['{name}'].data_limit_mb")
        if enabled and limit <= 0:
            raise SystemExit(f"sim_switch.operators['{name}']: an enabled operator "
                             f"needs data_limit_mb > 0 (got {limit}).")
        operators.append({
            "name": name,
            "iccid_prefixes": prefixes,
            # The mccmnc only ever lands in a generated-script comment, but a
            # stray character (a newline, a quote) there would corrupt the
            # script — keep the digits-and-dash shape and nothing else.
            "mccmnc": re.sub(r"[^0-9-]", "", str(raw.get("mccmnc", ""))),
            "data_limit_mb": limit,
            "reset_day": _whole_number(raw.get("reset_day", 1),
                                       f"sim_switch.operators['{name}'].reset_day"),
            "enabled": enabled,
        })
    return operators


def quota_unknown_operator(cfg: dict) -> dict:
    """The fallback row for a SIM no ICCID prefix matches (or an empty slot),
    normalised and validated like the quota_operators() rows."""
    unknown = {**QUOTA_UNKNOWN_OPERATOR, **(cfg.get("unknown_operator") or {})}
    enabled = bool(unknown.get("enabled", True))
    limit = _whole_number(unknown.get("data_limit_mb"),
                          "sim_switch.unknown_operator.data_limit_mb")
    if enabled and limit <= 0:
        raise SystemExit("sim_switch.unknown_operator: an enabled fallback needs "
                         f"data_limit_mb > 0 (got {limit}).")
    return {"data_limit_mb": limit, "enabled": enabled,
            "reset_day": _whole_number(unknown.get("reset_day"),
                                       "sim_switch.unknown_operator.reset_day")}


def validate_sim_switch_config(cfg: dict) -> None:
    """Fail fast on a bad `sim_switch` block. Meant to run BEFORE the pipeline
    touches the device: configure_sim_switch() commits UCI before
    install_quota_sync() parses the operator table, so a typo caught only there
    would leave the device half-configured."""
    sim_switch_options(SIM_SLOTS[0], cfg=cfg)   # validates the tunables
    quota_operators(cfg)
    quota_unknown_operator(cfg)


# The generated script's constant halves. Kept as text (not f-strings) because
# every other line contains shell `$`/`{}`; only the operator table and the
# handful of values above it are rendered per site.
_QUOTA_SYNC_PROLOGUE = """#!/bin/sh
# kela-quota-sync — per-operator mobile data limits (TEC-359).
#
# GENERATED by the bench OTD500 configurator from the sim_switch.operators table
# in its site.config.json. Change the bench config and re-provision the device
# rather than editing this file.
#
# SIMs are inserted in the field and get swapped between slots, so a data limit
# has to follow the SIM, not the slot. Each slot's operator is identified by the
# ICCID prefix in `simcard.@sim[N].iccid` — RutOS keeps that for the INACTIVE
# slots too, unlike the IMSI, which only the active SIM reports — and the
# matching quota_limit section is rewritten when it differs.
#
# Runs at boot (/etc/init.d/kela-quota-sync) and every 10 minutes from cron.

set -u

"""

_QUOTA_SYNC_BODY = """
log() { logger -t kela-quota-sync "$1"; }

# One run at a time: the boot hook (delayed 90 s) can land on the same second
# as a 10-minute cron tick, and two interleaved `uci set/commit` sequences
# would race. /tmp is wiped on boot, so a crashed run can't wedge the lock
# past a reboot; within an uptime the script finishes in seconds.
LOCK=/tmp/kela-quota-sync.lock
mkdir "$LOCK" 2>/dev/null || exit 0
trap 'rmdir "$LOCK"' EXIT

changed=0
detected=''

# set_opt <section> <option> <wanted value>
set_opt() {
    cur=$(uci -q get "quota_limit.$1.$2")
    [ "$cur" = "$3" ] && return 0
    uci set "quota_limit.$1.$2=$3"
    changed=1
    log "$1.$2: '$cur' -> '$3'"
}

# index_for <slot> -> the simcard section index for that slot, matched by its
# `position` option — file order is not trusted to equal slot order. Falls
# back to slot-1 for a firmware whose simcard sections carry no position.
index_for() {
    idx=$(uci show simcard 2>/dev/null |
        sed -n "s/^simcard\\.@sim\\[\\([0-9]*\\)\\]\\.position='$1'$/\\1/p" | head -n 1)
    echo "${idx:-$(($1 - 1))}"
}

for slot in $SLOTS; do
    section="mob1s${slot}a1"
    index=$(index_for "$slot")
    iccid=$(uci -q get "simcard.@sim[$index].iccid")
    set -- $(operator_for "$iccid")
    limit="$2" day="$3" on="$4"
    detected="$detected slot$slot=$1"

    if [ -z "$(uci -q get "quota_limit.$section")" ]; then
        # No section yet (a device that never had a limit configured): create it
        # with the fields the WebUI writes. 'interface' is the section type
        # quota_limit uses on 07.22.3.
        uci set "quota_limit.$section=interface"
        uci set "quota_limit.$section.ifname=$section"
        uci set "quota_limit.$section.sim=$slot"
        modem=$(uci -q get "simcard.@sim[$index].modem")
        [ -n "$modem" ] && uci set "quota_limit.$section.modem=$modem"
        changed=1
        log "$section: created"
    fi

    # event_sent is quota_limit's runtime state — deliberately never written.
    set_opt "$section" enabled "$on"
    set_opt "$section" data_limit "$limit"
    set_opt "$section" period "$PERIOD"
    set_opt "$section" reset_day "$day"
done

# The eSIM slot carries no plan of ours: keep its limit off if it has a section.
[ -n "$(uci -q get "quota_limit.$ESIM_SECTION")" ] && set_opt "$ESIM_SECTION" enabled 0

if [ "$changed" = 1 ]; then
    uci commit quota_limit
    /etc/init.d/quota_limit restart >/dev/null 2>&1
    log "applied:$detected"
fi
exit 0
"""

# The boot hook, deployed verbatim (nothing in it is per-site): run
# kela-quota-sync once per boot, so a SIM swapped while the device was powered
# off is picked up without waiting for the 10-minute cron tick.
QUOTA_SYNC_INIT = """#!/bin/sh /etc/rc.common
# GENERATED by the bench OTD500 configurator (TEC-359): run kela-quota-sync once
# per boot, so a SIM swapped while the device was powered off is picked up
# without waiting for the 10-minute cron tick.
START=""" + QUOTA_SYNC_START + """
STOP=""" + QUOTA_SYNC_START + """

start() {
    # Backgrounded with a delay: never hold up boot, and give the modem time to
    # publish the ICCID of a card it has not read yet.
    (sleep 90; """ + QUOTA_SYNC_PATH + """) &
}

stop() {
    return 0
}
"""


def render_quota_sync_script(cfg: dict) -> str:
    """The on-device operator→quota script, rendered from a `sim_switch` config
    block (its `operators` table becomes the ICCID-prefix case arms)."""
    unknown = quota_unknown_operator(cfg)
    arms = []
    for op in quota_operators(cfg):
        pattern = "|".join(f"{p}*" for p in op["iccid_prefixes"])
        arms.append(f"        {pattern}) echo '{op['name']} {op['data_limit_mb']} "
                    f"{op['reset_day']} {1 if op['enabled'] else 0}' ;;"
                    + (f"  # {op['mccmnc']}" if op["mccmnc"] else ""))
    arms.append(f"        *) echo 'unknown {unknown['data_limit_mb']} "
                f"{unknown['reset_day']} {1 if unknown['enabled'] else 0}' ;;"
                "  # unrecognised SIM or empty slot")
    return (
        _QUOTA_SYNC_PROLOGUE
        + f"SLOTS='{' '.join(str(s) for s in SIM_SLOTS)}'\n"
        + f"PERIOD='{QUOTA_PERIOD_MONTH}'\n"
        + f"ESIM_SECTION='mob1s{ESIM_SLOT}a1'\n"
        + "\n# operator_for <iccid> -> '<name> <data_limit_mb> <reset_day> <enabled>'\n"
        + "# Limits are DEVICE MB, which are binary: 3000000 MB reads as 2.86 TB.\n"
        + 'operator_for() {\n    case "$1" in\n'
        + "\n".join(arms)
        + "\n    esac\n}\n"
        + _QUOTA_SYNC_BODY
    )


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


def arp_table() -> dict[str, list[str]]:
    """{canonical MAC: [address, ...]} from the host's ARP cache.

    One listing covers the whole cache, so a caller looking for one device among
    many candidate addresses reads it once instead of shelling out per address.
    Every address a MAC appears at is kept, in the order the OS listed them: a
    device that has just moved is often cached at BOTH its old address (stale,
    not yet expired) and its new one, and picking one blind is how you end up
    chasing the address it left. Best-effort: empty when no listing command
    works."""
    cmds = ([["arp", "-a"]] if sys.platform == "win32"
            else [["arp", "-an"], ["ip", "neigh", "show"]])
    for cmd in cmds:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
        except Exception:  # noqa: BLE001 — try the next command / give up quietly
            continue
        table: dict[str, list[str]] = {}
        for ip, mac in _ARP_ROW_RE.findall(out or ""):
            canon = canonical_mac(mac)
            if canon in _NON_MACS:
                continue
            addresses = table.setdefault(canon, [])
            if ip not in addresses:
                addresses.append(ip)
        if table:
            return table
    return {}


def refresh_arp_cache(subnets: list[str], *, port: int,
                      timeout: float = SCAN_TCP_TIMEOUT,
                      workers: int = SCAN_WORKERS) -> None:
    """Knock on every address in `subnets` so the host's ARP cache learns who is
    out there (and forgets who isn't).

    Only the ARP side effect matters, so the results are dropped: address
    resolution happens BEFORE the connection is attempted, which means a device
    lands in the cache whether or not it is serving `port` yet. That is the
    point — a device still finishing its boot has no open port but does answer
    ARP."""
    candidates: list[str] = []
    for subnet in subnets or []:
        try:
            candidates += [str(h) for h in
                           ipaddress.ip_network(subnet, strict=False).hosts()]
        except ValueError:
            log.warning("Skipping invalid scan subnet %r.", subnet)
    if not candidates:
        return
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for ip in candidates:
            pool.submit(tcp_port_open, ip, port, timeout)
        # The pool's exit waits for every probe, so the cache is warm on return.


def find_ip_by_mac(mac: str, subnets: list[str], *, port: int,
                   confirm_timeout: float = 2.0,
                   timeout: float = SCAN_TCP_TIMEOUT,
                   workers: int = SCAN_WORKERS) -> Optional[str]:
    """The address of the host with `mac` on `subnets` that is answering on
    `port`, or None.

    For a device that has moved somewhere we can't predict — it was just
    switched to DHCP, say — so it has to be found by the one identifier that
    didn't change. Refreshes the ARP cache over `subnets`, then checks EVERY
    address that MAC is cached at, returning the first that actually answers.

    Two rules earn their keep here, both learned the hard way:

    * the answering check is a real connection with its own `confirm_timeout`,
      not the sweep's deliberately-impatient probe — a device that has an
      address but has not finished starting its web server is found, and the
      caller can wait for it rather than concluding it vanished;
    * every cached address is tried, so a stale entry pointing at the address
      the device just left neither hides the new one nor gets mistaken for it
      (the device it points at is gone, so it cannot answer).

    NOTE: ARP is link-local. This only ever finds a device on a subnet the host
    itself holds an address on — off-subnet, the cache resolves the router's
    MAC, never the device's."""
    want = canonical_mac(mac or "")
    if not want or want in _NON_MACS:
        return None
    refresh_arp_cache(subnets, port=port, timeout=timeout, workers=workers)
    for ip in arp_table().get(want, []):
        if tcp_port_open(ip, port, confirm_timeout):
            return ip
    return None


class MutationBlocked(Exception):
    """A read-only client was asked to change the device (TEC-348).

    Deliberately NOT a SystemExit. Pipelines catch SystemExit all over the place
    to record a step failure and carry on, and this must not be absorbed that
    way: reaching it means a verify pipeline tried to mutate, which is a bug in
    the pipeline, and the run has to fail loudly with the offending command
    named rather than quietly reporting one red row.
    """


# Commands that change a RutOS device, matched against anything handed to
# `ssh_exec` while the client is read-only. A deny-list over arbitrary shell is
# a seatbelt, not a proof — the proof is test_verify_only_is_mutation_free.py,
# which asserts a verify pipeline issues no such command in the first place.
# This exists to catch the case that test can't: a real device, a pipeline
# changed later, and nobody re-reading it.
#
# Bare word-boundary matching was tried first and is wrong in both directions.
# `grep -v '^#' /etc/sysupgrade.conf` is a READ that the SIM-switch check makes
# on every OTD500 run, and `\bsysupgrade\b` matches it — a check would have
# started refusing to run. So the destructive verbs have to be anchored at a
# command position: start of the command, or after a `;`, `|`, `&&` or `||`.
_CMD_START = r"(?:^|[;&|(]\s*)"

_MUTATING_COMMANDS = re.compile(
    rf"""
    # uci writes. Not anchored: `uci get`/`uci show` are excluded by the verb
    # list, so matching anywhere is safe and catches a chained `... && uci set`.
      \buci \s+ (?:-\S+\s+)* (?:set|add|add_list|del_list|delete|commit|revert|rename|batch)\b
    # Applying config: an init script that restarts is how a write takes effect.
    | /etc/init\.d/\S+ \s+ (?:restart|reload|start|stop)\b
    # Destructive verbs, only where they are the command being run.
    | {_CMD_START}
      (?:reboot|halt|poweroff|sysupgrade|firstboot|opkg|chpasswd|passwd
        |mv|cp|rm|mkdir|rmdir|chmod|chown|touch|tee|dd|kill|killall)
      (?:\s|$)
    | \bsed \s+ -i\b
    | \bln \s+ -s\b
    # Stepping the clock. Narrow on purpose: `date +%z` and `date -u +%s` are
    # reads the checks depend on, and only the `-s` form writes.
    | \bdate \s+ (?:-\S+\s+)* -s\b
    | {_CMD_START} crontab (?:\s|$)
    # Writing into the places device config lives. `2>/dev/null` is not one.
    | >>? \s* (?:/etc/|/tmp/|/proc/|/sys/|/usr/|/root/|/overlay/)
    | \btailscale \s+ (?:up|down|logout|set)\b
    | \bubus \s+ call \s+ \S+ \s+ (?:reload|restart|connect|set|write)\b
    # Downloading an eSIM profile writes to the modem; resetting it drops the
    # link. `--esim-list` and the other gsmctl reads are untouched.
    | \bgsmctl \s+ (?:--esim-download | -A\s+'AT\+CFUN)
    """,
    re.VERBOSE,
)


class TeltonikaClient:
    """One OTD500 over REST (primary) + SSH/UCI (config). Never opens SSH until
    a step actually needs it.

    A client can be put into **read-only** mode (`read_only=True`, TEC-348), in
    which every method that changes the device refuses. That is what makes the
    Verify pass mutation-free by construction rather than by careful reading of
    the pipeline — see `_refuse_mutation`.
    """

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
        # Read-only mode (TEC-348). Off by default: a configure pipeline is the
        # normal case, and a flag that had to be turned OFF to provision would
        # be the wrong way round.
        self.read_only: bool = False

    # --- read-only mode (TEC-348) -------------------------------------------
    def set_read_only(self, read_only: bool = True) -> None:
        """Refuse, from here on, anything that would change the device.

        Called by a verify pipeline once it has logged in — after, because
        logging in is a POST and the device's own session state is the one thing
        a check legitimately needs to establish (the password row IS "are we
        authenticated on the shared password").
        """
        self.read_only = read_only
        if read_only:
            log.info("Client is read-only for this run — nothing will be changed.")

    def _refuse_mutation(self, what: str) -> None:
        """Raise if this client is read-only. Called at the top of every method
        that writes, so the message names the step an operator would recognise
        rather than the shell command underneath it."""
        if self.read_only:
            raise MutationBlocked(
                f"refusing to {what}: this is a verify-only run and must not "
                "change the device")

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
        stuck on 'busy' until someone restarts the app.

        On a read-only client (TEC-348) a command that looks like it writes is
        refused before it reaches the device."""
        if self.read_only and _MUTATING_COMMANDS.search(command):
            raise MutationBlocked(
                f"refusing to run a command that changes the device on a "
                f"verify-only run: {command}")
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
        self._refuse_mutation(f"write UCI options in '{package}'")
        cmd = " && ".join([f"uci set {s}" for s in sets] + [f"uci commit {package}"])
        self.ssh_exec(cmd)

    @staticmethod
    def _uci_arg(path: str, value) -> str:
        """A shell-safe `package.section.option=value` argument for `uci set`.

        The WHOLE argument is quoted, not just the value: anonymous sections are
        addressed as `@sim[0]`, and an unquoted `[0]` is a shell glob."""
        return shlex.quote(f"{path}={value}")

    def _uci_package(self, package: str) -> tuple[dict, dict]:
        """`uci show <package>` parsed into ({section: type}, {(section, option): value}).

        For the packages whose section shape is not fixed across builds. RutOS
        names the `ntpclient` server sections `1`..`4` on the OTD500 but leaves
        firewall redirects anonymous (`@redirect[0]`), so callers discover what
        is there rather than addressing a section by a name that may not exist.
        A missing package reads as two empty dicts, not an error.
        """
        out = self.ssh_exec(f"uci show {package} 2>/dev/null", check=False)
        types: dict[str, str] = {}
        options: dict[tuple[str, str], str] = {}
        for line in out.splitlines():
            m = re.match(rf"{re.escape(package)}\.(@?[\w.\[\]-]+?)"
                         rf"(?:\.([\w-]+))?=(.*)$", line.strip())
            if not m:
                continue
            section, option = m.group(1), m.group(2)
            value = m.group(3).strip().strip("'\"")
            if option is None:
                types[section] = value
            else:
                options[(section, option)] = value
        return types, options

    def _uci_sections(self, types: dict, section_type: str) -> list[str]:
        """The sections of one type, in the order `uci show` listed them."""
        return [s for s, t in types.items() if t == section_type]

    def _uci_add(self, package: str, section_type: str) -> str:
        """`uci add` an anonymous section, returning the id UCI assigned it (e.g.
        'cfg0492bd'). The add is staged, so the caller's `uci commit` persists it."""
        self._refuse_mutation(f"add a '{section_type}' section to '{package}'")
        out = self.ssh_exec(f"uci add {package} {section_type}").strip()
        section = out.splitlines()[-1].strip() if out else ""
        if not section:
            raise SystemExit(f"'uci add {package} {section_type}' returned no section id.")
        return section

    def _ssh_report(self, command: str, what: str) -> None:
        """Run `command`, raising SystemExit("<what>: <the device's own error>").

        ssh_exec's failure message quotes the whole command, which for a file
        write means echoing the file back at the operator. This asks the shell
        for its stderr instead and reports only that — the difference between
        'Failure' and 'Read-only file system'."""
        out = self.ssh_exec(f"{{ {command}\n}} 2>&1 && echo __OK__", check=False)
        if "__OK__" in out.split():
            return
        detail = next((ln.strip() for ln in reversed(out.splitlines()) if ln.strip()),
                      "no error output")
        raise SystemExit(f"{what}: {detail}")

    # Heredoc delimiter for _put_file: a line equal to it would end the write
    # early, so it must not occur in anything we generate.
    PUT_FILE_EOF = "__KELA_BENCH_EOF__"

    def _put_file(self, path: str, content: str, *, mode: str = "644") -> None:
        """Write `content` to `path` on the device, atomically.

        Sent as a quoted heredoc over the same SSH exec channel every other step
        uses, NOT over SFTP: RutOS's sftp-server answers a refused open with a
        bare 'Failure', collapsing a read-only filesystem and a full one into the
        same word, and a device whose firmware ships no sftp subsystem can't take
        a file that way at all. The shell reports the real errno. (Firmware
        images still go by SFTP — they're megabytes, not a 2 KB script.)

        Written next to the target and moved into place, so neither cron nor the
        init system can catch a half-written file."""
        self._refuse_mutation(f"write {path} on the device")
        if self.PUT_FILE_EOF in content:
            raise SystemExit(f"Refusing to write {path}: it contains the heredoc "
                             f"delimiter {self.PUT_FILE_EOF}.")
        tmp, body = shlex.quote(f"{path}.new"), content.rstrip("\n")
        parent = shlex.quote(posixpath.dirname(path) or "/")
        self._ssh_report(f"mkdir -p {parent} && cat > {tmp} <<'{self.PUT_FILE_EOF}'\n"
                         f"{body}\n{self.PUT_FILE_EOF}",
                         f"Could not write {path} on the device")
        self._ssh_report(f"chmod {mode} {tmp} && mv {tmp} {shlex.quote(path)}",
                         f"Could not install {path} on the device")

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
        # After the early return, not before: a verify pass calls nothing here,
        # but a device already on the shared password is the no-op case and
        # refusing it would be refusing to do nothing.
        self._refuse_mutation("change the admin password")
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
        self._refuse_mutation("upgrade the firmware")
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
        self._refuse_mutation(f"run '{command}'")
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

    def hostname_check(self, hostname: str) -> dict:
        """One `{item, expected, actual, ok}` verification row for the hostname.

        Two things have to agree, for the same reason the timezone row checks
        two: `set_hostname` writes BOTH `system.system.hostname` and
        `/proc/sys/kernel/hostname`, and the check used to read back only the
        first. That is a tautology — `uci set` creates an option whether or not
        anything consumes it — and it is the same fault class that shipped a
        TSW202 on a UTC clock with four green rows.

        The RUNNING kernel hostname is the effect: it is what the device calls
        itself to syslog, to DHCP and to RMS, so it is the name an engineer will
        later search for. The UCI option is checked as well because that is what
        survives a reboot; a device right in one and wrong in the other is
        half-named, and which half is wrong changes what you do about it.
        """
        stored = self.ssh_exec(f"uci -q get {UCI_HOSTNAME}", check=False).strip()
        # `hostname` (the command) reads the running kernel value; the proc file
        # is the fallback for a build without the busybox applet.
        running = self.ssh_exec(
            "hostname 2>/dev/null || cat /proc/sys/kernel/hostname 2>/dev/null",
            check=False).strip()
        return {"item": "hostname", "expected": hostname,
                "actual": (f"running {running or '(unset)'}, "
                           f"stored {stored or '(unset)'}"),
                "ok": running == hostname and stored == hostname}

    def effective_utc_offset(self) -> str:
        """The device clock's CURRENT UTC offset, e.g. '+0300' — or "".

        The only layout-independent evidence that a timezone actually took: it
        is what the running clock does, not what a UCI option says. A switch
        whose `system.ntp.zoneName` reads back 'Asia/Jerusalem' while this
        returns '+0000' has been written to and is ignoring it.
        """
        return self.ssh_exec("date +%z", check=False).strip()

    def timezone_check(self, zonename: str) -> dict:
        """One `{item, expected, actual, ok}` verification row for the timezone.

        Shared by the routers and the switch because the failure was: the
        TSW202 passed a read-back of `system.ntp.zoneName` while running on
        UTC and displaying UTC.

        Two things have to agree. The CLOCK is the real evidence — `uci set`
        creates an option whether or not the device consumes it, so only the
        offset proves the zone took. The zone NAME is checked as well because
        that is what the WebUI's dropdown renders, and a device whose clock is
        right while its UI says UTC is one accidental Save & Apply away from
        losing the clock too.
        """
        offset = self.effective_utc_offset()
        shown = self.ssh_exec(f"uci -q get {UCI_ZONENAME}", check=False).strip()
        want = expected_utc_offset(zonename)
        if want:
            expected = f"{zonename} (clock at {want})"
            clock_ok = offset == want
        else:
            # No tz database on this station to say what the zone means today;
            # the clock having left UTC is the most this can honestly claim.
            expected = f"{zonename} (clock off UTC)"
            clock_ok = bool(offset) and offset != "+0000"
        return {"item": "timezone", "expected": expected,
                "actual": f"clock at {offset or 'unknown'}, "
                          f"WebUI shows {shown or '(unset)'}",
                "ok": clock_ok and shown == zonename}

    def configured_ntp_servers(self) -> list[str]:
        """Every NTP server the device has configured, in whatever section shape
        it keeps them, across BOTH time subsystems.

        Deliberately NOT `uci get system.ntp.server`: that reads back exactly
        what `set_ntp_server` writes, which passed verification on a TSW202
        whose WebUI still listed four Google servers untouched. Collecting every
        `server` / `hostname` option in the package finds the ones the device
        really uses, whether they are a `list server` on one section or a
        `hostname` on one section per server.

        The `ntpclient` package is scanned for the same reason one step on
        (TEC-857): stock RutOS ships `time2/3/4.google.com` there, a `uci show
        system` scan cannot see them, and on an offline site each one costs a
        failover timeout before the server that would have worked is tried.
        Devices without that package (the TSW202) simply contribute nothing.
        """
        servers: list[str] = []

        def collect(package: str, skip_device_hostname: bool) -> None:
            out = self.ssh_exec(f"uci show {package} 2>/dev/null", check=False)
            for line in out.splitlines():
                m = re.match(rf"{re.escape(package)}\.(@?[\w.\[\]-]+?)"
                             rf"\.(server|hostname)=(.*)$", line.strip())
                if not m:
                    continue
                section, option = m.group(1), m.group(2)
                # The DEVICE name, not a time server. It lives on the `system`
                # section, which a build may render named (`system.system`) or
                # anonymous (`system.@system[0]`) — matching only the first
                # collected the switch's own hostname as a server and failed a
                # row that should pass.
                if skip_device_hostname and option == "hostname" \
                        and re.fullmatch(r"system|@system\[\d+\]", section):
                    continue
                # uci renders a list as `opt='a' 'b' 'c'`.
                for token in m.group(3).split():
                    value = token.strip().strip("'\"")
                    if value and value not in servers:
                        servers.append(value)

        collect("system", skip_device_hostname=True)
        collect(NTP_CLIENT_PACKAGE, skip_device_hostname=False)
        return servers

    def running_ntp_servers(self) -> list[str]:
        """The servers the RUNNING ntpd was started with, off its command line.

        The companion to `configured_ntp_servers`: a committed config the daemon
        never picked up is exactly the failure the timezone had, and the only
        thing that settles it is what the live process is polling. The bracket
        in `[n]tpd` keeps grep from matching itself.
        """
        out = self.ssh_exec("ps w 2>/dev/null | grep '[n]tpd'", check=False)
        return re.findall(r"-p\s+(\S+)", out)

    def _ntp_client_sections(self, types: dict,
                             options: dict) -> tuple[list[str], list[str]]:
        """The `ntpclient` package's (server sections, settings sections).

        Both discovered rather than named, and the settings side is NOT "every
        section that is not a server". A real OTD500 carries a third section,
        `ntpdrift`, which owns only `freq` and sorts BEFORE the settings one — so
        that rule reads the poll interval off the wrong section (reporting a
        correctly configured device as blank) and writes enabled/interval/
        zoneName onto a section that owns none of them.

        Typing it `ntpclient` is not a guess: RutOS's own `/etc/init.d/ntpclient`
        does `config_foreach start_client ntpclient` and then reads `enabled` off
        each section it finds, defaulting to 0. So that section is also what
        decides whether the daemon starts at all — writing the flag anywhere
        else leaves a device with nothing polling.
        """
        server_sections = [s for t in NTP_SERVER_TYPES
                           for s in self._uci_sections(types, t)]
        if not server_sections:
            server_sections = [s for s in types if (s, "hostname") in options]

        # The settings section is the one typed after the package itself.
        settings = [s for s in self._uci_sections(types, NTP_CLIENT_PACKAGE)
                    if s not in server_sections]
        if not settings:
            # A build that types it differently: fall back to whatever already
            # carries the options we are about to read or write, which is still
            # narrower than "not a server".
            settings = [s for s in types
                        if s not in server_sections
                        and ((s, "interval") in options or (s, "enabled") in options)]
        return server_sections, settings

    def ntp_client_settings(self) -> dict:
        """The `ntpclient` package's own state: {servers, enabled, interval, zonename}.

        Empty when the device has no such package (the TSW202 keeps its client
        in `system.ntp` instead), which callers read as "not applicable" rather
        than "misconfigured".
        """
        types, options = self._uci_package(NTP_CLIENT_PACKAGE)
        if not types:
            return {}
        server_sections, settings = self._ntp_client_sections(types, options)
        first = settings[0] if settings else ""
        return {
            "servers": [options[(s, "hostname")] for s in server_sections
                        if options.get((s, "hostname"))],
            "enabled": options.get((first, "enabled"), "") if first else "",
            "interval": options.get((first, "interval"), "") if first else "",
            "zonename": options.get((first, "zoneName"), "") if first else "",
        }

    def set_ntp_client(self, server: str, *, interval: int = NTP_CLIENT_INTERVAL,
                       zonename: str = "") -> None:
        """Point the RutOS `ntpclient` at `server` as the device's ONLY time source.

        Not the same subsystem as `system.ntp` (see NTP_CLIENT_PACKAGE above),
        and on a RUTM08/OTD500 this is the one that actually polls. Both are
        written here: leaving `system.ntp` disabled on UTC while the client runs
        on local time is the state TEC-846 found in the field, and it makes the
        device's own log timestamps disagree with each other.

        The stock servers 2-4 are DELETED rather than left below ours. They are
        `time2/3/4.google.com`, which on an offline site resolve to nothing and
        cost a failover timeout each before the server that would have worked is
        reached. Section names are discovered, not assumed: TEC-857 quotes
        `ntpclient.1.hostname`, but that numbering is a build detail.
        """
        self._refuse_mutation(f"point the NTP client at {server}")
        # Clamp before writing, never after: a rejected value does not come back
        # as an error, it comes back as 600 on the next read, and the row would
        # then report a device polling slower than the config asked for with no
        # indication of why.
        if interval < NTP_CLIENT_INTERVAL_MIN:
            log.warning("Poll interval %ss is below the %ss this build accepts — "
                        "RutOS would silently substitute 600s, which is slower "
                        "than asking for nothing. Using %ss.",
                        interval, NTP_CLIENT_INTERVAL_MIN, NTP_CLIENT_INTERVAL_MIN)
            interval = NTP_CLIENT_INTERVAL_MIN
        log.info("Pointing the NTP client at %s (interval %ss) ...", server, interval)
        types, options = self._uci_package(NTP_CLIENT_PACKAGE)
        if not types:
            raise SystemExit(
                f"This device has no '{NTP_CLIENT_PACKAGE}' package "
                f"(`uci show {NTP_CLIENT_PACKAGE}` returned nothing), so there is "
                f"no client to point at {server}.")

        server_sections, settings_sections = self._ntp_client_sections(types, options)
        if server_sections:
            keep, drop = server_sections[0], server_sections[1:]
        else:
            keep, drop = self._uci_add(NTP_CLIENT_PACKAGE, NTP_SERVER_TYPES[0]), []

        def opt(section: str, option: str, value) -> str:
            return f"uci set {self._uci_arg(f'{NTP_CLIENT_PACKAGE}.{section}.{option}', value)}"

        cmds = [opt(keep, "hostname", server),
                # An inherited non-123 port would quietly miss the RUTM08's
                # forward, which only carries 123.
                opt(keep, "port", NTP_PORT)]
        # Reverse order: anonymous sections are addressed by index, so deleting
        # from the front renumbers the ones still to go.
        for section in reversed(drop):
            cmds.append(f"uci delete {shlex.quote(f'{NTP_CLIENT_PACKAGE}.{section}')}")

        if settings_sections:
            for section in settings_sections:
                cmds.append(opt(section, "enabled", 1))
                cmds.append(opt(section, "interval", interval))
                if zonename:
                    cmds.append(opt(section, "zoneName", zonename))
        else:
            log.warning("No %s settings section found — the poll interval is "
                        "whatever the device shipped with.", NTP_CLIENT_PACKAGE)
        cmds.append(f"uci commit {NTP_CLIENT_PACKAGE}")
        self.ssh_exec(" && ".join(cmds))

        # The other subsystem, so the two cannot disagree.
        self.ssh_exec(
            f"uci -q delete {UCI_NTP_SERVER}; "
            f"uci add_list {self._uci_arg(UCI_NTP_SERVER, server)} && "
            f"uci set {self._uci_arg(UCI_NTP_ENABLED, 1)} && "
            f"uci commit system")

        self.ssh_exec(f"/etc/init.d/{NTP_CLIENT_PACKAGE} restart", check=False)
        self.ssh_exec("/etc/init.d/sysntpd restart", check=False)
        log.info("NTP client set.")

    def ntp_client_check(self, server: str, *,
                         interval: int = NTP_CLIENT_INTERVAL) -> dict:
        """One `{item, expected, actual, ok}` row for the RutOS NTP client.

        Read-back, and deliberately so. The server sits on the assembly network,
        the bench provisions one device at a time, and an OTD500 is never cabled
        behind the RUTM08 that would carry it there — so there is no reachable
        server to sync against and nothing an `ntpclient -d` probe here could
        report but "no reply". `docs/verification-rows.md` records why this bench
        has no sync row and what would close the gap.

        Four facts, because each fails on its own: every server in EITHER time
        subsystem (a surviving `time2.google.com` is a device that will drift to
        internet time on a site that has no internet), BOTH enable flags, and
        the poll interval, since stock RutOS ships 86400 and one probe per day
        is not sync. Both flags because the demo pair ran with `system.ntp`
        disabled on UTC while the client was on local time.
        """
        servers = self.configured_ntp_servers()
        settings = self.ntp_client_settings()
        if not settings:
            return {"item": "NTP client", "expected": server,
                    "actual": f"no {NTP_CLIENT_PACKAGE} package on this device",
                    "ok": None}
        enabled = settings["enabled"]
        sys_enabled = self.ssh_exec(f"uci -q get {UCI_NTP_ENABLED}",
                                    check=False).strip()
        try:
            got_interval = int(settings["interval"])
        except (TypeError, ValueError):
            got_interval = 0
        interval_ok = bool(got_interval) and got_interval <= interval
        return {
            "item": "NTP client",
            "expected": f"{server} (only, both subsystems enabled, "
                        f"polled every {interval}s or sooner)",
            "actual": (f"configured {', '.join(servers) or '(none)'} "
                       f"({NTP_CLIENT_PACKAGE} enabled={enabled or '?'}, "
                       f"system.ntp enabled={sys_enabled or '?'}); "
                       f"interval {settings['interval'] or '(unset)'}"),
            "ok": (servers == [server] and enabled == "1"
                   and sys_enabled == "1" and interval_ok),
        }

    def ntp_daemon_state(self) -> dict:
        """`{pid, started, written}` for the running `ntpclient`, epochs as ints.

        `started` is the mtime of `/proc/<pid>`, which the kernel sets to when
        the process started; `written` is the mtime of the config file. Either
        can come back 0 on a build whose `date` has no `-r`, which callers read
        as "could not tell" rather than as a fault.
        """
        out = self.ssh_exec(
            "pid=$(ps w 2>/dev/null | grep '[n]tpclient' | awk '{print $1}' "
            "| head -n1); "
            'echo "pid=${pid:-}"; '
            'echo "started=$(date -r /proc/${pid:-0} +%s 2>/dev/null)"; '
            'echo "written=$(date -r ' + NTP_CLIENT_CONFIG + ' +%s 2>/dev/null)"',
            check=False)
        fields = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

        def epoch(key: str) -> int:
            try:
                return int(fields.get(key, "").strip())
            except (TypeError, ValueError):
                return 0

        return {"pid": fields.get("pid", "").strip(),
                "started": epoch("started"), "written": epoch("written")}

    def ntp_daemon_check(self) -> dict:
        """One row: the RUNNING client has actually read the config we committed.

        `ntp_client_check` proves what is in the config FILE. It cannot prove
        the daemon ever read it — RutOS's ntpclient loads that file once at
        startup, and `set_ntp_client` restarts it best-effort (`check=False`),
        so a restart that quietly failed leaves a device with a correct config,
        a green config row, and a daemon still polling whatever it read at boot.
        The same shape as the timezone bug: a committed option the running
        daemon never picked up, passing verification.

        `running_ntp_servers()` settles this for `ntpd` by reading the servers
        off its command line, but there is nothing to read here: RutOS starts
        this one as `ntpclient -s -l` and leaves the servers in the file. So the
        question becomes whether the process is OLDER than the file it reads,
        which answers it without needing the servers at all.
        """
        expected = "running, started after the last config write"
        state = self.ntp_daemon_state()
        pid = state["pid"]
        if not pid:
            return {"item": "NTP daemon", "expected": expected,
                    "actual": f"no {NTP_CLIENT_PACKAGE} process — nothing is "
                              f"polling, whatever the config says",
                    "ok": False}
        if not state["started"] or not state["written"]:
            return {"item": "NTP daemon", "expected": expected,
                    "actual": f"running (pid {pid}); device would not report "
                              f"the start/config timestamps",
                    "ok": None}
        stale = state["written"] - state["started"]
        if stale > 0:
            return {"item": "NTP daemon", "expected": expected,
                    "actual": f"running (pid {pid}) but started {stale}s BEFORE "
                              f"{NTP_CLIENT_CONFIG} was last written — it is "
                              f"still polling what it read at startup",
                    "ok": False}
        return {"item": "NTP daemon", "expected": expected,
                "actual": f"running (pid {pid}), started {-stale}s after the "
                          f"last write to {NTP_CLIENT_CONFIG}",
                "ok": True}

    def set_timezone(self, zonename: str) -> None:
        posix = POSIX_TZ.get(zonename)
        if not posix:
            raise SystemExit(f"No POSIX TZ mapping for '{zonename}'; add it to POSIX_TZ.")
        log.info("Setting timezone to %s ...", zonename)
        # Three keys for three consumers: libc reads the POSIX string, the WebUI
        # dropdown reads the zone name in the `system` section, and the
        # timeserver section keeps its own copy. Setting fewer than all three
        # leaves the device disagreeing with itself.
        self._uci(f"{UCI_TIMEZONE}='{posix}'",
                  f"{UCI_ZONENAME}='{zonename}'",
                  f"{UCI_ZONENAME_NTP}='{zonename}'",
                  package="system")
        # A fourth consumer on RutOS: the ntpclient package keeps its OWN
        # zoneName, and the demo pair in TEC-846 ran with it saying
        # Asia/Jerusalem while system.ntp said UTC. Skipped silently on builds
        # without the package (the TSW202), which have nothing to disagree with.
        types, opts = self._uci_package(NTP_CLIENT_PACKAGE)
        _, settings_sections = self._ntp_client_sections(types, opts)
        if settings_sections:
            self._uci(*[self._uci_arg(f"{NTP_CLIENT_PACKAGE}.{s}.zoneName", zonename)
                        for s in settings_sections],
                      package=NTP_CLIENT_PACKAGE)
        # A committed UCI option is not yet a timezone. libc reads /etc/TZ, and
        # nothing writes it until the system config is reloaded: the first
        # TSW202 off the bench held both options exactly right and ran on a
        # +0000 clock, because this step was missing.
        self.ssh_exec("/etc/init.d/system reload", check=False)
        self.ssh_exec("/etc/init.d/sysntpd restart", check=False)

        want = expected_utc_offset(zonename)
        if want and self.effective_utc_offset() != want:
            # Some builds' `system` init script doesn't own /etc/TZ. Write what
            # libc reads directly — /etc/TZ is usually a symlink to /tmp/TZ, so
            # both are written to cover either layout.
            log.warning("The clock is still not at %s after reloading the "
                        "system config — writing the TZ file directly.", want)
            qposix = shlex.quote(posix)
            self.ssh_exec(f"echo {qposix} > /tmp/TZ", check=False)
            self.ssh_exec(f"echo {qposix} > /etc/TZ", check=False)
        log.info("Timezone set.")

    # --- LAN address (must run LAST) ----------------------------------------
    def lan_ip_check(self, expected_ip: str) -> dict:
        """The `move_lan` verification row, asked without moving anything (TEC-348).

        This is the row that made the ambiguity in TEC-348 worth fixing. During
        a configure run the check has to be "did the device come back on the new
        address after we restarted its network", and a no-answer there is
        genuinely inconclusive — the device may be fine and the *station's* DHCP
        lease stale, which is why `move_lan` logs "it may still be fine".

        On a verify pass there is no such doubt. The unit is already at its
        final address or it is not, and we are talking to it right now, so the
        strongest possible evidence is simply which address that is. Both halves
        are reported: `self.host` is where the SSH/REST session actually reached
        it, and the UCI value is what it will come back on after a reboot — a
        device answering on the right address with the wrong config is a real
        finding (someone set it by hand and never committed).
        """
        configured = self.current_lan_ip()
        reached_ok = self.host == expected_ip
        if not reached_ok:
            actual = (f"reached it on {self.host}, not {expected_ip} "
                      f"(configured {configured or 'unreadable'})")
        elif configured and configured != expected_ip:
            actual = (f"answering on {expected_ip}, but configured as "
                      f"{configured} — it will move on the next reboot")
        else:
            actual = f"answering on {expected_ip}"
        return {"item": "LAN IP", "expected": expected_ip, "actual": actual,
                "ok": reached_ok and (not configured or configured == expected_ip)}

    def current_lan_ip(self) -> str:
        return self.ssh_exec(
            f"uci get network.{self.mgmt_section()}.ipaddr 2>/dev/null",
            check=False).strip()

    def _network_option(self, option: str) -> dict:
        """Every `network.<section>.<option>` the device has, as {section: value}.

        Section names come back exactly as UCI addresses them, so an anonymous
        `config interface` block appears as `@interface[0]` — which is a valid
        UCI path and an invalid bare shell word, hence `_uci_arg` everywhere
        these are used."""
        out = self.ssh_exec("uci show network 2>/dev/null", check=False)
        pattern = re.compile(rf"network\.(@?[\w.\[\]-]+?)\.{option}=(.*)$")
        found = {}
        for line in out.splitlines():
            m = pattern.match(line.strip())
            if m:
                found[m.group(1)] = m.group(2).strip().strip("'\"")
        return found

    def network_addresses(self) -> dict:
        """Every statically-addressed section, as {section: ip}."""
        return self._network_option("ipaddr")

    def network_protos(self) -> dict:
        """Every section's addressing protocol, as {section: proto}.

        The companion to `network_addresses` for the DHCP case (TEC-848): an
        interface taking its address over DHCP has no `ipaddr` in UCI at all, so
        it is invisible to that one and has to be recognised by its protocol.
        """
        return self._network_option("proto")

    def dhcp_sections(self) -> list[str]:
        """The sections set to take their address over DHCP."""
        return [s for s, proto in self.network_protos().items() if proto == "dhcp"]

    # --- WAN address, DHCP pool and the NTP forward (TEC-857) ----------------
    @staticmethod
    def _uci_list(value: str) -> list[str]:
        """The members of a UCI list option, which `uci show` renders `'a' 'b'`."""
        return [token.strip().strip("'\"") for token in value.split() if token.strip()]

    def set_wan_static(self, ipaddr: str, *, netmask: str = "255.255.255.0",
                       gateway: str = "", dns: str = "", section: str = "wan") -> None:
        """Give the WAN interface a fixed address instead of a DHCP lease.

        The point is fleet-constancy, not this one router: with every RUTM08 on
        the same WAN address, the OTD500 upstream of it gets one identical NTP
        line fleet-wide instead of a per-site lookup, and a replacement unit
        needs no site knowledge (TEC-857).

        Applied with `reload` rather than `restart` so only the interface that
        changed is reconfigured and the LAN session this runs over survives.
        Note that on the bench this is the step that ENDS internet access — the
        WAN port stops taking a lease from the uplink — so it belongs after
        anything that needs to be online.
        """
        self._refuse_mutation(f"give {section} the static address {ipaddr}")
        if section not in self.network_protos():
            raise SystemExit(
                f"No 'network.{section}' interface on this device — "
                f"cannot give it the static address {ipaddr}.")
        log.info("Setting %s to static %s (gw %s) ...", section, ipaddr, gateway or "none")
        options = {"proto": "static", "ipaddr": ipaddr, "netmask": netmask}
        if gateway:
            options["gateway"] = gateway
        if dns:
            options["dns"] = dns
        self._uci(*[self._uci_arg(f"network.{section}.{option}", value)
                    for option, value in options.items()],
                  package="network")
        self.ssh_exec("/etc/init.d/network reload", check=False)
        log.info("WAN address set.")

    def wan_static_check(self, ipaddr: str, *, netmask: str = "255.255.255.0",
                         gateway: str = "", section: str = "wan") -> dict:
        """One row for the fixed WAN address.

        Read-back: the interface faces the site's OTD500, which is not on the
        bench, so there is nothing upstream to reach and confirm it against. The
        proto is checked alongside the address because an interface left on
        `dhcp` with a stale `ipaddr` still reads back the right number while
        taking whatever the site hands it.
        """
        _, options = self._uci_package("network")
        got = {option: options.get((section, option), "")
               for option in ("proto", "ipaddr", "netmask", "gateway")}
        want = {"proto": "static", "ipaddr": ipaddr, "netmask": netmask}
        if gateway:
            want["gateway"] = gateway
        wrong = {k: v for k, v in want.items() if got[k] != v}
        return {
            "item": "WAN address",
            "expected": f"{ipaddr}/{netmask} via {gateway or 'no gateway'} (static)",
            "actual": (f"{got['proto'] or '(unset)'} {got['ipaddr'] or '(unset)'}/"
                       f"{got['netmask'] or '(unset)'} via "
                       f"{got['gateway'] or '(none)'}"),
            "ok": not wrong,
        }

    def set_dhcp_pool(self, start: int, limit: int, *, section: str = "lan") -> None:
        """Narrow the LAN DHCP pool so the low addresses are never leased out.

        The RUTM08 downstream holds its WAN address statically and will not
        defend it, so anything this server might hand to another client has to
        stay clear of it (TEC-857).
        """
        self._refuse_mutation(f"narrow the {section} DHCP pool")
        log.info("Setting the %s DHCP pool to start=%s limit=%s ...", section, start, limit)
        self._uci(self._uci_arg(f"dhcp.{section}.start", start),
                  self._uci_arg(f"dhcp.{section}.limit", limit),
                  package="dhcp")
        self.ssh_exec("/etc/init.d/dnsmasq reload", check=False)
        log.info("DHCP pool set.")

    def dhcp_pool_check(self, start: int, limit: int, *, reserved: str = "",
                        section: str = "lan") -> dict:
        """One row for the DHCP pool, asked as "is the reserved address safe?".

        Checking the two numbers read back would be a tautology dressed up as a
        range. The question worth failing on is whether the pool can still hand
        out the address the downstream router is holding statically, so that is
        what the row computes — a pool starting at .1 fails even though both
        options committed exactly as written.
        """
        _, options = self._uci_package("dhcp")
        got_start = options.get((section, "start"), "")
        got_limit = options.get((section, "limit"), "")
        try:
            low = int(got_start)
            high = low + int(got_limit) - 1
        except (TypeError, ValueError):
            return {"item": "DHCP pool",
                    "expected": f"start {start}, {limit} addresses",
                    "actual": f"unreadable (start={got_start or '(unset)'}, "
                              f"limit={got_limit or '(unset)'})",
                    "ok": False}
        actual = f"leases .{low}-.{high}"
        ok = low >= start and high <= start + limit - 1
        if reserved:
            host = int(reserved.rsplit(".", 1)[-1])
            clear = host < low or host > high
            actual += f", {reserved} {'excluded' if clear else 'INSIDE the pool'}"
            ok = ok and clear
        return {"item": "DHCP pool",
                "expected": (f"start {start}, {limit} addresses"
                             + (f", {reserved} excluded" if reserved else "")),
                "actual": actual, "ok": ok}

    def firewall_zone_networks(self, zone: str) -> list[str]:
        """The interfaces a named firewall zone covers.

        A forward is matched by zone, so an interface that is not a member of
        the one the rule names never sees it — the trap TEC-857 lists as a
        prerequisite, after finding a `wan` zone holding only the mobile
        interfaces on the demo unit.
        """
        types, options = self._uci_package("firewall")
        for section in self._uci_sections(types, "zone"):
            if options.get((section, "name"), "") == zone:
                return self._uci_list(options.get((section, "network"), ""))
        return []

    def set_ntp_port_forward(self, *, dest_ip: str, src_ip: str = "",
                             zone: str = "wan", name: str = NTP_FORWARD_NAME) -> None:
        """Forward NTP arriving on the WAN to the site's time server.

        This is what lets an OTD500 sitting upstream of the router reach a
        server on the router's LAN, which it cannot address directly (TEC-846
        confirmed the DNAT end to end). `src_ip` restricts the rule to the one
        device that should be using it.

        Found by name and updated in place, so re-running the tool on a device
        that already has the rule does not stack a second copy.
        """
        self._refuse_mutation(f"forward NTP to {dest_ip}")
        types, options = self._uci_package("firewall")
        existing = [s for s in self._uci_sections(types, "redirect")
                    if options.get((s, "name"), "") == name]
        section = existing[0] if existing else self._uci_add("firewall", "redirect")
        log.info("Forwarding UDP %s on %s to %s:%s (%s) ...",
                 NTP_PORT, zone, dest_ip, NTP_PORT,
                 "updating existing rule" if existing else "new rule")
        rule = {"name": name, "target": "DNAT", "src": zone, "proto": "udp",
                "src_dport": NTP_PORT, "dest_ip": dest_ip, "dest_port": NTP_PORT,
                "enabled": 1}
        if src_ip:
            rule["src_ip"] = src_ip
        self._uci(*[self._uci_arg(f"firewall.{section}.{option}", value)
                    for option, value in rule.items()],
                  package="firewall")
        self.ssh_exec("/etc/init.d/firewall reload", check=False)
        members = self.firewall_zone_networks(zone)
        log.info("NTP forward set. The '%s' zone covers: %s",
                 zone, ", ".join(members) or "(nothing readable)")

    def ntp_forward_check(self, *, dest_ip: str, src_ip: str = "",
                          zone: str = "wan", wan_section: str = "wan",
                          name: str = NTP_FORWARD_NAME) -> dict:
        """One row for the NTP port forward.

        Read-back of the rule, plus the one fact that makes the rule mean
        anything: that the wired WAN interface is actually in the zone the rule
        matches on. TEC-857 lists that as a prerequisite because the demo unit's
        `wan` zone held only the mobile interfaces, and a rule on a zone the
        traffic never enters is a green row over a dead path.

        Zone membership is only allowed to FAIL the row when it could be read.
        An unreadable firewall package says nothing either way, and rounding
        that up to a failure would be as dishonest as rounding it up to a pass.
        """
        types, options = self._uci_package("firewall")
        found = [s for s in self._uci_sections(types, "redirect")
                 if options.get((s, "name"), "") == name]
        if not found:
            return {"item": "NTP forward",
                    "expected": f"UDP {NTP_PORT} on {zone} to {dest_ip}",
                    "actual": f"no '{name}' rule on this device", "ok": False}
        section = found[0]
        want = {"target": "DNAT", "src": zone, "proto": "udp",
                "src_dport": str(NTP_PORT), "dest_ip": dest_ip,
                "dest_port": str(NTP_PORT)}
        if src_ip:
            want["src_ip"] = src_ip
        wrong = {k: v for k, v in want.items()
                 if options.get((section, k), "") != v}
        # RutOS omits `enabled` on a rule that is on; only an explicit 0 is off.
        off = options.get((section, "enabled"), "1") == "0"

        members = self.firewall_zone_networks(zone)
        zone_ok = wan_section in members if members else None
        if zone_ok is None:
            zone_note = f"'{zone}' zone membership unreadable"
        elif zone_ok:
            zone_note = f"'{zone}' zone covers {wan_section}"
        else:
            zone_note = (f"'{zone}' zone does NOT cover {wan_section} "
                         f"(covers {', '.join(members)}) — nothing arriving "
                         f"there matches this rule")
        detail = (f"UDP {options.get((section, 'src_dport'), '?')} on "
                  f"{options.get((section, 'src'), '?')} to "
                  f"{options.get((section, 'dest_ip'), '?')}:"
                  f"{options.get((section, 'dest_port'), '?')}"
                  + (f" from {options.get((section, 'src_ip'), 'anywhere')}"
                     if src_ip else "")
                  + (" [DISABLED]" if off else ""))
        return {"item": "NTP forward",
                "expected": (f"UDP {NTP_PORT} on {zone} to {dest_ip}:{NTP_PORT}"
                             + (f" from {src_ip}" if src_ip else "")),
                "actual": f"{detail}; {zone_note}",
                "ok": not wrong and not off and zone_ok is not False}

    def mgmt_section(self, addresses: Optional[dict] = None) -> str:
        """The `network` section that carries the address we are talking to.

        `lan` on every RutOS router, but NOT on every device in the family: the
        TSW202 answered `uci: Invalid argument` to `uci set network.lan.ipaddr`,
        which is what UCI says when a section does not resolve. Asking the
        device which section actually holds its current address is both the
        correct answer and one that needs no per-model table.

        In order: the section whose address is the one we reached the device on,
        then the conventional `lan`, then the only addressed section if there
        happens to be exactly one. `""` when it is genuinely ambiguous — better
        a caller that reports the candidates than one that moves the wrong
        interface and strands the device.
        """
        addresses = self.network_addresses() if addresses is None else addresses
        for section, ip in addresses.items():
            if ip == self.host:
                return section
        if "lan" in addresses:
            return "lan"
        if len(addresses) == 1:
            return next(iter(addresses))
        return ""

    def move_lan(self, new_ip: str, wait: int = 120, *, netmask: str = "",
                 gateway: str = "", renew_dhcp: bool = True) -> dict:
        """Point the management interface at `new_ip`. We are talking to the
        device OVER that interface, so the network restart drops the connection
        by design: commit synchronously, restart fire-and-forget, then confirm
        by reaching the device on the new address.

        The section written is whichever one currently holds our address (see
        `mgmt_section`) rather than a hard-coded `lan`.

        `netmask`/`gateway` are for devices that need them stated. A router
        (RUTM08) needs neither — its DHCP pool follows the interface subnet, and
        it IS the gateway — so only `ipaddr` changes there. A switch's
        management interface has to be told both.

        `renew_dhcp` renews the HOST's lease so the laptop follows the device
        into the new subnet. Only correct when the device serves DHCP there: on
        macOS the renew is `ipconfig set <iface> DHCP`, which would throw away a
        statically-configured bench adapter. Devices that don't serve DHCP
        (switches) must pass False and rely on the station already having an
        address in the target subnet.

        Returns a verification-style check dict; never raises after the commit
        (past that point the device is moving whether we can see it or not)."""
        # A verify pass wants `lan_ip_check()` — the same row, asked by reaching
        # the device rather than by re-addressing it.
        self._refuse_mutation(f"move the management address to {new_ip}")
        addresses = self.network_addresses()
        section = self.mgmt_section(addresses)
        if not section:
            found = ", ".join(f"network.{s}={ip}" for s, ip in addresses.items())
            candidates = found or ("none — is the address configured outside the "
                                   "`network` package?")
            raise SystemExit(
                f"Cannot tell which network section carries {self.host}, so the "
                "move would risk re-addressing the wrong interface. Addressed "
                f"sections: {candidates}")

        cur = addresses.get(section, "")
        if cur == new_ip:
            log.info("Management IP is already %s; skipping the move.", new_ip)
            return {"item": "LAN IP", "expected": new_ip,
                    "actual": f"{new_ip} (already set)", "ok": True}

        # Resolve the host-side interface while the device is still reachable.
        iface = host_iface_for(self.host)
        log.info("Moving network.%s from %s to %s — the connection will drop ...",
                 section, cur or self.host, new_ip)
        options = {"ipaddr": new_ip}
        if netmask:
            options["netmask"] = netmask
        if gateway:
            options["gateway"] = gateway
        sets = [f"uci set {self._uci_arg(f'network.{section}.{option}', value)}"
                for option, value in options.items()]
        self.ssh_exec(" && ".join(sets + ["uci commit network"]))
        self._fire_and_forget("sleep 1; /etc/init.d/network restart")
        self.close()
        self.host = new_ip
        self.base = f"{self.scheme}://{new_ip}/api"

        port = 443 if self.scheme == "https" else 80
        deadline = time.time() + wait
        time.sleep(5)
        if renew_dhcp:
            renew_host_dhcp(iface)
        renewed_again = False
        while time.time() < deadline:
            if self._port_open(port):
                log.info("Device is answering on %s.", new_ip)
                return {"item": "LAN IP", "expected": new_ip,
                        "actual": f"answering on {new_ip}", "ok": True}
            if renew_dhcp and not renewed_again and time.time() > deadline - wait / 2:
                renew_host_dhcp(iface)
                renewed_again = True
            time.sleep(3)
        hint = ("laptop lease may be stale" if renew_dhcp else
                f"is this station on the {new_ip.rsplit('.', 1)[0]}.x subnet?")
        log.warning("Device did not answer on %s within %ds — it may still be fine; "
                    "%s", new_ip, wait, hint)
        return {"item": "LAN IP", "expected": new_ip,
                "actual": f"no answer on {new_ip} after {wait}s ({hint})", "ok": False}

    def move_lan_dhcp(self, *, mac: str, subnets: list[str],
                      wait: int = 300) -> dict:
        """Leave the management interface taking its address over DHCP — the
        alternative last step to `move_lan`, for a site whose own DHCP server
        owns the address (TEC-848).

        The static options are DELETED, not just overridden by `proto`. A
        leftover `ipaddr` is what a device falls back to when no lease arrives,
        so leaving one behind would make "on DHCP" quietly mean "on DHCP, or
        else back on the bench address" — two different devices to ship.

        Nothing here picks an address, so the device is found again by MAC.
        That is also why `renew_dhcp` has no counterpart here: the station's own
        lease is irrelevant when we no longer know which subnet to expect the
        device on, and the sweep covers `subnets` instead.

        Returns a verification-style check dict; never raises after the commit
        (past that point the device is moving whether we can see it or not).
        """
        self._refuse_mutation("leave the management interface on DHCP")
        row = {"item": "LAN IP", "expected": "a lease from the site's DHCP server"}
        section = self.mgmt_section()
        if not section:
            # No section carries an address. Either the device is already on
            # DHCP — in which case there is nothing to do and saying so beats
            # failing — or its addressing lives somewhere this cannot see.
            on_dhcp = self.dhcp_sections()
            if len(on_dhcp) == 1:
                log.info("network.%s is already on DHCP; skipping the move.",
                         on_dhcp[0])
                return {**row, "actual": "already configured for DHCP", "ok": True}
            raise SystemExit(
                f"Cannot tell which network section carries {self.host}, so "
                "switching it to DHCP would risk re-addressing the wrong "
                "interface.")

        log.info("Leaving network.%s on DHCP — the address may change from %s, "
                 "so the device will be found again by its MAC.",
                 section, self.host)
        base = f"network.{section}"
        # `|| exit 1` rather than chaining with `&&`: the deletes are `-q` and
        # report failure for an option that was never there, so `&&` would stop
        # on the first one a given device happens not to have. The guard still
        # has to be there, because committing the deletes without the `proto`
        # would leave the device with no addressing at all.
        drops = "; ".join(f"uci -q delete {shlex.quote(f'{base}.{opt}')}"
                          for opt in ("ipaddr", "netmask", "gateway"))
        self.ssh_exec(f"uci set {self._uci_arg(f'{base}.proto', 'dhcp')} || exit 1; "
                      f"{drops}; uci commit network")
        self._fire_and_forget("sleep 1; /etc/init.d/network restart")
        self.close()

        if not mac:
            return {**row, "actual": "the device's MAC is unknown, so it could "
                                     "not be found again after the change",
                    "ok": False}

        port = 443 if self.scheme == "https" else 80
        deadline = time.time() + wait
        time.sleep(5)
        while time.time() < deadline:
            found = find_ip_by_mac(mac, subnets, port=port)
            if found:
                log.info("Device is answering on %s.", found)
                self.host = found
                self.base = f"{self.scheme}://{found}/api"
                return {**row, "actual": f"answering on {found}", "ok": True}
            time.sleep(5)
        log.warning("Device was not found by MAC %s on %s within %ds — it may "
                    "still be fine; is this station on the subnet it leased "
                    "from?", mac, ", ".join(subnets), wait)
        return {**row,
                "actual": f"not found by MAC on {', '.join(subnets)} after "
                          f"{wait}s (is this station on that subnet?)",
                "ok": False}

    def lan_dhcp_check(self) -> dict:
        """The `move_lan_dhcp` verification row, asked without changing anything
        (TEC-348) — the DHCP twin of `lan_ip_check`.

        There is no address to hold the device to, so the answerable question is
        whether it is configured to ask for one. The lease it currently holds is
        reported alongside, because that is where a reader will actually find
        the device — but it is a finding, not the expectation.
        """
        row = {"item": "LAN IP", "expected": "configured for DHCP"}
        leftover = self.current_lan_ip()
        if not self.dhcp_sections():
            found = (f"static {leftover} — this unit was not left on DHCP"
                     if leftover else "no interface is set to take a lease")
            return {**row, "actual": found, "ok": False}
        if leftover:
            return {**row, "actual": f"on DHCP, but a static {leftover} is "
                                     "still configured — it will fall back to "
                                     "that if no lease arrives", "ok": False}
        return {**row, "actual": f"on DHCP, currently reached at {self.host}",
                "ok": True}

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

    def attached_access_tech(self) -> tuple[Optional[int], str]:
        """`(AcT code, raw reply)` — the radio technology the modem is CURRENTLY
        attached on, or `(None, raw)` when it is not attached (or the reply can't
        be parsed).

        `AT+COPS?` answers `+COPS: <mode>,<format>,<operator>,<AcT>` while
        registered and drops the last two fields when it is not, so a missing
        `AcT` genuinely means "not on a network" rather than "no idea". That
        distinction is what lets the 4G row degrade honestly instead of guessing.

        NOTE: parsed from an unsolicited-response format that varies a little
        between modem firmwares. An unrecognised reply returns None, which the
        caller reports as "cannot confirm" — never as a pass.
        """
        raw = self.ssh_exec("gsmctl -A 'AT+COPS?' 2>/dev/null", check=False).strip()
        # <operator> may be a quoted string ("Partner") or a number (LAC format
        # 2), so match either without letting it swallow the field separator.
        m = re.search(r"\+COPS:\s*\d+\s*,\s*\d+\s*,\s*(?:\"[^\"]*\"|[^,\s]+)\s*,\s*(\d+)",
                      raw)
        return (int(m.group(1)) if m else None), raw

    def sim_4g_check(self) -> dict:
        """One `{item, expected, actual, ok}` row for "the SIMs are 4G-only".

        Two halves, for the same reason the timezone and hostname rows have two.
        The INTENT is `simcard.@sim[N].service='lte'`, which is what
        `set_sims_4g_only` writes — reading it back cannot fail, so on its own it
        was a tautology. The EFFECT is the technology the modem actually attached
        on: a device that says `lte` in its config and is sitting on 3G is not
        4G-only, and the config alone will never say so.

        Degrades rather than guessing. A modem with no SIM, or one that has not
        registered yet (very common on a bench with no outdoor antenna), yields
        `ok=None` and "cannot confirm" — never a pass. A bench sweep that turned
        "I couldn't check" into green would be the tautology all over again.
        """
        idxs = self._sim_indices()
        vals = [self.ssh_exec(f"uci get simcard.@sim[{i}].service 2>/dev/null",
                              check=False).strip() for i in idxs]
        configured = ",".join(vals) or "(none)"
        intent_ok = bool(idxs) and all(v == SIM_SERVICE_4G for v in vals)

        act, raw = self.attached_access_tech()
        tech = ACCESS_TECH.get(act) if act is not None else None
        expected = f"lte x{len(idxs)}, attached on 4G"

        if not intent_ok:
            # The config is wrong, which is decidable without the modem.
            attached = f"attached on {tech}" if tech else "not attached"
            return {"item": "SIM 4G-only", "expected": expected,
                    "actual": f"config says {configured}; {attached}", "ok": False}
        if act in FOURG_ACCESS_TECH:
            return {"item": "SIM 4G-only", "expected": expected,
                    "actual": f"config says {configured}; attached on {tech}",
                    "ok": True}
        if tech:
            return {"item": "SIM 4G-only", "expected": expected,
                    "actual": f"config says {configured}; attached on {tech} — "
                              "the modem is not on 4G", "ok": False}
        return {"item": "SIM 4G-only", "expected": expected,
                "actual": f"config says {configured}; not attached — cannot "
                          f"confirm what it uses ({raw[:60] or 'no reply'})",
                "ok": None}

    def _sim_indices(self) -> list[int]:
        """SIM section indices, e.g. [0,1,2] from simcard.@sim[0]=sim ..."""
        out = self.ssh_exec(
            "uci show simcard 2>/dev/null | "
            "sed -n 's/^simcard\\.@sim\\[\\([0-9]*\\)\\]=sim$/\\1/p'", check=False)
        return [int(x) for x in out.split() if x.strip().isdigit()]

    # --- SIM switch + per-operator data limits (TEC-359) --------------------
    def _sim_switch_state(self) -> list[tuple[str, dict[str, str]]]:
        """[(section id, {option: value})] for every `sim` section in the
        sim_switch package, in file order — parsed from ONE `uci show`, so
        configure/verify don't pay an SSH round trip per option read."""
        out = self.ssh_exec(f"uci show {SIM_SWITCH_PACKAGE} 2>/dev/null", check=False)
        state: list[tuple[str, dict[str, str]]] = []
        by_id: dict[str, dict[str, str]] = {}
        for line in out.splitlines():
            m = re.match(rf"^{SIM_SWITCH_PACKAGE}\.([^.=]+)(?:\.([^.=]+))?=(.*)$",
                         line.strip())
            if not m:
                continue
            section_id, option, value = m.group(1), m.group(2), m.group(3).strip()
            if option is None:
                if value == "sim":
                    by_id[section_id] = {}
                    state.append((section_id, by_id[section_id]))
            elif section_id in by_id:
                by_id[section_id][option] = value.strip("'\"")
        return state

    @staticmethod
    def _sim_switch_slots(state: list[tuple[str, dict[str, str]]]
                          ) -> tuple[dict[int, str], list[str]]:
        """({slot number: section id}, [orphan section ids]).

        Sections are keyed by their `position` option, NOT by file order —
        position is what ties a rule to a physical slot. A section without one
        (a leftover from an interrupted run) falls back to its ordinal.
        Whatever remains unclaimed — a duplicate position, a position beyond
        the slots this device has — is an orphan: configure_sim_switch()
        disables those, because a leftover section that stays enabled would
        keep failing over with stale rules."""
        sections: dict[int, str] = {}
        orphans: list[str] = []
        for ordinal, (section_id, options) in enumerate(state, start=1):
            position = options.get("position", "")
            slot = int(position) if position.isdigit() else ordinal
            if sections.setdefault(slot, section_id) != section_id:
                orphans.append(section_id)
        managed = set(SIM_SLOTS) | {ESIM_SLOT}
        orphans += [sid for slot, sid in sections.items() if slot not in managed]
        return {slot: sid for slot, sid in sections.items() if slot in managed}, orphans

    def _modem_id(self, state: list[tuple[str, dict[str, str]]]) -> str:
        """The modem id sim_switch sections belong to ('2-1' on the OTD500).

        Read from the device's CONFIG only (existing sim_switch sections first,
        then `simcard`) — never from the modem — so it works with no SIM in."""
        for _, options in state:
            if options.get("modem"):
                return options["modem"]
        from_simcard = self.ssh_exec("uci -q get simcard.@sim[0].modem", check=False).strip()
        return from_simcard or DEFAULT_MODEM_ID

    def _warn_if_sim_switch_fw_unverified(self) -> None:
        """The sim_switch option names are undocumented and were verified on one
        firmware. Say so loudly on any other one — but don't fail: a warning that
        the map may have drifted is useful, refusing to provision is not."""
        fw = self.ssh_exec("cat /etc/version 2>/dev/null", check=False).strip()
        if fw_carries_version(fw, VERIFIED_SIM_SWITCH_FW):
            return
        log.warning("sim_switch UCI names verified on %s — this device reports %s. "
                    "Re-verify with `uci export sim_switch` on this firmware.",
                    VERIFIED_SIM_SWITCH_FW, fw or "no version")

    def configure_sim_switch(self, cfg: dict) -> None:
        """Provision the RutOS `sim_switch` service: sticky, symmetric failover
        between SIM slot 1 and slot 2 (TEC-359).

        We only write the RULES — the device's own sim_switch service does the
        switching. Pure UCI plus a service restart, so it needs NO SIM inserted
        (the normal bench state), never reads modem state, and never touches
        `simcard.@sim[].primary`. Restarting sim_switch does not bounce the modem
        or drop back to the primary SIM (verified on 07.22.3)."""
        self._warn_if_sim_switch_fw_unverified()
        state = self._sim_switch_state()
        sections, orphans = self._sim_switch_slots(state)
        modem = self._modem_id(state)
        rules = dict(sim_switch_options(SIM_SLOTS[0], cfg=cfg))
        log.info("Configuring SIM switch on modem %s (check every %ss x%s, weak signal "
                 "below %s dBm, ICMP %s, sticky) ...", modem, rules["interval"],
                 rules["retry_count"], rules["weak_signal"], rules["data_fail_host"])
        sets: list[str] = []
        for slot in SIM_SLOTS + (ESIM_SLOT,):
            section_id = sections.get(slot) or self._uci_add(SIM_SWITCH_PACKAGE, "sim")
            for option, value in sim_switch_options(slot, cfg=cfg, modem=modem,
                                                    enabled=slot in SIM_SLOTS):
                sets.append(self._uci_arg(
                    f"{SIM_SWITCH_PACKAGE}.{section_id}.{option}", value))
        if orphans:
            log.warning("sim_switch has %d leftover section(s) (%s) — disabling them; "
                        "an enabled duplicate would keep failing over with stale rules.",
                        len(orphans), ", ".join(orphans))
            sets += [self._uci_arg(f"{SIM_SWITCH_PACKAGE}.{sid}.enabled", "0")
                     for sid in orphans]
        self._uci(*sets, package=SIM_SWITCH_PACKAGE)
        self.ssh_exec(f"/etc/init.d/{SIM_SWITCH_PACKAGE} restart")
        log.info("SIM switch configured on slot(s) %s; slot %d (eSIM) left disabled.",
                 ", ".join(str(s) for s in SIM_SLOTS), ESIM_SLOT)

    def _keep_across_upgrade(self) -> None:
        """List the quota-sync files in /etc/sysupgrade.conf, so a keep-settings
        firmware upgrade restores them instead of leaving the (preserved) cron
        entry calling a script that is no longer there. See QUOTA_SYNC_KEEP.

        Appends to whatever the operator already has in there, dropping only our
        own previous block, so re-running is a no-op rather than four more
        lines. Both comment lines name QUOTA_SYNC_NAME for that reason: the
        cleanup sed matches on it, and a line without it would survive and
        accumulate one copy per provisioning run."""
        block = "\n".join((
            f"# {QUOTA_SYNC_NAME} (TEC-359): keep the operator->quota script "
            "and its boot hook.",
            f"# This file is listed too, so {QUOTA_SYNC_NAME} survives the NEXT "
            "upgrade as well.",
            *QUOTA_SYNC_KEEP))
        self._ssh_report(
            f"touch {SYSUPGRADE_CONF} && "
            f"sed -i -e '\\,{QUOTA_SYNC_NAME},d' "
            f"-e '\\,^{SYSUPGRADE_CONF}$,d' {SYSUPGRADE_CONF} && "
            f"cat >> {SYSUPGRADE_CONF} <<'{self.PUT_FILE_EOF}'\n"
            f"{block}\n{self.PUT_FILE_EOF}",
            f"Could not add the quota-sync files to {SYSUPGRADE_CONF}")

    def install_quota_sync(self, cfg: dict) -> None:
        """Deploy the on-device operator→quota script (TEC-359 Phase 1b).

        Which data limit a slot needs depends on which operator's SIM is in it,
        and the SIMs are inserted in the field — so the decision can't be made
        here. The device re-derives it from each slot's ICCID at boot and every
        10 minutes instead. Run once at the end, so a device leaves the bench
        with its limits already written rather than up to 10 minutes later."""
        operators = quota_operators(cfg)
        log.info("Installing %s (%d operator(s): %s) ...", QUOTA_SYNC_PATH, len(operators),
                 ", ".join(f"{o['name']} {o['iccid_prefixes'][0]}*" for o in operators)
                 or "none — every SIM gets the fallback limit")
        self._put_file(QUOTA_SYNC_PATH, render_quota_sync_script(cfg), mode="755")
        self._put_file(QUOTA_SYNC_INIT_PATH, QUOTA_SYNC_INIT, mode="755")
        self.ssh_exec(f"{QUOTA_SYNC_INIT_PATH} enable")
        self._keep_across_upgrade()
        # Rewrite rather than append-if-missing, so a changed schedule replaces
        # the old entry instead of running alongside it.
        self.ssh_exec(
            f"mkdir -p $(dirname {CRONTAB_PATH}) && touch {CRONTAB_PATH} && "
            f"sed -i '/{QUOTA_SYNC_NAME}/d' {CRONTAB_PATH} && "
            f"echo {shlex.quote(QUOTA_SYNC_CRON)} >> {CRONTAB_PATH} && "
            "{ /etc/init.d/cron enable >/dev/null 2>&1; /etc/init.d/cron restart; }")
        self.ssh_exec(QUOTA_SYNC_PATH)
        log.info("Quota sync installed: runs at boot and every 10 minutes "
                 "(limits follow the SIM's operator, not the slot).")

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

    # How far the device clock may sit from the bench host's before it counts as
    # unset. An hour is far outside anything a polling client drifts to and far
    # inside the months-long error an unsynced RutOS device shows, so this never
    # touches a device that is genuinely keeping time.
    CLOCK_SKEW_TOLERANCE = 3600

    def device_clock_skew(self) -> Optional[int]:
        """Device clock minus this machine's, in seconds — None if the device
        will not report it. The bench host is the reference because it is the one
        machine in the room that is definitely synced."""
        out = self.ssh_exec("date -u +%s", check=False).strip()
        return int(out) - int(time.time()) if out.isdigit() else None

    def ensure_clock_sane(self) -> None:
        """Step the device clock to this machine's when it is grossly wrong.

        A RutOS unit with no battery-backed clock does not boot "unset": OpenWrt
        seeds the clock from the newest mtime under /etc, which in practice is
        the firmware image's build date. So a unit that reboots mid-run — the
        FOTA step does exactly that — comes back MONTHS in the past, and every
        HTTPS fetch after it fails certificate validation, because the server's
        certificate was issued after the date the device thinks it is. `opkg
        update` reports that as `wget returned 5`, and `opkg install` then reports
        the missing index as `Unknown package`.

        Nothing on the bench corrects it. The configured time server is on the
        assembly network and unreachable from here by construction, and an
        Ethernet-only RUTM08 has no cellular modem clock to fall back on the way
        an OTD500 does (docs/verification-rows.md).

        Deliberately `date` and not UCI: the NTP configuration this run writes is
        the SHIPPING state, and editing it to work around a bench condition is
        how a unit leaves with a time source nobody chose. This also only ever
        steps the clock towards real time, so it cannot strand a unit running
        ahead of the server it will later sync against — the one state this
        build's ntpclient refuses to correct."""
        skew = self.device_clock_skew()
        if skew is None or abs(skew) <= self.CLOCK_SKEW_TOLERANCE:
            return
        self._refuse_mutation("set the device clock")
        was = self.ssh_exec("date -u", check=False).strip()
        log.warning("Device clock is %+d days out (reads '%s') — HTTPS fetches "
                    "cannot validate certificates until it is corrected.",
                    round(skew / 86400), was or "?")
        now = int(time.time())
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now))
        # BusyBox `date -s` accepts several syntaxes and builds differ on which,
        # so try them and read the clock back after each rather than trusting an
        # exit status. Unambiguous form first.
        for arg in (f"@{now}", stamp, time.strftime("%Y%m%d%H%M.%S", time.gmtime(now))):
            self.ssh_exec(f"date -u -s {shlex.quote(arg)} >/dev/null 2>&1", check=False)
            if (skew := self.device_clock_skew()) is not None \
                    and abs(skew) <= self.CLOCK_SKEW_TOLERANCE:
                log.info("Device clock stepped to %s UTC.", stamp)
                return
        # Only now is the bench's time situation worth explaining: it is the
        # difference between "retry" and "stop and fetch an engineer".
        stuck = [s for s in (self.ntp_client_settings().get("servers") or [])
                 if self._time_server_unreachable(s)]
        raise SystemExit(
            f"The device clock reads '{was or 'unknown'}', which is "
            f"{round(abs(skew or 0) / 86400)} days from this machine's time, and it "
            "could not be set (no `date -s` syntax this build accepts). Every HTTPS "
            "fetch on the device — the opkg feed included — fails certificate "
            "validation until that is fixed."
            + (" It will not correct itself either: the configured time server(s) "
               f"{', '.join(stuck)} do not answer from this bench." if stuck else ""))

    def _opkg_feeds(self) -> list[str]:
        """The feed URLs configured on the device, in order.

        Read off the device rather than constructed, because RutOS feed paths are
        an opaque per-build hash — a real one is
        `https://opkg.teltonika-networks.com/e6d1d303.../Packages.gz`. There is
        nothing in the URL to derive from the model and firmware version, so the
        device's own `distfeeds.conf` is the only source for it."""
        conf = self.ssh_exec("cat /etc/opkg/distfeeds.conf 2>/dev/null", check=False)
        return re.findall(r"https?://\S+", conf)

    def _time_server_unreachable(self, server: str) -> bool:
        """True when the device cannot reach `server` at the IP layer at all.

        Deliberately a weak probe: NTP is UDP, and this RutOS build's `ntpclient`
        ignores a positional hostname (docs/verification-rows.md), so there is no
        way to ask "did THIS server answer". Unreachable at the IP layer is still
        conclusive in the only direction being claimed — a host that does not
        answer at all is not serving time."""
        return self.ssh_exec(
            f"ping -c1 -W2 {shlex.quote(server)} >/dev/null 2>&1 && echo OK || echo FAIL",
            check=False).strip().endswith("FAIL")

    def _opkg_index_blocker(self, update_output: str = "") -> str:
        """Why the feed index could not be fetched, in one line — or "".

        `wait_for_internet` is satisfied by an ICMP ping to a literal address,
        which proves routing and nothing else. Fetching the index is an HTTPS GET
        of a NAMED host, so it additionally needs a resolver and a trusted
        certificate chain. Both read as online to the ping probe, which is why
        neither is guessable from the run log.

        The clock — the usual reason that chain is rejected — is not checked here
        because `ensure_clock_sane` has already run and either fixed it or failed
        the step with its own message. A certificate still being rejected AFTER
        that points at the device's CA bundle instead, which is why the note below
        does not name the clock."""
        notes = []
        # opkg reports the fetch failure as its downloader's exit status, and 5
        # is specifically 'certificate verification failed'. Worth reading out of
        # the output rather than re-probing for, because it is conclusive: the
        # transport worked and only the trust check failed.
        if re.search(r"wget returned 5\b", update_output):
            notes.append("the device rejected the feed's HTTPS certificate "
                         "(wget returned 5) even with its clock correct — check "
                         "the ca-bundle / ca-certificates packages")
        feeds = self._opkg_feeds()
        host = re.sub(r"^https?://([^/]+).*$", r"\1", feeds[0]) if feeds else ""
        if host and self.ssh_exec(
                f"nslookup {shlex.quote(host)} >/dev/null 2>&1 && echo OK || echo FAIL",
                check=False).strip().endswith("FAIL"):
            notes.append(f"the device cannot resolve the feed host {host}")
        return "; ".join(notes)

    def ensure_tailscale_installed(self) -> None:
        """Tailscale is an add-on package, absent on a fresh OTD500. Install it via
        opkg (needs internet) and VERIFY the binary + RutOS service wrapper are
        actually present — raising with the opkg output if not.

        Each of the three ways this fails gets its own message, because they need
        different actions and opkg's own output conflates them. `opkg update` used
        to run with its result discarded, so a feed that never downloaded surfaced
        as opkg's "Unknown package 'tailscale'" from the install below — which
        reads as "this package does not exist" and sends the operator looking at
        the wrong thing entirely."""
        if "__HAVE__" in self.ssh_exec(self.HAVE_TS, check=False):
            log.info("Tailscale already installed.")
            return
        log.info("Installing the Tailscale package (opkg) ...")
        # The feed is HTTPS, so the index download fails outright on a device
        # whose clock is months out — which is the state a unit boots into after
        # the FOTA reboot. The pipelines already call this right after the
        # firmware step; repeating it costs one `date` read and keeps the step
        # correct on its own, including after a reboot in between.
        self.ensure_clock_sane()
        # opkg pulls over the uplink — allow well beyond the default bound.
        upd = self.ssh_exec("opkg update 2>&1; echo __rc=$?", check=False, exec_timeout=180)
        if "__rc=0" not in upd:
            why = self._opkg_index_blocker(upd)
            raise SystemExit(
                "Could not download the opkg package index, so Tailscale cannot be "
                "installed" + (f" — {why}" if why else "") + ".\nopkg update said:\n"
                + upd[-500:])
        # With a good index in hand, a `tailscale` that still isn't listed means
        # THIS firmware's Teltonika feed does not carry the package. That is a
        # different fault from a download that broke halfway, and no retry fixes it.
        if not self.ssh_exec("opkg list tailscale 2>/dev/null", check=False).strip():
            fw = self.ssh_exec("cat /etc/version 2>/dev/null", check=False).strip()
            feeds = self._opkg_feeds()
            raise SystemExit(
                "The package index downloaded and does not list 'tailscale', so the "
                f"feed for this device ({fw or 'no version'}) does not carry the "
                "package."
                + (f"\nFeed searched: {feeds[0]}" if feeds else "")
                + "\nThe feed is chosen per build, so check that the firmware step "
                "actually ran. Do NOT use opkg's suggested '--force_feeds': it "
                "installs unsupported OpenWrt builds on a fleet device.")
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
    # The sim_switch options worth reading back: the ones that decide WHETHER a
    # slot fails over and on what, rather than every option we write.
    SIM_SWITCH_VERIFIED_OPTIONS = ("enabled", "interval", "retry_count",
                                   "weak_signal", "enable_back", "data_fail_host")

    def _verify_sim_switch(self, cfg: dict) -> list[dict]:
        """Verification rows for the sim_switch rules and the quota-sync script.
        Every row is `ok=None` (skipped) when sim_switch isn't enabled in the
        config, so the table shows the feature as out of scope, not as passing."""
        rows: list[dict] = []

        def add(item, expected, actual, ok):
            rows.append({"item": item, "expected": expected, "actual": actual, "ok": ok})

        if not (cfg or {}).get("enabled"):
            for slot in SIM_SLOTS:
                add(f"SIM switch slot {slot}", "(skipped)", "-", None)
            add("quota sync script", "(skipped)", "-", None)
            add("quota sync cron", "(skipped)", "-", None)
            add("quota sync survives upgrade", "(skipped)", "-", None)
            return rows

        state = self._sim_switch_state()
        sections, _ = self._sim_switch_slots(state)
        options_by_id = dict(state)
        for slot in SIM_SLOTS:
            wanted = dict(sim_switch_options(slot, cfg=cfg))
            expected = " ".join(f"{o}={wanted[o]}" for o in self.SIM_SWITCH_VERIFIED_OPTIONS)
            section_id = sections.get(slot)
            if not section_id:
                add(f"SIM switch slot {slot}", expected, "(no sim_switch section)", False)
                continue
            device = options_by_id[section_id]
            actual = " ".join(f"{o}={device.get(o) or '-'}"
                              for o in self.SIM_SWITCH_VERIFIED_OPTIONS)
            add(f"SIM switch slot {slot}", expected, actual, actual == expected)

        installed = self.ssh_exec(
            f"[ -x {QUOTA_SYNC_PATH} ] && echo script; "
            f"[ -x {QUOTA_SYNC_INIT_PATH} ] && echo boot-hook", check=False).split()
        add("quota sync script", "script + boot-hook",
            " + ".join(installed) or "(not installed)",
            "script" in installed and "boot-hook" in installed)

        cron = self.ssh_exec(f"grep -F {QUOTA_SYNC_NAME} {CRONTAB_PATH} 2>/dev/null",
                             check=False).strip()
        add("quota sync cron", QUOTA_SYNC_CRON, cron or "(no cron entry)",
            cron == QUOTA_SYNC_CRON)

        # The cron entry survives a keep-settings upgrade on its own; the script
        # only does if it is listed here.
        kept = set(self.ssh_exec(f"grep -v '^#' {SYSUPGRADE_CONF} 2>/dev/null",
                                 check=False).split())
        missing = [p for p in QUOTA_SYNC_KEEP if p not in kept]
        add("quota sync survives upgrade", f"{len(QUOTA_SYNC_KEEP)} paths kept",
            "all listed" if not missing else f"missing {', '.join(missing)}",
            not missing)
        return rows

    def verify_configuration(self, *, hostname: str, zonename: str, new_password: str,
                             sim_4g: bool, rms: bool, tailscale: bool,
                             esim: bool = False, expected_firmware: str = "",
                             rms_api_token: str = "", serial: str = "",
                             sim_switch: Optional[dict] = None) -> list[dict]:
        """Re-read every setting back off the device and confirm it actually took.
        Returns a list of checks: {item, expected, actual, ok} where ok is True
        (passed), False (failed) or None (not in scope / skipped)."""
        checks: list[dict] = []

        def add(item, expected, actual, ok):
            checks.append({"item": item, "expected": expected, "actual": actual, "ok": ok})

        # Password: we are still authenticated, and SSH answers, on new_password.
        # Neither side of this check may carry an actual password. `expected`
        # would pin the station's shared password into every run record, and
        # `actual` is worse: when the change did NOT take, the password still in
        # use is the device's per-device label password (TEC-349 now reads that
        # off the sticker, so the bench is trusted with it), and these checks are
        # shipped to bench-central verbatim. The outcome is all a reader needs.
        on_shared = self.password == new_password
        add("admin/root password", "the shared password",
            "in use" if on_shared else "NOT set — the device is still on another "
                                       "password",
            on_shared)

        checks.append(self.hostname_check(hostname))
        checks.append(self.timezone_check(zonename))

        if sim_4g:
            checks.append(self.sim_4g_check())
        else:
            add("SIM 4G-only", "(skipped)", "-", None)

        # `sim_switch` is the whole config block: {} / disabled shows as skipped,
        # None leaves the rows out entirely (a tool that has no SIM switch).
        if sim_switch is not None:
            checks += self._verify_sim_switch(sim_switch)

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
