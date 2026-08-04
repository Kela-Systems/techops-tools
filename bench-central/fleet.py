#!/usr/bin/env python3
"""Fleet management for bench stations — pinned releases + check-ins.

The push half of "push updates to the benches": an admin pins a release here
(any git ref, resolved to a SHA at pin time), and every station converges to
that pin at launch. The station half lives in bench/scripts/updater.py: it asks for
the desired version, downloads the bundle when it differs from what's on
disk, and checks in with what it is actually running. Stations need no git
and no GitHub credentials — this service builds the code bundle itself with
`git archive` from its own repo checkout.

API (mounted by collector.create_app, same no-auth/tailnet-perimeter model):

    GET  /api/v1/fleet/desired            {"version": sha|null, "ref", "pinned_at"}
    POST /api/v1/fleet/desired            body {"ref": "main"|tag|sha}
         200  pinned (bundle built + cached) {"version": sha, ...}
         400  unknown ref, or the ref has no bench/ subtree
    GET  /api/v1/fleet/bundle/{sha}.zip   the bench/ subtree at that SHA, with
                                          a .bench-build.json stamp injected
    POST /api/v1/fleet/checkin            {station_id, hostname?, platform?,
                                          version?} -> {"ok", "desired"}
    GET  /api/v1/fleet/stations           fleet listing for the dashboard
    GET  /setup.ps1, /setup.sh            the station installers (served from
                                          the pinned SHA, so installer and
                                          code always match)

Bundles are cached next to the database (<db dir>/bundles/<sha>.zip) and
rebuilt on demand if the cache is cleared. Pinning "main" resolves
origin/main after a best-effort `git fetch`, so the pin means "main on
GitHub right now", not whatever the deploy checkout happens to have.
"""
from __future__ import annotations

import json
import re
import subprocess
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

FLEET_SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    station_id TEXT PRIMARY KEY,
    hostname   TEXT,
    platform   TEXT,
    version    TEXT,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fleet_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# The subtree stations run, and the stamp the updater compares against the pin.
BUNDLE_SUBTREE = "bench"
STAMP_NAME = ".bench-build.json"

