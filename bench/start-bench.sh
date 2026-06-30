#!/usr/bin/env bash
# ===========================================================================
#  Kela Bench - macOS/Linux master launcher  (the equivalent of
#  Start Bench Tools.bat on Windows)
#
#  Run this from a terminal to start ALL bench configurators at once and open
#  the dashboard in one browser tab:
#
#    Magos Radar      -> http://127.0.0.1:8001   (adapter on 192.168.40.x)
#    Magos APU        -> http://127.0.0.1:8002   (adapter on 192.168.40.x)
#    Teltonika OTD500 -> http://127.0.0.1:8003   (adapter on 192.168.1.x)
#    Teltonika RUTM08 -> http://127.0.0.1:8004   (adapter on 192.168.1.x)
#    Raythink Camera  -> http://127.0.0.1:8005   (adapter on 192.168.1.x)
#
#  All five tools share ONE .venv in this folder. Every launch pulls the latest
#  tools (git) and installs requirements (needs internet that once, then it's a
#  near-instant no-op). Set BENCH_NO_PULL=1 to freeze the on-disk version.
#
#  Usage:   ./start-bench.sh        (chmod +x once if needed)
#  Leave this window open. Press Ctrl+C here to stop everything.
# ===========================================================================
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
. "$ROOT/_lib.sh"

# Each tool opens its own browser tab unless told not to; this launcher opens
# the dashboard (which links to all five) instead. Exported so the backgrounded
# server processes inherit it.
export BENCH_NO_BROWSER=1

# ── Step 1: prepare the single shared venv + deps ──────────────────────────
echo "Preparing the shared environment (pull + install; first run ~1 min)..."
bench_ensure_venv || { read -r -p "Dependency setup failed. Press Return to close..." _; exit 1; }

# ── Step 2: start the servers in the background ─────────────────────────────
pids=()
cleanup() {
  echo
  echo "Stopping all bench tools..."
  if [ "${#pids[@]}" -gt 0 ]; then
    for p in "${pids[@]}"; do
      kill "$p" 2>/dev/null || true
    done
  fi
}
trap cleanup EXIT INT TERM

start() {  # label  folder  app
  echo ">> starting $1 ..."
  ( cd "$2" && exec "$ROOT/.venv/bin/python" "$3" ) &
  pids+=("$!")
}

start "Magos Radar"      "magos-config-ui"    "app.py"
start "Magos APU"        "magos-config-ui"    "apu_app.py"
start "Teltonika OTD500" "otd-config-ui"      "otd_app.py"
start "Teltonika RUTM08" "rutm-config-ui"     "rutm_app.py"
start "Raythink Camera"  "raythink-config-ui" "raythink_app.py"

# Give the servers a moment to come up (cold start is ~5s), then open the
# dashboard once. The dashboard re-polls, so any tool still warming up will flip
# to online on its own shortly after.
sleep 6
# Open the dashboard with whatever's available: `open` on macOS, `xdg-open`
# (or sensible-browser) on Linux, else just print the path to open manually.
DASH="$ROOT/launcher/index.html"
if command -v open >/dev/null 2>&1; then
  open "$DASH" >/dev/null 2>&1 || true
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$DASH" >/dev/null 2>&1 || true
elif command -v sensible-browser >/dev/null 2>&1; then
  sensible-browser "$DASH" >/dev/null 2>&1 || true
else
  echo "  (no browser opener found — open this in your browser: file://$DASH)"
fi

echo
echo "All five tools are running. Dashboard: launcher/index.html"
echo "Leave this window open. Press Ctrl+C to stop everything."
wait
