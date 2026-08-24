#!/usr/bin/env python3
"""
Notice when an OTD500 fails over to its backup SIM, from RMS.

WHY A POLLER AND NOT AN RMS ALERT
Failover is sticky (`enable_back=0`): a device that moves to slot 2 stays there
until someone puts it back. So the thing worth knowing is not the instant of the
switch but the STATE — "this device is running on its backup SIM and will not
return on its own" — and that is visible in every poll, with a duration attached.
A fire-once event alert cannot tell you that a device has been on its backup SIM
for six days. As of writing, 25 of 117 OTD500s in RMS were already on slot 2,
which is exactly the kind of thing this makes visible.

It also needs no per-device configuration (RMS alerts are configured per device,
and there are 120 of them) and no extra token scope: `sim_slot` comes back in the
same GET /devices this repo already uses for everything else.

WHAT IT COSTS
Three requests per run for the whole fleet, whatever the fleet size, because
/devices pages 100 at a time. RMS gives a company 100,000 requests per 30 days,
so a 15-minute schedule is ~8,600 and a 5-minute one ~26,000.

WHAT IT CANNOT SEE
State at poll time, so a device that fails over and comes back inside one
interval is invisible. Anything RMS itself does not know is also invisible: an
offline device's slot is whatever it last reported, so a switch made while it was
unreachable shows up when it reconnects, not when it happened.

USAGE
    python3 sim_switch_watch.py                  # report, and update the baseline
    python3 sim_switch_watch.py --quiet          # print ONLY when something changed
    python3 sim_switch_watch.py --no-save        # look without moving the baseline
    python3 sim_switch_watch.py --webhook URL    # also POST the report as JSON

The first run has nothing to compare against: it records the baseline and reports
the standing state, without claiming that any of it just happened.

ON A SCHEDULE (every 15 minutes, mail only when something changed)
    */15 * * * * cd /path/to/techops-tools/scripts && .venv/bin/python \
        sim_switch_watch.py --quiet

`--quiet` prints nothing on an uneventful run, so cron stays silent until a
device actually moves. For Slack or Teams, add --webhook with an incoming webhook
URL (or set $SIM_SWITCH_WEBHOOK).
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from sim_audit import fetch_rms_devices

HERE = Path(__file__).resolve().parent
STATE_PATH = HERE / "sim_switch_state.json"

# Only OTD500s carry our sim_switch rules. Both spellings are in the inventory
# ("OTD500" for 117 of them, a bare "OTD" for three not yet fully registered).
MODEL_PREFIX = "OTD"
# RMS device.status: 1 is online. 0 and 2 both mean "not talking to RMS" (they
# differ only in how long it has been), and None is a device that never has.
ONLINE = 1
SLOT_LABELS = {1: "slot 1", 2: "slot 2 (backup)", 3: "slot 3 (eSIM)"}


def slot_label(slot) -> str:
    if slot is None:
        return "no slot reported"
    return SLOT_LABELS.get(slot, f"slot {slot}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def since_text(iso: str) -> str:
    """How long ago, in the coarsest unit that is still informative. A switch
    three weeks old and one from this morning need different reactions."""
    if not iso:
        return "unknown"
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - then).total_seconds()
    if seconds < 90 * 60:
        return f"{int(seconds // 60)} min"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} days"


def snapshot(dev: dict) -> dict:
    """The few fields a failover shows up in, keyed off RMS's own record."""
    return {
        "name": dev.get("name") or "",
        "serial": str(dev.get("serial") or ""),
        "slot": dev.get("sim_slot"),
        "iccid": (dev.get("iccid") or "").strip(),
        "operator": (dev.get("operator") or "").strip(),
        "online": dev.get("status") == ONLINE,
        "last_connection_at": dev.get("last_connection_at") or "",
    }


def compare(devices: list[dict], state: dict) -> tuple[list[dict], list[dict], dict]:
    """(events, on_backup, new_state).

    Keyed by RMS device id, not name or serial: a device can be renamed in RMS
    and a serial can come back empty, but the id is RMS's own identity and is
    what every other call here addresses a device by.
    """
    events: list[dict] = []
    on_backup: list[dict] = []
    fresh: dict = {}

    for dev in devices:
        if not str(dev.get("model") or "").upper().startswith(MODEL_PREFIX):
            continue
        key = str(dev.get("id") or "")
        if not key:
            continue
        now = snapshot(dev)
        was = state.get(key) or {}
        # When the slot has not moved, keep the timestamp from when it first did,
        # so "on the backup SIM" can be reported with an age instead of just a
        # flag. A device already on slot 2 at baseline keeps an empty `since`,
        # which is honestly reported as unknown rather than as "just now".
        since = was.get("since", "")
        if was and was.get("slot") != now["slot"]:
            since = now_iso()
            kind = ("recovered" if now["slot"] == 1
                    else "switched" if was.get("slot") is not None
                    else "reported a slot")
            events.append({**now, "kind": kind, "from": was.get("slot"),
                           "to": now["slot"], "at": since})
        elif was and was.get("iccid") != now["iccid"] and now["iccid"]:
            # Same slot, different SIM: someone swapped a card in the field. Not
            # a failover, but it changes which plan's limit that slot gets.
            events.append({**now, "kind": "sim swapped", "from": was.get("iccid"),
                           "to": now["iccid"], "at": now_iso()})
        elif not was:
            events.append({**now, "kind": "new to the watch", "from": None,
                           "to": now["slot"], "at": now_iso()})

        fresh[key] = {**now, "since": since, "seen": now_iso()}
        if now["slot"] not in (None, 1):
            on_backup.append({**now, "since": since})

    return events, on_backup, fresh


