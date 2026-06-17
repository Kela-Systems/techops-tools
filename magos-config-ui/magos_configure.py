#!/usr/bin/env python3
"""
Configure a Magos AR-300 radar (dashboard API) over HTTP.

Discovered API (base = http://<host>/dshb/v1):
  POST /login        {"username","password"}            -> sets a session cookie
  GET  /system       -> {ntpAutomatic, ntpServer, timezone, swComponents, users}
  POST /system       {ntpAutomatic, ntpServer, timezone} -> updates NTP
  GET  /networking   -> {ip4Method, ip4Address(CIDR), ip4Gateway, ip4DNS[], ip4OverrideDNS, ...}
  POST /networking   {<full object with edits>}          -> updates IP / gateway / DNS

Auth is cookie/session based (login response sets a `session` cookie). We use a
requests.Session so the cookie jar is reused automatically for later calls.

Strategy: GET current config, change ONLY the requested fields, POST it back.
That preserves every other setting on the device.

NOTE: changing the IP address drops the connection you're talking over, so the
final POST /networking will usually not return a clean response — that's expected.
After it, reach the radar on its NEW address.

A fresh AR-300 ships on 192.168.40.50 with admin:password, so all of those are
the script defaults — out of the box you can just run:

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

Password: pass --password, or set MAGOS_PASSWORD, or you'll be prompted when run
in a terminal (Enter at the prompt keeps the factory default 'password').
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import re
import socket
import sys
import time
from getpass import getpass

try:
    import requests
except ImportError:
    sys.exit("This script needs 'requests'.  Install it with:  pip install requests")


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
_LOG_CTX = {"sn": "-"}


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.sn = _LOG_CTX["sn"]
        record.levelname_lc = record.levelname.lower()
        return True


log.addFilter(_ContextFilter())

# Format used by both the CLI and the web UI for each step line.
LOG_LINE_FORMAT = "[%(levelname_lc)s] [%(sn)s] %(message)s"


def set_log_serial(serial: str | None) -> None:
    """Tag subsequent log lines with this radar's serial (None resets to '-')."""
    _LOG_CTX["sn"] = serial or "-"

# Field names (normalised: lowercased, non-alphanumerics stripped) that the
# dashboard API may use for each identity attribute. We search the JSON it
# returns for any of these, so we don't have to hard-code one firmware's schema.
SERIAL_KEYS = ("serialnumber", "serial", "serialno", "sn", "deviceserial")
MAC_KEYS = ("mac", "macaddress", "macaddr", "hwaddr", "hwaddress", "ethernetmac", "ethmac")
MODEL_KEYS = ("model", "modelname", "productname", "product", "devicemodel",
              "hardwaremodel", "hwmodel", "devicetype", "boardtype")


# --- AR-300 factory defaults ------------------------------------------------
DEFAULT_HOST = "192.168.40.50"      # radar's initial (factory) IP
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "password"
DEFAULT_GATEWAY = "192.168.88.1"
DEFAULT_DNS = "192.168.88.1"
DEFAULT_NETMASK = "255.255.255.0"
DEFAULT_NTP = "192.168.88.10"
DEFAULT_TIMEZONE = "Asia/Jerusalem"

# channel number -> static IP on the operational subnet
CHANNEL_IPS = {
    "0": "192.168.88.50",
    "1": "192.168.88.51",
    "2": "192.168.88.52",
    "3": "192.168.88.53",
}


def netmask_to_prefix(netmask: str) -> int:
    return ipaddress.IPv4Network(f"0.0.0.0/{netmask}").prefixlen


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
                     verify_tls: bool = True) -> dict:
    """Confirm a just-provisioned device actually answers on its NEW address.

    Polls `ip` until it answers HTTP (any status), then — if credentials are
    given — logs in and checks that GET /networking mentions `expect_substring`
    (the address we just set). Returns {"verified", "skipped", "detail"}.

    If the laptop has no adapter on the target subnet, verification is skipped
    immediately (rather than burning `total_timeout` on a guaranteed miss).
    """
    host = ip.split("/")[0]
    if not is_on_link(host):
        detail = (f"skipped — this PC has no adapter on {host}'s subnet, "
                  "so the new address cannot be reached to confirm it")
        log.warning("Verification %s.", detail)
        return {"verified": False, "skipped": True, "detail": detail}

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
            return {"verified": True, "skipped": False, "detail": detail}
        time.sleep(interval)

    detail = (f"no HTTP answer at {host} within {int(total_timeout)}s — the device may "
              "not have applied the change (or is still rebooting)")
    log.warning("Verification FAILED: %s.", detail)
    return {"verified": False, "skipped": False, "detail": detail}


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


def fetch_identity(session, base: str, timeout: int, paths=("/system", "/networking",
                   "/about", "/device", "/info"), label: str = "Device") -> dict:
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
    return identity


