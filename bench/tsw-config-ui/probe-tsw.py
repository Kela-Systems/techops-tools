#!/usr/bin/env python3
"""Read back the device assumptions this tool was written against (TEC-791).

Every TSW202 fact in `tsw_configure.py` came from Teltonika's documentation
rather than a switch on a bench, and the TSW2 firmware line is a thinner build
than the RUTM/OTD RutOS. This asks a real unit whether each assumption holds,
and changes nothing on the device — every command is a read.

    ../.venv/bin/python probe-tsw.py [host] [--password PW]

Defaults to the host in config/tsw.config.json (or 192.168.1.2) and the shared
password; pass the label password for a factory-fresh unit.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

from bench_core import (
    shared_new_password,
    DEFAULT_SCHEME,
    DEFAULT_USERNAME,
    load_settings,
)

from tsw_configure import DEFAULT_TSW_HOST, TswClient

VERDICTS = {True: "  OK  ", False: " FAIL ", None: " ???  "}


def main() -> int:
    p = argparse.ArgumentParser(description="Probe a TSW202's UCI/ubus surface.")
    p.add_argument("host", nargs="?", help="switch address (default: from the config)")
    p.add_argument("--password", help="admin password (default: the shared one)")
    args = p.parse_args()

    system_show = ""
    settings = {}
    config = BASE_DIR / "config" / "tsw.config.json"
    if config.exists():
        settings = load_settings(str(config))
    host = args.host or settings.get("host", DEFAULT_TSW_HOST)
    password = args.password or shared_new_password(settings)

    print(f"Probing {host} (read-only) ...\n")
    client = TswClient(host=host,
                       username=settings.get("username", DEFAULT_USERNAME),
                       scheme=settings.get("scheme", DEFAULT_SCHEME),
                       verify=not settings.get("insecure", True))
    findings: list[tuple[bool | None, str]] = []
    try:
        client.login(password)

        # 1. Is the model readable, and from where? assert_device_model refuses
        #    to provision a device whose model it cannot read.
        mnf = client.ssh_exec("ubus call mnfinfo get 2>/dev/null", check=False).strip()
        board = client._board_model()
        identity = client.get_identity()
        findings.append((bool(mnf), f"ubus call mnfinfo get -> "
                                    f"{mnf[:70] or '(nothing — the fallback carries it)'}"))
        findings.append((bool(board), f"board model -> {board or '(nothing)'}"))
        findings.append((identity["model"] != "unknown",
                         f"resolved model -> {identity['model']} "
                         f"(serial {identity['serial']}, MAC {identity['mac']})"))

        # 2. Is NTP at system.ntp, or a separate ntpclient package? The first
        #    real unit accepted our writes to system.ntp.* and went on using
        #    four Google servers on UTC, so the whole `system` package is dumped
        #    below: whichever section the WebUI's "Time servers" table really
        #    reads is in there, and guessing it again is how we got here.
        ntp_section = client.ssh_exec("uci -q get system.ntp", check=False).strip()
        servers = client.configured_ntp_servers()
        configs = client.ssh_exec("ls /etc/config", check=False).split()
        findings.append((bool(ntp_section),
                         f"system.ntp section -> {ntp_section or '(absent — it gets created)'}"))
        findings.append((None, "every server/hostname option in `system` -> "
                               f"{', '.join(servers) or '(none)'}"))
        findings.append(("ntpclient" not in configs,
                         f"/etc/config -> {' '.join(configs) or '(unreadable)'}"
                         + ("  <-- an ntpclient package would mean the NTP paths "
                            "are wrong" if "ntpclient" in configs else "")))

        # 3. Did the timezone actually reach the clock? `date +%z` is the only
        #    answer that doesn't just echo the option we wrote.
        offset = client.effective_utc_offset()
        findings.append((offset not in ("", "+0000"),
                         f"clock offset (date +%z) -> {offset or '(unreadable)'}"
                         + ("   <-- still on UTC" if offset == "+0000" else "")))
        findings.append((None, "device clock -> "
                         + client.ssh_exec("date", check=False).strip()))
        # The WebUI's Date & Time dropdown renders this one, not the timeserver
        # section's copy. A blank here is a switch that shows UTC on a correct
        # clock, one Save & Apply away from losing the clock too.
        shown = client.ssh_exec("uci -q get system.system.zoneName",
                                check=False).strip()
        findings.append((bool(shown), "zone the WebUI renders "
                         f"(system.system.zoneName) -> {shown or '(unset)'}"))

        # 4. Which network section carries the management address? On a RutOS
        #    router it is `lan`; the first TSW202 on the bench answered
        #    `uci: Invalid argument` to network.lan, so this is discovered.
        addresses = client.network_addresses()
        section = client.mgmt_section(addresses)
        findings.append((bool(addresses), "addressed network sections -> "
                         + (", ".join(f"{s}={ip}" for s, ip in addresses.items())
                            or "(none — the address is configured elsewhere)")))
        findings.append((bool(section),
                         f"management section for {host} -> "
                         f"network.{section or '(ambiguous — the move refuses)'}"
                         + ("" if section == "lan" else "   <-- not `lan`")))
        if section:
            shown = client.ssh_exec(f"uci show network.{section} 2>/dev/null",
                                    check=False)
            for option in ("netmask", "gateway"):
                findings.append((f".{option}=" in shown,
                                 f"network.{section}.{option} already set -> "
                                 f"{f'.{option}=' in shown}"))

        print(f"version: {client.ssh_exec('cat /etc/version', check=False).strip()}\n")
        system_show = client.ssh_exec("uci show system 2>/dev/null", check=False)
    except SystemExit as e:
        print(f"Could not probe: {e}", file=sys.stderr)
        return 1
    finally:
        client.close()

    for ok, line in findings:
        print(f"[{VERDICTS[ok]}] {line}")

    if system_show:
        print("\n── uci show system ─────────────────────────────────────────")
        print(system_show.strip())
        print("── end ────────────────────────────────────────────────────")

    failures = [line for ok, line in findings if ok is False]
    print(f"\n{len(findings) - len(failures)} of {len(findings)} assumptions hold.")
    if failures:
        print("Anything FAILing above needs the matching path in tsw_configure.py "
              "changed before this tool is trusted on a real unit.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
