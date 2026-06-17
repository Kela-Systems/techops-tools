#!/usr/bin/env python3
"""
Bulk-register OTD500 devices in Teltonika RMS from the same manifest CSV the
bench tool uses — the "layer two" of the hybrid approach.

Once a device is in RMS (matched by serial + LAN MAC), it becomes remotely
manageable the moment it connects. The bench tool (otd_configure.py) now
also registers each device during a normal run, so this script is for bulk
PRE-registration (register the whole manifest up front, before touching the
units). It shares the exact same registration call as the bench tool
(register_in_rms) so there is a single source of truth for the payload/auth.

RMS API:  https://developers.rms.teltonika-networks.com/  (v3-beta)
  base = https://api.rms.teltonika-networks.com
  Auth = Bearer <personal access token>   (rms.api_token in site.config.json)
  The token needs the 'devices:read' + 'devices:write' scopes and the account
  must have 2FA enabled, else the API returns 403.

Usage:
  python3 rms_register.py                       # dry-run from manifest.csv
  python3 rms_register.py --apply               # actually create the devices
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

try:
    import requests  # noqa: F401  (imported so we fail early with a clear message)
except ImportError:
    sys.exit("This script needs 'requests'.  Install it with:  pip install requests")

from teltonika_provision import register_in_rms

BASE_DIR = Path(__file__).resolve().parent


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"{path.name} not found — copy site.config.example.json and fill in rms.api_token.")
    with open(path, "r", encoding="utf-8") as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}


def load_manifest(path: Path) -> list[dict]:
    if not path.exists():
        sys.exit(f"{path.name} not found — copy manifest.example.csv.")
    with open(path, "r", encoding="utf-8") as f:
        lines = [ln for ln in f if not ln.lstrip().startswith("#")]
    rows = []
    for r in csv.DictReader(lines):
        if not r.get("mac"):
            continue
        rows.append({
            "name": "otd-" + (r.get("site_name") or "").strip().lower(),
            "serial": (r.get("serial") or "").strip(),
            "mac": (r.get("mac") or "").strip(),
            "imei": (r.get("imei") or "").strip(),
        })
    return rows


def main():
    p = argparse.ArgumentParser(description="Bulk-register OTD500s in Teltonika RMS.")
    p.add_argument("--config", default=str(BASE_DIR / "site.config.json"))
    p.add_argument("--manifest", default=str(BASE_DIR / "manifest.csv"))
    p.add_argument("--apply", action="store_true", help="actually create devices (default: dry-run)")
    args = p.parse_args()

    cfg = load_config(Path(args.config))
    rms = cfg.get("rms", {}) or {}
    token, company = rms.get("api_token"), rms.get("company_id")
    series = rms.get("device_series", "otd")
    # RMS requires the device password (password_confirmation) at registration.
    # Every unit ends up on the shared default after a bench run, so use that.
    device_password = cfg.get("new_password", "")
    rows = load_manifest(Path(args.manifest))

    print(f"{len(rows)} device(s) from manifest. series={series}")
    if not args.apply:
        for row in rows:
            print(f"  DRY-RUN would register {row['name']:24} serial={row['serial']} mac={row['mac']}")
        print("\nRe-run with --apply to create them in RMS.")
        return

    if not token or not company:
        sys.exit("rms.api_token and rms.company_id are required in site.config.json for --apply.")
    if not device_password:
        print("WARNING: new_password is empty in config — registering without password_confirmation "
              "may be rejected by RMS.")

    ok = 0
    for row in rows:
        if not row["serial"] or not row["mac"]:
            print(f"  SKIP {row['name']}: needs both serial and mac in the manifest.")
            continue
        try:
            # wait=0: bulk pre-registration runs from a machine with its own
            # internet (not through a device), so no need to wait out a modem reset.
            register_in_rms(token, company, name=row["name"], serial=row["serial"],
                            mac=row["mac"], device_password=device_password,
                            device_series=series, wait=0)
            print(f"  OK   {row['name']:24} registered")
            ok += 1
        except SystemExit as e:
            print(f"  FAIL {row['name']:24} {e}")
    print(f"\nRegistered {ok}/{len(rows)}.")


if __name__ == "__main__":
    main()
