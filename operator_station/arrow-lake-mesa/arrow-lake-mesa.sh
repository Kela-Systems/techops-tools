#!/usr/bin/env bash
# arrow-lake-mesa.sh — fix software rendering on Arrow Lake operator stations (TEC-880).
#
# Stations with an Intel Arrow Lake GPU (PCI 8086:7d67) on the Ubuntu 22.04 image ship
# Mesa 23.2.1, which does not know that GPU, so Chrome and the desktop draw on the CPU.
# A newer Mesa (kisak "turtle" PPA) fixes the drawing; video decode stays on the CPU.
#
# Run from your laptop. Every step SSHes to the station; sudo steps ask for the
# KelaAdmin password on the station (it is never stored or passed by this script).
set -euo pipefail

PPA="ppa:kisak/turtle"
PKGS="libgl1-mesa-dri libglx-mesa0 libegl-mesa0 libgbm1 mesa-vulkan-drivers"
USER_NAME="${ARROW_USER:-KelaAdmin}"
REPORT_DIR="${ARROW_REPORT_DIR:-./arrow-lake-reports}"
SSH_EXTRA="${ARROW_SSH_OPTS:-}"   # e.g. "-i ~/.ssh/my_key -o IdentitiesOnly=yes"

usage() {
  cat <<'EOF'
Usage: arrow-lake-mesa.sh <command> <station-host> [options]

Commands
  check     <host> [--label NAME]   Read-only snapshot of the station (GPU, Mesa, CPU
                                    pressure, compositor/Chrome CPU, Chrome's own GPU
                                    report). Saved to ./arrow-lake-reports/.
  apply     <host> [--no-reboot] [--force]
                                    Pre-checks, saves a "before" snapshot, installs the
                                    newer Mesa, then reboots the station (asks first).
  verify    <host> [--before FILE]  Takes an "after" snapshot and compares it with the
                                    latest "before" for this host. Prints PASS / FAIL.
  rollback  <host> [--no-reboot]    Removes the PPA and returns to Ubuntu's Mesa, reboots.

Examples
  arrow-lake-mesa.sh check   kela-fob-08-operator
  arrow-lake-mesa.sh apply   kela-fob-08-operator
  # ...wait ~5 min after the reboot, until the kiosk is back on every screen...
  arrow-lake-mesa.sh verify  kela-fob-08-operator

Environment
  ARROW_USER        SSH user (default KelaAdmin)
  ARROW_REPORT_DIR  where snapshots are saved (default ./arrow-lake-reports)
  ARROW_SSH_OPTS    extra ssh options, e.g. "-i ~/.ssh/key -o IdentitiesOnly=yes"

The reboot takes the station's screens down for ~2-3 minutes. Coordinate with the site.
EOF
}

die() { echo "error: $*" >&2; exit 1; }

# One SSH connection per run, so the password (if any) is typed once for read-only steps.
CTL_DIR="$(mktemp -d "${TMPDIR:-/tmp}/arrowlake.XXXXXX")"
trap 'ssh -o ControlPath="$CTL_DIR/c" -O exit "$TARGET" >/dev/null 2>&1 || true; rm -rf "$CTL_DIR"' EXIT
TARGET=""

rsh() {  # rsh <cmd...>  — run on the station (no TTY)
  # shellcheck disable=SC2086
  ssh $SSH_EXTRA -o ControlMaster=auto -o ControlPath="$CTL_DIR/c" -o ControlPersist=300 \
      -o ConnectTimeout=15 "$TARGET" "$@"
}
rsh_tty() {  # rsh_tty <cmd> — with a TTY, for sudo (asks the KelaAdmin password)
  # shellcheck disable=SC2086
  ssh $SSH_EXTRA -t -o ControlMaster=auto -o ControlPath="$CTL_DIR/c" -o ControlPersist=300 \
      -o ConnectTimeout=15 "$TARGET" "$@"
}

