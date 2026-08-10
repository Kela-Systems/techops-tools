#!/usr/bin/env bash
# ============================================================================
#  Kela Bench - station installer (macOS/Linux)
# ----------------------------------------------------------------------------
#  One-shot setup of a fresh bench machine from bench-central. The machine
#  must already reach bench-central (tailnet or LAN). Run:
#
#    curl -fsS http://techops-automations-host:8100/setup.sh -o setup.sh
#    bash setup.sh --central-url http://techops-automations-host:8100
#
#  Optional:
#    --station-id bench-3     (default: $BENCH_STATION_ID, else this
#                              machine's hostname)
#    --dir ~/kela-bench       (default: ~/kela-bench)
#
#  What it does (idempotent - safe to re-run over an existing install):
#    1. checks for Python 3.10+ (the tools' floor)
#    2. downloads the release pinned on bench-central and extracts it
#    3. writes .bench-station.json (station id + central URL) and seeds each
#       tool's config from its committed *.example.* template
#    4. creates the shared .venv + installs requirements
#    5. verifies, checks the station in (it appears on the Fleet page), and
#       offers to launch
#
#  From then on every run of ./start-bench.sh converges the station to
#  whatever release is pinned on bench-central (updater.py).
# ============================================================================
set -uo pipefail

CENTRAL_URL="${BENCH_CENTRAL_URL:-}"
# Same precedence the rest of the stack uses for the station identity:
# explicit flag > BENCH_STATION_ID in the environment > the hostname.
STATION_ID="${BENCH_STATION_ID:-$(hostname -s 2>/dev/null || hostname)}"
INSTALL_DIR="$HOME/kela-bench"

while [ $# -gt 0 ]; do
  case "$1" in
    --central-url) CENTRAL_URL="$2"; shift 2 ;;
    --station-id)  STATION_ID="$2";  shift 2 ;;
    --dir)         INSTALL_DIR="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

fail() { echo "ERROR: $*" >&2; exit 1; }
step() { echo "==> [$1/5] $2"; }

if [ -z "$CENTRAL_URL" ]; then
  read -r -p "bench-central URL (e.g. http://techops-automations-host:8100): " CENTRAL_URL
fi
[ -n "$CENTRAL_URL" ] || fail "a bench-central URL is required"
CENTRAL_URL="${CENTRAL_URL%/}"

echo "Kela Bench station installer"
echo "  central:  $CENTRAL_URL"
echo "  station:  $STATION_ID"
echo "  into:     $INSTALL_DIR"
echo

# ---- 1. Python 3.10+ (needed for extraction, the venv, and the tools) -------
step 1 "Checking for Python 3.10+"
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  command -v "$c" >/dev/null 2>&1 || continue
  if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)' 2>/dev/null; then
    PY="$(command -v "$c")"
    break
  fi
done
[ -n "$PY" ] || fail "Python 3.10+ was not found. Install it from https://www.python.org/downloads/ or 'brew install python@3.12', then re-run."
echo "    using: $PY"

# ---- 2. bench-central reachable + download the pinned bundle ----------------
step 2 "Downloading the pinned bench release"
curl -fsS --max-time 10 "$CENTRAL_URL/api/v1/health" >/dev/null \
  || fail "bench-central is not reachable at $CENTRAL_URL — is this machine on the tailnet?"
SHA="$(curl -fsS --max-time 10 "$CENTRAL_URL/api/v1/fleet/desired" \
       | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("version") or "")')"
[ -n "$SHA" ] || fail "no release is pinned on bench-central yet — pin one on the dashboard ($CENTRAL_URL) first."
echo "    pinned release: ${SHA:0:12}"

mkdir -p "$INSTALL_DIR"
ZIP="$(mktemp -t bench-bundle.XXXXXX).zip"
curl -fsS --max-time 300 -o "$ZIP" "$CENTRAL_URL/api/v1/fleet/bundle/$SHA.zip" \
  || fail "bundle download failed"
# Extract with file modes restored (git archive records them; plain
# extractall would drop the executable bit from start-bench.sh).
"$PY" - "$ZIP" "$INSTALL_DIR" <<'PYEOF'
import os, sys, zipfile
zip_path, dest = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(zip_path) as zf:
    for info in zf.infolist():
        zf.extract(info, dest)
        mode = (info.external_attr >> 16) & 0o777
        if mode and not info.is_dir():
            os.chmod(os.path.join(dest, info.filename), mode)
PYEOF
rm -f "$ZIP"
echo "    extracted to $INSTALL_DIR"

# ---- 3. Station identity + config seeding -----------------------------------
step 3 "Writing station identity + seeding configs"
"$PY" - "$INSTALL_DIR/.bench-station.json" "$STATION_ID" "$CENTRAL_URL" <<'PYEOF'
import json, sys
path, station_id, central_url = sys.argv[1:4]
with open(path, "w", encoding="utf-8") as fh:
    json.dump({"station_id": station_id, "central_url": central_url}, fh, indent=2)
PYEOF
( cd "$INSTALL_DIR" && "$PY" scripts/updater.py --seed-configs )

# ---- 4. Shared venv + dependencies -------------------------------------------
step 4 "Creating the shared .venv + installing dependencies"
# bench_ensure_venv also re-runs the updater — a no-op right after the
# download above — and prints the running version.
( cd "$INSTALL_DIR" && . ./scripts/_lib.sh && bench_ensure_venv ) \
  || fail "environment setup failed — see the messages above."

# ---- 5. Verify + first check-in ----------------------------------------------
step 5 "Verifying"
PASS=0; FAIL=0
check() { local d="$1"; shift; if "$@" >/dev/null 2>&1; then echo "PASS  $d"; PASS=$((PASS+1)); else echo "FAIL  $d"; FAIL=$((FAIL+1)); fi; }

check "bundle stamp present (.bench-build.json)" test -f "$INSTALL_DIR/.bench-build.json"
check "installed version matches the pin" \
  "$PY" -c "import json; raise SystemExit(0 if json.load(open('$INSTALL_DIR/.bench-build.json'))['version'] == '$SHA' else 1)"
# Read the station file back through updater.py — the exact path the
# launchers use — so "written but unreadable" fails here, not silently later.
check "station file readable (central_url pinned)" \
  test "$(cd "$INSTALL_DIR" && "$PY" scripts/updater.py --print-station central_url 2>/dev/null)" = "$CENTRAL_URL"
check "launcher present + executable (start-bench.sh)" test -x "$INSTALL_DIR/start-bench.sh"
check "shared .venv usable" test -x "$INSTALL_DIR/.venv/bin/python"
check "checked in with bench-central (see the Fleet page)" \
  curl -fsS --max-time 10 -X POST -H "Content-Type: application/json" \
    -d "{\"station_id\": \"$STATION_ID\", \"hostname\": \"$(hostname)\", \"platform\": \"$(uname -s)\", \"version\": \"$SHA\"}" \
    "$CENTRAL_URL/api/v1/fleet/checkin"

echo
echo "setup-station: $PASS pass, $FAIL fail"
[ "$FAIL" -eq 0 ] || fail "fix the failures above and re-run this script."

echo
echo "Station '$STATION_ID' is installed."
echo "Launch anytime with:  cd \"$INSTALL_DIR\" && ./start-bench.sh"
read -r -p "Launch the bench tools now? [Y/n] " answer
case "${answer:-Y}" in
  [Yy]*|"") ( cd "$INSTALL_DIR" && exec ./start-bench.sh ) ;;
esac
