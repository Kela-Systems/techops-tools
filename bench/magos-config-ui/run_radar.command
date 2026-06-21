#!/usr/bin/env bash
# ===========================================================================
#  Magos Radar Configurator - macOS launcher
#
#  Double-click this in Finder (it opens in Terminal). On the first run it
#  creates a local virtual environment and installs the dependencies (needs
#  internet that one time); every run after that just starts the app and opens
#  your browser.
#
#  Requires Python 3.10+ (3.11+ recommended; python.org installer or
#  `brew install python`). The launcher picks a suitable interpreter for you.
#
#  NOTE: your network adapter must be on the radar's factory subnet
#        (192.168.40.x) to reach a radar at 192.168.40.50.
#  NOTE: this folder must sit next to bench-core (the shared package it
#        installs). Keeping the whole bench/ folder together guarantees this.
# ===========================================================================
set -euo pipefail
cd "$(dirname "$0")"
. "$(dirname "$0")/../_lib.sh"

echo "Checking dependencies..."
bench_ensure_venv || { read -r -p "Press Return to close..." _; exit 1; }

echo
echo "Starting Magos Radar Configurator - a browser window will open at http://127.0.0.1:8001"
echo "Close this window or press Ctrl+C to stop the server."
echo
exec .venv/bin/python app.py
