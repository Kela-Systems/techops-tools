#!/usr/bin/env python3
"""The factory-label password record (TEC-845) — a device fact, not a run fact.

A Teltonika ships with a unique admin password printed on its sticker. The
bench reads it (scanned off the QR, or typed by the operator), logs in with it
once, and replaces it with the station's shared password. Until now that was
the end of it: the factory value was used and dropped. But it is the password
the unit REVERTS to on a factory reset, so a device reset in the field — or
returned for rework — is unreachable, and recovering it is hand work.

So the bench keeps it. Centrally, keyed to the serial, forever.

Why its own record and not a field on the run record: the factory password is
a property of the DEVICE, and a run record is the story of one visit. A unit
configured, re-run and verified five times has five run records and exactly
one factory password. Folding it into the run record would copy it into all
five, put it in the per-run JSON on every bench that ever touched the unit,
and hand it to every consumer of the run feed (the read-only dashboard,
TEC-575) whether or not they asked. A separate record with a separate endpoint
and a separate table keeps the answer in one row per device and keeps the run
feed exactly as password-free as it has always been (TEC-349).

It is not treated as a secret beyond that. The whole point is that somebody
can look it up when a unit comes back, so it is stored in the clear and shown
in the dashboard; the access control is the same as everything else on
bench-central — reachability over the tailnet.

Schema `bench-device-label/1`:

    schema      "bench-device-label/1"
    serial      the device serial — the key. Never empty (see below).
    password    the factory-label password, verbatim
    source      where it came from: "scan" (read off the QR) or "typed"
    tool        which app captured it: "otd" | "rutm" | "tsw"
    mac         LAN MAC, canonicalized, or "" — the secondary lookup handle
    model       "OTD500" / "RUTM08" / "TSW202", or ""
    username    the account the password belongs to (the label's `U:`, "admin")
    imei        from a cellular unit's label, else ""
    batch       the label's `B:` batch code, else ""
    captured_at ISO-8601 UTC of the run that read it
    run_id      the run record it was captured during, for cross-reference
    station_id  which bench read it
    operator    who was at that bench

The serial is required, and that requirement is what keeps this simple. A scan
carries its own serial, so a scanned label is always keyed even if the device
never answered; a typed password is keyed by the serial the pipeline read off
the device after logging in. What is left over is "typed a password AND the
device never logged in" — where the password is most likely wrong (that is
usually why the login failed) and the operator is standing in front of the
unit able to retry. Recording a probably-wrong password under a synthetic key
would be worse than recording nothing.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from bench_core import canonical_mac

LABEL_RECORD_SCHEMA = "bench-device-label/1"

# Where a retained password came from. "shared-fallback" is deliberately not
# here: it means no factory password was supplied at all, so there is nothing
# to keep.
SOURCE_SCAN = "scan"
SOURCE_TYPED = "typed"
SOURCES = (SOURCE_SCAN, SOURCE_TYPED)


def build_label_record(*, serial: str, password: str, source: str, tool: str,
                       mac: str = "", model: str = "", username: str = "",
                       imei: str = "", batch: str = "",
                       run_id: Optional[str] = None,
                       captured_at: Optional[str] = None) -> dict:
    """Build one device-label record (see the module docstring for the
    field-by-field schema).

    Raises ValueError when there is no serial, no password, or an unknown
    source — all three are programming errors here rather than bad input, since
    the only caller (`BenchConfigurator._retain_label_password`) has already
    decided there is something worth keeping.
    """
    serial = (serial or "").strip()
    if not serial:
        raise ValueError("a device-label record needs a serial to key on")
    if not password:
        raise ValueError("a device-label record needs a password")
    if source not in SOURCES:
        raise ValueError(f"Unknown label password source '{source}' — "
                         f"expected one of {SOURCES}.")
    return {
        "schema": LABEL_RECORD_SCHEMA,
        "serial": serial,
        "password": password,
        "source": source,
        "tool": tool,
        "mac": canonical_mac(mac) if mac else "",
        "model": model or "",
        "username": username or "",
        "imei": imei or "",
        "batch": batch or "",
        "captured_at": captured_at or datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "station_id": "",
        "operator": "",
    }


def parse_label_record(data: dict) -> dict:
    """Read one device-label record as posted by a station and return it in the
    canonical shape, with every key present.

    The collector's gate, so it is forgiving about everything except the two
    fields the store cannot work without — a record with no serial has nothing
    to key on and one with no password has nothing to keep, and both come back
    with those fields empty for the caller to reject.
    """
    mac = str(data.get("mac") or "")
    source = str(data.get("source") or "")
    return {
        "schema": LABEL_RECORD_SCHEMA,
        "serial": str(data.get("serial") or "").strip(),
        "password": str(data.get("password") or ""),
        "source": source if source in SOURCES else "",
        "tool": str(data.get("tool") or ""),
        "mac": canonical_mac(mac) if mac else "",
        "model": str(data.get("model") or ""),
        "username": str(data.get("username") or ""),
        "imei": str(data.get("imei") or ""),
        "batch": str(data.get("batch") or ""),
        "captured_at": data.get("captured_at") or None,
        "run_id": data.get("run_id") or None,
        "station_id": str(data.get("station_id") or ""),
        "operator": str(data.get("operator") or ""),
    }
