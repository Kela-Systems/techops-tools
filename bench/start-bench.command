#!/usr/bin/env bash
# ===========================================================================
#  Kela Bench - macOS master launcher  (the equivalent of START HERE.bat)
#
#  Double-click this in Finder to start ALL bench configurators at once and open
#  the dashboard in one browser tab:
#
#    Magos Radar      -> http://127.0.0.1:8001   (adapter on 192.168.40.x)
#    Magos APU        -> http://127.0.0.1:8002   (adapter on 192.168.40.x)
#    Teltonika OTD500 -> http://127.0.0.1:8003   (adapter on 192.168.1.x)
#    Teltonika RUTM08 -> http://127.0.0.1:8004   (adapter on 192.168.1.x)
#    Raythink Camera  -> http://127.0.0.1:8005   (adapter on 192.168.1.x)
#
#  First run creates each tool's .venv and installs deps (needs internet that
#  once). To run just one tool, double-click its own run_*.command instead.
#
#  Leave this window open. Press Ctrl+C here to stop everything.
# ===========================================================================
set -uo pipefail
cd "$(dirname "$0")"
. "$(dirname "$0")/_lib.sh"

# Each tool opens its own browser tab unless told not to; this launcher opens
# the dashboard (which links to all four) instead. Exported so the backgrounded
# server processes inherit it.
export BENCH_NO_BROWSER=1

# ── Step 1: prepare each tool's venv + deps SEQUENTIALLY ────────────────────
# The radar and APU share one folder/venv, so installing them concurrently would
# race on pip. Doing setup serially up front avoids that and keeps first-run
# output readable; once installed it's a near-instant no-op.
setup_tool() {  # folder
  echo ">> preparing $1 ..."
  ( cd "$1" && bench_ensure_venv ) || echo "!! dependency setup failed for $1"
}

echo "Preparing tools (first run installs dependencies, ~1 min)..."
setup_tool "magos-config-ui"
setup_tool "otd-config-ui"
setup_tool "rutm-config-ui"
setup_tool "raythink-config-ui"

# ── Step 2: start the servers in the background ─────────────────────────────
pids=()
cleanup() {
  echo
  echo "Stopping all bench tools..."
  # Guard the loop: macOS ships bash 3.2, where "${arr[@]}" on an empty array
  # trips `set -u`.
  if [ "${#pids[@]}" -gt 0 ]; then
    for p in "${pids[@]}"; do
      kill "$p" 2>/dev/null || true
    done
  fi
}
trap cleanup EXIT INT TERM

start() {  # label  folder  app
  echo ">> starting $1 ..."
  ( cd "$2" && exec .venv/bin/python "$3" ) &
  pids+=("$!")
}

start "Magos Radar"      "magos-config-ui" "app.py"
start "Magos APU"        "magos-config-ui" "apu_app.py"
start "Teltonika OTD500" "otd-config-ui"   "otd_app.py"
start "Teltonika RUTM08" "rutm-config-ui"  "rutm_app.py"
start "Raythink Camera"  "raythink-config-ui" "raythink_app.py"

# Give the servers a moment to come up (cold start is ~5s), then open the
# dashboard once. The dashboard re-polls, so any tool still warming up will flip
# to online on its own shortly after.
sleep 6
open "launcher/index.html" 2>/dev/null || true

echo
echo "All five tools are running. Dashboard: launcher/index.html"
echo "Leave this window open. Press Ctrl+C to stop everything."
wait
