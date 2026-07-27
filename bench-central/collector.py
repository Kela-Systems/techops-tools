#!/usr/bin/env python3
"""The central run-record collector (TEC-574) — the durable audit trail.

Receive side of centralizing run records (TEC-347): every bench station with
BENCH_CENTRAL_URL set spools completed run records to a local outbox and a
background uploader POSTs them here over the tailnet (bench_core.central,
TEC-573). This service stores them in SQLite and NEVER prunes — the stations
keep only their newest 500 per tool, so this database is the only place
"which devices were configured last month" can be answered.

Ingest contract (the uploader's side is documented in bench_core/central.py):

    POST /api/v1/runs          body = one run-record JSON
        201  stored                          {"stored": run_id}
        409  duplicate run_id (idempotent retries are free)
        400  not a usable record (no run_id) — the uploader quarantines it

Read side:

    GET /                      the read-only dashboard (static/index.html,
                               TEC-575) — list/search runs, click into one
    GET /api/v1/health         liveness + row count + db size
    GET /api/v1/runs           summaries, newest first; filters:
                               station_id, tool, status, serial, site,
                               operator, q (substring across serial/mac/
                               hostname/site/operator/model), since/until
                               (ISO), limit (default 100, max 1000), offset
    GET /api/v1/runs/{run_id}  the full stored record
    GET /api/v1/filters        distinct stations/tools/sites/operators for
                               the dashboard's dropdowns

Durability: WAL journal + synchronous=FULL — a row acknowledged with 201 is
on disk before the uploader deletes its outbox copy, so an instance restart
(or crash) loses nothing that was acknowledged.

Security model: no auth by design — the tailnet is the perimeter. Deploy on
a host with NO inbound security-group rules; only Tailscale peers can reach
the port (see README.md / deploy/bench-central.service).

Config (env):
    BENCH_CENTRAL_DB     SQLite file path      (default ./bench-central.db)
    BENCH_CENTRAL_HOST   bind address          (default 127.0.0.1; the systemd
                         unit sets 0.0.0.0 — safe with an empty inbound SG)
    BENCH_CENTRAL_PORT   port                  (default 8100)
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from bench_core.run_record import parse_run_record

DEFAULT_PORT = 8100

# The queryable core of a record, lifted into real columns at ingest time.
# The full record JSON is kept verbatim alongside — columns are for WHERE
# clauses, the JSON is the audit trail.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    received_at   TEXT NOT NULL,
    timestamp     TEXT,
    tool          TEXT,
    status        TEXT,
    serial        TEXT,
    mac           TEXT,
    model         TEXT,
    firmware      TEXT,
    duration_s    INTEGER,
    verified      INTEGER,
    error         TEXT,
    operator      TEXT,
    station_id    TEXT,
    bench_version TEXT,
    site          TEXT,
    hostname      TEXT,
    record        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_time    ON runs(timestamp);
CREATE INDEX IF NOT EXISTS idx_runs_station ON runs(station_id);
CREATE INDEX IF NOT EXISTS idx_runs_serial  ON runs(serial);
"""

_SUMMARY_COLS = ("run_id", "received_at", "timestamp", "tool", "status",
                 "serial", "mac", "model", "firmware", "duration_s",
                 "verified", "error", "operator", "station_id",
                 "bench_version", "site", "hostname")

# Substring search (?q=) matches any of these columns.
_SEARCH_COLS = ("serial", "mac", "hostname", "site", "operator", "model")

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # FULL, not WAL's default NORMAL: a 201 must mean "on disk" — the uploader
    # deletes its outbox copy on our word.
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")  # persistent once set
        conn.executescript(_SCHEMA)
        # TEC-575 migration: databases created before the dashboard lack the
        # site/hostname columns (they lived only inside the record JSON).
        # Add them and backfill from the stored records, once.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        if "site" not in cols:
            with conn:
                conn.execute("ALTER TABLE runs ADD COLUMN site TEXT")
                conn.execute("ALTER TABLE runs ADD COLUMN hostname TEXT")
                conn.execute("""
                    UPDATE runs SET
                        site     = json_extract(record, '$.device.site_name'),
                        hostname = json_extract(record, '$.device.hostname')
                """)


def _row_from(entry: dict, received_at: str) -> dict:
    verified = entry.get("verified")
    device = entry.get("device") or {}
    return {
        "site": device.get("site_name"),
        "hostname": device.get("hostname"),
        "run_id": entry["run_id"],
        "received_at": received_at,
        "timestamp": entry.get("timestamp"),
        "tool": entry.get("tool"),
        "status": entry.get("status"),
        "serial": entry.get("serial"),
        "mac": entry.get("mac"),
        "model": entry.get("model"),
        "firmware": entry.get("firmware"),
        "duration_s": entry.get("duration_s"),
        "verified": None if verified is None else int(bool(verified)),
        "error": entry.get("error"),
        "operator": entry.get("operator"),
        "station_id": entry.get("station_id"),
        "bench_version": entry.get("bench_version"),
        "record": json.dumps(entry, ensure_ascii=False),
    }


