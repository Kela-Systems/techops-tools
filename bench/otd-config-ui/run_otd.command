#!/usr/bin/env bash
# ===========================================================================
#  OTD500 Configurator - macOS launcher
#
#  Double-click this in Finder (it opens in Terminal). First run creates a local
#  virtual environment and installs dependencies (needs internet once); later
#  runs just start the app and open your browser.
#
#  Requires Python 3.10+ (3.11+ recommended; python.org installer or
#  `brew install python`). The launcher picks a suitable interpreter for you.
#
#  NOTE: your network adapter must be on the OTD500's factory subnet
#        (192.168.1.x) to reach the device at 192.168.1.1.
#  NOTE: this folder must sit next to bench-core (the shared package it
#        installs). Keeping the whole bench/ folder together guarantees this.
#
#  Runs on port 8003, independently of the radar (8001) / APU (8002) tools.
#
#  Before first use: copy site.config.example.json -> site.config.json and
#  manifest.example.csv -> manifest.csv, then fill in real values.
# ===========================================================================
set -euo pipefail
cd "$(dirname "$0")"
. "$(dirname "$0")/../_lib.sh"

echo "Checking dependencies..."
bench_ensure_venv || { read -r -p "Press Return to close..." _; exit 1; }

echo
echo "Starting OTD500 Configurator - a browser window will open at http://127.0.0.1:8003"
echo "Close this window or press Ctrl+C to stop the server."
echo
exec .venv/bin/python otd_app.py