_INSTALLERS = {
    "setup.ps1": f"{BUNDLE_SUBTREE}/scripts/setup-station.ps1",
    "setup.sh": f"{BUNDLE_SUBTREE}/scripts/setup-station.sh",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git(repo: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, timeout=timeout)


def git_fetch(repo: Path) -> None:
    """Best-effort fetch so a pin of 'main' means main on GitHub right now.
    A repo with no remote (tests, air-gapped deploys) just resolves locally."""
    _git(repo, "fetch", "--tags", "origin", timeout=120)


def resolve_ref(repo: Path, ref: str) -> Optional[str]:
    """The full commit SHA for a ref, or None. origin/<ref> wins over the
    local <ref>: the deploy checkout's own branches are usually stale."""
    for candidate in (f"origin/{ref}", ref):
        out = _git(repo, "rev-parse", "--verify", "--quiet",
                   f"{candidate}^{{commit}}")
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    return None


def build_bundle(repo: Path, sha: str, dest: Path) -> None:
    """git-archive the bench/ subtree at `sha` into `dest`, with the
    .bench-build.json stamp the station updater reads. Atomic: a station
    downloading mid-build can never see a half-written zip."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".building")
    out = _git(repo, "archive", "--format=zip", "-o", str(tmp),
               f"{sha}:{BUNDLE_SUBTREE}", timeout=120)
    if out.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise ValueError(
            f"git archive failed for {sha}:{BUNDLE_SUBTREE} — "
            f"{out.stderr.strip() or 'is bench/ present at that revision?'}")
    with zipfile.ZipFile(tmp, "a") as zf:
        zf.writestr(STAMP_NAME, json.dumps(
            {"version": sha, "built_at": _now()}, indent=2))
    tmp.replace(dest)


def add_fleet_routes(app: FastAPI, connect: Callable, repo: Path,
                     bundle_dir: Path) -> None:
    """Mount the fleet endpoints. `connect` is the collector's DB opener (the
    fleet tables live in the same SQLite file as the run records)."""

    def desired() -> dict:
        with closing(connect()) as conn:
            rows = {r["key"]: r["value"] for r in conn.execute(
                "SELECT key, value FROM fleet_state")}
        return {"version": rows.get("desired_version"),
                "ref": rows.get("desired_ref"),
                "pinned_at": rows.get("pinned_at")}

    def bundle_path(sha: str) -> Path:
        return bundle_dir / f"{sha}.zip"

    @app.get("/api/v1/fleet/desired")
    def get_desired():
        return desired()

    @app.post("/api/v1/fleet/desired")
    def set_desired(body: dict = Body(...)):
        ref = (body.get("ref") or "").strip()
        if not ref:
            raise HTTPException(status_code=400, detail="body needs a 'ref'")
        git_fetch(repo)
        sha = resolve_ref(repo, ref)
        if sha is None:
            raise HTTPException(status_code=400,
                                detail=f"unknown ref {ref!r} (after fetch)")
        # Build (and cache) the bundle NOW — a pin whose bundle can't be
        # built must fail here, at the admin's face, not at station launch.
        try:
            if not bundle_path(sha).exists():
                build_bundle(repo, sha, bundle_path(sha))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        with closing(connect()) as conn, conn:
            conn.executemany(
                "INSERT INTO fleet_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [("desired_version", sha), ("desired_ref", ref),
                 ("pinned_at", _now())])
        return desired()

    @app.get("/api/v1/fleet/bundle/{sha}.zip")
    def get_bundle(sha: str):
        if not _SHA_RE.match(sha):
            raise HTTPException(status_code=400,
                                detail="bundle name must be a full commit sha")
        path = bundle_path(sha)
        if not path.exists():
            # Cache cleared (or a station asking for an old pin): rebuild if
            # the SHA is real, 404 if it never was.
            if resolve_ref(repo, sha) != sha:
                raise HTTPException(status_code=404, detail=f"no bundle {sha}")
            try:
                build_bundle(repo, sha, path)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
        return FileResponse(path, media_type="application/zip",
                            filename=f"bench-{sha[:12]}.zip")

    @app.post("/api/v1/fleet/checkin")
    def checkin(body: dict = Body(...)):
        station_id = (body.get("station_id") or "").strip()
        if not station_id:
            raise HTTPException(status_code=400,
                                detail="body needs a 'station_id'")
        now = _now()
        with closing(connect()) as conn, conn:
            conn.execute(
                "INSERT INTO stations (station_id, hostname, platform, "
                "version, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(station_id) DO UPDATE SET "
                "hostname = excluded.hostname, platform = excluded.platform, "
                "version = excluded.version, last_seen = excluded.last_seen",
                (station_id, body.get("hostname"), body.get("platform"),
                 body.get("version"), now, now))
        return {"ok": True, "desired": desired()["version"]}

    @app.get("/api/v1/fleet/stations")
    def stations():
        with closing(connect()) as conn:
            rows = conn.execute(
                "SELECT station_id, hostname, platform, version, first_seen, "
                "last_seen FROM stations ORDER BY station_id").fetchall()
        return {"desired": desired(), "stations": [dict(r) for r in rows]}

    def installer(name: str) -> PlainTextResponse:
        # Serve from the pinned SHA so the installer a new PC runs always
        # matches the code it installs; fall back to the working tree when
        # nothing is pinned yet (bootstrap of the very first station).
        relpath = _INSTALLERS[name]
        sha = desired()["version"]
        if sha:
            out = _git(repo, "show", f"{sha}:{relpath}")
            if out.returncode == 0:
                return PlainTextResponse(out.stdout)
        path = repo / relpath
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"no {name} available")
        return PlainTextResponse(path.read_text(encoding="utf-8"))

    @app.get("/setup.ps1", include_in_schema=False)
    def setup_ps1():
        return installer("setup.ps1")

    @app.get("/setup.sh", include_in_schema=False)
    def setup_sh():
        return installer("setup.sh")
