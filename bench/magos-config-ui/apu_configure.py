#!/usr/bin/env python3
"""
Configure a Magos AR Processing Unit (APU) over its dashboard API.

Requires firmware 3.1.2 (rc builds like 3.1.2-rc5 accepted) — the multi-radar
firmware where one APU controls TWO radars. Older APUs are refused with a
message asking the operator to upgrade them manually first.

Dashboard base = http://<host>/dshb/v1 (auth is session-cookie based; login
sets it); the multi-radar assignment lives on its own base, /apu/v1.

Endpoints:

  POST /dshb/v1/login   {"username","password"}            -> sets session cookie

  GET  /dshb/v1/system  -> {swComponents, ntpAutomatic, ntpServer, timezone, users}
  POST /dshb/v1/system  {"ntpAutomatic","ntpServer","timezone"}  -> NTP + timezone

  POST /apu/v1/settings {"radars":[{"radar_id","remote_base_url","name"},...]}
                        -> assign ALL controlled radars in one call (firmware
                           3.1.2+; replaces the old single-radar /phoenix_ip).
                           radar_id (e.g. "radar_0") is used as instanceId in
                           MASS. remote_base_url must be a FULL URL
                           ("http://192.168.88.50") — a bare IP is rejected
                           with a pydantic url_parsing error.

  GET  /dshb/v1/networking -> {"netInterfaces":{"port1":{ip4Method, ip4Address,
                          ip4Netmask, ip4Gateway, ip4DNS[], ...}}, ...top-level...}
  POST /dshb/v1/networking <full object, port1 edited>     -> IP / gateway / DNS

Notes specific to the APU (differ from the AR-300 radar):
  * the APU's initial (factory) IP is always 192.168.40.60.
  * networking is PER-INTERFACE (netInterfaces.port1) and ip4Address is a PLAIN ip
    with a SEPARATE ip4Netmask (the radar used CIDR in ip4Address).

APU mapping (a full system = 4 radars + 2 APUs):
  APU 0 -> 192.168.88.60, controlling radar_0 (192.168.88.50, channel 0)
                                  and radar_1 (192.168.88.51, channel 1)
  APU 1 -> 192.168.88.61, controlling radar_2 (192.168.88.52, channel 2)
                                  and radar_3 (192.168.88.53, channel 3)

Defaults match a fresh APU, so the common case is:

  python3 apu_configure.py --interactive

Order: NTP/timezone and radar assignment first; networking LAST, because changing
the APU's own IP drops the connection you're talking over (that's expected).

Password: pass --password, or set APU_PASSWORD, or you'll be prompted when run
in a terminal (Enter at the prompt keeps the factory default 'password').
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import sys
from getpass import getpass

try:
    import requests
except ImportError:
    sys.exit("This script needs 'requests'.  Install it with:  pip install requests")

# Reuse the radar tool's logger, identity lookup, schema-cleanup, and the radar
# channel IPs (which the APU "controls").
from magos_configure import (  # noqa: E402
    CHANNEL_IPS as RADAR_CHANNEL_IPS,
    DEFAULT_TIMEZONE,
    LOG_LINE_FORMAT,
    MagosError,
    _find_field,
    fetch_identity,
    log,
    rejected_extra_fields,
    set_log_serial,
    verify_device_at,
)


# --- APU factory defaults ---------------------------------------------------
DEFAULT_HOST = "192.168.40.60"      # APU's initial (factory) IP
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "password"
DEFAULT_GATEWAY = "192.168.88.1"
DEFAULT_DNS = "192.168.88.1"
DEFAULT_NETMASK = "255.255.255.0"
DEFAULT_NTP = "192.168.88.10"
DEFAULT_IFACE = "port1"

# APU index -> the APU's own static IP. Firmware 3.1.2 controls TWO radars per
# APU, so a full system is 2 APUs (.60/.61), not 4.
APU_CHANNEL_IPS = {
    "0": "192.168.88.60",
    "1": "192.168.88.61",
}
# APU index -> the radars it controls. radar_id is "radar_<radar channel>" (it
# becomes the instanceId in MASS): APU 0 gets channels 0+1, APU 1 gets 2+3.
APU_RADAR_ASSIGNMENTS = {
    apu: [{"radar_id": f"radar_{ch}", "ip": RADAR_CHANNEL_IPS[ch], "name": f"Radar {ch}"}
          for ch in chans]
    for apu, chans in (("0", ("0", "1")), ("1", ("2", "3")))
}

# The multi-radar firmware this tool provisions. Anything else is refused and
# the operator is asked to upgrade the unit manually first.
REQUIRED_APU_FIRMWARE = "3.1.2"

# Firmware-version field names searched (normalised) in the identity payloads
# — same approach as SERIAL_KEYS & co in magos_configure. Deliberately NO bare
# "version": /system's swComponents lists sub-component versions (chrony,
# dashboard, phoenix, ...) under that key, and those must never be mistaken
# for the unit's firmware. The real one is /systemStatus's softwareVersion
# (e.g. "3.1.2-rc5", confirmed live).
FIRMWARE_KEYS = ("softwareversion", "swversion", "firmwareversion", "firmware",
                 "fwversion")


def radars_from_ips(ips: list[str]) -> list[dict]:
    """Manual radar list: IDs are assigned radar_0, radar_1, ... in the order
    the IPs were given (for channel-based targets use APU_RADAR_ASSIGNMENTS)."""
    return [{"radar_id": f"radar_{i}", "ip": ip, "name": f"Radar {i}"}
            for i, ip in enumerate(ips)]


def radar_base_url(ip: str) -> str:
    """remote_base_url must be a FULL URL — the 3.1.2 firmware
    pydantic-validates it and rejects a bare IP with 'url_parsing ... relative
    URL without a base' (despite the vendor example showing a bare IP). A
    value that already carries a scheme is passed through untouched."""
    return ip if "://" in ip else f"http://{ip}"


def firmware_version(raw: dict) -> str | None:
    """The unit's firmware version from the raw identity payloads collected by
    fetch_identity. On this firmware family it is /systemStatus's
    softwareVersion; searching every payload for the FIRMWARE_KEYS covers
    schema drift (component versions are excluded by the key list)."""
    return _find_field(raw, FIRMWARE_KEYS)


def firmware_ok(version: str | None) -> bool:
    """True for the required release and its rc pre-releases (current units
    ship 3.1.2-rc5); anything else — including an unreadable version — fails."""
    if not version:
        return False
    v = version.strip().lower().lstrip("v")
    return v == REQUIRED_APU_FIRMWARE or v.startswith(REQUIRED_APU_FIRMWARE + "-rc")


def firmware_error(version: str | None) -> str:
    """The operator-facing refusal message for a unit that failed the gate."""
    seen = version or "unreadable"
    return (f"APU firmware is {seen} — this tool requires {REQUIRED_APU_FIRMWARE}. "
            "Upgrade the APU manually via its dashboard, then plug it in again.")


def _remove_field(net: dict, field: str) -> None:
    """Drop a field the API rejected as 'extra' — handles both a flat top-level
    key and a dotted path (e.g. netInterfaces.port1.hasCertificates), and also
    sweeps every interface object for a bare leaf key."""
    if "." in field:
        *parents, leaf = field.split(".")
        node = net
        for p in parents:
            if isinstance(node, dict) and p in node:
                node = node[p]
            else:
                node = None
                break
        if isinstance(node, dict):
            node.pop(leaf, None)
        return
    net.pop(field, None)
    for iface in net.get("netInterfaces", {}).values():
        if isinstance(iface, dict):
            iface.pop(field, None)


class APUClient:
    def __init__(self, host: str, scheme: str = "http", verify: bool = True, timeout: int = 15):
        self.base = f"{scheme}://{host}/dshb/v1"
        self.apu_base = f"{scheme}://{host}/apu/v1"   # multi-radar settings (3.1.2+)
        self.s = requests.Session()
        self.s.headers.update({"Content-Type": "application/json"})
        self.s.verify = verify
        self.timeout = timeout

    # --- auth ---------------------------------------------------------------
    def login(self, username: str, password: str) -> None:
        r = self.s.post(f"{self.base}/login",
                        json={"username": username, "password": password},
                        timeout=self.timeout)
        if r.status_code != 200:
            raise MagosError(f"Login failed (HTTP {r.status_code}): {r.text[:300]}")
        log.info("Logged in as '%s'.", username)

    # --- identity -----------------------------------------------------------
    def get_identity(self) -> dict:
        # /systemStatus is where serial+model live on this firmware family (same
        # as the radar on SW 3.x); the rest are best-effort fallbacks.
        return fetch_identity(
            self.s, self.base, self.timeout,
            paths=("/systemStatus", "/system", "/networking", "/phoenix_ip",
                   "/about", "/device", "/info"),
            label="APU",
        )

    # --- NTP + timezone -----------------------------------------------------
    def set_ntp_tz(self, ntp_server: str, timezone: str) -> None:
        cur = self.s.get(f"{self.base}/system", timeout=self.timeout)
        cur.raise_for_status()
        sysinfo = cur.json()
        body = {
            "ntpAutomatic": False,
            "ntpServer": ntp_server or sysinfo.get("ntpServer"),
            "timezone": timezone or sysinfo.get("timezone"),
        }
        r = self.s.post(f"{self.base}/system", json=body, timeout=self.timeout)
        if r.status_code not in (200, 204):
            raise MagosError(f"system (NTP/TZ) update failed (HTTP {r.status_code}): {r.text[:300]}")
        log.info("NTP server = %s, timezone = %s.", body["ntpServer"], body["timezone"])

    # --- controlled radars (firmware 3.1.2+) ---------------------------------
    def set_radars(self, radars: list[dict]) -> None:
        """Assign ALL the APU's controlled radars in one call. Each entry is
        {"radar_id","ip","name"}; POSTing the array replaces any previous
        assignment (the old single-radar /phoenix_ip is gone in 3.1.2)."""
        body = {"radars": [{"radar_id": r["radar_id"],
                            "remote_base_url": radar_base_url(r["ip"]),
                            "name": r["name"]} for r in radars]}
        r = self.s.post(f"{self.apu_base}/settings", json=body, timeout=self.timeout)
        if r.status_code not in (200, 204):
            raise MagosError(f"apu settings (controlled radars) update failed "
                             f"(HTTP {r.status_code}): {r.text[:300]}")
        log.info("Controlled radars set: %s.",
                 ", ".join(f"{x['radar_id']}={x['ip']}" for x in radars))

    # --- networking (do last) ----------------------------------------------
    def set_network(self, iface: str, ip: str, netmask: str, gateway: str, dns: str) -> None:
        cur = self.s.get(f"{self.base}/networking", timeout=self.timeout)
        cur.raise_for_status()
        net = cur.json()
        ifaces = net.get("netInterfaces", {})
        if iface not in ifaces:
            raise MagosError(f"Interface '{iface}' not found. Available: {list(ifaces)}")

        port = ifaces[iface]
        port["ip4Method"] = "manual"
        port["ip4Address"] = ip            # plain IP (no /prefix on the APU)
        port["ip4Netmask"] = netmask
        port["ip4Gateway"] = gateway
        port["ip4OverrideDNS"] = True
        port["ip4DNS"] = [dns]             # primary DNS (first entry)

        log.info("Applying on %s: ip=%s netmask=%s gateway=%s primary-DNS=%s",
                 iface, ip, netmask, gateway, dns)
        log.info("(the connection will drop if the IP changes — this is expected)")

        # Like the radar, the POST schema may forbid extra fields the GET
        # returns. Send it back and drop exactly the fields it rejects, retrying
        # until it accepts (or the IP change drops the connection).
        for _ in range(10):
            try:
                r = self.s.post(f"{self.base}/networking", json=net, timeout=self.timeout)
            except requests.exceptions.RequestException as e:
                log.info("Connection dropped after sending networking change (expected): %s", e)
                return

            if r.status_code in (200, 204):
                log.info("Networking updated.")
                return

            if r.status_code == 400:
                extras = rejected_extra_fields(r)
                if extras:
                    for field in extras:
                        _remove_field(net, field)
                    log.info("Dropping %d field(s) the API rejected as extra: %s",
                             len(extras), ", ".join(sorted(extras)))
                    continue

            raise MagosError(f"Networking update failed (HTTP {r.status_code}): {r.text[:500]}")

        raise MagosError("Networking update failed: too many schema-cleanup retries.")


def prompt_channel() -> tuple[str, list[dict], str]:
    """Ask which APU this is; return (apu_ip, radars, apu_label)."""
    print("Which APU is this?")
    for apu in APU_CHANNEL_IPS:
        radars = " + ".join(f"{r['radar_id']} ({r['ip']})"
                            for r in APU_RADAR_ASSIGNMENTS[apu])
        print(f"  {apu} -> APU {APU_CHANNEL_IPS[apu]} controlling {radars}")
    print("  other -> enter the APU IP manually")

    while True:
        choice = input("APU [0/1/other]: ").strip().lower()
        if choice in APU_CHANNEL_IPS:
            return APU_CHANNEL_IPS[choice], APU_RADAR_ASSIGNMENTS[choice], choice
        if choice in ("other", "o", "manual", "m"):
            while True:
                manual = input("Enter APU IP: ").strip()
                try:
                    ipaddress.ip_address(manual)
                except ValueError:
                    print("  Not a valid IP address — try again.")
                    continue
                raw = input("Enter controlled radar IPs, comma-separated (blank to skip): ").strip()
                ips = [s.strip() for s in raw.split(",") if s.strip()]
                return manual, radars_from_ips(ips), "other"
        print("  Please choose 0, 1, or 'other'.")


def main():
    p = argparse.ArgumentParser(description="Configure Magos APU: NTP/TZ, controlled radars, networking.")
    p.add_argument("--host", default=DEFAULT_HOST,
                   help=f"host:port (default: {DEFAULT_HOST}, the APU factory IP)")
    p.add_argument("--username", default=DEFAULT_USERNAME, help=f"login username (default: {DEFAULT_USERNAME})")
    p.add_argument("--password",
                   help=f"login password (default: {DEFAULT_PASSWORD}; else APU_PASSWORD env)")
    p.add_argument("--scheme", default="http", choices=["http", "https"])
    p.add_argument("--insecure", action="store_true", help="skip TLS verify (https)")

    p.add_argument("--interactive", action="store_true",
                   help="ask which APU this is (0 or 1) and pick the IPs for you")
    p.add_argument("--ntp", default=DEFAULT_NTP, help=f"NTP server IP (default: {DEFAULT_NTP})")
    p.add_argument("--timezone", default=DEFAULT_TIMEZONE, help=f"IANA timezone (default: {DEFAULT_TIMEZONE})")
    p.add_argument("--radar-ips", dest="radar_ips",
                   help="comma-separated controlled radar IPs, assigned IDs "
                        "radar_0, radar_1, ... in order")

    p.add_argument("--iface", default=DEFAULT_IFACE, help=f"network interface (default {DEFAULT_IFACE})")
    p.add_argument("--ip", help="new APU IPv4 address (plain, e.g. 192.168.88.61)")
    p.add_argument("--netmask", default=DEFAULT_NETMASK, help=f"netmask (default {DEFAULT_NETMASK})")
    p.add_argument("--gateway", default=DEFAULT_GATEWAY, help=f"default gateway IP (default {DEFAULT_GATEWAY})")
    p.add_argument("--dns", default=DEFAULT_DNS, help=f"primary DNS IP (default {DEFAULT_DNS})")

    p.add_argument("--yes", action="store_true", help="don't prompt before the network change")
    args = p.parse_args()

    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(LOG_LINE_FORMAT))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    set_log_serial(None)

    pwd = args.password or os.environ.get("APU_PASSWORD")
    if not pwd:
        if sys.stdin.isatty():
            pwd = getpass(f"Password for {args.username} "
                          f"(Enter = factory default '{DEFAULT_PASSWORD}'): ") or DEFAULT_PASSWORD
        else:
            pwd = DEFAULT_PASSWORD

    radars: list[dict] = []
    if args.interactive and not args.ip:
        args.ip, radars, _ = prompt_channel()
    if not radars and args.radar_ips:
        radars = radars_from_ips([s.strip() for s in args.radar_ips.split(",") if s.strip()])

    want_net = args.ip is not None

    client = APUClient(args.host, scheme=args.scheme, verify=not args.insecure)

    try:
        print("Authenticating...")
        client.login(args.username, pwd)

        ident = client.get_identity()
        fw = firmware_version(ident.get("raw", {}))
        if not firmware_ok(fw):
            raise MagosError(firmware_error(fw))

        print("Setting NTP + timezone...")
        client.set_ntp_tz(args.ntp, args.timezone)

        if radars:
            print("Setting controlled radars...")
            client.set_radars(radars)

        if want_net:
            if not args.yes:
                ans = input(f"Apply {args.iface}: IP={args.ip}/{args.netmask}, "
                            f"GW={args.gateway}, DNS={args.dns}? [y/N] ")
                if ans.strip().lower() not in ("y", "yes"):
                    print("Skipped networking change.")
                    print("Done.")
                    return
            print("Updating networking (last step)...")
            client.set_network(args.iface, args.ip, args.netmask, args.gateway, args.dns)
            verify_device_at(args.ip, scheme=args.scheme, username=args.username,
                             password=pwd, expect_substring=args.ip,
                             verify_tls=not args.insecure)
    except MagosError as e:
        sys.exit(f"ERROR: {e}")

    print("Done.")


if __name__ == "__main__":
    main()
