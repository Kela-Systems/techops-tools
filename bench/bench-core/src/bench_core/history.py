#!/usr/bin/env python3
"""What a finished device was SUPPOSED to be (TEC-348).

A verify-only pass has a problem a configure run doesn't: the operator standing
in front of a provisioned unit has no idea what it was provisioned as. Most
expectations come from the station config (timezone, NTP, firmware floor, the
shared password, the final LAN address) and are the same for every unit, but
the per-unit ones do not: the OTD500 and RUTM08 hostnames are built from a site
name somebody typed weeks ago, a Raythink camera's address depends on which
octet it was given.

So the intent is recovered from the run record the configure run already wrote,
looked up by serial (then MAC), newest first:

    1. the station's own logs/ directory  — the common case, same bench
    2. bench-central                      — the unit was provisioned elsewhere
    3. nothing found                      — see below

Only `kind == "configure"` records count. A verify run copies the expectations
it was given into its own record, so accepting one as a source would let a
wrong expectation launder itself into looking authoritative on the next sweep.

**The third case must not be silent.** Skipping the checks whose expectations
are missing would produce a green table for a device nobody ever configured —
the same "a check that cannot fail" fault this whole issue exists to remove. So
a miss produces a FAILING row (`prior_run_row`) and the caller runs whatever
config-derived checks it can. The operator can supply the site name by hand to
override, which is a deliberate action recorded as such.
"""
from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from typing import Optional

from bench_core.central import central_url
from bench_core.run_record import KIND_CONFIGURE, parse_run_record

try:
    import requests
except ImportError:  # pragma: no cover - requests is a hard dep in practice
    requests = None

log = logging.getLogger("teltonika")

# Central lookups sit in front of an operator waiting at the bench, and the
# collector is on a tailnet that may be down. Fail fast and fall through.
CENTRAL_TIMEOUT_SEC = 5

# How many summary rows to ask the collector for. One would do against a
# collector that honours `kind=configure`; a few are needed to walk past the
# verify runs an older one hands back, and a unit with more than this many
# sweeps and no configure run in between is one an engineer should look at.
CENTRAL_SCAN_LIMIT = 5

# How many per-run JSON files to open before giving up. save_run_record keeps
# 500 per tool and names them with a leading timestamp, so a reverse sort walks
# them newest-first and the match is normally in the first few — but a station
# that has provisioned 500 units since is not worth reading in full.
SCAN_LIMIT = 500


def _matches(entry: dict, serial: str, mac: str) -> bool:
    """Is `entry` about the device identified by `serial` / `mac`?

    Serial is the real identity and is tried first. MAC is the fallback for a
    device whose serial could not be read on one of the two runs — it is the
    LAN MAC, so it is stable across a provision, but a record may carry
    "unknown" for either field and "unknown" must never match "unknown".
    """
    if serial and serial != "unknown" and entry.get("serial") == serial:
        return True
    if mac and mac != "unknown":
        recorded = (entry.get("mac") or "").lower()
        return recorded == mac.lower()
    return False


