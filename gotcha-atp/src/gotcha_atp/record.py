"""The run record: bench-run-record/1 core + a `stages[]` extension.

Own implementation of the schema bench-central already ingests (see
bench/bench-core/src/bench_core/run_record.py for the field-by-field contract),
so a POST to /api/v1/runs stores it unchanged and its site/hostname columns are
filled from `device.site_name` / `device.hostname`.

Mapping for a whole-unit ATP run:
    tool        "gotcha-atp"
    kind        "verify" — the ATP never mutates anything
    serial      the unit (site) name: a Gotcha unit has no single serial; every
                part's serial is in device.inventory
    model       "Gotcha"
    verified    stamp_eligible (the strictest reading of "this unit passed")
    verification  every row as {item: "<id> <check>", expected, actual, ok}
    warnings    medium/low fails, as "<id> <check>: <actual>"
    device      site_name, hostname, route, release, identity, inventory,
                verdict, stamp_eligible, full_run, stages_requested, soak
    stages      [{id, name, state, rows: [full rows incl. severity/state/hint]}]

No password appears anywhere: the finished record is passed through the run's
Redactor before it is written or uploaded.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import PROJECT_DIR
from .context import Context
from .model import Row, StageSpec, stage_state

SCHEMA = "bench-run-record/1"
TOOL = "gotcha-atp"
KIND = "verify"

RUNS_DIR = Path(os.environ.get("GOTCHA_ATP_RUNS", PROJECT_DIR / "runs"))


def build(*, run_id: str, ctx: Context, rows: list[Row], stage_specs: list[StageSpec],
          summary: dict, started: datetime, finished: datetime, steps: list[dict],
          engineer: str, station: str, version: str, requested: Optional[list[str]],
          soak: bool, error: str = "") -> dict:
    by_id = {r.id: r for r in rows}
    stages_out = []
    for st in stage_specs:
        st_rows = [by_id[s.id] for s in st.rows if s.id in by_id]
        stages_out.append({"id": st.id, "name": st.name, "implemented": st.implemented,
                           "state": stage_state(st_rows) if st_rows else "pending",
                           "rows": [r.to_dict() for r in st_rows]})
    identity = ctx.facts.get("identity") or {}
    server = identity.get("server") or {}
    release = ctx.release.info() if ctx.release else {"error": ctx.release_error}
    verify_detail = None
    if not summary["stamp_eligible"]:
        verify_detail = summary["stamp_reason"]
    entry = {
        "schema": SCHEMA,
        "run_id": run_id,
        "tool": TOOL,
        "kind": KIND,
        "timestamp": finished.isoformat(),
        "time": finished.strftime("%H:%M:%S"),
        "status": "error" if error else "ok",
        "error": error or None,
        "serial": ctx.unit.site,
        "mac": "unknown",
        "model": "Gotcha",
        "firmware": None,
        "duration_s": int((finished - started).total_seconds()),
        "verified": bool(summary["stamp_eligible"]),
        "verify_detail": verify_detail,
        "verification": [{"item": f"{r.id} {r.item}", "expected": r.expected,
                          "actual": r.actual, "ok": r.ok} for r in rows],
        "warnings": [f"{r.id} {r.item}: {r.actual}" for r in rows if r.warning],
        "steps": steps,
        "log": "\n".join(f"{s['time']} {s['level']} {s['msg']}" for s in steps),
        "device": {
            "site_name": ctx.unit.site,
            "hostname": server.get("hostname") or ctx.unit.site,
            "route": ctx.session.route if ctx.session else ctx.facts.get("route"),
            "release": release,
            "identity": identity,
            "inventory": ctx.facts.get("inventory") or [],
            "fleet_node": ctx.facts.get("fleet_node"),
            "verdict": summary["verdict"],
            "stamp_eligible": summary["stamp_eligible"],
            "stamp_reason": summary["stamp_reason"],
            "full_run": summary["full_run"],
            "stages_requested": requested,
            "soak": soak,
            "started_at": started.isoformat(),
        },
        "stages": stages_out,
        "summary": summary,
        "operator": engineer or None,
        "station_id": station,
        "bench_version": version,
        "config_hash": ctx.release.sha256[:12] if ctx.release else None,
    }
    return ctx.redact.scrub(entry)


def write(entry: dict, runs_dir: Path = RUNS_DIR) -> dict[str, Path]:
    """runs/<site>/<UTC timestamp>.json (atomic). Returns {'json': path}."""
    site = entry.get("serial") or "unknown"
    stamp = datetime.fromisoformat(entry["timestamp"]).strftime("%Y-%m-%dT%H-%M-%SZ")
    out = Path(runs_dir) / _safe(site)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{stamp}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return {"json": path}


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "-" for c in name)


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def rows_of(entry: dict) -> dict[str, Row]:
    """The rows of a stored record, for a re-run's prerequisite lookups."""
    return {r["id"]: Row.from_dict(r) for st in entry.get("stages") or [] for r in st.get("rows") or []}
