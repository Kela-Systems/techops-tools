#!/usr/bin/env python3
"""Bench station updater — converge this station to the centrally-pinned
release, and check in.

The station half of push updates: bench-central holds a pinned release
(bench-central/fleet.py) and builds code bundles with `git archive`; this
script — run by the launchers before anything starts — downloads the pinned
bundle when it differs from what's on disk and extracts it over this folder.
Stations need no git and no GitHub credentials.

Everything here is FAIL-OPEN, like the git pull it replaces: an offline
bench, an unreachable central, or a half-broken zip must never block a
launch — warn and launch what's on disk. Exit code is always 0 for the
update flow.

Identity comes from two gitignored files at the bench root:

    .bench-station.json   {"station_id": ..., "central_url": ...}
                          written once by the installer (setup-station.*);
                          env BENCH_STATION_ID / BENCH_CENTRAL_URL override
    .bench-build.json     {"version": <full sha>, "built_at": ...}
                          stamped into every bundle by bench-central; this is
                          the version compared against the pin (and surfaced
                          as BENCH_VERSION)

Rules the update obeys:

    * BENCH_NO_PULL=1     skip the update (freeze the on-disk version), but
                          still check in best-effort so the fleet page stays
                          honest about what this station runs
    * a git checkout      (a .git here or one level up) is an engineer's
                          working copy — never self-update it
    * local state         configs, .venv, logs, the two .bench-*.json files —
                          none of it is in the bundle (gitignored files never
                          reach the archive), so extraction can't touch it;
                          files are also only rewritten when their content
                          actually changed, and via write-tempfile-then-rename
                          so a script the shell is mid-way through reading is
                          swapped whole, never truncated in place
    * deletions           files removed from the repo are NOT removed here
                          (documented trade-off; orphans are harmless)

Also callable by the launchers/installers for the plumbing they'd otherwise
each reimplement (paths relative to the bench root):

    scripts/updater.py --print-version      the short (12-char) stamped
                                    version, git rev-parse as the dev-checkout
                                    fallback, else "unknown"
    scripts/updater.py --print-station KEY  a value from .bench-station.json
                                    ("" if absent) — the launchers export
                                    BENCH_STATION_ID / BENCH_CENTRAL_URL this way
    scripts/updater.py --seed-configs       copy every committed *.example.*
                                    template to its real name where missing
                                    (install time; config_check.py nags about
                                    the placeholder values from launch one)
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

# This file lives in bench/scripts/; the bench root (what bundles extract
# over, and where the station-state files live) is one level up.
ROOT = Path(__file__).resolve().parent.parent
STATION_FILE = ROOT / ".bench-station.json"
STAMP_FILE = ROOT / ".bench-build.json"
HTTP_TIMEOUT = 10          # desired/check-in calls
DOWNLOAD_TIMEOUT = 120     # the bundle itself (a few MB over the tailnet)

# Never seed templates from (or into) local-state trees.
_SEED_SKIP_DIRS = {".venv", ".git", "logs", "firmware", "__pycache__",
                   ".pytest_cache", "node_modules"}


def _say(msg: str) -> None:
    print(f"  {msg}")


def _load_json(path: Path) -> dict:
    # utf-8-sig, not utf-8: early setup-station.ps1 wrote the station file
    # with a UTF-8 BOM (Windows PowerShell 5.1's Set-Content), and json.loads
    # rejects a BOM — the file would silently read as {} on those stations.
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def station_config() -> dict:
    """Station identity: the installer-written file, with env overrides (the
    pre-fleet way of configuring a station keeps working)."""
    cfg = _load_json(STATION_FILE)
    if os.environ.get("BENCH_STATION_ID"):
        cfg["station_id"] = os.environ["BENCH_STATION_ID"]
    if os.environ.get("BENCH_CENTRAL_URL"):
        cfg["central_url"] = os.environ["BENCH_CENTRAL_URL"]
    cfg.setdefault("station_id", platform.node() or "unknown-station")
    return cfg


def local_version() -> Optional[str]:
    """The full sha this copy was bundled from, or None (dev checkout, or a
    hand-copied folder that never came from a bundle)."""
    return _load_json(STAMP_FILE).get("version") or None


def short_version() -> str:
    """For --print-version / BENCH_VERSION: the stamped version, git as the
    dev-checkout fallback, else 'unknown'."""
    version = local_version()
    if version:
        return version[:12]
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=str(ROOT), capture_output=True, text=True,
                             timeout=3)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def is_dev_checkout() -> bool:
    return (ROOT / ".git").exists() or (ROOT.parent / ".git").exists()


def _api(base: str, path: str, payload: Optional[dict] = None,
         timeout: int = HTTP_TIMEOUT) -> dict:
    request = urllib.request.Request(base.rstrip("/") + path)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, data=data, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _extract_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bool:
    """Extract one member; returns True if the file was (re)written.

    Unchanged files are left alone, and changed ones are swapped in whole via
    tempfile + os.replace — an in-place truncate-and-rewrite would yank the
    bytes out from under the very launcher scripts that invoked us (both bash
    and cmd.exe read scripts incrementally)."""
    target = (ROOT / info.filename).resolve()
    if ROOT not in target.parents and target != ROOT:
        raise ValueError(f"bundle member escapes the bench root: {info.filename}")
    if info.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        return False
    content = zf.read(info)
    try:
        if target.read_bytes() == content:
            return False
    except OSError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent),
                                    prefix=f".{target.name}.", suffix=".update")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        mode = (info.external_attr >> 16) & 0o777  # git archive keeps modes
        if mode:
            os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return True


def apply_bundle(zip_path: Path) -> int:
    """Extract a downloaded bundle over the bench root. Returns how many
    files changed. Local state is safe by construction: gitignored files are
    never in a `git archive`, so they are never members here."""
    changed = 0
    failed = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            try:
                if _extract_member(zf, info):
                    changed += 1
            except OSError as exc:
                failed.append(f"{info.filename} ({exc})")
    if failed:
        _say(f"[warn] {len(failed)} file(s) could not be updated "
             f"(in use?): {', '.join(failed[:3])}"
             + (" ..." if len(failed) > 3 else ""))
    return changed


def update(central_url: str) -> None:
    """Converge to the pin (unless frozen), fail-open on everything."""
    if os.environ.get("BENCH_NO_PULL", "0") == "1":
        _say("(BENCH_NO_PULL=1 — skipping the update; launching what's on disk)")
        return
    try:
        desired = _api(central_url, "/api/v1/fleet/desired").get("version")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _say(f"[warn] could not reach bench-central at {central_url} ({exc}) "
             "— launching what's on disk.")
        return
    if not desired:
        _say("(no release pinned on bench-central — launching what's on disk)")
        return
    current = local_version()
    if current == desired:
        _say(f"bench is on the pinned release ({desired[:12]}).")
        return
    _say(f"updating {(current or 'unknown')[:12]} -> {desired[:12]} ...")
    try:
        url = (central_url.rstrip("/")
               + f"/api/v1/fleet/bundle/{desired}.zip")
        with tempfile.NamedTemporaryFile(dir=str(ROOT), suffix=".bundle.zip",
                                         delete=False) as tmp:
            tmp_path = Path(tmp.name)
            with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as resp:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    tmp.write(chunk)
        try:
            changed = apply_bundle(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)
    except (urllib.error.URLError, OSError, ValueError,
            zipfile.BadZipFile) as exc:
        _say(f"[warn] update failed ({exc}) — launching what's on disk.")
        return
    _say(f"updated to {desired[:12]} ({changed} file(s) changed).")


def checkin(central_url: str, station_id: str) -> None:
    """Best-effort: the fleet page's 'what is this station running' row."""
    try:
        _api(central_url, "/api/v1/fleet/checkin", payload={
            "station_id": station_id,
            "hostname": platform.node(),
            "platform": platform.system(),
            "version": local_version() or short_version(),
        })
    except (urllib.error.URLError, OSError, ValueError):
        _say("[warn] check-in with bench-central failed (offline?).")