def render(events: list[dict], on_backup: list[dict], first_run: bool) -> str:
    lines: list[str] = []
    moves = [e for e in events if e["kind"] != "new to the watch"]

    if first_run:
        lines.append(f"First run: recorded {len(events)} OTD device(s) as the baseline. "
                     f"Nothing below just happened — it is the state as RMS has it.")
    elif moves:
        lines.append(f"{len(moves)} change(s) since the last run:")
        for e in sorted(moves, key=lambda e: e["name"]):
            where = "" if e["online"] else "  (offline in RMS — reported on reconnect)"
            if e["kind"] == "sim swapped":
                detail = f"SIM swapped in {slot_label(e['slot'])}, now {e['operator']}"
            else:
                detail = (f"{e['kind']}: {slot_label(e['from'])} -> "
                          f"{slot_label(e['to'])}, now {e['operator'] or 'no operator'}")
            lines.append(f"  {e['name']:<34} {detail}{where}")
        added = [e for e in events if e["kind"] == "new to the watch"]
        if added:
            lines.append(f"  ({len(added)} device(s) new to the watch, "
                         f"recorded without comment)")
    else:
        lines.append("No slot changes since the last run.")

    if on_backup:
        lines.append("")
        lines.append(f"{len(on_backup)} device(s) currently NOT on slot 1. Failover is "
                     f"sticky, so these stay put until someone moves them:")
        for d in sorted(on_backup, key=lambda d: (not d["online"], d["name"])):
            state = "online" if d["online"] else f"offline since {d['last_connection_at']}"
            age = (f"for {since_text(d['since'])}" if d["since"]
                   else "since before this watch started")
            lines.append(f"  {d['name']:<34} {slot_label(d['slot']):<18} "
                         f"{d['operator'] or '-':<14} {age}, {state}")
    return "\n".join(lines)


def slack_payload(text: str) -> dict:
    """The report as an incoming-webhook body.

    Slack renders `text` as mrkdwn, which collapses runs of spaces — the device
    table would arrive as a wall of words with its columns gone. Everything but
    the first line therefore goes inside a code fence, and the first line stays
    outside it so the channel list and the push notification show something
    readable instead of a backtick. Teams and Mattermost accept the same body.
    """
    head, _, rest = text.partition("\n")
    if not rest.strip():
        return {"text": head}
    # Only blank lines are trimmed, never leading spaces: a plain strip() would
    # eat the first row's indent and knock that one line out of column.
    return {"text": f"*{head}*\n```\n{rest.strip(chr(10))}\n```"}


def notify(url: str, text: str) -> None:
    """POST the report to an incoming webhook. A failure here is reported but
    must not lose the report itself, which has already gone to stdout."""
    try:
        resp = requests.post(url, json=slack_payload(text), timeout=30)
        if resp.status_code >= 300:
            print(f"webhook returned HTTP {resp.status_code}: {resp.text[:200]}",
                  file=sys.stderr)
    except requests.RequestException as exc:
        print(f"webhook failed: {type(exc).__name__}: {exc}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quiet", action="store_true",
                    help="print only when something changed (for cron)")
    ap.add_argument("--no-save", action="store_true",
                    help="do not move the baseline, so the same changes report again")
    ap.add_argument("--webhook", default=os.environ.get("SIM_SWITCH_WEBHOOK", ""),
                    help="also POST the report as JSON (Slack/Teams incoming webhook)")
    ap.add_argument("--state", default=str(STATE_PATH), help="where the baseline lives")
    opts = ap.parse_args()

    if not os.environ.get("RMS_API_TOKEN"):
        raise SystemExit(f"Needs an RMS API token: put RMS_API_TOKEN=... in "
                         f"{HERE / '.env'} (see .env.example) or export it.")

    state_path = Path(opts.state)
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}

    # fetch_rms_devices narrates its pagination, which would make every --quiet
    # run mail its progress. Held back and printed only if there is a report.
    chatter = io.StringIO()
    with contextlib.redirect_stdout(chatter):
        devices = fetch_rms_devices()

    events, on_backup, fresh = compare(devices, state)
    report = render(events, on_backup, first_run=not state)
    moved = any(e["kind"] != "new to the watch" for e in events)

    if not opts.quiet or moved or not state:
        print(chatter.getvalue(), end="")
        print(report)
    if opts.webhook and (moved or not state):
        notify(opts.webhook, report)
    if not opts.no_save:
        state_path.write_text(json.dumps(fresh, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