def _repo_version() -> str:
    """The short git revision of this collector's checkout, or 'unknown'.
    Comparable with the stations' bench_version — same repo, same rev format."""
    with contextlib.suppress(Exception):
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=str(Path(__file__).resolve().parent),
                             capture_output=True, text=True, timeout=3)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    return "unknown"


def create_app(db_path: Optional[Path] = None) -> FastAPI:
    db = Path(db_path or os.environ.get("BENCH_CENTRAL_DB", "bench-central.db"))
    init_db(db)
    version = _repo_version()  # once at startup, not per health poll
    app = FastAPI(title="Bench Central Collector")

    @app.post("/api/v1/runs", status_code=201)
    def ingest(record: dict = Body(...)):
        # parse_run_record() reads any dict (that's its job — it infers even
        # pre-schema shapes), so the real gate is run_id: without one there is
        # no idempotency key, and everything the TEC-573 uploader ships has one.
        entry = parse_run_record(record)
        if not entry.get("run_id"):
            raise HTTPException(status_code=400,
                                detail="record has no run_id")
        row = _row_from(entry, datetime.now(timezone.utc).isoformat())
        cols = ", ".join(row)
        marks = ", ".join(":" + c for c in row)
        try:
            with closing(_connect(db)) as conn, conn:
                conn.execute(f"INSERT INTO runs ({cols}) VALUES ({marks})", row)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409,
                                detail=f"run {entry['run_id']} already stored")
        return {"stored": entry["run_id"]}

    @app.get("/api/v1/runs")
    def list_runs(station_id: Optional[str] = None, tool: Optional[str] = None,
                  status: Optional[str] = None, serial: Optional[str] = None,
                  site: Optional[str] = None, operator: Optional[str] = None,
                  q: Optional[str] = None,
                  since: Optional[str] = None, until: Optional[str] = None,
                  limit: int = Query(default=100, ge=1, le=1000),
                  offset: int = Query(default=0, ge=0)):
        clauses, params = [], []
        for col, value in (("station_id", station_id), ("tool", tool),
                           ("status", status), ("serial", serial),
                           ("site", site), ("operator", operator)):
            if value is not None:
                clauses.append(f"{col} = ?")
                params.append(value)
        if q:
            needle = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("(" + " OR ".join(
                f"{col} LIKE ? ESCAPE '\\'" for col in _SEARCH_COLS) + ")")
            params.extend([f"%{needle}%"] * len(_SEARCH_COLS))
        # Records missing their own timestamp still sort/filter by arrival.
        when = "COALESCE(timestamp, received_at)"
        if since is not None:
            clauses.append(f"{when} >= ?")
            params.append(since)
        if until is not None:
            clauses.append(f"{when} <= ?")
            params.append(until)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with closing(_connect(db)) as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM runs {where}",
                                 params).fetchone()[0]
            rows = conn.execute(
                f"SELECT {', '.join(_SUMMARY_COLS)} FROM runs {where} "
                f"ORDER BY {when} DESC LIMIT ? OFFSET ?",
                (*params, limit, offset)).fetchall()
        runs = [dict(r) for r in rows]
        for r in runs:
            r["verified"] = None if r["verified"] is None else bool(r["verified"])
        return {"count": len(runs), "total": total, "offset": offset,
                "runs": runs}

    @app.get("/api/v1/filters")
    def filters():
        """Distinct values for the dashboard's dropdowns."""
        out = {}
        with closing(_connect(db)) as conn:
            for key, col in (("stations", "station_id"), ("tools", "tool"),
                             ("sites", "site"), ("operators", "operator")):
                rows = conn.execute(
                    f"SELECT DISTINCT {col} FROM runs "
                    f"WHERE {col} IS NOT NULL AND {col} != '' ORDER BY {col}")
                out[key] = [r[0] for r in rows]
        return out

    @app.get("/api/v1/runs/{run_id}")
    def get_run(run_id: str):
        with closing(_connect(db)) as conn:
            row = conn.execute("SELECT record FROM runs WHERE run_id = ?",
                               (run_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"no run {run_id}")
        return json.loads(row["record"])

    @app.get("/api/v1/health")
    def health():
        with closing(_connect(db)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        return {"ok": True, "runs": count, "version": version,
                "db": str(db), "db_bytes": db.stat().st_size}

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(STATIC_DIR / "index.html")

    return app


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("BENCH_CENTRAL_HOST", "127.0.0.1")
    port = int(os.environ.get("BENCH_CENTRAL_PORT", str(DEFAULT_PORT)))
    print(f"Bench Central Collector — http://{host}:{port}")
    print(f"  db: {os.environ.get('BENCH_CENTRAL_DB', 'bench-central.db')}")
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")
