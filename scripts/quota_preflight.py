#!/usr/bin/env python3
"""
Read-only pre-flight for the TEC-359 fleet rollout (SIM failover + per-operator
data limits), sourced entirely from Tobee.

Answers the one question the bench cannot: if `install_quota_sync` ran against
every deployed OTD500 right now, which SIMs would have their data cut the moment
the script wrote its limits?

That risk is real because the limit is enforced, not advisory. quota_limit with
`enabled=1` and a limit below what the SIM has already spent this period cuts the
data, and the sim_switch rules use `data_limit` as a failover trigger — so such a
device fails over on the spot, stickily (`enable_back=0`). If both its SIMs are
over, it has no uplink and will not recover on its own.

Tobee's /sims page already joins everything needed: RMS inventory, per-slot
ICCIDs (including standby slots read from UCI), the Droam plan and this period's
usage, firmware, and tailnet pairing. So this needs no credentials, touches no
device, and cannot write anything anywhere — it is one HTTP GET plus arithmetic.

For every OTD500 it reports, per managed slot (1 and 2 — the eSIM slot is left
unlimited by design):
  * the ICCID in it and the operator row its prefix matches, using bench_core's
    own table and matching order, so the prediction is what the device would do
  * the limit and reset day it would be given, and whether it would be enforced
  * this period's usage from Tobee, and the verdict:
        WOULD-CUT   already at or over the limit it would be given
        AT-RISK     at 80% or more of it
        UNKNOWN     no usage figure to compare against
        OK          headroom, or no limit would be enforced
  * whether the prefix matched nothing, so the SIM takes the fallback limit

One caveat worth keeping in mind: quota_limit enforces against the device's own
counter (it calls `ubus call mdcollect get_raw_total`), while Tobee reports the
carrier-side figure from Droam. They normally agree closely, but a SIM moved
between devices or slots mid-period will have spent data this device never
counted, so treat WOULD-CUT as "check this one" rather than a certainty.

Usage:
    python3 quota_preflight.py                      # whole OTD500 fleet
    python3 quota_preflight.py --only kela-fob-27   # one device (substring)
    python3 quota_preflight.py --html saved.html    # re-parse a saved page
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
# bench_core owns the operator table semantics; import it rather than reimplement
# the prefix matching, or this report would drift from what the device does.
sys.path.insert(0, str(REPO / "bench" / "bench-core" / "src"))

from bench_core import (  # noqa: E402
    ESIM_SLOT,
    SIM_SLOTS,
    VERIFIED_SIM_SWITCH_FW,
    fw_carries_version,
    quota_operators,
    quota_unknown_operator,
)

DEFAULT_CONFIG = REPO / "bench" / "otd-config-ui" / "config" / "site.config.json"
EXAMPLE_CONFIG = REPO / "bench" / "otd-config-ui" / "config" / "site.config.example.json"
DEFAULT_TOBEE = "http://tobee:8090"

AT_RISK_PCT = 80.0
# Tobee renders binary units (a "1.5TB" plan shows as 1536.0 GB), and device
# limits are binary MB too, so everything below is MiB and the two are comparable.
_UNIT_MIB = {"KB": 1 / 1024, "MB": 1.0, "GB": 1024.0, "TB": 1024.0 * 1024}


# --- the config table -------------------------------------------------------

def load_table(path: Path) -> tuple[list[dict], dict]:
    """(operators, unknown_operator) from a site.config.json's sim_switch block."""
    cfg = json.loads(path.read_text()).get("sim_switch") or {}
    if not cfg:
        raise SystemExit(f"{path} has no sim_switch block.")
    return quota_operators(cfg), quota_unknown_operator(cfg)


def match_operator(iccid: str, operators: list[dict], unknown: dict) -> tuple[str, dict]:
    """(name, row) for an ICCID — first prefix match wins, exactly like the case
    arms in the generated script, where arm order decides."""
    digits = "".join(c for c in (iccid or "") if c.isdigit())
    for op in operators:
        if any(digits.startswith(p) for p in op["iccid_prefixes"]):
            return op["name"], op
    return "unknown", unknown


# --- Tobee ------------------------------------------------------------------

