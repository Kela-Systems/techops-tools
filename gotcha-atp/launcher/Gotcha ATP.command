#!/bin/bash
# Gotcha ATP — macOS launcher. Double-click in Finder: starts the local UI and
# opens http://127.0.0.1:8190 in the browser. Close this window to stop it.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
  for p in /opt/homebrew/bin /usr/local/bin "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    [ -x "$p/uv" ] && export PATH="$p:$PATH" && break
  done
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not installed. Install it with:  brew install uv"
  read -r -p "Press Enter to close." _
  exit 1
fi

# WeasyPrint (the PDF report) needs pango from Homebrew on macOS.
export DYLD_FALLBACK_LIBRARY_PATH="${DYLD_FALLBACK_LIBRARY_PATH:-/opt/homebrew/lib:/usr/local/lib}"

exec uv run gotcha-atp ui
