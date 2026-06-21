#!/usr/bin/env bash
# ===========================================================================
#  RUTM08 Configurator - macOS launcher
#
#  Double-click this in Finder (it opens in Terminal). First run creates a local
#  virtual environment and installs dependencies (needs internet once); later
#  runs just start the app and open your browser.
#
#  Requires Python 3.10+ (3.11+ recommended; python.org installer or
#  `brew install python`). The launcher picks a suitable interpreter for you.
#
#  NOTE: your network adapter must be on the RUTM08's factory subnet
#        (192.168.1.x) to reach the device at 192.168.1.1. After the run the
#        device moves to 192.168.88.1 — keep the adapter on DHCP so it follows.
#  NOTE: this folder must sit next to bench-core (the shared package it
#        installs). Keeping the whole bench/ folder together guarantees this.
#
#  Runs on port 8004, independently of the radar (8001) / APU (8002) /
#  OTD (8003) tools.
#
#  Before first use: copy rutm.config.example.json -> rutm.config.json and
#  fill in real values.
# ===========================================================================
set -euo pipefail
cd "$(dirname "$0")"
. "$(dirname "$0")/../_lib.sh"

echo "Checking dependencies..."
bench_ensure_venv || { read -r -p "Press Return to close..." _; exit 1; }

echo
echo "Starting RUTM08 Configurator - a browser window will open at http://127.0.0.1:8004"
echo "Close this window or press Ctrl+C to stop the server."
echo
exec .venv/bin/python rutm_app.py