def fetch_page(url: str, timeout: int = 60) -> str:
    try:
        resp = requests.get(url, timeout=timeout)
    except requests.RequestException as exc:
        raise SystemExit(f"Cannot reach Tobee at {url}: {type(exc).__name__}. "
                         f"On the tailnet? Try --html with a saved page.")
    if resp.status_code != 200:
        raise SystemExit(f"Tobee returned HTTP {resp.status_code} for {url}")
    return resp.text


def _plain(chunk: str) -> str:
    """Markup to line-per-row text, with table cells separated by ' | '."""
    text = re.sub(r"<(tr|div|p|h\d|li|br)\b[^>]*/?>", "\n", chunk)
    text = re.sub(r"</t[dh]>", " | ", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def _field(text: str, label: str) -> str:
    """The value of a `Label | value |` detail row."""
    m = re.search(rf"^{re.escape(label)} \| (.+?)(?: \||$)", text, flags=re.M)
    return m.group(1).strip() if m else ""


def clean_iccid(raw: str) -> str:
    """An ICCID only if it really is one.

    Tobee prints 'N/A' (and occasionally '-') for a slot whose ICCID it could not
    read. Taken literally that looks like a SIM belonging to an operator we have
    never heard of, which is a different problem with a different fix, so anything
    without a plausible ICCID's worth of digits is treated as absent.
    """
    raw = (raw or "").strip()
    return raw if len(re.sub(r"\D", "", raw)) >= 18 else ""


def parse_usage(line: str) -> dict:
    """
    Tobee's usage sentence -> {used_mib, plan_mib, kind}.

    Seen in the wild:
        '354.5 GB used this period of 1536.0 GB (23.1%)'
        '11 MB used this period of 1536.0 GB (0.0%)'
        '0 MB used this period (pay-per-MB plan — no cap)'
        ''                                        (no Droam match for the SIM)
    """
    if not line:
        return {"used_mib": None, "plan_mib": None, "kind": "none"}
    used = re.search(r"([\d.]+)\s*(KB|MB|GB|TB)\s+used", line)
    plan = re.search(r"of\s+([\d.]+)\s*(KB|MB|GB|TB)", line)
    kind = "uncapped" if "pay-per-MB" in line else ("metered" if plan else "none")
    return {
        "used_mib": float(used.group(1)) * _UNIT_MIB[used.group(2)] if used else None,
        "plan_mib": float(plan.group(1)) * _UNIT_MIB[plan.group(2)] if plan else None,
        "kind": kind,
    }


def parse_devices(page: str) -> list[dict]:
    """Every device row on Tobee's /sims page, with its detail panel parsed.

    The page is server-rendered with one `<tr class=simrow>` per device carrying
    data-* attributes (model, status, ...) followed by a detail panel holding the
    device fields and a `Slot N` block per SIM.
    """
    body = re.sub(r"<(style|script)\b.*?</\1>", "", page, flags=re.S)
    marks = [m.start() for m in re.finditer(r"<tr class=simrow", body)]
    if not marks:
        raise SystemExit("No device rows found — has Tobee's /sims markup changed?")

    devices = []
    for start, end in zip(marks, marks[1:] + [len(body)]):
        block = body[start:end]
        attrs = dict(re.findall(r"(data-[a-z-]+)='([^']*)'", block))
        text = _plain(block)
        words = (attrs.get("data-text") or "").split()

        slots = []
        parts = re.split(r"^Slot (\d)\b", text, flags=re.M)
        for i in range(1, len(parts), 2):
            chunk = parts[i + 1]
            usage_line = ""
            m = re.search(r"^Data usage \|?\s*$\n(.+)$", chunk, flags=re.M)
            if m:
                usage_line = m.group(1).strip().rstrip("|").strip()
            raw_iccid = _field(chunk, "ICCID")
            slots.append({
                "slot": int(parts[i]),
                "iccid": clean_iccid(raw_iccid),
                "iccid_raw": raw_iccid,
                "plan": _field(chunk, "Droam plan"),
                "droam_status": _field(chunk, "Droam status"),
                "sim_state": _field(chunk, "SIM state"),
                "standby": "standby" in _field(chunk, "Source").lower(),
                "usage_text": usage_line,
                **parse_usage(usage_line),
            })

        devices.append({
            "name": words[0] if words else "",
            "model_tag": attrs.get("data-model", ""),
            "model": _field(text, "Model"),
            "site": _field(text, "RMS tags"),
            "serial": _field(text, "Serial"),
            "firmware": _field(text, "Firmware"),
            "status": attrs.get("data-status", ""),
            "last_seen": _field(text, "Last seen"),
            "tailscale": _field(text, "Tailscale"),
            "tailscale_ip": _field(text, "Tailscale IP"),
            "slots": slots,
        })
    return devices


# --- the assessment ---------------------------------------------------------

def verdict_for(used_mib, limit_mib: int, enforced: bool) -> tuple[str, str]:
    if not enforced:
        return "OK", "no limit would be enforced"
    if used_mib is None:
        return "UNKNOWN", "no usage figure to compare"
    if used_mib >= limit_mib:
        return "WOULD-CUT", f"{_gb(used_mib)} used vs a {_gb(limit_mib)} limit"
    pct = used_mib / limit_mib * 100 if limit_mib else 0.0
    if pct >= AT_RISK_PCT:
        return "AT-RISK", f"{pct:.0f}% of the limit already used"
    return "OK", f"{pct:.0f}% of the limit used ({_gb(used_mib)})"


def _gb(mib) -> str:
    if mib is None:
        return "-"
    return f"{mib / 1024:,.1f} GB" if mib >= 1024 else f"{mib:,.0f} MB"


_ORDER = {"WOULD-CUT": 0, "UNKNOWN": 1, "AT-RISK": 2, "OK": 3}


def assess(dev: dict, operators: list[dict], unknown: dict) -> dict:
    """One report row per device, with a row per managed slot."""
    by_slot = {s["slot"]: s for s in dev["slots"]}
    rows = []
    for slot in SIM_SLOTS:
        sim = by_slot.get(slot) or {}
        iccid = (sim.get("iccid") or "").strip()
        if not iccid:
            # No ICCID to match on means the fallback row applies, which is
            # harmless for an empty slot — there is no SIM to cut. It only matters
            # when a card IS present and we cannot identify it, so trust the
            # reported SIM state over the missing ICCID.
            state = (sim.get("sim_state") or "").lower()
            unreadable = bool(sim.get("iccid_raw")) and "not inserted" not in state
            rows.append({
                "slot": slot, "iccid": "", "iccid_raw": sim.get("iccid_raw", ""),
                "operator": "unknown (ICCID unreadable)" if unreadable
                            else "unknown (empty slot)",
                "would_limit_mib": unknown["data_limit_mb"],
                "would_reset_day": unknown["reset_day"],
                "would_enforce": unknown["enabled"],
                "used_mib": None, "plan_mib": None, "plan": "",
                "usage_kind": "none", "usage_text": "",
                "droam_status": sim.get("droam_status", ""),
                "sim_state": sim.get("sim_state", ""),
                "verdict": "UNKNOWN" if unreadable and unknown["enabled"] else "OK",
                "why": ("ICCID unreadable — would take the enforced fallback"
                        if unreadable else
                        f"no SIM in the slot{' (' + sim['sim_state'] + ')' if sim.get('sim_state') else ''}"),
                "matched_prefix": False, "standby": False,
            })
            continue
        name, row = match_operator(iccid, operators, unknown)
        verdict, why = verdict_for(sim.get("used_mib"), row["data_limit_mb"],
                                   row["enabled"])
        rows.append({
            "slot": slot,
            "iccid": iccid,
            "operator": name,
            "would_limit_mib": row["data_limit_mb"],
            "would_reset_day": row["reset_day"],
            "would_enforce": row["enabled"],
            "used_mib": sim.get("used_mib"),
            "plan_mib": sim.get("plan_mib"),
            "plan": sim.get("plan", ""),
            "usage_kind": sim.get("kind", "none"),
            "usage_text": sim.get("usage_text", ""),
            "droam_status": sim.get("droam_status", ""),
            "sim_state": sim.get("sim_state", ""),
            "verdict": verdict,
            "why": why,
            "matched_prefix": name != "unknown",
            "standby": bool(sim.get("standby")),
        })

    # What a human would have to look at before pushing to this device.
    checks = []
    for row in rows:
        if row["verdict"] == "WOULD-CUT":
            checks.append(f"slot {row['slot']}: over the cap "
                          f"({_gb(row['used_mib'])} vs {_gb(row['would_limit_mib'])})")
        elif row["verdict"] == "AT-RISK":
            checks.append(f"slot {row['slot']}: close to the cap ({row['why']})")
        elif row["verdict"] == "UNKNOWN" and row["iccid"]:
            checks.append(f"slot {row['slot']}: no usage figure ({row['operator']})")
        elif row["verdict"] == "UNKNOWN":
            checks.append(f"slot {row['slot']}: ICCID unreadable")

    firmware = dev["firmware"]
    firmware_verified = bool(firmware) and fw_carries_version(
        firmware, VERIFIED_SIM_SWITCH_FW)
    flags = []
    if not firmware:
        flags.append("firmware unknown")
    elif not firmware_verified:
        flags.append(f"firmware {firmware.split('_')[-1]}")
    if any(r["iccid"] and not r["matched_prefix"] for r in rows):
        flags.append("unmatched prefix")
    # A cap above the plan can never fire: the carrier runs out first.
    if any(r["would_enforce"] and r["plan_mib"] and r["would_limit_mib"] > r["plan_mib"]
           for r in rows):
        flags.append("cap above plan")
    if any(r["usage_kind"] == "uncapped" and r["would_enforce"] for r in rows):
        flags.append("capping a pay-per-MB SIM")
    if not dev["tailscale_ip"]:
        flags.append("no tailnet address")
    esim = by_slot.get(ESIM_SLOT)
    return {
        "name": dev["name"],
        "site": dev["site"],
        "serial": dev["serial"],
        "model": dev["model"] or dev["model_tag"].upper(),
        "status": dev["status"],
        "last_seen": dev["last_seen"],
        "firmware": firmware,
        "firmware_verified": firmware_verified,
        "online": dev["status"] == "online",
        "tailscale_ip": dev["tailscale_ip"],
        "esim_present": bool(esim and esim.get("iccid")),
        "flags": flags,
        "checks": checks,
        "slots": rows,
        "worst": min((r["verdict"] for r in rows),
                     key=lambda v: _ORDER.get(v, 9), default="OK"),
    }


# --- reporting --------------------------------------------------------------

def flag_duplicate_iccids(rows: list[dict]) -> int:
    """Mark slots whose ICCID is reported on more than one device.

    A physical SIM can only be in one device, so these are stale RMS records. The
    device itself reads its real ICCID at runtime and will do the right thing
    either way — but the prediction for such a slot may describe a SIM that is
    not there, so it should not be read as confidently as the rest.
    """
    where: dict[str, list[str]] = {}
    for dev in rows:
        for slot in dev["slots"]:
            if slot["iccid"]:
                where.setdefault(slot["iccid"], []).append(dev["name"])
    affected = 0
    for dev in rows:
        for slot in dev["slots"]:
            others = [n for n in where.get(slot["iccid"], []) if n != dev["name"]]
            slot["iccid_also_on"] = others
            if others:
                affected += 1
                note = f"ICCID also on {len(others)} other device(s)"
                if note not in dev["flags"]:
                    dev["flags"].append(note)
                dev["checks"].append(f"slot {slot['slot']}: same ICCID reported on "
                                     + ", ".join(others))
    return affected


def assign_groups(rows: list[dict]) -> None:
    """Sort every device into the one bucket that decides what happens to it next.

    Offline first: nothing can be pushed to a device we cannot reach, and that is
    also where the never-deployed spares live. Then firmware, because the
    sim_switch option names were only verified on one version and a device on
    another needs checking before anything is written to it at all. Then the
    per-SIM checks, which are about data being cut rather than about the write
    succeeding.
    """
    for dev in rows:
        if not dev["online"]:
            dev["group"] = "offline"
        elif not dev["firmware"] or not dev["firmware_verified"]:
            dev["group"] = "firmware"
        elif dev["checks"]:
            dev["group"] = "checks"
        else:
            dev["group"] = "ready"


def slot_summary(dev: dict) -> str:
    """The managed slots in one short phrase: `s1 cellcom 24% · s2 empty`."""
    parts = []
    for row in dev["slots"]:
        if not row["iccid"]:
            parts.append(f"s{row['slot']} empty")
            continue
        if not row["would_enforce"]:
            parts.append(f"s{row['slot']} {row['operator']} (no cap)")
        elif row["used_mib"] is None:
            parts.append(f"s{row['slot']} {row['operator']} usage unknown")
        else:
            pct = row["used_mib"] / row["would_limit_mib"] * 100
            parts.append(f"s{row['slot']} {row['operator']} {pct:.0f}% of cap")
    return " · ".join(parts)


def _fw(dev: dict) -> str:
    return dev["firmware"].split("_")[-1].replace("00.", "") or "unknown"


def write_markdown(report: dict, path: Path) -> None:
    t = report["totals"]
    g = report["groups"]

    def group(name: str) -> list[dict]:
        return sorted((d for d in report["devices"] if d["group"] == name),
                      key=lambda d: d["name"])

    lines = [
        "# TEC-359 rollout pre-flight",
        "",
        f"Generated {report['generated_at']} from {report['source']} — "
        "**read-only**, no device was touched.",
        f"Operator table: `{report['config']}`",
        "",
        f"{t['devices']} OTD devices, in the order they need attention:",
        "",
        f"- **{g['ready']} ready and online** — push to these",
        f"- **{g['firmware']} online but on an unverified firmware** — check the "
        f"`sim_switch` option names first",
        f"- **{g['checks']} online, right firmware, but something to look at** — "
        f"mostly SIMs with no usage figure",
        f"- **{g['offline']} not reachable** ({t['offline']} offline, "
        f"{t['unknown_status']} no recent contact) — nothing to do until they are back",
        "",
        "## Ready and online",
        "",
        "| Device | Site | Firmware | Managed slots | Reach it via |",
        "|---|---|---|---|---|",
    ]
    for dev in group("ready"):
        lines.append(f"| {dev['name']} | {dev['site'] or '—'} | {_fw(dev)} | "
                     f"{slot_summary(dev)} | "
                     f"{dev['tailscale_ip'] or 'RMS (no tailnet address)'} |")

    lines += ["", "## Online, but on a firmware the options were never verified on", "",
              f"The `sim_switch` UCI names were only verified on "
              f"{VERIFIED_SIM_SWITCH_FW}, and `uci set` accepts a renamed option "
              f"without complaining, so a write here can silently do nothing.", "",
              "| Device | Site | Firmware | Also worth checking |", "|---|---|---|---|"]
    for dev in group("firmware"):
        lines.append(f"| {dev['name']} | {dev['site'] or '—'} | {_fw(dev)} | "
                     f"{'; '.join(dev['checks']) or 'nothing else'} |")

    lines += ["", "## Online and on the right firmware, with something to check", "",
              "| Device | Site | What to check | Managed slots |", "|---|---|---|---|"]
    for dev in group("checks"):
        lines.append(f"| {dev['name']} | {dev['site'] or '—'} | "
                     f"{'; '.join(dev['checks'])} | {slot_summary(dev)} |")

    lines += ["", "## Not reachable right now", "",
              "| Device | Site | Status | Last seen | Firmware |", "|---|---|---|---|---|"]
    for dev in group("offline"):
        lines.append(f"| {dev['name']} | {dev['site'] or '—'} | {dev['status']} | "
                     f"{dev['last_seen'] or '—'} | {_fw(dev)} |")

    lines += ["", "## Reference: the limits that would be written", "",
              "| Operator | ICCID prefix | Cap | Reset day | Enforced |",
              "|---|---|---|---|---|"]
    fallback = {"name": "unknown (fallback)", **report["unknown_operator"]}
    for op in report["operators"] + [fallback]:
        prefixes = ",".join(op.get("iccid_prefixes") or []) or "no prefix match"
        lines.append(f"| {op['name']} | {prefixes} | {_gb(op['data_limit_mb'])} | "
                     f"{op['reset_day']} | {'yes' if op['enabled'] else 'no'} |")
    lines += ["", f"Slot {ESIM_SLOT} (eSIM) is left unlimited by design.", ""]
    path.write_text("\n".join(lines) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tobee", default=DEFAULT_TOBEE, help=f"default {DEFAULT_TOBEE}")
    ap.add_argument("--html", type=Path, help="parse a saved /sims page instead")
    ap.add_argument("--config", type=Path, help="site.config.json with the operators")
    ap.add_argument("--only", default="", help="only devices whose name contains this")
    ap.add_argument("--output-dir", type=Path, default=HERE)
    args = ap.parse_args()

    config = args.config or (DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else EXAMPLE_CONFIG)
    operators, unknown = load_table(config)
    print(f"Operator table from {config}:")
    for op in operators:
        print(f"  {op['name']:<12} {','.join(op['iccid_prefixes']):<18} "
              f"{_gb(op['data_limit_mb']):>10}  day {op['reset_day']:<3}"
              f"{'enforced' if op['enabled'] else 'NOT enforced'}")
    print(f"  {'unknown':<12} {'(fallback)':<18} {_gb(unknown['data_limit_mb']):>10}  "
          f"day {unknown['reset_day']:<3}"
          f"{'enforced' if unknown['enabled'] else 'NOT enforced'}")
    print(f"  slot {ESIM_SLOT} (eSIM) is left unlimited by design.\n")

    if args.html:
        source = str(args.html)
        page = args.html.read_text(errors="replace")
    else:
        source = f"{args.tobee.rstrip('/')}/sims"
        print(f"Fetching {source} ...")
        page = fetch_page(source)

    every = parse_devices(page)
    devices = [d for d in every if d["model_tag"].startswith("otd")]
    if args.only:
        devices = [d for d in devices if args.only.lower() in d["name"].lower()]
    print(f"{len(every)} device(s) on the page, {len(devices)} OTD"
          + (f" matching {args.only!r}" if args.only else "") + ".")
    if not devices:
        return

    rows = [assess(d, operators, unknown) for d in devices]
    duplicate_slots = flag_duplicate_iccids(rows)
    assign_groups(rows)
    slots = [s for r in rows for s in r["slots"]]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "config": str(config),
        "verified_firmware": VERIFIED_SIM_SWITCH_FW,
        "managed_slots": list(SIM_SLOTS),
        "operators": operators,
        "unknown_operator": unknown,
        "totals": {
            "devices": len(rows),
            "online": sum(1 for r in rows if r["status"] == "online"),
            "offline": sum(1 for r in rows if r["status"] == "offline"),
            "unknown_status": sum(1 for r in rows if r["status"] not in
                                  ("online", "offline")),
            "would_cut": sum(1 for s in slots if s["verdict"] == "WOULD-CUT"),
            "at_risk": sum(1 for s in slots if s["verdict"] == "AT-RISK"),
            "no_usage": sum(1 for s in slots if s["verdict"] == "UNKNOWN"),
            # The blind spots that actually matter: a cap would be enforced and we
            # have no idea how much the SIM has already spent.
            "blind_enforced": sum(1 for s in slots if s["would_enforce"]
                                  and s["iccid"] and s["used_mib"] is None),
            "unmatched": sum(1 for s in slots if s["iccid"] and not s["matched_prefix"]),
            "unreadable_iccid": sum(1 for s in slots
                                    if not s["iccid"] and s.get("iccid_raw")),
            "firmware_other": sum(1 for r in rows
                                  if r["firmware"] and not r["firmware_verified"]),
            "firmware_unknown": sum(1 for r in rows if not r["firmware"]),
            "duplicate_iccid_slots": duplicate_slots,
            "no_tailnet": sum(1 for r in rows if not r["tailscale_ip"]),
            "devices_flagged": sum(1 for r in rows if r["worst"] != "OK"),
        },
        "groups": {name: sum(1 for r in rows if r["group"] == name)
                   for name in ("ready", "firmware", "checks", "offline")},
        "firmware_spread": {fw: sum(1 for r in rows if r["firmware"] == fw)
                            for fw in sorted({r["firmware"] for r in rows})},
        "devices": rows,
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    if not args.html:
        # Keep the page the numbers came from: Tobee is live, so without this the
        # report cannot be re-derived or argued with later.
        (args.output_dir / "quota_preflight_page.html").write_text(page)
    json_path = args.output_dir / "quota_preflight.json"
    json_path.write_text(json.dumps(report, indent=2) + "\n")
    write_markdown(report, args.output_dir / "quota_preflight.md")

    g, t = report["groups"], report["totals"]
    print(f"\n{g['ready']} ready and online, {g['firmware']} online on an unverified "
          f"firmware,\n{g['checks']} online with something to check, {g['offline']} not "
          f"reachable.\nAcross the fleet: {t['would_cut']} slot(s) would be cut, "
          f"{t['blind_enforced']} capped with no usage figure.")
    print(f"Wrote {json_path} and {args.output_dir / 'quota_preflight.md'}")


if __name__ == "__main__":
    main()
