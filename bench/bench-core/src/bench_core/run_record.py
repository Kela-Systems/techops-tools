#!/usr/bin/env python3
"""The canonical bench run-record schema (TEC-346).

Every tool used to declare its own history-entry shape in `build_entry`; they
were ~80% identical but drifted (Teltonika entries had `duration_s` +
`verification`, Magos entries had `verified`/`verify_detail` + `channel`,
Raythink added `profile`). Central reporting and label printing need ONE
schema, so it lives here: all `build_entry` hooks call `build_run_entry()`,
and any consumer reads a record — including a pre-schema JSON from an old
bench — through `parse_run_record()`.

Schema `bench-run-record/1` — the common core, identical for every tool:

    schema        "bench-run-record/1"
    run_id        unique id of this run (uuid4 string), minted when the entry
                  is built — the idempotency key for central upload (TEC-347)
    tool          which app produced it: "otd" | "rutm" | "raythink" |
                  "speaker" | "magos-radar" | "magos-apu"
    timestamp     run end, full ISO-8601 UTC datetime
    time          run end, UTC "HH:MM:SS" (kept for the UI history column)
    status        "ok" | "error"
    error         failure summary (str) or None
    serial        device serial ("unknown" when unreadable)
    mac           device MAC ("unknown" when unreadable)
    model         device model ("unknown" when unreadable)
    firmware      firmware version, or None for families that don't report one
    duration_s    whole-run wall time in seconds, or None
    verified      post-configure verification outcome: True / False / None
                  (None = no verification was in scope)
    verify_detail short human reason when verified is False, else None
    verification  itemised checks: [{item, expected, actual, ok}, ...]
                  (empty for families that verify with a single probe)
    warnings      non-fatal warnings collected during the run: [str, ...]
    steps         timestamped step log: [{time, level, sn, msg}, ...]
    log           the step log flattened to one string
    device        per-family extension block (see below)

The bench bases stamp four provenance fields into every entry after it is
built (`operator`, `station_id`, `bench_version` — TEC-345 — and
`config_hash`, the fingerprint of the redacted station config the run was
provisioned under — TEC-356) plus `log_file`.
(Before `run_id`/`timestamp` joined the core, the full ISO timestamp was added
only by the per-run JSON writer — `parse_run_record()` still accepts those.)

`device` extension blocks (per-family fields, everything else stays core):

    otd           hostname, site_name, imei
    rutm          hostname, site_name
    raythink      hostname, profile, ip
    speaker       hostname, ip, from_host
    magos-radar   channel, ip, from_host, ntp, timezone
    magos-apu     channel, ip, radar_ip, from_host, ntp, timezone
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

RUN_RECORD_SCHEMA = "bench-run-record/1"

TOOLS = ("otd", "rutm", "raythink", "speaker", "magos-radar", "magos-apu")

# Per-family keys that legacy (pre-schema) records carried at the top level;
# parse_run_record() lifts them into the `device` block.
_DEVICE_FIELDS = ("hostname", "site_name", "imei", "profile", "ip",
                  "from_host", "radar_ip", "channel", "ntp", "timezone")


def verification_outcome(checks: Optional[list]) -> tuple[Optional[bool], Optional[str]]:
    """Collapse an itemised verification list into the core (verified,
    verify_detail) pair. Checks with ok=None are out of scope (skipped);
    no in-scope checks at all means "not verified either way" (None)."""
    scoped = [c for c in checks or [] if c.get("ok") is not None]
    if not scoped:
        return None, None
    failed = [str(c.get("item", "?")) for c in scoped if not c["ok"]]
    if failed:
        return False, "failed: " + ", ".join(failed)
    return True, None


def build_run_entry(*, tool: str, ok: bool, error: Optional[str] = None,
                    serial: str = "unknown", mac: str = "unknown",
                    model: str = "unknown", firmware: Optional[str] = None,
                    duration_s: Optional[int] = None,
                    verified: Optional[bool] = None,
                    verify_detail: Optional[str] = None,
                    verification: Optional[list] = None,
                    warnings: Optional[list] = None,
                    steps: Optional[list] = None, log: str = "",
                    device: Optional[dict] = None) -> dict:
    """Build one canonical run-record entry (see the module docstring for the
    field-by-field schema). When `verified` isn't supplied it is derived from
    the itemised `verification` checks, so the two verification styles
    (single probe vs check list) land in the same core fields."""
    if tool not in TOOLS:
        raise ValueError(f"Unknown tool '{tool}' — expected one of {TOOLS}.")
    if verified is None and verification:
        verified, verify_detail = verification_outcome(verification)
    now = datetime.now(timezone.utc)
    return {
        "schema": RUN_RECORD_SCHEMA,
        "run_id": str(uuid.uuid4()),
        "tool": tool,
        "timestamp": now.isoformat(),
        "time": now.strftime("%H:%M:%S"),
        "status": "ok" if ok else "error",
        "error": error,
        "serial": serial,
        "mac": mac,
        "model": model,
        "firmware": firmware,
        "duration_s": duration_s,
        "verified": verified,
        "verify_detail": verify_detail,
        "verification": verification or [],
        "warnings": warnings or [],
        "steps": steps or [],
        "log": log,
        "device": device or {},
    }


def _infer_legacy_tool(data: dict) -> str:
    """Which family produced a pre-schema record, from its distinctive keys.

    The single-key checks come first — each key was only ever written by that
    one family, so they can't shadow each other. The radar has no such key
    (`channel` isn't guaranteed on every radar record), so it is recognised
    LAST by the wider set of keys the Magos tools stamp into every entry;
    by then only the speaker is left, and speaker records carry none of them."""
    if "radar_ip" in data:
        return "magos-apu"
    if "profile" in data:
        return "raythink"
    if "imei" in data:
        return "otd"
    if "site_name" in data:
        return "rutm"
    if any(k in data for k in ("channel", "verified", "verify_detail",
                               "ntp", "timezone")):
        return "magos-radar"
    return "speaker"


def parse_run_record(data: dict) -> dict:
    """Read ANY tool's run-record JSON — canonical or legacy (pre-schema) —
    and return it in the canonical shape. Legacy records get their family
    inferred, their per-family top-level keys lifted into `device`, and their
    verification style collapsed into the core verified/verify_detail pair.
    Extra keys (operator, station_id, bench_version, log_file,
    raw_identity_payloads, ...) pass through unchanged. Records written before
    run_id/timestamp joined the core get None for whichever is missing, so
    consumers can rely on the keys being present."""
    if data.get("schema") == RUN_RECORD_SCHEMA:
        entry = dict(data)
        entry.setdefault("device", {})
        entry.setdefault("run_id", None)
        entry.setdefault("timestamp", None)
        return entry

    tool = _infer_legacy_tool(data)
    entry = {k: v for k, v in data.items() if k not in _DEVICE_FIELDS}
    entry["schema"] = RUN_RECORD_SCHEMA
    entry["tool"] = tool
    entry["device"] = {k: data[k] for k in _DEVICE_FIELDS if k in data}
    entry.setdefault("run_id", None)
    entry.setdefault("timestamp", None)  # per-run JSON files carry one; history entries don't
    entry.setdefault("error", None)
    entry.setdefault("firmware", None)
    entry.setdefault("duration_s", None)
    for key in ("verification", "warnings", "steps"):
        entry.setdefault(key, [])
    entry.setdefault("log", "")
    if "verified" not in entry:  # Teltonika-style: itemised checks only
        entry["verified"], entry["verify_detail"] = \
            verification_outcome(entry["verification"])
    entry.setdefault("verify_detail", None)
    return entry
