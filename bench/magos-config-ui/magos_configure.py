#!/usr/bin/env python3
"""
Configure a Magos AR-300 radar (dashboard API) over HTTP.

Discovered API (base = http://<host>/dshb/v1):
  POST /login        {"username","password"}            -> sets a session cookie
  GET  /system       -> {ntpAutomatic, ntpServer, timezone, swComponents, users}
  POST /system       {ntpAutomatic, ntpServer, timezone} -> updates NTP
  GET  /networking   -> IP / gateway / DNS config (schema varies by firmware, below)
  POST /networking   {<full object with edits>}          -> updates IP / gateway / DNS

Networking schema differs across firmware and set_network handles both:
  * legacy (flat):   {"ip4Method","ip4Address":"x.x.x.x/NN","ip4Gateway","ip4DNS":[...],...}
  * firmware >= 3.x: {"netInterfaces":{"port1":{"ip4Method","ip4Address":"x.x.x.x",
                       "ip4Netmask":"255.255.255.0","ip4Gateway","ip4DNS":[...],...}}, ...}
                     (per-interface, PLAIN address + separate netmask, not CIDR)
The POST rejects read-only fields it returned on GET (certificates, mass ports);
set_network drops exactly those on a 400 and retries.

Firmware >= 3.x adds an RF "Channel" (so neighbouring radars can use different
frequencies). It lives under a SEPARATE API base and is NOT set over REST:
  GET  /radar/v1/listVariants -> {"variantList":[{"id":"chan0","description":"Channel 0"},...]}
  ws(s)://<host>/radar/v1/detections  (the dashboard pushes the channel here)
     on connect the radar pushes, among alerts/op_state/heartbeat frames:
     recv {"op":"radar_settings","payload":{"tx_enabled":true,...,"variant":"chanN"}}
       -- the only read of the current channel there is (MagosClient.current_variant)
     send {"op":"set_params","payload":{"variant":"chanN"},"id":<n>}
     recv {"op":"ack","inResponseTo":<n>}   (or {"op":"error",...} on failure)
See MagosClient.set_channel for the exact handshake.

Auth is cookie/session based (login response sets a `session` cookie). We use a
requests.Session so the cookie jar is reused automatically for later calls (and
the same cookie is handed to the channel WebSocket).

Strategy: GET current config, change ONLY the requested fields, POST it back.
That preserves every other setting on the device.

NOTE: changing the IP address drops the connection you're talking over, so the
final POST /networking will usually not return a clean response — that's expected.
After it, reach the radar on its NEW address.

A fresh AR-300 ships on 192.168.40.50 with admin:password, so all of those are
the script defaults (operator-editable via the "ar300" section of
config/magos.config.json; missing keys fall back to the built-in values) — out
of the box you can just run:

  python3 magos_configure.py --interactive

Usage (explicit):
  python3 magos_configure.py \
      --host 192.168.40.50 \
      --username admin \
      --ntp 192.168.88.10 \
      --ip 192.168.88.50 --netmask 255.255.255.0 \
      --gateway 192.168.88.1 \
      --dns 192.168.88.1

Interactive mode (--interactive) asks which channel the radar is (0-3) and maps
it to a static IP, or lets you enter one manually:
  channel 0 -> 192.168.88.50
  channel 1 -> 192.168.88.51
  channel 2 -> 192.168.88.52
  channel 3 -> 192.168.88.53
  other     -> manual entry
Picking a channel (0-3), interactively or via --channel, also sets the radar's
RF Channel to the matching variant (channel 0 -> chan0, ...) on firmware that
supports it; older radars without the setting are left untouched.

Password: pass --password, or set MAGOS_PASSWORD, or you'll be prompted when run
in a terminal (Enter at the prompt keeps the factory default 'password').
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import re
import socket
import sys
import time
from getpass import getpass
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    sys.exit("This script needs 'requests'.  Install it with:  pip install requests")

from bench_core import (
    LOG_LINE_FORMAT,
    MutationBlocked,
    format_verification,
    install_log_context,
    load_settings,
    set_log_serial,
)


class MagosError(Exception):
    """A provisioning step failed (bad login, HTTP error, schema mismatch).

    Raised by the device clients instead of SystemExit so embedding callers
    (the web UIs) can catch it without also swallowing interpreter-level
    exceptions. The CLIs convert it to an exit code in main().
    """

# All device-talking steps log through this so callers (CLI, web UI) can route
# them wherever they like. The CLI attaches a stdout handler in main().
log = logging.getLogger("magos")

# Every log record is tagged with the current radar's serial (once known) and a
# lowercased level name, so handlers can render lines like:
#   [info] [AR300-SN-0042] Logged in as 'admin'.
# The context filter, LOG_LINE_FORMAT and set_log_serial are shared (bench_core).
install_log_context(log)

# Field names (normalised: lowercased, non-alphanumerics stripped) that the
# dashboard API may use for each identity attribute. We search the JSON it
# returns for any of these, so we don't have to hard-code one firmware's schema.
SERIAL_KEYS = ("serialnumber", "serial", "serialno", "sn", "deviceserial", "productserial")
MAC_KEYS = ("mac", "macaddress", "macaddr", "hwaddr", "hwaddress", "ethernetmac", "ethmac")
MODEL_KEYS = ("model", "modelname", "productname", "product", "devicemodel",
              "hardwaremodel", "hwmodel", "devicetype", "boardtype", "productmodel")
# Field names that may carry the RF channel a radar is currently on (see
# MagosClient.current_variant). Deliberately NOT a bare "channel": the dashboard
# payloads use that word for unrelated things, and a wrong read here would put a
# green tick on the wrong frequency.
VARIANT_KEYS = ("variant", "currentvariant", "activevariant", "selectedvariant")


# --- factory-defaults config file --------------------------------------------
# config/magos.config.json holds the operator-editable factory defaults, in an
# "ar300" and an "apu" section (the APU tool loads its own via the helper
# below). Any missing key — or the whole file — falls back to the built-in
# values, which match a fresh unit.
FACTORY_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "magos.config.json"


def load_factory_defaults(section: str) -> dict:
    """The `section` ("ar300" / "apu") of the factory-defaults config file,
    with `_`-prefixed comment keys dropped. {} when the file doesn't exist
    (built-in defaults apply); a hard exit on unparseable JSON — a typo in the
    config must be fixed, not silently replaced by built-ins."""
    try:
        cfg = load_settings(str(FACTORY_CONFIG_PATH))
    except FileNotFoundError:
        return {}
    except ValueError as e:
        sys.exit(f"Invalid JSON in {FACTORY_CONFIG_PATH}: {e}")
    section_cfg = cfg.get(section) or {}
    return {k: v for k, v in section_cfg.items() if not k.startswith("_")}


# --- AR-300 factory defaults ------------------------------------------------
_cfg = load_factory_defaults("ar300")
DEFAULT_HOST = _cfg.get("host", "192.168.40.50")      # radar's initial (factory) IP
DEFAULT_USERNAME = _cfg.get("username", "admin")
DEFAULT_PASSWORD = _cfg.get("password", "password")
DEFAULT_GATEWAY = _cfg.get("gateway", "192.168.88.1")
DEFAULT_DNS = _cfg.get("dns", "192.168.88.1")
DEFAULT_NETMASK = _cfg.get("netmask", "255.255.255.0")
DEFAULT_NTP = _cfg.get("ntp", "192.168.88.10")
DEFAULT_TIMEZONE = _cfg.get("timezone", "Asia/Jerusalem")

# channel number -> static IP on the operational subnet. The config file's
# "channel_ips" merges over the built-ins per channel, so an operator can
# change one channel's IP without restating the rest (and the channel keys —
# which also select the RF variant chan0..chan3 — always stay present).
CHANNEL_IPS = {
    "0": "192.168.88.50",
    "1": "192.168.88.51",
    "2": "192.168.88.52",
    "3": "192.168.88.53",
}
CHANNEL_IPS.update({str(k): str(v) for k, v in (_cfg.get("channel_ips") or {}).items()})
del _cfg


def netmask_to_prefix(netmask: str) -> int:
    return ipaddress.IPv4Network(f"0.0.0.0/{netmask}").prefixlen


def prefix_to_netmask(prefix) -> str:
    """Dotted netmask for a /NN prefix, or "" when it isn't a prefix. The
    legacy networking schema stores one CIDR address where the current one has
    a separate netmask, so a read-back has to be able to go back the other way."""
    try:
        return str(ipaddress.IPv4Network(f"0.0.0.0/{int(prefix)}").netmask)
    except (TypeError, ValueError):
        return ""


def to_cidr(ip: str, netmask: str | None) -> str:
    """Return ip in CIDR form (the API stores ip4Address as 'x.x.x.x/NN')."""
    if "/" in ip:                      # already CIDR
        return ip
    prefix = netmask_to_prefix(netmask) if netmask else 24
    return f"{ip}/{prefix}"


def probe_http(hostport: str, scheme: str = "http", timeout: float = 1.5,
               verify: bool = True) -> bool:
    """True only if something answers HTTP on the dashboard path.

    Stricter than a bare TCP connect: any HTTP response (incl. 401/404) counts
    as "a device is there", but a silent open port does not.
    """
    try:
        requests.get(f"{scheme}://{hostport}/dshb/v1/system", timeout=timeout, verify=verify)
        return True
    except requests.exceptions.RequestException:
        return False


def local_source_ip_for(host: str) -> str | None:
    """The local address the OS would route to `host` from (no packets sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((host, 9))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


def is_on_link(host: str, prefixlen: int = 24) -> bool:
    """True if a local adapter sits on the same /prefixlen subnet as `host`."""
    src = local_source_ip_for(host)
    if not src:
        return False
    try:
        return ipaddress.ip_address(src) in ipaddress.ip_network(
            f"{host}/{prefixlen}", strict=False)
    except ValueError:
        return False


def verify_device_at(ip: str, scheme: str = "http", username: str | None = None,
                     password: str | None = None, expect_substring: str | None = None,
                     total_timeout: float = 45.0, interval: float = 3.0,
                     verify_tls: bool = True) -> list[dict]:
    """The `reached at` verification row for a device we have just moved.

    Polls `ip` until it answers HTTP (any status), then — if credentials are
    given — logs in and checks that GET /networking mentions `expect_substring`
    (the address we just set). Effect-based: the address the device is actually
    answering on, which is the one fact a configure run can establish about its
    own networking change.

    Returns a one-row list in the shared schema (TEC-851), so a Magos run
    records the same `{item, expected, actual, ok}` rows every other tool does:

      * `ok=True`  — it answered, and the row says whether the address was also
                     confirmed out of the device's own config
      * `ok=False` — it never answered inside `total_timeout`
      * `ok=None`  — this PC has no adapter on the target subnet, so the address
                     could not be reached to confirm it either way. Skipped
                     immediately rather than burning `total_timeout` on a
                     guaranteed miss, and amber rather than green: "I could not
                     look" is not "it is fine".
    """
    host = ip.split("/")[0]

    def row(actual: str, ok: Optional[bool]) -> list[dict]:
        return [{"item": "reached at", "expected": host, "actual": actual, "ok": ok}]

    if not is_on_link(host):
        detail = (f"skipped — this PC has no adapter on {host}'s subnet, "
                  "so the new address cannot be reached to confirm it")
        log.warning("Verification %s.", detail)
        return row(detail, None)

    log.info("Verifying the device answers at %s (up to %ds)...", host, int(total_timeout))
    deadline = time.monotonic() + total_timeout
    while time.monotonic() < deadline:
        if probe_http(host, scheme=scheme, timeout=2.0, verify=verify_tls):
            detail = f"device answers HTTP at {host}"
            if username is not None and expect_substring:
                try:
                    s = requests.Session()
                    s.verify = verify_tls
                    base = f"{scheme}://{host}/dshb/v1"
                    s.post(f"{base}/login",
                           json={"username": username, "password": password}, timeout=5)
                    r = s.get(f"{base}/networking", timeout=5)
                    if r.status_code == 200 and expect_substring in r.text:
                        detail = f"{host} confirms address {expect_substring}"
                except requests.exceptions.RequestException:
                    pass  # reachable is already a pass; address check is best-effort
            log.info("Verified: %s.", detail)
            return row(detail, True)
        time.sleep(interval)

    detail = (f"no HTTP answer at {host} within {int(total_timeout)}s — the device may "
              "not have applied the change (or is still rebooting)")
    log.warning("Verification FAILED: %s.", detail)
    return row(detail, False)


def _norm_key(k: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


def _find_field(data, candidate_keys) -> str | None:
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


def rejected_extra_fields(resp) -> set[str]:
    """Top-level field names the API rejected as 'extra' (HTTP 400 schema error)."""
    try:
        data = resp.json()
    except ValueError:
        return set()
    extras = set()
    for fe in data.get("fieldErrors", []):
        if "extra" in (fe.get("errorMessage") or "").lower():
            name = fe.get("fieldName")
            if name:
                extras.add(name)
    return extras


def fetch_identity(session, base: str, timeout: int, paths=("/systemStatus", "/system",
                   "/networking", "/about", "/device", "/info"), label: str = "Device") -> dict:
    """Best-effort serial / MAC / model from a dashboard API (shared by radar + APU).

    GETs each path and searches the JSON for known-ish field names. Returns the
    raw payloads too, so unknown fields can be discovered from the saved logs.
    Tags subsequent log lines with the discovered serial.
    """
    raw: dict = {}
    for path in paths:
        try:
            r = session.get(f"{base}{path}", timeout=timeout)
        except requests.exceptions.RequestException:
            continue
        if r.status_code != 200:
            continue
        try:
            raw[path] = r.json()
        except ValueError:
            continue

    identity = {
        "serial": _find_field(raw, SERIAL_KEYS) or "unknown",
        "mac": _find_field(raw, MAC_KEYS) or "unknown",
        "model": _find_field(raw, MODEL_KEYS) or "unknown",
        "raw": raw,
    }
    set_log_serial(identity["serial"])
    log.info("%s identity: model=%s  serial=%s  MAC=%s",
             label, identity["model"], identity["serial"], identity["mac"])
    # When nothing matched, surface what the device DID return so the schema can
    # be discovered from the logs without another live capture.
    if identity["serial"] == identity["mac"] == identity["model"] == "unknown":
        if raw:
            for path, payload in raw.items():
                keys = list(payload) if isinstance(payload, dict) else f"<{type(payload).__name__}>"
                log.warning("identity: %s answered but no known fields; top-level keys: %s",
                            path, keys)
        else:
            log.warning("identity: no endpoint returned JSON (tried: %s).", ", ".join(paths))
    return identity


class MagosHttpClient:
    """The dashboard-API plumbing both Magos devices share: the session, the
    login, the reads a verification pass makes, and the read-only guard.

    The guard is two layers, because "mutation-free" is a claim about code
    nobody re-reads (TEC-851):

    * **`_post` is the backstop.** Every change either device makes is a POST,
      and the only POST that changes nothing is `/login`, so one gate there
      covers the whole HTTP surface — a write added later has to go through it
      to reach the device, so it cannot be forgotten. The radar's RF channel is
      the one thing it cannot see, because that is pushed over a WebSocket;
      `set_channel` checks for itself.
    * **Each named mutator refuses up front**, so a refused change never
      reaches the device at all — `set_ntp` reads the current settings before
      writing them, and on a verify run even that read should not happen.
    """

    def __init__(self, host: str, scheme: str = "http", verify: bool = True, timeout: int = 15):
        self.host = host
        self.scheme = scheme
        self.verify = verify
        self.base = f"{scheme}://{host}/dshb/v1"
        self.s = requests.Session()
        self.s.headers.update({"Content-Type": "application/json"})
        self.s.verify = verify
        self.timeout = timeout
        self.read_only = False

    def set_read_only(self) -> None:
        """Refuse every write from here on. One-way on purpose: nothing in a
        verify pass has a reason to turn it back off."""
        self.read_only = True
        log.info("Client is now read-only — any write will be refused.")

    def _refuse_mutation(self, what: str) -> None:
        if self.read_only:
            raise MutationBlocked(
                f"refusing to {what}: this is a verify-only run and must not "
                "change the device")

    # --- transport ----------------------------------------------------------
    def _get(self, url: str, **kw):
        kw.setdefault("timeout", self.timeout)
        return self.s.get(url, **kw)

    def _post(self, url: str, **kw):
        if self.read_only and not url.endswith("/login"):
            raise MutationBlocked(
                f"read-only client refused a write: POST {url}")
        kw.setdefault("timeout", self.timeout)
        return self.s.post(url, **kw)

    # --- auth ---------------------------------------------------------------
    def login(self, username: str, password: str) -> None:
        r = self._post(f"{self.base}/login",
                       json={"username": username, "password": password})
        if r.status_code != 200:
            raise MagosError(f"Login failed (HTTP {r.status_code}): {r.text[:300]}")
        if "session" not in self.s.cookies and not self.s.cookies:
            log.warning("No session cookie returned; continuing anyway.")
        log.info("Logged in as '%s'.", username)

    # --- reads a verification pass makes ------------------------------------
    def get_system(self) -> dict:
        """`GET /system` — NTP server, timezone, component versions."""
        r = self._get(f"{self.base}/system")
        r.raise_for_status()
        return r.json()

    def get_networking(self) -> dict:
        """`GET /networking` — the address config, in whichever of the two
        schemas this firmware speaks (see the module docstring)."""
        r = self._get(f"{self.base}/networking")
        r.raise_for_status()
        return r.json()

class MagosClient(MagosHttpClient):
    def __init__(self, host: str, scheme: str = "http", verify: bool = True, timeout: int = 15):
        super().__init__(host, scheme=scheme, verify=verify, timeout=timeout)
        # Radar-specific config (RF channel etc.) lives under a separate API base.
        self.radar_base = f"{scheme}://{host}/radar/v1"

    # --- identity -----------------------------------------------------------
    def get_identity(self) -> dict:
        ident = fetch_identity(self.s, self.base, self.timeout, label="Radar")
        # On firmware >= 3.x serial+model come from /dshb/v1/systemStatus (covered
        # by fetch_identity above, and readable even while the radar is in Raw
        # mode). Only if we still lack the serial AND model do we fall back to the
        # radar API base — those endpoints can 403 in Raw mode, so we avoid the
        # extra round-trips whenever systemStatus already answered.
        if ident["serial"] == "unknown" and ident["model"] == "unknown":
            raw: dict = {}
            for path in ("/sensors", "/remoteProductInfo"):
                try:
                    r = self._get(f"{self.radar_base}{path}")
                except requests.exceptions.RequestException:
                    continue
                if r.status_code == 200:
                    try:
                        raw[path] = r.json()
                    except ValueError:
                        pass
            if raw:
                found = False
                for key, keys in (("serial", SERIAL_KEYS), ("mac", MAC_KEYS),
                                  ("model", MODEL_KEYS)):
                    if ident[key] == "unknown":
                        val = _find_field(raw, keys)
                        if val:
                            ident[key] = val
                            found = True
                ident.setdefault("raw", {}).update(raw)
                if found:
                    set_log_serial(ident["serial"])
                    log.info("Radar identity (radar API): model=%s  serial=%s  MAC=%s",
                             ident["model"], ident["serial"], ident["mac"])
        return ident

    # --- NTP ----------------------------------------------------------------
    def set_ntp(self, ntp_server: str, timezone: str | None = None) -> None:
        self._refuse_mutation(f"set the NTP server to {ntp_server}")
        log.info("Setting NTP server to %s ...", ntp_server)
        sysinfo = self.get_system()
        body = {
            "ntpAutomatic": False,          # manual NTP server
            "ntpServer": ntp_server,
            "timezone": timezone or sysinfo.get("timezone") or DEFAULT_TIMEZONE,
        }
        r = self._post(f"{self.base}/system", json=body)
        if r.status_code not in (200, 204):
            raise MagosError(f"NTP update failed (HTTP {r.status_code}): {r.text[:300]}")
        log.info("NTP server set to %s (timezone %s).", ntp_server, body["timezone"])

    # --- RF channel (firmware >= 3.x) ---------------------------------------
    def list_variants(self) -> dict:
        """{variantId: description} of RF channels this radar supports.

        Newer AR-300 firmware exposes an RF "Channel" (called a `variant`
        internally) so neighbouring radars can transmit on different frequencies.
        GET /radar/v1/listVariants returns e.g.:
            {"variantList": [{"id": "chan0", "description": "Channel 0"}, ...]}

        Returns an empty dict on older firmware that has no such endpoint, so
        callers can treat "no channel support" as a no-op rather than an error.
        """
        try:
            r = self._get(f"{self.radar_base}/listVariants")
        except requests.exceptions.RequestException:
            return {}
        if r.status_code != 200:
            return {}
        try:
            data = r.json()
        except ValueError:
            return {}
        return {v["id"]: v.get("description", v["id"])
                for v in data.get("variantList", []) if isinstance(v, dict) and "id" in v}

    def current_variant(self) -> Optional[str]:
        """The RF channel the radar is on NOW, or None when this firmware does
        not report it.

        There is no REST read for it. The radar states its channel on the same
        WebSocket `set_channel` writes it over: on connect, `/radar/v1/detections`
        pushes a `radar_settings` frame whose payload carries `variant` (seen on
        3.1.0: `{"op":"radar_settings","payload":{"tx_enabled":true,...,
        "variant":"chan1"}}`). That is the read. The REST payloads that describe
        the radar are searched first, cheaply, in case a firmware ever exposes
        the field there — on 3.1.0 none does, and `/radar/v1/sensors` is 403
        while the radar is in Raw mode.

        Either way a value is trusted only when the unit itself lists it as one
        of its variants. An unrecognised value reads as "cannot confirm" rather
        than as a mismatch, because a field named `variant` on some future
        firmware need not mean the RF channel.

        A pure read: nothing is sent on the socket, so it is legal on a
        verify-only run.
        """
        variants = self.list_variants()
        if not variants:
            return None
        raw: dict = {}
        for base, path in ((self.base, "/systemStatus"),
                           (self.radar_base, "/sensors"),
                           (self.radar_base, "/remoteProductInfo")):
            try:
                r = self._get(f"{base}{path}")
            except requests.exceptions.RequestException:
                continue
            if r.status_code == 200:
                try:
                    raw[path] = r.json()
                except ValueError:
                    pass
        found = _find_field(raw, VARIANT_KEYS)
        if found is None:
            found = self._variant_from_settings_frame()
        return found if found in variants else None

    def _variant_from_settings_frame(self) -> Optional[str]:
        """`payload.variant` out of the first `radar_settings` frame the
        detections WebSocket pushes after connect, or None if none arrives
        within the client timeout (or the socket cannot be opened at all).

        The socket also streams alerts, op_state, heartbeats and detections;
        only the settings frame is read, and nothing is sent.
        """
        try:
            with self._open_ws() as ws:
                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    try:
                        raw = ws.recv(timeout=2)
                    except TimeoutError:
                        continue
                    try:
                        msg = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(msg, dict) or msg.get("op") != "radar_settings":
                        continue
                    payload = msg.get("payload")
                    variant = payload.get("variant") if isinstance(payload, dict) else None
                    return str(variant) if variant not in (None, "") else None
        except Exception as e:
            log.info("Could not read the RF channel over the radar WebSocket: %s", e)
        return None

    def _open_ws(self):
        """A connected `/radar/v1/detections` WebSocket carrying this session's
        cookie — the one channel the radar reports and accepts its RF variant
        on. A context manager; the caller reads/sends and lets it close."""
        try:
            from websockets.sync.client import connect as ws_connect
        except ImportError:
            raise MagosError("Talking to the radar WebSocket needs the 'websockets' "
                             "package (pip install websockets).")

        ws_scheme = "wss" if self.scheme == "https" else "ws"
        url = f"{ws_scheme}://{self.host}/radar/v1/detections"
        cookie = "; ".join(f"{c.name}={c.value}" for c in self.s.cookies)
        kwargs: dict = {
            "additional_headers": {"Cookie": cookie} if cookie else {},
            "open_timeout": self.timeout,
            "max_size": None,
        }
        if ws_scheme == "wss" and not self.verify:
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            kwargs["ssl"] = ctx
        return ws_connect(url, **kwargs)

    def set_channel(self, channel: str) -> None:
        """Set the radar's RF Channel (firmware >= 3.x only).

        Unlike the other settings, the channel is NOT POSTed over REST — the
        dashboard pushes it over the radar WebSocket. We mirror that exactly:

            ws(s)://<host>/radar/v1/detections
              send {"op":"set_params","payload":{"variant":"chanN"},"id":<n>}
              recv {"op":"ack","inResponseTo":<n>}                       (success)
                or {"op":"error","inResponseTo":<n>,"payload":{"message":...}}

        `channel` may be a plain digit ("0".."3") or a full variant id ("chan0").
        On firmware that has no RF channel, this logs a note and returns without
        error, so it is safe to call unconditionally in the provisioning flow.

        Call this BEFORE set_network — changing the IP drops the connection this
        WebSocket rides on.
        """
        # The one write that does not go through `_post`, so it carries its own
        # guard: a WebSocket frame is invisible to the HTTP gate.
        self._refuse_mutation(f"set the RF channel to {channel}")
        variants = self.list_variants()
        if not variants:
            log.info("Radar has no RF Channel setting (older firmware); skipping channel step.")
            return

        variant = str(channel).strip().lower()
        if variant.isdigit():
            variant = f"chan{variant}"
        if variant not in variants:
            available = ", ".join(f"{vid} ({desc})" for vid, desc in sorted(variants.items()))
            raise MagosError(f"Radar has no channel '{channel}'. Available: {available}")

        log.info("Setting RF channel to %s (%s) ...", variant, variants[variant])
        req_id = 1
        try:
            with self._open_ws() as ws:
                ws.send(json.dumps(
                    {"op": "set_params", "payload": {"variant": variant}, "id": req_id}))
                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    try:
                        raw = ws.recv(timeout=2)
                    except TimeoutError:
                        continue
                    try:
                        msg = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    # The socket also streams detections/heartbeats/etc.; only the
                    # reply tagged with our request id matters.
                    if not isinstance(msg, dict) or msg.get("inResponseTo") != req_id:
                        continue
                    if msg.get("op") == "ack":
                        log.info("RF channel set to %s (%s).", variant, variants[variant])
                        return
                    if msg.get("op") == "error":
                        detail = (msg.get("payload") or {}).get("message", "unknown error")
                        raise MagosError(f"Radar rejected channel {variant}: {detail}")
        except MagosError:
            raise
        except Exception as e:
            raise MagosError(f"Could not set RF channel over WebSocket: {e}")
        raise MagosError(f"No ack from radar after setting channel {variant} (timed out).")

    # --- networking ---------------------------------------------------------
    def set_network(self, ip_cidr: str, gateway: str, dns: str) -> None:
        self._refuse_mutation(f"move the radar to {ip_cidr}")
        net = self.get_networking()

        iface = ipaddress.ip_interface(ip_cidr)
        ports = net.get("netInterfaces")
        if isinstance(ports, dict) and ports:
            # Firmware >= 3.x: per-interface settings nested under
            # netInterfaces.<port>, with a PLAIN ip4Address plus a separate
            # ip4Netmask (older firmware used a single top-level CIDR address).
            port = ports.get("port1") or next(iter(ports.values()))
            port["ip4Method"] = "manual"
            port["ip4Address"] = str(iface.ip)        # plain IP, no /prefix
            port["ip4Netmask"] = str(iface.netmask)
            port["ip4Gateway"] = gateway
            port["ip4OverrideDNS"] = True
            port["ip4DNS"] = [dns]
        else:
            # Legacy flat schema: a single top-level CIDR address.
            net["ip4Method"] = "manual"
            net["ip4Address"] = ip_cidr
            net["ip4Gateway"] = gateway
            net["ip4OverrideDNS"] = True    # use the DNS we provide
            net["ip4DNS"] = [dns]           # primary DNS (list; first = primary)

        log.info("Applying network: ip=%s gateway=%s primary-DNS=%s", ip_cidr, gateway, dns)
        log.info("(the connection will drop if the IP changes — this is expected)")

        # The GET returns a superset of what POST accepts (certificates, mass
        # ports, ...) and the POST schema forbids unknown fields. Send it back
        # and, whenever the API flags fields as "extra", drop exactly those and
        # retry — so we don't have to hard-code the firmware's exact schema.
        for _ in range(10):
            try:
                r = self._post(f"{self.base}/networking", json=net)
            except requests.exceptions.RequestException as e:
                # No response usually means the IP changed and the socket died.
                log.info("Connection dropped after sending networking change (expected): %s", e)
                return

            if r.status_code in (200, 204):
                log.info("Networking updated.")
                return

            if r.status_code == 400:
                extras = rejected_extra_fields(r)
                if extras:
                    for field in extras:
                        net.pop(field, None)
                    log.info("Dropping %d field(s) the API rejected as extra: %s",
                             len(extras), ", ".join(sorted(extras)))
                    continue

            raise MagosError(
                f"Networking update failed (HTTP {r.status_code}): {r.text[:500]}"
            )

        raise MagosError("Networking update failed: too many schema-cleanup retries.")


def prompt_channel_ip() -> tuple[str, str | None]:
    """Ask which channel the radar is; return (ip, channel).

    `channel` is the chosen channel key ("0".."3") so the caller can also set the
    radar's RF channel, or None when a manual IP is entered.
    """
    print("Which channel is this radar?")
    for ch, ip in CHANNEL_IPS.items():
        print(f"  {ch} -> {ip}")
    print("  other -> enter an IP manually")

    while True:
        choice = input("Channel [0/1/2/3/other]: ").strip().lower()
        if choice in CHANNEL_IPS:
            ip = CHANNEL_IPS[choice]
            print(f"  Channel {choice} -> {ip}")
            return ip, choice
        if choice in ("other", "o", "manual", "m"):
            while True:
                manual = input("Enter IP (plain or CIDR): ").strip()
                try:
                    ipaddress.ip_interface(manual)  # accepts both plain and CIDR
                    return manual, None
                except ValueError:
                    print("  Not a valid IP address — try again.")
        print("  Please choose 0, 1, 2, 3, or 'other'.")


def main():
    p = argparse.ArgumentParser(description="Configure Magos AR-300 NTP + networking.")
    p.add_argument("--host", default=DEFAULT_HOST,
                   help=f"host:port (default: {DEFAULT_HOST}, the AR-300 factory IP)")
    p.add_argument("--username", default=DEFAULT_USERNAME,
                   help=f"login username (default: {DEFAULT_USERNAME})")
    p.add_argument("--password",
                   help=f"login password (default: {DEFAULT_PASSWORD}; else MAGOS_PASSWORD env or prompt)")
    p.add_argument("--scheme", default="http", choices=["http", "https"])
    p.add_argument("--insecure", action="store_true", help="skip TLS verify (https)")

    p.add_argument("--interactive", action="store_true",
                   help="ask which channel the radar is (0-3) and pick the IP for you")
    p.add_argument("--channel",
                   help="radar RF channel to set on firmware >=3.x (0-3, or a variant id "
                        "like 'chan0'); interactive/channel selection sets this automatically")
    p.add_argument("--ntp", default=DEFAULT_NTP, help=f"NTP server IP (default: {DEFAULT_NTP})")
    p.add_argument("--timezone", default=DEFAULT_TIMEZONE,
                   help=f"IANA timezone to set with NTP (default: {DEFAULT_TIMEZONE})")
    p.add_argument("--ip", help="IPv4 address (plain or CIDR, e.g. 192.168.88.50 or .../24)")
    p.add_argument("--netmask", default=DEFAULT_NETMASK,
                   help=f"netmask if --ip has no /prefix (default: {DEFAULT_NETMASK})")
    p.add_argument("--gateway", default=DEFAULT_GATEWAY,
                   help=f"default gateway IP (default: {DEFAULT_GATEWAY})")
    p.add_argument("--dns", default=DEFAULT_DNS, help=f"primary DNS IP (default: {DEFAULT_DNS})")
    p.add_argument("--yes", action="store_true", help="don't prompt before applying network change")
    args = p.parse_args()

    # Route the device logger to stdout with the [level] [serial] prefix. We
    # attach our own handler (instead of basicConfig) so the custom format only
    # applies to our records, not to other libraries logging via the root.
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(LOG_LINE_FORMAT))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    set_log_serial(None)

    pwd = args.password or os.environ.get("MAGOS_PASSWORD")
    if not pwd:
        if sys.stdin.isatty():
            pwd = getpass(f"Password for {args.username} "
                          f"(Enter = factory default '{DEFAULT_PASSWORD}'): ") or DEFAULT_PASSWORD
        else:
            pwd = DEFAULT_PASSWORD

    if args.interactive and not args.ip:
        args.ip, picked_channel = prompt_channel_ip()
        if args.channel is None:
            args.channel = picked_channel  # also set the radar's RF channel to match

    want_net = args.ip is not None

    client = MagosClient(args.host, scheme=args.scheme, verify=not args.insecure)

    try:
        print("Authenticating...")
        client.login(args.username, pwd)

        client.get_identity()

        if args.ntp:
            print("Updating NTP...")
            client.set_ntp(args.ntp, args.timezone)

        # Set the RF channel before networking — changing the IP drops the link.
        if args.channel is not None:
            print("Setting RF channel...")
            client.set_channel(args.channel)

        if want_net:
            ip_cidr = to_cidr(args.ip, args.netmask)
            if not args.yes:
                ans = input(f"Apply IP={ip_cidr}, GW={args.gateway}, DNS={args.dns}? [y/N] ")
                if ans.strip().lower() not in ("y", "yes"):
                    print("Aborted networking change.")
                    return
            print("Updating networking (do this last)...")
            client.set_network(ip_cidr, args.gateway, args.dns)
            rows = verify_device_at(ip_cidr, scheme=args.scheme, username=args.username,
                                    password=pwd, expect_substring=args.ip.split("/")[0],
                                    verify_tls=not args.insecure)
            print(format_verification(rows))
    except MagosError as e:
        sys.exit(f"ERROR: {e}")

    print("Done.")


if __name__ == "__main__":
    main()