def seed_configs() -> None:
    """Copy every committed *.example.* template to its live name where the
    live file is missing (install time). config_check.py takes it from there,
    nagging about the placeholder values at every launch."""
    seeded = 0
    for example in sorted(ROOT.rglob("*.example.*")):
        if _SEED_SKIP_DIRS.intersection(example.relative_to(ROOT).parts):
            continue
        target = example.with_name(example.name.replace(".example", "", 1))
        if target.exists():
            continue
        target.write_bytes(example.read_bytes())
        _say(f"seeded {target.relative_to(ROOT)} (from {example.name})")
        seeded += 1
    _say(f"config seeding done ({seeded} file(s) created).")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--print-version", action="store_true",
                      help="print the short bench version and exit")
    mode.add_argument("--print-station", metavar="KEY",
                      help="print a .bench-station.json value and exit")
    mode.add_argument("--seed-configs", action="store_true",
                      help="seed missing configs from *.example.* templates")
    args = parser.parse_args()

    if args.print_version:
        print(short_version())
        return 0
    if args.print_station:
        print(station_config().get(args.print_station) or "")
        return 0
    if args.seed_configs:
        seed_configs()
        return 0

    if is_dev_checkout():
        _say("(git checkout detected — dev mode, no self-update/check-in)")
        return 0
    cfg = station_config()
    central_url = cfg.get("central_url")
    if not central_url:
        _say("(no .bench-station.json / BENCH_CENTRAL_URL — skipping "
             "central update; launching what's on disk)")
        return 0
    update(central_url)
    checkin(central_url, cfg["station_id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