# ---- the read-only snapshot, run on the station --------------------------------------
read -r -d '' SNAPSHOT <<'REMOTE' || true
set +e
echo "taken_at=$(date -u +%FT%TZ)"
echo "host=$(hostname)"
echo "os=$(lsb_release -ds 2>/dev/null)"
echo "kernel=$(uname -r)"
echo "uptime_min=$(awk '{print int($1/60)}' /proc/uptime)"
echo "cpus=$(nproc)"
echo "gpu_id=$(lspci -nn | grep -i -E 'vga|3d controller' | grep -o -E '\[8086:[0-9a-f]{4}\]' | head -1 | tr -d '[]')"
echo "gpu_driver=$(lspci -k -s 00:02.0 2>/dev/null | awk -F': ' '/Kernel driver in use/{print $2}')"
echo "mesa=$(dpkg-query -W -f='${Version}' libgl1-mesa-dri 2>/dev/null)"
echo "ppa_turtle=$(grep -rl 'kisak/turtle' /etc/apt/sources.list.d/ 2>/dev/null | head -1 | grep -q . && echo yes || echo no)"
echo "load1=$(cut -d' ' -f1 /proc/loadavg)"
awk '/^some/{for(i=2;i<=4;i++){split($i,a,"=");printf "psi_%s=%s\n",a[1],a[2]}}' /proc/pressure/cpu
echo "screens_connected=$(cat /sys/class/drm/card*-*/status 2>/dev/null | grep -c '^connected')"
echo "kiosk_tabs=$(curl -s --max-time 3 http://127.0.0.1:9222/json/list | grep -c '"type": *"page"')"
# CPU per process right now: second sample of top over 5 s (the first is since boot).
top -b -n 2 -d 5 -w 200 | awk '/^top -/{n++} n==2 && $1 ~ /^[0-9]+$/ {c[$12]+=$9} END{
  printf "cpu_gnome_shell=%.0f\ncpu_xorg=%.0f\ncpu_chrome_total=%.0f\n", c["gnome-shell"]+0, c["Xorg"]+0, c["chrome"]+0}'
