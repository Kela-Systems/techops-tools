#!/usr/bin/env python3
"""
probe_sims.py — Diagnostic: discover how to read all SIM slot ICCIDs on an OTD500.

Connects to one device over SSH and tries every known method:

  1. /etc/config/simcard  — UCI config stores ICCID of every slot seen historically ✔
  2. gsm.modem0 info      — active SIM's ICCID + simcount (same as RMS API)
  3. gsm.modem0 get_sim_stat  — per-slot presence / state
  4. gsm.modem0 get_iccid {"sim": N}  — per-slot ICCID (returns active slot for all N)
  5. AT commands via exec (QUIMSLOT?, QSIMSTAT?, QCCID)
  6. esim.modem0          — eSIM slot data

Requires: paramiko (pip install paramiko)

Usage:
  python3 probe_sims.py otd-kela-fob-01 --pass Kelasys123!
  python3 probe_sims.py 192.168.1.1 --user root --key ~/.ssh/id_rsa
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional


def require_paramiko():
    try:
        import paramiko  # noqa: F401
        return paramiko
    except ImportError:
        print("ERROR: paramiko is required. Install with: pip install paramiko", file=sys.stderr)
        sys.exit(1)


def make_client(host: str, user: str, port: int, key: Optional[str], password: Optional[str]):
    paramiko = require_paramiko()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs: dict = {"hostname": host, "username": user, "port": port, "timeout": 15}
    if key:
        kwargs["key_filename"] = key
    elif password:
        kwargs["password"] = password
    else:
        kwargs["look_for_keys"] = True
        kwargs["allow_agent"] = True
    client.connect(**kwargs)
    return client


def run(client, cmd: str, timeout: int = 15) -> tuple[str, int]:
    _, stdout, _ = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    rc = stdout.channel.recv_exit_status()
    return out, rc


def section(title: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


def show(label: str, raw: str, rc: int) -> Optional[dict]:
    if rc != 0 or not raw:
        print(f"  {label}: ✗ (rc={rc})")
        return None
    try:
        data = json.loads(raw)
        print(f"  {label}: ✔")
        print(json.dumps(data, indent=4))
        return data
    except json.JSONDecodeError:
        print(f"  {label}: ✔ (raw text)")
        print(f"    {raw[:400]}")
        return None


def probe(host: str, user: str, port: int, key: Optional[str], password: Optional[str]) -> None:
    client = make_client(host, user, port, key, password)
    print(f"\n🔍 Connected to {user}@{host}:{port}\n")

    # ── 1. /etc/config/simcard (gold standard) ────────────────────────────────
    section("1. /etc/config/simcard  ← BEST METHOD: historical ICCID per slot")
    out, rc = run(client, "uci show simcard 2>/dev/null")
    if rc == 0 and out:
        print(out)
        # Quick parse
        import re
        slot_iccids: dict[str, str] = {}
        current = None
        for line in out.splitlines():
            m = re.match(r"simcard\.@sim\[(\d+)\]=", line)
            if m:
                current = m.group(1)
                continue
            if current:
                m2 = re.match(r"simcard\.@sim\[\d+\]\.(\w+)='?([^']*)'?", line)
                if m2 and m2.group(1) == "iccid":
                    slot_iccids[current] = m2.group(2)
        if slot_iccids:
            print(f"\n  ★ ICCIDs by slot index: {slot_iccids}")
    else:
        print(f"  ✗ (rc={rc})")

    # ── 2. gsm.modem0 info ───────────────────────────────────────────────────
    section("2. gsm.modem0 info  (active SIM only)")
    out, rc = run(client, "ubus call gsm.modem0 info 2>/dev/null")
    info = show("gsm.modem0 info", out, rc)
    simcount = 3
    if info:
        cache = info.get("cache", {})
        simcount = info.get("simcount", 3)
        print(f"\n  → active slot={cache.get('sim')}  simcount={simcount}  ICCID={cache.get('iccid')}")

    # ── 3. get_sim_stat ──────────────────────────────────────────────────────
    section("3. gsm.modem0 get_sim_stat")
    out, rc = run(client, "ubus call gsm.modem0 get_sim_stat 2>/dev/null")
    show("get_sim_stat", out, rc)

    # ── 4. get_iccid per slot ────────────────────────────────────────────────
    section(f"4. gsm.modem0 get_iccid per slot  (simcount={simcount})")
    for slot in range(1, simcount + 1):
        out, rc = run(client, f"ubus call gsm.modem0 get_iccid '{{\"sim\": {slot}}}' 2>/dev/null")
        data = show(f"  get_iccid slot={slot}", out, rc)
        if data and data.get("iccid"):
            print(f"  → {data['iccid']}")

    # ── 5. AT commands ───────────────────────────────────────────────────────
    section("5. AT commands via gsm.modem0 exec")
    for at_cmd in ["AT+QUIMSLOT?", "AT+QSIMSTAT?", "AT+QCCID"]:
        payload = json.dumps({"command": at_cmd, "timeout": 5})
        out, rc = run(client, f"ubus call gsm.modem0 exec '{payload}' 2>/dev/null")
        show(at_cmd, out, rc)

    # ── 6. esim.modem0 ──────────────────────────────────────────────────────
    section("6. esim.modem0")
    for cmd in ["ubus call esim.modem0 info 2>/dev/null",
                "ubus call esim.modem0 get_profiles 2>/dev/null"]:
        out, rc = run(client, cmd)
        show(cmd, out, rc)

    client.close()
    print("\n✔ Done.\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe SIM slot ICCIDs on a Teltonika OTD500 via SSH",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("host", help="SSH hostname or IP of the device")
    parser.add_argument("--user", default="root", help="SSH username (default: root)")
    parser.add_argument("--port", type=int, default=22, help="SSH port (default: 22)")
    parser.add_argument("--key", default=None, help="Path to SSH private key")
    parser.add_argument("--pass", dest="password", default=None, help="SSH password")
    args = parser.parse_args()

    probe(args.host, args.user, args.port, args.key, args.password)


if __name__ == "__main__":
    main()
