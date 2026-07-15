#!/usr/bin/env bash
# Shared helpers for the macOS/Linux bench launcher (start-bench.sh).
# This file is SOURCED, not executed.
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

# Best-effort fast-forward pull so the bench always launches the latest tools.
# Skipped entirely when BENCH_NO_PULL=1 (freeze a known-good state mid-run).
# Offline, or local edits that block a fast-forward, just warn and continue.
bench_git_pull() {
  if [ "${BENCH_NO_PULL:-0}" = "1" ]; then
    echo "  (BENCH_NO_PULL=1 — skipping git pull; launching what's on disk)"
    return 0
  fi
  if ! command -v git >/dev/null 2>&1; then
    echo "  (git not found — launching the version already on disk)"
    return 0
  fi
  echo "  updating to the latest bench tools (git pull)..."
  git pull --ff-only || echo "  [warn] git pull failed (offline, or local changes) — launching what's on disk."
}

# Short git revision this checkout is on, or "unknown" (no git / not a repo).
# Recorded at launch so 5+ stations can be told apart when debugging.
bench_version() {
  if command -v git >/dev/null 2>&1; then
    git rev-parse --short HEAD 2>/dev/null || echo "unknown"
  else
    echo "unknown"
  fi
}

# In the current directory (the bench root): pull, ensure .venv exists on a
# >=3.10 Python, and (re)install requirements.txt on every launch. Returns
# non-zero (with a message) on failure.
bench_ensure_venv() {
  local py
  bench_git_pull
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
  # Fail-open, like bench_git_pull: an offline bench (the device subnets have no
  # internet) or a flaky pip index must not block a launch when the .venv already
  # has working packages. We only hard-abort above when .venv can't be created.
  .venv/bin/python -m pip install -r requirements.txt \
    || echo "  [warn] dependency install failed (offline, or a flaky pip index) — launching with the packages already in .venv."
}