# Chrome's own GPU report (same data as chrome://gpu), read-only, via the local DevTools port.
python3 - <<'PY' 2>/dev/null || echo "chrome_gpu=unavailable"
import base64, json, os, socket, urllib.request
ws = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=3))["webSocketDebuggerUrl"]
path = ws.split("9222", 1)[1]
s = socket.create_connection(("127.0.0.1", 9222), timeout=5)
key = base64.b64encode(os.urandom(16)).decode()
s.sendall(("GET %s HTTP/1.1\r\nHost: 127.0.0.1:9222\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
           "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, key)).encode())
buf = b""
while b"\r\n\r\n" not in buf:
    buf += s.recv(4096)
msg = json.dumps({"id": 1, "method": "SystemInfo.getInfo"}).encode()
mask = os.urandom(4)
s.sendall(bytes([0x81, 0x80 | len(msg)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(msg)))
data = b""
def need(n):
    global data
    while len(data) < n:
        data += s.recv(65536)
need(2); ln = data[1] & 0x7F; off = 2
if ln == 126: need(4); ln = int.from_bytes(data[2:4], "big"); off = 4
elif ln == 127: need(10); ln = int.from_bytes(data[2:10], "big"); off = 10
need(off + ln)
gpu = json.loads(data[off:off + ln])["result"]["gpu"]
fs = gpu.get("featureStatus", {})
aux = gpu.get("auxAttributes", {})
print("chrome_gl_renderer=" + str(aux.get("glRenderer", "?")).replace("\n", " "))
print("chrome_gpu_compositing=" + str(fs.get("gpu_compositing", "?")))
print("chrome_rasterization=" + str(fs.get("rasterization", "?")))
print("chrome_hw_decode_profiles=%d" % len(gpu.get("videoDecoding", [])))
PY
REMOTE

snapshot() {  # snapshot <label> -> path of the saved file
  local label="$1" file
  mkdir -p "$REPORT_DIR"
  file="$REPORT_DIR/${TARGET#*@}-$(date -u +%Y%m%dT%H%M%SZ)-${label}.txt"
  rsh "bash -s" <<<"$SNAPSHOT" >"$file"
  echo "label=$label" >>"$file"
  echo "$file"
}

val() { grep -m1 "^$1=" "$2" 2>/dev/null | cut -d= -f2-; }

show() {  # show <file>
  local f="$1"
  printf '  %-26s %s\n' "station" "$(val host "$f")  ($(val os "$f"), kernel $(val kernel "$f"))"
  printf '  %-26s %s\n' "GPU / kernel driver" "$(val gpu_id "$f") / $(val gpu_driver "$f")"
  printf '  %-26s %s\n' "Mesa" "$(val mesa "$f")"
  printf '  %-26s %s\n' "Chrome renderer" "$(val chrome_gl_renderer "$f")"
  printf '  %-26s %s\n' "Chrome GPU compositing" "$(val chrome_gpu_compositing "$f")"
  printf '  %-26s %s / %s / %s %%\n' "CPU pressure 10s/60s/300s" "$(val psi_avg10 "$f")" "$(val psi_avg60 "$f")" "$(val psi_avg300 "$f")"
  printf '  %-26s %s %%\n' "desktop (gnome-shell) CPU" "$(val cpu_gnome_shell "$f")"
  printf '  %-26s %s %%\n' "Xorg CPU" "$(val cpu_xorg "$f")"
  printf '  %-26s %s %%\n' "Chrome CPU (all procs)" "$(val cpu_chrome_total "$f")"
  printf '  %-26s %s (on %s CPUs)\n' "load (1 min)" "$(val load1 "$f")" "$(val cpus "$f")"
  printf '  %-26s %s screens, %s kiosk tabs\n' "display" "$(val screens_connected "$f")" "$(val kiosk_tabs "$f")"
  printf '  %-26s %s min\n' "uptime" "$(val uptime_min "$f")"
}

cmd_check() {
  local label="check"
  [[ "${1:-}" == "--label" ]] && label="${2:?--label needs a name}"
  echo "Read-only snapshot of $TARGET ..."
  local f; f="$(snapshot "$label")"
  show "$f"
  echo "Saved: $f"
  if [[ "$(val gpu_id "$f")" != "8086:7d67" ]]; then
    echo "Note: not an Arrow Lake GPU (8086:7d67) — this fix does not apply."
  elif [[ "$(val chrome_gl_renderer "$f")" == *"Mesa Intel"* ]]; then
    echo "Note: Chrome already renders on the GPU — this station looks fixed."
  else
    echo "Note: Arrow Lake rendering in software — candidate for 'apply'."
  fi
}

cmd_apply() {
  local reboot=1 force=0
  for a in "$@"; do
    case "$a" in --no-reboot) reboot=0 ;; --force) force=1 ;; *) die "unknown option $a" ;; esac
  done
  echo "== 1/4 pre-checks + 'before' snapshot (read-only)"
  local f; f="$(snapshot before)"
  show "$f"
  echo "Saved: $f"
  [[ "$(val gpu_id "$f")" == "8086:7d67" || $force == 1 ]] \
    || die "GPU is '$(val gpu_id "$f")', not Arrow Lake 8086:7d67 — not applying (use --force to override)."
  [[ "$(val os "$f")" == *"22.04"* || $force == 1 ]] || die "not Ubuntu 22.04 ($(val os "$f")) — not applying."
  [[ "$(val gpu_driver "$f")" == "i915" || $force == 1 ]] \
    || die "kernel driver is '$(val gpu_driver "$f")', expected i915 — escalate, do not apply."
  if [[ "$(val chrome_gl_renderer "$f")" == *"Mesa Intel"* && $force == 0 ]]; then
    echo "Chrome already renders on the GPU (Mesa $(val mesa "$f")). Nothing to do."; exit 0
  fi

  echo "== 2/4 install the newer Mesa from $PPA (asks for the KelaAdmin password)"
  # NEEDRESTART_MODE=l: only list services, never the interactive 'restart services?' dialog
  rsh_tty "sudo add-apt-repository -y $PPA && sudo apt-get update -q \
    && sudo env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get install -y -q --only-upgrade $PKGS"
  local mesa; mesa="$(rsh "dpkg-query -W -f='\${Version}' libgl1-mesa-dri")"
  echo "Mesa now: $mesa"
  [[ "$mesa" == *kisak* ]] || die "Mesa did not upgrade (still $mesa) — see the install output above."

  echo "== 3/4 reboot"
  if [[ $reboot == 0 ]]; then
    echo "Skipped (--no-reboot). The new Mesa is used only after the station reboots."
  else
    read -r -p "Reboot $TARGET now? Screens go dark for ~2-3 min. [y/N] " ok
    if [[ "$ok" == [yY]* ]]; then
      rsh_tty "sudo systemctl reboot" || true
      echo "Rebooting. Wait ~5 minutes until the kiosk is back on every screen."
    else
      echo "Not rebooted. Reboot later; the fix takes effect only after a reboot."
    fi
  fi
  echo "== 4/4 then run:  $0 verify ${TARGET#*@}"
}