def find_last_configure_run(log_dir: Path, *, serial: str = "", mac: str = "",
                            tool: str = "") -> Optional[dict]:
    """The newest local configure record for this device, or None.

    Reads `log_dir/*.json` newest-first. The filenames written by
    `save_run_record` lead with the run's own timestamp, so a reverse
    lexicographic sort is chronological without opening anything — but the
    verify-run prefix means names no longer sort purely by time, so the
    candidates are sorted by mtime instead (which the writer sets and never
    touches again).
    """
    if not (serial or mac):
        return None
    with contextlib.suppress(OSError):
        files = sorted(log_dir.glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files[:SCAN_LIMIT]:
            try:
                entry = parse_run_record(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue  # a torn or hand-edited file is not a reason to stop
            if entry.get("kind") != KIND_CONFIGURE:
                continue
            if tool and entry.get("tool") != tool:
                continue
            if _matches(entry, serial, mac):
                return entry
    return None


def fetch_last_configure_run(*, serial: str = "", tool: str = "",
                             ) -> Optional[dict]:
    """The newest configure record for this serial from bench-central, or None.

    Two hops: the summary list (newest first), then the full record by run_id,
    because the per-family `device` block a caller needs (a camera's
    ip/profile) lives only in the stored JSON and not in the summary columns.

    `kind=configure` is passed as a filter AND re-checked on the record that
    comes back, which is not belt-and-braces: an older collector does not know
    the parameter, and FastAPI ignores query parameters a route never declared,
    so the filter silently degrades to "the newest run of any kind". Trusting
    it there would hand a verify record back as the source of truth — the
    laundering this module's docstring rules out — and a station can be pointed
    at any collector, so the version on the other end is not ours to assume.
    Rejecting locally costs a station running an old collector nothing worse
    than the honest "prior run" row.

    Best-effort throughout: central shipping off, collector unreachable, a
    schema surprise — all return None so verify falls through to the failing
    "prior run" row rather than erroring out at the bench. MAC is not a
    parameter because the collector cannot filter on it.
    """
    url = central_url()
    if not url or requests is None or not serial or serial == "unknown":
        return None
    try:
        listing = requests.get(f"{url}/api/v1/runs",
                               params={"serial": serial, "tool": tool,
                                       "kind": KIND_CONFIGURE,
                                       "limit": CENTRAL_SCAN_LIMIT},
                               timeout=CENTRAL_TIMEOUT_SEC)
        listing.raise_for_status()
        for row in listing.json().get("runs") or []:
            run_id = row.get("run_id")
            # A collector that knows about `kind` says so in the summary, so
            # skip a verify run without paying for the second hop.
            if not run_id or (row.get("kind") or KIND_CONFIGURE) != KIND_CONFIGURE:
                continue
            full = requests.get(f"{url}/api/v1/runs/{run_id}",
                                timeout=CENTRAL_TIMEOUT_SEC)
            full.raise_for_status()
            entry = parse_run_record(full.json())
            # The authority. A record with no `kind` at all predates the mode
            # and reads as a configure run, which is what it is.
            if entry.get("kind") == KIND_CONFIGURE:
                return entry
        return None
    except Exception as e:  # noqa: BLE001 — any network/JSON failure falls through
        log.info("bench-central lookup for serial %s failed (%s) — falling back "
                 "to this station's own records.", serial, e)
        return None


def prior_run_row(*, serial: str, mac: str, searched_central: bool) -> dict:
    """The FAILING verification row for a device with no configure record.

    Not a skip and not a warning. A unit nobody can show was ever configured is
    exactly what a pre-ship QA gate exists to catch, and TEC-352 prints a label
    on `verified is True` — so this row is what stops an unprovisioned box from
    earning one.
    """
    identified = serial if serial and serial != "unknown" else f"MAC {mac or 'unknown'}"
    where = ("this station's records or bench-central" if searched_central
             else "this station's records (bench-central is not configured here)")
    return {"item": "prior run",
            "expected": "a recorded configure run for this unit",
            "actual": f"none found for {identified} in {where}",
            "ok": False}


def expected_from(entry: Optional[dict]) -> dict:
    """The per-unit expectations carried by a configure record: its `device`
    block (hostname, site_name, ip, profile, channel, ...) — which is exactly
    the set of per-family fields the schema was built to hold. Empty dict for
    no record, so callers can treat "nothing known" uniformly."""
    return dict((entry or {}).get("device") or {})


def resolve_expected(log_dir: Path, *, serial: str = "", mac: str = "",
                     tool: str = "", overrides: Optional[dict] = None,
                     ) -> tuple[dict, Optional[dict], str]:
    """Everything a verify pass needs to know about one unit's intended state.

    Returns `(expected, prior_row, source)`:

    * `expected` — the per-unit fields, operator overrides winning over the
      record so an engineer can verify a unit against what it SHOULD be rather
      than what it was mistakenly set to.
    * `prior_row` — None when a configure record was found, else the failing
      row from `prior_run_row()` to append to the check list.
    * `source` — "override" / "station" / "central" / "none", for the run log.

    An override alone is enough: an operator who names the site is stating the
    intent themselves, which is a stronger claim than a lookup, so no failing
    row is added. Overrides that only partly cover the per-unit fields still
    leave the lookup in charge of the rest.
    """
    # A field the operator left alone arrives as "" (or as whitespace from a
    # scanner) and is not a statement of intent.
    overrides = {k: (v.strip() if isinstance(v, str) else v)
                 for k, v in (overrides or {}).items()
                 if (v.strip() if isinstance(v, str) else v)}

    entry = find_last_configure_run(log_dir, serial=serial, mac=mac, tool=tool)
    source = "station"
    if entry is None:
        entry = fetch_last_configure_run(serial=serial, tool=tool)
        source = "central" if entry is not None else "none"

    expected = {**expected_from(entry), **overrides}
    if entry is not None:
        log.info("Verifying against the %s configure run of %s (%s).",
                 "local" if source == "station" else "central",
                 entry.get("serial") or "unknown",
                 (entry.get("timestamp") or "time unknown")[:19])
        if overrides:
            log.info("Operator supplied %s — overriding the recorded value.",
                     ", ".join(sorted(overrides)))
        return expected, None, ("override" if overrides else source)

    if overrides:
        log.info("No configure record for this unit; verifying against the "
                 "operator-supplied %s.", ", ".join(sorted(overrides)))
        return expected, None, "override"

    log.warning("No configure run recorded for this unit — it cannot be shown "
                "to have been provisioned. Verification will FAIL on that row.")
    return expected, prior_run_row(serial=serial, mac=mac,
                                   searched_central=bool(central_url())), "none"
