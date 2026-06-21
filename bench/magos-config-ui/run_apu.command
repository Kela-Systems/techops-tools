#!/usr/bin/env bash
# ===========================================================================
#  Magos APU Configurator - macOS launcher
#
#  Double-click this in Finder (it opens in Terminal). First run creates a local
#  virtual environment and installs dependencies (needs internet once); later
#  runs just start the app and open your browser.
#
#  Requires Python 3.10+ (3.11+ recommended; python.org installer or
#  `brew install python`). The launcher picks a suitable interpreter for you.
#
#  NOTE: your network adapter must be on the APU's factory subnet
#        (192.168.40.x) to reach an APU at 192.168.40.60.
#  NOTE: this folder must sit next to bench-core (the shared package it
#        installs). Keeping the whole bench/ folder together guarantees this.
#
#  Runs on port 8002, independently of the radar tool (8001), so both can run
#  at the same time.
# ===========================================================================
set -euo pipefail
cd "$(dirname "$0")"
. "$(dirname "$0")/../_lib.sh"

echo "Checking dependencies..."
bench_ensure_venv || { read -r -p "Press Return to close..." _; exit 1; }

echo
echo "Starting Magos APU Configurator - a browser window will open at http://127.0.0.1:8002"
echo "Close this window or press Ctrl+C to stop the server."
echo
exec .venv/bin/python apu_app.py
