#!/usr/bin/env bash
# Shared helpers for the macOS/Linux bench launcher (start-bench.sh).
# This file lives in scripts/ and is SOURCED, not executed; every helper
# assumes the caller's working directory is the bench root.
#
# The bench tools need Python 3.10+ (the pinned web stack — websockets 16 — needs
# it). On macOS the stock `python3` is often 3.9, so these helpers pick the
# newest 3.10+ interpreter available and rebuild a venv that was made with an
# older one.
#
# All the tools share ONE virtual environment at the bench root (this folder).

# Echo the path of the newest available Python >= 3.10, or return 1.
bench_pick_python() {
  local c
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)' 2>/dev/null; then
      command -v "$c"
      return 0
    fi
  done
  return 1
}

# A Python for updater.py (stdlib-only, so any python3 will do — no 3.10
# floor). Used before the venv exists, and for the tiny stamp/station reads.
bench_updater_python() {
  bench_pick_python 2>/dev/null && return 0
  command -v python3 >/dev/null 2>&1 && { command -v python3; return 0; }
  return 1
}

# Best-effort convergence to the release pinned on bench-central (replaces the
# old blind `git pull` of main). updater.py is fail-open by design: offline, no
# .bench-station.json, a git (dev) checkout, or BENCH_NO_PULL=1 all just print
# why and launch what's on disk. It also checks this station in, so the fleet
# page on bench-central knows what this bench is running.
bench_update() {
  local py
  py="$(bench_updater_python)" || {
    echo "  (no python3 found for the updater — launching what's on disk)"
    return 0
  }
  echo "  checking bench-central for the pinned bench release..."
  "$py" scripts/updater.py || echo "  [warn] updater failed — launching what's on disk."
}

# Short version this copy is running: the .bench-build.json stamp bench-central
# injects into every bundle, with `git rev-parse` as updater.py's fallback for
# engineer checkouts. Recorded at launch so 5+ stations can be told apart.
bench_version() {
  local py
  py="$(bench_updater_python)" || { echo "unknown"; return 0; }
  "$py" scripts/updater.py --print-version 2>/dev/null || echo "unknown"
}

# Export BENCH_STATION_ID / BENCH_CENTRAL_URL from .bench-station.json (written
# once by setup-station.sh) so nobody has to remember to set env vars on a
# fresh bench machine. Values already in the environment win.
bench_export_station_env() {
  local py value
  py="$(bench_updater_python)" || return 0
  if [ -z "${BENCH_STATION_ID:-}" ]; then
    value="$("$py" scripts/updater.py --print-station station_id 2>/dev/null)"
    [ -n "$value" ] && export BENCH_STATION_ID="$value"
  fi
  if [ -z "${BENCH_CENTRAL_URL:-}" ]; then
    value="$("$py" scripts/updater.py --print-station central_url 2>/dev/null)"
    [ -n "$value" ] && export BENCH_CENTRAL_URL="$value"
  fi
}

# In the current directory (the bench root): update, ensure .venv exists on a
# >=3.10 Python, and (re)install requirements.txt on every launch. Returns
# non-zero (with a message) on failure.
bench_ensure_venv() {
  local py
  bench_update
  echo "  bench version: $(bench_version)"
  if [ -x ".venv/bin/python" ] && \
     ! .venv/bin/python -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)' 2>/dev/null; then
    echo "  (existing .venv uses an unsupported Python — recreating)"
    rm -rf .venv
  fi
  if [ ! -x ".venv/bin/python" ]; then
    py="$(bench_pick_python)" || {
      echo "Need Python 3.10+ but only an older one was found."
      echo "Install it from https://www.python.org/downloads/ or 'brew install python@3.12'."
      return 1
    }
    echo "  creating .venv with $py ..."
    "$py" -m venv .venv
    .venv/bin/python -m pip install --upgrade pip >/dev/null
  fi
  # Fail-open, like bench_update: an offline bench (the device subnets have no
  # internet) or a flaky pip index must not block a launch when the .venv already
  # has working packages. We only hard-abort above when .venv can't be created.
  .venv/bin/python -m pip install -r requirements.txt \
    || echo "  [warn] dependency install failed (offline, or a flaky pip index) — launching with the packages already in .venv."
}
