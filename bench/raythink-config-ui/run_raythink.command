#!/usr/bin/env bash
# ===========================================================================
#  Raythink Camera Configurator - macOS launcher
#
#  Double-click this in Finder (it opens in Terminal). First run creates a local
#  virtual environment and installs dependencies (needs internet once); later
#  runs just start the app and open your browser.
#
#  Requires Python 3.10+ (3.11+ recommended; python.org installer or
#  `brew install python`). The launcher picks a suitable interpreter for you.
#
#  NOTE: your network adapter must be on the camera's factory subnet
#        (192.168.1.x) to reach it at 192.168.1.123. After the run the camera
#        moves to 192.168.88.XX — keep the adapter able to reach that subnet to
#        confirm the move (DHCP or a 192.168.88.x address).
#  NOTE: this folder must sit next to bench-core (the shared package it
#        installs). Keeping the whole bench/ folder together guarantees this.
#
#  Runs on port 8005, independently of the radar (8001) / APU (8002) /
#  OTD (8003) / RUTM (8004) tools.
#
#  Before first use: copy raythink.config.example.json -> raythink.config.json,
#  and drop your real profiles/lan.json + profiles/cellular.json in place.
# ===========================================================================
set -euo pipefail
cd "$(dirname "$0")"
. "$(dirname "$0")/../_lib.sh"

echo "Checking dependencies..."
bench_ensure_venv || { read -r -p "Press Return to close..." _; exit 1; }

echo
echo "Starting Raythink Camera Configurator - a browser window will open at http://127.0.0.1:8005"
echo "Close this window or press Ctrl+C to stop the server."
echo
exec .venv/bin/python raythink_app.py
