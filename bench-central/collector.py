#!/usr/bin/env python3
"""The central run-record collector (TEC-574) — the durable audit trail.

Receive side of centralizing run records (TEC-347): every bench station with
BENCH_CENTRAL_URL set spools completed run records to a local outbox and a
background uploader POSTs them here over the tailnet (bench_core.central,
TEC-573). This service stores them in SQLite and NEVER prunes — the stations
keep only their newest 500 per tool, so this database is the only place
"which devices were configured last month" can be answered.

Alongside the runs it keeps the **device-label store** (TEC-845): the
factory-label password each Teltonika shipped with, one row per device, kept
forever so a unit factory-reset in the field is still reachable. That is a
fact about a device, not about a visit, so it gets its own table and its own
endpoint rather than riding inside run records — see
`bench_core/label_record.py` for the reasoning.

Ingest contract (the uploader's side is documented in bench_core/central.py):

    POST /api/v1/runs          body = one run-record JSON
        201  stored                          {"stored": run_id}
        409  duplicate run_id (idempotent retries are free)
        400  not a usable record (no run_id) — the uploader quarantines it

    POST /api/v1/device-labels body = one device-label JSON
        200  stored/updated/kept             {"stored": serial, "outcome": ...}
        400  no serial, no password, or an unknown source

Read side:

    GET /                      the read-only dashboard (static/index.html,
                               TEC-575) — list/search runs, look factory
                               passwords up, click into one run
    GET /api/v1/health         liveness + row counts + db size
    GET /api/v1/runs           summaries, newest first; filters:
                               station_id, tool, kind (configure|verify),
                               status, serial, site, operator, q (substring
                               across serial/mac/hostname/site/operator/model),
                               since/until (ISO), limit (default 100, max
                               1000), offset
    GET /api/v1/runs/{run_id}  the full stored record
    GET /api/v1/filters        distinct stations/tools/sites/operators for
                               the dashboard's dropdowns
    GET /api/v1/device-labels  factory passwords, newest reading first;
                               filters: serial, mac, tool, q (substring across
                               serial/mac/model/imei/batch), limit, offset
    GET /api/v1/device-labels/{serial}
                               one device's factory password (a MAC works too,
                               for a unit whose sticker is unreadable)

Fleet (station install + pinned releases, see fleet.py):

    /api/v1/fleet/*            desired pin, code bundles, station check-ins
    /setup.ps1, /setup.sh      the one-command station installers

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
    BENCH_CENTRAL_REPO   repo checkout bundles are built from
                         (default: this file's own checkout)
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

from bench_core import canonical_mac
from bench_core.label_record import SOURCE_SCAN, SOURCES, parse_label_record
from bench_core.run_record import parse_run_record

from fleet import FLEET_SCHEMA, add_fleet_routes

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
    kind          TEXT,
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

# The factory-password store (TEC-845). One row per device, keyed on the
# serial — a device has exactly one factory password however many times it is
# run, which is the whole reason this is not a column on `runs`.
_LABELS_SCHEMA = """
CREATE TABLE IF NOT EXISTS device_labels (
    serial      TEXT PRIMARY KEY,
    password    TEXT NOT NULL,
    source      TEXT,
    tool        TEXT,
    mac         TEXT,
    model       TEXT,
    username    TEXT,
    imei        TEXT,
    batch       TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    conflicts   INTEGER NOT NULL DEFAULT 0,
    run_id      TEXT,
    station_id  TEXT,
    operator    TEXT
);
CREATE INDEX IF NOT EXISTS idx_labels_mac  ON device_labels(mac);
CREATE INDEX IF NOT EXISTS idx_labels_seen ON device_labels(last_seen);
"""

_SUMMARY_COLS = ("run_id", "received_at", "timestamp", "tool", "kind", "status",
                 "serial", "mac", "model", "firmware", "duration_s",
                 "verified", "error", "operator", "station_id",
                 "bench_version", "site", "hostname")

# Substring search (?q=) matches any of these columns.
_SEARCH_COLS = ("serial", "mac", "hostname", "site", "operator", "model")

_LABEL_COLS = ("serial", "password", "source", "tool", "mac", "model",
               "username", "imei", "batch", "first_seen", "last_seen",
               "conflicts", "run_id", "station_id", "operator")
_LABEL_SEARCH_COLS = ("serial", "mac", "model", "imei", "batch")

# Identity fields a later reading may fill in but must never blank out: a run
# where the operator typed the password carries no label, so it knows nothing
# about the batch or IMEI a previous scan of the same unit already told us.
_LABEL_FILL_COLS = ("tool", "mac", "model", "username", "imei", "batch")

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
        conn.executescript(_LABELS_SCHEMA)
        conn.executescript(FLEET_SCHEMA)
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
        # TEC-348 migration: a stored record from before the verify mode has no
        # `kind`. Every run there was mutated the device, so the backfill says
        # "configure" rather than leaving a NULL that a `kind=configure` filter
        # would silently drop.
        if "kind" not in cols:
            with conn:
                conn.execute("ALTER TABLE runs ADD COLUMN kind TEXT")
                conn.execute(
                    "UPDATE runs SET kind = "
                    "COALESCE(json_extract(record, '$.kind'), 'configure')")


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
        # parse_run_record() defaults this for pre-TEC-348 records.
        "kind": entry.get("kind"),
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


def _store_label(conn: sqlite3.Connection, entry: dict, seen_at: str) -> str:
    """Insert or merge one device-label record. Returns what happened:
    `"stored"` (first reading of this device), `"confirmed"` (the same password
    again), `"updated"` (a different password won), or `"kept"` (a different
    password lost, and the stored one stands).

    A device's factory password is printed on it and never changes, so two
    different readings of the same serial mean somebody made a mistake — a
    mistyped character, or the wrong unit's sticker. Silently taking the newest
    would be a poor way to treat the one value this whole store exists to
    preserve, so the rule is explicit: a scan beats a typed reading, because it
    was machine-read off the QR rather than transcribed by eye; between two
    readings of the same kind the newer one wins, since the likeliest reason to
    re-enter a password is that the last one was wrong. Either way `conflicts`
    counts the disagreement, so the dashboard can flag a row that needs a human
    to look at the actual sticker.
    """
    row = conn.execute("SELECT * FROM device_labels WHERE serial = ?",
                       (entry["serial"],)).fetchone()
    if row is None:
        fresh = {c: entry.get(c) for c in _LABEL_COLS}
        fresh.update(first_seen=seen_at, last_seen=seen_at, conflicts=0)
        conn.execute(
            f"INSERT INTO device_labels ({', '.join(_LABEL_COLS)}) "
            f"VALUES ({', '.join(':' + c for c in _LABEL_COLS)})", fresh)
        return "stored"

    agrees = row["password"] == entry["password"]
    stored_wins = (not agrees
                   and row["source"] == SOURCE_SCAN
                   and entry["source"] != SOURCE_SCAN)
    merged = {c: (entry.get(c) or row[c]) for c in _LABEL_FILL_COLS}
    merged["serial"] = entry["serial"]
    merged["last_seen"] = seen_at
    merged["conflicts"] = row["conflicts"] + (0 if agrees else 1)
    # The password and everything that describes THIS reading of it move
    # together, so a row never says a password was scanned during a run that
    # actually carried a different one.
    for col in ("password", "source", "run_id", "station_id", "operator"):
        merged[col] = row[col] if stored_wins else entry.get(col)
    conn.execute(
        "UPDATE device_labels SET "
        + ", ".join(f"{c} = :{c}" for c in merged if c != "serial")
        + " WHERE serial = :serial", merged)
    if agrees:
        return "confirmed"
    return "kept" if stored_wins else "updated"


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


def create_app(db_path: Optional[Path] = None,
               repo_root: Optional[Path] = None) -> FastAPI:
    db = Path(db_path or os.environ.get("BENCH_CENTRAL_DB", "bench-central.db"))
    init_db(db)
    version = _repo_version()  # once at startup, not per health poll
    app = FastAPI(title="Bench Central Collector")

    # Fleet: pinned releases + station check-ins (fleet.py). Bundles are
    # built from this checkout and cached next to the database, so a redeploy
    # never touches them and StateDirectory covers both.
    repo = Path(repo_root or os.environ.get(
        "BENCH_CENTRAL_REPO", Path(__file__).resolve().parent.parent))
    add_fleet_routes(app, connect=lambda: _connect(db), repo=repo,
                     bundle_dir=db.parent / "bundles")

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
                  kind: Optional[str] = None,
                  status: Optional[str] = None, serial: Optional[str] = None,
                  site: Optional[str] = None, operator: Optional[str] = None,
                  q: Optional[str] = None,
                  since: Optional[str] = None, until: Optional[str] = None,
                  limit: int = Query(default=100, ge=1, le=1000),
                  offset: int = Query(default=0, ge=0)):
        clauses, params = [], []
        for col, value in (("station_id", station_id), ("tool", tool),
                           ("kind", kind),
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

    # ── the factory-password store (TEC-845) ─────────────────────────────────

    @app.post("/api/v1/device-labels")
    def ingest_label(record: dict = Body(...)):
        """Store one device's factory-label password.

        An upsert, not a create: the station ships the current best reading for
        a serial and this is where the readings are reconciled (`_store_label`).
        Unlike a run record there is no idempotency key to 409 on — the same
        device read twice is a normal thing that happens, not a retry.
        """
        entry = parse_label_record(record)
        if not entry["serial"]:
            raise HTTPException(status_code=400,
                                detail="device label has no serial to key on")
        if not entry["password"]:
            raise HTTPException(status_code=400,
                                detail="device label has no password")
        if not entry["source"]:
            raise HTTPException(
                status_code=400,
                detail=f"device label source must be one of {SOURCES}")
        seen_at = entry["captured_at"] or datetime.now(timezone.utc).isoformat()
        with closing(_connect(db)) as conn, conn:
            outcome = _store_label(conn, entry, seen_at)
        return {"stored": entry["serial"], "outcome": outcome}

    @app.get("/api/v1/device-labels")
    def list_labels(serial: Optional[str] = None, mac: Optional[str] = None,
                    tool: Optional[str] = None, q: Optional[str] = None,
                    limit: int = Query(default=100, ge=1, le=1000),
                    offset: int = Query(default=0, ge=0)):
        clauses, params = [], []
        for col, value in (("serial", serial), ("tool", tool)):
            if value is not None:
                clauses.append(f"{col} = ?")
                params.append(value)
        if mac is not None:
            # Stored canonicalized, so a query pasted from `arp` or off a
            # sticker has to be put in the same shape to match.
            clauses.append("mac = ?")
            params.append(canonical_mac(mac))
        if q:
            needle = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("(" + " OR ".join(
                f"{col} LIKE ? ESCAPE '\\'" for col in _LABEL_SEARCH_COLS) + ")")
            params.extend([f"%{needle}%"] * len(_LABEL_SEARCH_COLS))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with closing(_connect(db)) as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM device_labels {where}",
                                 params).fetchone()[0]
            rows = conn.execute(
                f"SELECT {', '.join(_LABEL_COLS)} FROM device_labels {where} "
                f"ORDER BY last_seen DESC LIMIT ? OFFSET ?",
                (*params, limit, offset)).fetchall()
        return {"count": len(rows), "total": total, "offset": offset,
                "labels": [dict(r) for r in rows]}

    @app.get("/api/v1/device-labels/{device}")
    def get_label(device: str):
        """One device's factory password, by serial — or by MAC, which is the
        handle left when the sticker a serial would be read off is the thing
        that got damaged.

        The serial is tried first and on its own, so a serial can never be
        beaten by a MAC that happens to canonicalize to the same digits.
        """
        select = f"SELECT {', '.join(_LABEL_COLS)} FROM device_labels WHERE "
        with closing(_connect(db)) as conn:
            row = conn.execute(select + "serial = ?", (device,)).fetchone()
            if row is None:
                row = conn.execute(select + "mac = ? AND mac != ''",
                                   (canonical_mac(device),)).fetchone()
        if row is None:
            raise HTTPException(status_code=404,
                                detail=f"no factory password on record for {device}")
        return dict(row)

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
            labels = conn.execute(
                "SELECT COUNT(*) FROM device_labels").fetchone()[0]
        return {"ok": True, "runs": count, "device_labels": labels,
                "version": version, "db": str(db),
                "db_bytes": db.stat().st_size}

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