cmd_verify() {
  local before=""
  [[ "${1:-}" == "--before" ]] && before="${2:?--before needs a file}"
  if [[ -z "$before" ]]; then
    before="$(ls -t "$REPORT_DIR/${TARGET#*@}"-*-before.txt 2>/dev/null | head -1 || true)"
  fi
  local after; after="$(snapshot after)"
  if [[ -n "$before" && -f "$before" ]]; then
    echo "BEFORE ($before)"; show "$before"
  else
    echo "(no 'before' snapshot found for this host — showing 'after' only)"
  fi
  echo "AFTER ($after)"; show "$after"

  local fail=0 r gs up
  r="$(val chrome_gl_renderer "$after")"
  gs="$(val cpu_gnome_shell "$after")"
  up="$(val uptime_min "$after")"
  echo "Checks:"
  if [[ "$(val mesa "$after")" == *kisak* ]]; then echo "  PASS  Mesa is the new one ($(val mesa "$after"))"; else echo "  FAIL  Mesa is still $(val mesa "$after")"; fail=1; fi
  if [[ "$r" == *"Mesa Intel"* ]]; then echo "  PASS  Chrome renders on the GPU ($r)"
  elif [[ "$r" == "?" || -z "$r" ]]; then echo "  WARN  could not read Chrome's GPU report (is the kiosk up?)"; fail=1
  else echo "  FAIL  Chrome still renders in software ($r)"; fail=1; fi
  if [[ "$(val chrome_gpu_compositing "$after")" == enabled* ]]; then echo "  PASS  GPU compositing enabled"; else echo "  FAIL  GPU compositing: $(val chrome_gpu_compositing "$after")"; fail=1; fi
  if [[ -z "$gs" ]]; then echo "  FAIL  could not read the desktop compositor's CPU (is the operator logged in?)"; fail=1
  elif [[ "$gs" -lt 20 ]]; then echo "  PASS  desktop compositor ${gs}% CPU (software rendering typically 70-80%)"
  else echo "  FAIL  desktop compositor ${gs}% CPU (expected under 20%)"; fail=1; fi
  if [[ "$(val kiosk_tabs "$after")" -ge 1 ]]; then echo "  PASS  kiosk is up ($(val kiosk_tabs "$after") tab(s) on $(val screens_connected "$after") screen(s))"; else echo "  FAIL  no kiosk tab found"; fail=1; fi
  if [[ -n "$up" && "$up" -lt 5 ]]; then echo "  NOTE  station up only ${up} min — numbers settle after ~5 min; re-run verify later."; fi
  echo "Note: video decoding stays on the CPU (no Arrow Lake video driver on 22.04) — expected."
  if [[ $fail == 0 ]]; then echo "RESULT: PASS"; else echo "RESULT: FAIL — see above; rollback with: $0 rollback ${TARGET#*@}"; exit 2; fi
}

cmd_rollback() {
  local reboot=1
  [[ "${1:-}" == "--no-reboot" ]] && reboot=0
  echo "Removing $PPA and returning to Ubuntu's Mesa (asks for the KelaAdmin password)"
  rsh_tty "sudo env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get install -y -q ppa-purge \
    && sudo env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l ppa-purge -y $PPA"
  echo "Mesa now: $(rsh "dpkg-query -W -f='\${Version}' libgl1-mesa-dri")"
  if [[ $reboot == 1 ]]; then
    read -r -p "Reboot $TARGET now? [y/N] " ok
    if [[ "$ok" == [yY]* ]]; then rsh_tty "sudo systemctl reboot" || true; echo "Rebooting."; fi
  fi
}

main() {
  local cmd="${1:-}"
  case "$cmd" in ""|-h|--help|help) usage; exit 0 ;; esac
  local host="${2:-}"
  [[ -n "$host" ]] || { usage; exit 1; }
  [[ "$host" =~ ^[A-Za-z0-9._@-]+$ ]] || die "bad host '$host'"
  [[ "$host" == *@* ]] && TARGET="$host" || TARGET="$USER_NAME@$host"
  shift 2
  case "$cmd" in
    check) cmd_check "$@" ;;
    apply) cmd_apply "$@" ;;
    verify) cmd_verify "$@" ;;
    rollback) cmd_rollback "$@" ;;
    *) die "unknown command '$cmd' (see --help)" ;;
  esac
}

main "$@"
