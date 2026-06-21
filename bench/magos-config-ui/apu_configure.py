#!/usr/bin/env python3
"""
Configure a Magos AR Processing Unit (APU) over its dashboard API.

Verified live against: MSA1588APU "AR Processing Unit (APU)", SW 3.0.1, API v2.
Base = http://<host>/dshb/v1  (auth is session-cookie based; login sets it)

Endpoints (all confirmed on the device):

  POST /login        {"username","password"}            -> sets session cookie

  GET  /system       -> {swComponents, ntpAutomatic, ntpServer, timezone, users}
  POST /system       {"ntpAutomatic","ntpServer","timezone"}  -> NTP + timezone

  GET  /phoenix_ip   -> {"remote_url_base":"<radar ip>", "srv_port":"/tmp/phoenix.sock"}
  POST /phoenix_ip   <same object, remote_url_base changed>   -> set controlled radar

  GET  /networking   -> {"netInterfaces":{"port1":{ip4Method, ip4Address, ip4Netmask,
                          ip4Gateway, ip4DNS[], ip4OverrideDNS, ...}}, ...top-level...}
  POST /networking   <full object, port1 edited>               -> IP / gateway / DNS

Notes specific to the APU (differ from the AR-300 radar):
  * the APU's initial (factory) IP is always 192.168.40.60.
  * networking is PER-INTERFACE (netInterfaces.port1) and ip4Address is a PLAIN ip
    with a SEPARATE ip4Netmask (the radar used CIDR in ip4Address).
  * the "controlled radar" is the `remote_url_base` field of /phoenix_ip.

Channel mapping (mirrors the radar): channel N -> APU 192.168.88.6N controlling
radar 192.168.88.5N. Defaults match a fresh APU, so the common case is:

  python3 apu_configure.py --interactive

Order: NTP/timezone and controlled-radar first; networking LAST, because changing
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

# channel -> APU's own static IP (.6N), mirroring the radar's .5N scheme.
APU_CHANNEL_IPS = {
    "0": "192.168.88.60",
    "1": "192.168.88.61",
    "2": "192.168.88.62",
    "3": "192.168.88.63",
}
# channel -> the radar this APU controls (phoenix_ip remote_url_base) = .5N.
CONTROLLED_RADAR_IPS = dict(RADAR_CHANNEL_IPS)


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
        return fetch_identity(
            self.s, self.base, self.timeout,
            paths=("/system", "/networking", "/phoenix_ip", "/about", "/device", "/info"),
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

    # --- controlled radar ---------------------------------------------------
    def set_controlled_radar(self, radar_ip: str) -> None:
        cur = self.s.get(f"{self.base}/phoenix_ip", timeout=self.timeout)
        cur.raise_for_status()
        cfg = cur.json()                 # {"remote_url_base":..., "srv_port":...}
        cfg["remote_url_base"] = radar_ip
        r = self.s.post(f"{self.base}/phoenix_ip", json=cfg, timeout=self.timeout)
        if r.status_code not in (200, 204):
            raise MagosError(f"phoenix_ip (controlled radar) update failed "
                             f"(HTTP {r.status_code}): {r.text[:300]}")
        log.info("Controlled radar set to %s.", radar_ip)

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


def prompt_channel() -> tuple[str, str, str]:
    """Ask which channel this APU is; return (apu_ip, radar_ip, channel_label)."""
    print("Which channel is this APU?")
    for ch in APU_CHANNEL_IPS:
        print(f"  {ch} -> APU {APU_CHANNEL_IPS[ch]} controlling radar {CONTROLLED_RADAR_IPS[ch]}")
    print("  other -> enter the APU IP manually")

    while True:
        choice = input("Channel [0/1/2/3/other]: ").strip().lower()
        if choice in APU_CHANNEL_IPS:
            return APU_CHANNEL_IPS[choice], CONTROLLED_RADAR_IPS[choice], choice
        if choice in ("other", "o", "manual", "m"):
            while True:
                manual = input("Enter APU IP: ").strip()
                try:
                    ipaddress.ip_address(manual)
                except ValueError:
                    print("  Not a valid IP address — try again.")
                    continue
                radar = input("Enter controlled radar IP (blank to skip): ").strip()
                return manual, radar, "other"
        print("  Please choose 0, 1, 2, 3, or 'other'.")


def main():
    p = argparse.ArgumentParser(description="Configure Magos APU: NTP/TZ, controlled radar, networking.")
    p.add_argument("--host", default=DEFAULT_HOST,
                   help=f"host:port (default: {DEFAULT_HOST}, the APU factory IP)")
    p.add_argument("--username", default=DEFAULT_USERNAME, help=f"login username (default: {DEFAULT_USERNAME})")
    p.add_argument("--password",
                   help=f"login password (default: {DEFAULT_PASSWORD}; else APU_PASSWORD env)")
    p.add_argument("--scheme", default="http", choices=["http", "https"])
    p.add_argument("--insecure", action="store_true", help="skip TLS verify (https)")

    p.add_argument("--interactive", action="store_true",
                   help="ask which channel the APU is (0-3) and pick the IPs for you")
    p.add_argument("--ntp", default=DEFAULT_NTP, help=f"NTP server IP (default: {DEFAULT_NTP})")
    p.add_argument("--timezone", default=DEFAULT_TIMEZONE, help=f"IANA timezone (default: {DEFAULT_TIMEZONE})")
    p.add_argument("--radar-ip", dest="radar_ip", help="controlled radar IP (remote_url_base)")

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

    if args.interactive and not args.ip:
        args.ip, radar, _ = prompt_channel()
        if radar and not args.radar_ip:
            args.radar_ip = radar

    want_net = args.ip is not None

    client = APUClient(args.host, scheme=args.scheme, verify=not args.insecure)

    try:
        print("Authenticating...")
        client.login(args.username, pwd)

        client.get_identity()

        print("Setting NTP + timezone...")
        client.set_ntp_tz(args.ntp, args.timezone)

        if args.radar_ip:
            print("Setting controlled radar...")
            client.set_controlled_radar(args.radar_ip)

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
