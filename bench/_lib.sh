#!/usr/bin/env bash
# Shared helpers for the macOS bench launchers (run_*.command, start-bench.command).
# This file is SOURCED, not executed.
#
# The bench tools need Python 3.10+ (the pinned web stack — websockets 16 — needs
# it). On macOS the stock `python3` is often 3.9, so these helpers pick the
# newest 3.10+ interpreter available and rebuild a venv that was made with an
# older one.

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

# In the current directory: ensure .venv exists on a >=3.10 Python and that
# requirements.txt is installed. Returns non-zero (with a message) on failure.
bench_ensure_venv() {
  local py
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
  .venv/bin/python -m pip install -r requirements.txt
}