class MagosClient:
    def __init__(self, host: str, scheme: str = "http", verify: bool = True, timeout: int = 15):
        self.base = f"{scheme}://{host}/dshb/v1"
        self.s = requests.Session()
        self.s.headers.update({"Content-Type": "application/json"})
        self.s.verify = verify
        self.timeout = timeout

    # --- auth ---------------------------------------------------------------
    def login(self, username: str, password: str) -> None:
        r = self.s.post(
            f"{self.base}/login",
            json={"username": username, "password": password},
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise MagosError(f"Login failed (HTTP {r.status_code}): {r.text[:300]}")
        if "session" not in self.s.cookies and not self.s.cookies:
            log.warning("No session cookie returned; continuing anyway.")
        log.info("Logged in as '%s'.", username)

    # --- identity -----------------------------------------------------------
    def get_identity(self) -> dict:
        return fetch_identity(self.s, self.base, self.timeout, label="Radar")

    # --- NTP ----------------------------------------------------------------
    def set_ntp(self, ntp_server: str, timezone: str | None = None) -> None:
        log.info("Setting NTP server to %s ...", ntp_server)
        cur = self.s.get(f"{self.base}/system", timeout=self.timeout)
        cur.raise_for_status()
        sysinfo = cur.json()
        body = {
            "ntpAutomatic": False,          # manual NTP server
            "ntpServer": ntp_server,
            "timezone": timezone or sysinfo.get("timezone") or DEFAULT_TIMEZONE,
        }
        r = self.s.post(f"{self.base}/system", json=body, timeout=self.timeout)
        if r.status_code not in (200, 204):
            raise MagosError(f"NTP update failed (HTTP {r.status_code}): {r.text[:300]}")
        log.info("NTP server set to %s (timezone %s).", ntp_server, body["timezone"])

    # --- networking ---------------------------------------------------------
    def set_network(self, ip_cidr: str, gateway: str, dns: str) -> None:
        cur = self.s.get(f"{self.base}/networking", timeout=self.timeout)
        cur.raise_for_status()
        net = cur.json()

        net["ip4Method"] = "manual"
        net["ip4Address"] = ip_cidr
        net["ip4Gateway"] = gateway
        net["ip4OverrideDNS"] = True        # use the DNS we provide
        net["ip4DNS"] = [dns]               # primary DNS (list; first = primary)

        log.info("Applying network: ip=%s gateway=%s primary-DNS=%s", ip_cidr, gateway, dns)
        log.info("(the connection will drop if the IP changes — this is expected)")

        # The GET returns a superset of what POST accepts (certificates, mass
        # ports, ...) and the POST schema forbids unknown fields. Send it back
        # and, whenever the API flags fields as "extra", drop exactly those and
        # retry — so we don't have to hard-code the firmware's exact schema.
        for _ in range(10):
            try:
                r = self.s.post(f"{self.base}/networking", json=net, timeout=self.timeout)
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


def prompt_channel_ip() -> str:
    """Ask which channel the radar is and return the matching IP (or a manual one)."""
    print("Which channel is this radar?")
    for ch, ip in CHANNEL_IPS.items():
        print(f"  {ch} -> {ip}")
    print("  other -> enter an IP manually")

    while True:
        choice = input("Channel [0/1/2/3/other]: ").strip().lower()
        if choice in CHANNEL_IPS:
            ip = CHANNEL_IPS[choice]
            print(f"  Channel {choice} -> {ip}")
            return ip
        if choice in ("other", "o", "manual", "m"):
            while True:
                manual = input("Enter IP (plain or CIDR): ").strip()
                try:
                    ipaddress.ip_interface(manual)  # accepts both plain and CIDR
                    return manual
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
        args.ip = prompt_channel_ip()

    want_net = args.ip is not None

    client = MagosClient(args.host, scheme=args.scheme, verify=not args.insecure)

    try:
        print("Authenticating...")
        client.login(args.username, pwd)

        client.get_identity()

        if args.ntp:
            print("Updating NTP...")
            client.set_ntp(args.ntp, args.timezone)

        if want_net:
            ip_cidr = to_cidr(args.ip, args.netmask)
            if not args.yes:
                ans = input(f"Apply IP={ip_cidr}, GW={args.gateway}, DNS={args.dns}? [y/N] ")
                if ans.strip().lower() not in ("y", "yes"):
                    print("Aborted networking change.")
                    return
            print("Updating networking (do this last)...")
            client.set_network(ip_cidr, args.gateway, args.dns)
            verify_device_at(ip_cidr, scheme=args.scheme, username=args.username,
                             password=pwd, expect_substring=ip_cidr,
                             verify_tls=not args.insecure)
    except MagosError as e:
        sys.exit(f"ERROR: {e}")

    print("Done.")


if __name__ == "__main__":
    main()
