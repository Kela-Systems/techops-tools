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
#    ISR Speaker      -> http://127.0.0.1:8006   (DHCP — scans 192.168.1/2/88.x)
#
#  All the tools share ONE .venv in this folder. Every launch converges to the
#  release pinned on bench-central (updater.py) and installs requirements
#  (needs the tailnet/internet that once, then it's a near-instant no-op).
#  Set BENCH_NO_PULL=1 to freeze the on-disk version.
#
#  Usage:   ./start-bench.sh        (chmod +x once if needed)
#  Leave this window open. Press Ctrl+C here to stop everything.
# ===========================================================================
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
. "$ROOT/scripts/_lib.sh"

# The tools never auto-open their own browser tab (that's opt-in via
# BENCH_OPEN_BROWSER=1); this launcher opens the one dashboard itself below.

TOOL_PORTS="8001 8002 8003 8004 8005 8006"
DASH="$ROOT/launcher/index.html"

# True if something is already listening on 127.0.0.1:$1 (bash /dev/tcp — no nc
# dependency). A successful connect means a server owns the port.
port_in_use() {
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && { exec 3>&- 3<&-; return 0; }
  return 1
}

# Open the dashboard with whatever's available: `open` on macOS, `xdg-open`
# (or sensible-browser) on Linux, else just print the path to open manually.
open_dashboard() {
  if command -v open >/dev/null 2>&1; then open "$DASH" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$DASH" >/dev/null 2>&1 || true
  elif command -v sensible-browser >/dev/null 2>&1; then sensible-browser "$DASH" >/dev/null 2>&1 || true
  else echo "  (no browser opener found — open this in your browser: file://$DASH)"; fi
}

# ── Guard against a double-launch ───────────────────────────────────────────
# If a bench is already running, a second set would just fail to bind all the
# tool ports (errors scrolling past) while the dashboard shows the FIRST instance's
# green dots. Detect it and only (re)open the dashboard instead.
for p in $TOOL_PORTS; do
  if port_in_use "$p"; then
    echo "A bench instance is already running (port $p is in use) — opening its dashboard."
    open_dashboard
    exit 0
  fi
done

# ── Step 1: prepare the single shared venv + deps ──────────────────────────
echo "Preparing the shared environment (update + install; first run ~1 min)..."
bench_ensure_venv || { read -r -p "Dependency setup failed. Press Return to close..." _; exit 1; }

# ── Step 2: start the servers in the background ─────────────────────────────
# Pass the running version to every tool so it can log/surface it (traceability),
# and the station identity from .bench-station.json (installer-written) so
# run records and central shipping work without hand-set env vars.
export BENCH_VERSION="$(bench_version)"
bench_export_station_env
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
start "ISR Speaker"      "speaker-config-ui"  "speaker_app.py"

# Open the dashboard immediately — it self-polls every few seconds, so any tool
# still doing its ~5s cold start shows a grey dot that flips green on its own.
open_dashboard

echo
echo "All bench tools are running (bench version: ${BENCH_VERSION:-unknown}). Dashboard: launcher/index.html"
echo "Leave this window open. Press Ctrl+C to stop everything."
wait
