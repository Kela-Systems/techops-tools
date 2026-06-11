#!/usr/bin/env python3
"""
pd_session_timeouts.py — View and configure PagerDuty account session timeouts.

PagerDuty session timeouts are API-only (no UI). This script wraps the
/session_configurations endpoints:
  https://support.pagerduty.com/main/docs/session-timeouts

Usage:
  cp .env.example .env                      # set PAGERDUTY_API_TOKEN
  python3 pd_session_timeouts.py            # show current timeouts (web + mobile)

  # Set timeouts (durations accept 90s, 30m, 12h, 7d, or plain seconds):
  python3 pd_session_timeouts.py --web-idle 30d --web-absolute 90d
  python3 pd_session_timeouts.py --mobile-idle 180d --mobile-absolute 210d

  # Revert a platform to PagerDuty defaults:
  python3 pd_session_timeouts.py --reset web

  WARNING: changing or resetting a platform's configuration immediately
  revokes all existing sessions of that type (everyone on the account gets
  logged out of that platform). The script asks for confirmation; pass
  --yes to skip.

Requires a REST API key from an admin or account owner:
  PagerDuty web app → Integrations → API Access Keys → Create New API Key
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

PD_API_BASE = "https://api.pagerduty.com"
PD_TOKEN = os.environ.get("PAGERDUTY_API_TOKEN", "")

# Allowed ranges in seconds, from the PagerDuty OpenAPI schema.
IDLE_MIN, IDLE_MAX = 60, 15_552_000          # 60s .. 180 days
ABSOLUTE_MIN, ABSOLUTE_MAX = 600, 18_144_000  # 10m .. 210 days

DURATION_RE = re.compile(r"^(\d+)\s*([smhd]?)$", re.IGNORECASE)
UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: str) -> int:
    m = DURATION_RE.match(value.strip())
    if not m:
        raise argparse.ArgumentTypeError(
            f"invalid duration {value!r} — use e.g. 90s, 30m, 12h, 7d, or seconds"
        )
    return int(m.group(1)) * UNIT_SECONDS[m.group(2).lower()]


def humanize(seconds: int) -> str:
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            n, seconds = divmod(seconds, size)
            parts.append(f"{n}{unit}")
    return " ".join(parts) if parts else "0s"


def session() -> requests.Session:
    if not PD_TOKEN:
        sys.exit("PAGERDUTY_API_TOKEN is not set — add it to scripts/.env")
    s = requests.Session()
    s.headers.update(
        {
            "Authorization": f"Token token={PD_TOKEN}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    )
    return s


def get_configurations(s: requests.Session) -> list[dict]:
    resp = s.get(f"{PD_API_BASE}/session_configurations")
    if resp.status_code == 404:
        return []  # no custom configuration — account is on PagerDuty defaults
    resp.raise_for_status()
    return resp.json()["session_configurations"]


def put_configuration(s: requests.Session, platform: str, idle: int, absolute: int) -> list[dict]:
    resp = s.put(
        f"{PD_API_BASE}/session_configurations",
        params={"type": platform},
        json={"session_configuration": {"idle_session_ttl": idle, "absolute_session_ttl": absolute}},
    )
    if not resp.ok:
        sys.exit(f"PUT failed ({resp.status_code}): {resp.text}")
    return resp.json()["session_configurations"]


def delete_configuration(s: requests.Session, platform: str) -> None:
    resp = s.delete(f"{PD_API_BASE}/session_configurations", params={"type": platform})
    if not resp.ok:
        sys.exit(f"DELETE failed ({resp.status_code}): {resp.text}")


def show(configs: list[dict]) -> None:
    if not configs:
        print("No custom session configuration set — account uses PagerDuty defaults")
        print("(historically ~90 days for web sessions, up to 5 years for mobile).")
        return
    print(f"{'platform':<10} {'idle timeout':<22} {'absolute timeout':<22}")
    print("-" * 54)
    for cfg in sorted(configs, key=lambda c: c["type"]):
        idle, absolute = cfg["idle_session_ttl"], cfg["absolute_session_ttl"]
        print(
            f"{cfg['type']:<10} "
            f"{humanize(idle) + f' ({idle}s)':<22} "
            f"{humanize(absolute) + f' ({absolute}s)':<22}"
        )


def confirm(platform: str, action: str, assume_yes: bool) -> None:
    print(f"WARNING: {action} the '{platform}' configuration immediately revokes")
    print(f"all existing {platform} sessions for EVERY user on the account.")
    if assume_yes:
        return
    answer = input("Proceed? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        sys.exit("Aborted.")


def validate(idle: int, absolute: int) -> None:
    if not IDLE_MIN <= idle <= IDLE_MAX:
        sys.exit(f"idle timeout must be {IDLE_MIN}s–{IDLE_MAX}s ({humanize(IDLE_MAX)})")
    if not ABSOLUTE_MIN <= absolute <= ABSOLUTE_MAX:
        sys.exit(f"absolute timeout must be {ABSOLUTE_MIN}s–{ABSOLUTE_MAX}s ({humanize(ABSOLUTE_MAX)})")
    if idle > absolute:
        sys.exit("idle timeout cannot be longer than the absolute timeout")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="View and configure PagerDuty account session timeouts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Durations accept 90s, 30m, 12h, 7d, or plain seconds.\n"
               "With no flags, the current configuration is displayed.",
    )
    parser.add_argument("--web-idle", type=parse_duration, metavar="DUR", help="web idle (inactivity) timeout")
    parser.add_argument("--web-absolute", type=parse_duration, metavar="DUR", help="web absolute (max session) timeout")
    parser.add_argument("--mobile-idle", type=parse_duration, metavar="DUR", help="mobile idle (inactivity) timeout")
    parser.add_argument("--mobile-absolute", type=parse_duration, metavar="DUR", help="mobile absolute (max session) timeout")
    parser.add_argument("--reset", choices=["web", "mobile"], action="append", default=[],
                        help="delete a platform's configuration, reverting to PagerDuty defaults")
    parser.add_argument("--yes", "-y", action="store_true", help="skip confirmation prompts")
    args = parser.parse_args()

    updates = {
        "web": (args.web_idle, args.web_absolute),
        "mobile": (args.mobile_idle, args.mobile_absolute),
    }
    updates = {p: v for p, v in updates.items() if any(x is not None for x in v)}

    for platform in args.reset:
        if platform in updates:
            parser.error(f"--reset {platform} conflicts with setting {platform} timeouts")

    s = session()
    current = {c["type"]: c for c in get_configurations(s)}

    if not updates and not args.reset:
        show(list(current.values()))
        return

    for platform, (idle, absolute) in updates.items():
        # The API requires both TTLs on every PUT; fill the one not given
        # from the existing configuration.
        existing = current.get(platform)
        if idle is None:
            if not existing:
                sys.exit(f"no existing {platform} configuration — specify both "
                         f"--{platform}-idle and --{platform}-absolute")
            idle = existing["idle_session_ttl"]
        if absolute is None:
            if not existing:
                sys.exit(f"no existing {platform} configuration — specify both "
                         f"--{platform}-idle and --{platform}-absolute")
            absolute = existing["absolute_session_ttl"]
        validate(idle, absolute)
        print(f"Setting {platform}: idle={humanize(idle)}, absolute={humanize(absolute)}")
        confirm(platform, "Updating", args.yes)
        put_configuration(s, platform, idle, absolute)
        print(f"{platform} configuration updated.\n")

    for platform in args.reset:
        confirm(platform, "Deleting", args.yes)
        delete_configuration(s, platform)
        print(f"{platform} configuration deleted — back to PagerDuty defaults.\n")

    print("Current configuration:")
    show(get_configurations(s))


if __name__ == "__main__":
    main()
