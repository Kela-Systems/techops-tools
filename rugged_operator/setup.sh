#!/usr/bin/env bash
# ============================================================================
#  Kela operator machine setup — Ubuntu 24.04 on Panasonic Toughbook CF-33
# ----------------------------------------------------------------------------
#  Run AFTER a fresh Ubuntu 24.04 desktop install where the kela user already
#  exists. Run as root via sudo (preserves env vars).
#
#  Usage:
#    SITE_NAME=fob-12 \
#    TS_AUTHKEY=tskey-auth-... \
#    TS_TAGS=tag:operator,tag:fob-12 \
#    sudo -E bash operator-setup.sh
#
#  By default the server cert is fetched from the site server on the LAN
#  (kela.local / 192.168.88.10). If the station is built BEFORE that server
#  exists, the cert step is skipped (non-fatal) and you can install it later
#  once the server is up with:
#    sudo kela-install-cert
#  Override the source host/port if needed:
#    CERT_FETCH_HOST=<host>  CERT_FETCH_PORT=443
# ============================================================================

set -Eeuo pipefail

# Self-diagnosing failures: the first-boot log should end with WHERE it died.
trap 'echo "ERROR: setup failed at line $LINENO: $BASH_COMMAND" >&2' ERR

# Stamped into /etc/kela/build-info for fleet audits. Bump on every change.
SETUP_VERSION="2026-07-06.9"

# ---------- config ----------------------------------------------------------
SITE_NAME="${SITE_NAME:-CHANGE-ME}"
KELA_USER="kela"
ANYDESK_PASS="Kelasys123!"

# Per-site LAN addressing. Defaults match the standard 192.168.88.0/24 site
# layout, but every value is overridable from the environment (the first-boot
# runner exports secrets.env before invoking this script), so a site with
# different addressing needs NO script edits. These are persisted to
# /etc/kela/station.conf below and re-read at runtime by kela-kiosk,
# kela-install-cert, and kela-verify — making that file the single on-box knob
# for re-pointing a station in the field.
KELA_LOCAL_IP="${KELA_LOCAL_IP:-192.168.88.10}"
KELA_LOCAL_HOST="${KELA_LOCAL_HOST:-kela.local}"
KELA_LOCAL_URL="https://${KELA_LOCAL_HOST}/"
# Kiosk tabs. TAB1 defaults to the hub; TAB2/TAB3 are OPTIONAL — each opens
# an extra tab only when set (e.g. TAB2=https://kela.local/location-updater,
# TAB3=http://192.168.88.210:6010/ for a camera). Configure in secrets.env at
# build time, or edit station.conf on-box + restart the kiosk. A default
# build opens ONE tab (the hub).
TAB1="${TAB1:-${KELA_LOCAL_URL}}"
TAB2="${TAB2:-}"
TAB3="${TAB3:-}"
# Display names for the managed bookmarks in Chrome (Regular) — cosmetic
# only (kiosk tabs have no visible labels). Default: the tab's URL.
TAB1_NAME="${TAB1_NAME:-${TAB1}}"
TAB2_NAME="${TAB2_NAME:-${TAB2}}"
TAB3_NAME="${TAB3_NAME:-${TAB3}}"
NTP_SERVER="${NTP_SERVER:-${KELA_LOCAL_IP}}"

# Tailscale (optional — if TS_AUTHKEY unset, `tailscale up` is left manual)
TS_AUTHKEY="${TS_AUTHKEY:-}"
TS_TAGS="${TS_TAGS:-}"

# Server cert fetch. Default source is the site server on the LAN
# (kela.local / 192.168.88.10). The fetch+install logic is installed as the
# standalone, re-runnable `kela-install-cert` command in §7, so a station
# built before its server exists can have the cert added later.
CERT_FETCH_HOST="${CERT_FETCH_HOST:-${KELA_LOCAL_IP}}"
CERT_FETCH_PORT="${CERT_FETCH_PORT:-443}"

if [[ "$SITE_NAME" == "CHANGE-ME" ]]; then
  echo "ERROR: set SITE_NAME, e.g.: SITE_NAME=fob-12 sudo -E bash $0" >&2
  exit 1
fi

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: run with sudo (sudo -E bash $0)" >&2
  exit 1
fi

if ! id "$KELA_USER" &>/dev/null; then
  echo "ERROR: user '$KELA_USER' does not exist; create it during OS install" >&2
  exit 1
fi

HOSTNAME_NEW="${SITE_NAME}-operator"
KELA_HOME="$(getent passwd "$KELA_USER" | cut -d: -f6)"
echo "==> Building $HOSTNAME_NEW (setup version ${SETUP_VERSION})"

# ---------- station.conf: single source of truth for site addressing --------
# Everything downstream (Chrome policy, /etc/hosts, NTP, cert fetch, kiosk tabs,
# kela-verify) derives from these values. The on-box scripts source this file at
# runtime, so re-pointing a station is a one-file edit + kiosk restart — no
# script surgery, and kela-verify checks against these values, not literals.
install -d -m 0755 /etc/kela
cat > /etc/kela/station.conf <<EOF
# Kela station addressing — written by operator-setup.sh ${SETUP_VERSION}.
# Sourced at runtime by kela-kiosk, kela-install-cert, kela-verify.
# To re-point this station: edit the values, then re-run \`sudo kela-verify\`
# and restart the kiosk (\`systemctl --user restart kela-kiosk\` as kela).
# NOTE: the Chrome managed policy (homepage/bookmarks) is baked at build time;
# re-run operator-setup.sh to regenerate it. The kiosk tabs below are live.
SITE_NAME='${SITE_NAME}'
KELA_LOCAL_IP='${KELA_LOCAL_IP}'
KELA_LOCAL_HOST='${KELA_LOCAL_HOST}'
KELA_LOCAL_URL='${KELA_LOCAL_URL}'
TAB1='${TAB1}'
TAB2='${TAB2}'
TAB3='${TAB3}'
TAB1_NAME='${TAB1_NAME}'
TAB2_NAME='${TAB2_NAME}'
TAB3_NAME='${TAB3_NAME}'
NTP_SERVER='${NTP_SERVER}'
CERT_FETCH_HOST='${CERT_FETCH_HOST}'
CERT_FETCH_PORT='${CERT_FETCH_PORT}'
EOF
chmod 0644 /etc/kela/station.conf

# ---------- 0. wait for network ----------------------------------------------
# A slow DHCP lease at first boot fails the apt/Tailscale/cert steps, so wait
# up to 45s for a default route — then warn-and-continue (the Tailscale gate
# in kela-first-boot-run keeps the retry path if connectivity never comes).
echo "==> [0/11] Waiting for a default route (max 45s)"
for _ in $(seq 1 45); do
  ip route show default 2>/dev/null | grep -q . && break
  sleep 1
done
if ! ip route show default 2>/dev/null | grep -q .; then
  echo "WARN: no default route after 45s — continuing, but network steps may fail."
fi

# ---------- 1. hostname -----------------------------------------------------
echo "==> [1/11] Setting hostname"
hostnamectl set-hostname "$HOSTNAME_NEW"
# (sed exits 0 even with no match, so guard with grep rather than `||`.)
if grep -q '^127\.0\.1\.1' /etc/hosts; then
  sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t${HOSTNAME_NEW}/" /etc/hosts
else
  printf '127.0.1.1\t%s\n' "$HOSTNAME_NEW" >> /etc/hosts
fi

# ---------- 1b. timezone + NTP ----------------------------------------------
# Timezone Asia/Jerusalem; sync time from the site NTP source (the hub by
# default). UFW (§10) is LAN-only egress, so the NTP server must be on the LAN.
echo "==> [1b/11] Timezone Asia/Jerusalem + NTP ${NTP_SERVER}"
timedatectl set-timezone Asia/Jerusalem 2>/dev/null || \
  ln -sf /usr/share/zoneinfo/Asia/Jerusalem /etc/localtime
mkdir -p /etc/systemd/timesyncd.conf.d
# Hub is the primary time source. Fallback to ntp.ubuntu.com so a station
# whose RTC drifted while the hub is down can still get sane time (TLS
# breaks with "cert not yet valid" otherwise). UFW (§10) allows 123/udp out.
cat > /etc/systemd/timesyncd.conf.d/kela-ntp.conf <<EOF
[Time]
NTP=${NTP_SERVER}
FallbackNTP=ntp.ubuntu.com
EOF
timedatectl set-ntp true 2>/dev/null || true
systemctl restart systemd-timesyncd 2>/dev/null || true

# ---------- 2. base packages + updates --------------------------------------
echo "==> [2/11] apt update + base tools"
export DEBIAN_FRONTEND=noninteractive

# First-boot lock race: ubuntu-desktop-minimal ships apt-daily{,-upgrade} and
# unattended-upgrades, which fire on the first boot of a fresh install and can
# hold the dpkg/lists lock right when we run. Under `set -e` that turns into a
# fatal "Could not get lock" mid-setup. Stop the racers up front (the fleet is
# patched centrally over Tailscale, so these aren't wanted anyway) and, as a
# belt-and-braces net, every apt call below waits on the lock via APT_LOCK.
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
systemctl mask unattended-upgrades.service 2>/dev/null || true
# Kill any in-flight job that already grabbed the lock before we got here.
systemctl stop apt-daily.service apt-daily-upgrade.service unattended-upgrades.service 2>/dev/null || true

# Optional bench apt cache (APT_PROXY, from secrets.env or the environment):
# points apt at e.g. apt-cacher-ng on the imaging bench so N parallel builds
# download packages once at LAN speed. Deliberately non-persistent — nothing
# is written under /etc/apt, so a station imaged at the bench never carries a
# dead proxy to the field.
APT_PROXY="${APT_PROXY:-}"
APT_OPTS=()
if [[ -n "$APT_PROXY" ]]; then
  APT_OPTS+=( -o "Acquire::http::Proxy=${APT_PROXY}" -o "Acquire::https::Proxy=${APT_PROXY}" )
  echo "    apt proxy: ${APT_PROXY}"
fi

# Wait up to 10 min for the dpkg/apt lock instead of failing hard if something
# still holds it. Applied to the wrapper AND the raw apt-get calls below.
APT_LOCK=( -o DPkg::Lock::Timeout=600 )

apt_get() { apt-get "${APT_LOCK[@]}" "${APT_OPTS[@]}" "$@"; }

apt_get update -y

# apt upgrade is intentionally skipped (it was a redundant re-download). Patch
# centrally over Tailscale instead. Set KELA_APT_UPGRADE=1 to force it.
if [[ "${KELA_APT_UPGRADE:-0}" == "1" ]]; then
  apt_get upgrade -y
fi

apt_get install -y curl wget ca-certificates apt-transport-https \
                   gnupg lsb-release ufw rfkill openssh-server \
                   libnss3-tools power-profiles-daemon dconf-cli \
                   onboard evtest xdotool

# ---------- 3. Tailscale ----------------------------------------------------
echo "==> [3/11] Installing Tailscale"
if ! command -v tailscale >/dev/null 2>&1; then
  # --retry-all-errors: vendor endpoints blip (AnyDesk's keyserver once served
  # Cloudflare 522 mid-build, killing a 20-min run at the last mile). Ride out
  # short blips; a genuine outage still fails cleanly to the re-run path.
  curl -fsSL --retry 5 --retry-delay 15 --retry-all-errors https://tailscale.com/install.sh | sh
fi
systemctl enable --now tailscaled

# Wait for tailscaled to come up before calling `tailscale up`
for _ in 1 2 3 4 5 6 7 8 9 10; do
  systemctl is-active --quiet tailscaled && break
  sleep 1
done

if [[ -n "$TS_AUTHKEY" ]]; then
  echo "==> Bringing Tailscale up as $HOSTNAME_NEW"
  # --timeout: without it `tailscale up` retries the control plane FOREVER —
  # an unattended first boot with no cable (or a key pending admin approval)
  # hangs indefinitely instead of falling through to the WARN below and the
  # first-boot gate's clear "Tailscale NOT connected" message. 2 min is ample
  # for a healthy link+auth; on failure the gate keeps the retry wiring.
  TS_UP_ARGS=( --reset \
               --authkey="$TS_AUTHKEY" \
               --hostname="$HOSTNAME_NEW" \
               --timeout=120s \
               --accept-routes )

  if [[ -n "$TS_TAGS" ]]; then
    # Accept "operator,fob-12" / "tag:operator,tag:fob-12" / mixed.
    TS_TAGS_CLEAN="$(echo "$TS_TAGS" \
      | sed 's/tag://g' \
      | tr ',' '\n' \
      | sed '/^[[:space:]]*$/d; s/^[[:space:]]*//; s/[[:space:]]*$//; s/^/tag:/' \
      | paste -sd ',' -)"
    TS_UP_ARGS+=( --advertise-tags="$TS_TAGS_CLEAN" )
    echo "    Tags: $TS_TAGS_CLEAN"
  fi

  if ! tailscale up "${TS_UP_ARGS[@]}"; then
    echo "WARN: 'tailscale up' failed — verify TS_AUTHKEY/TS_TAGS and re-run manually."
  else
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      tailscale ip -4 >/dev/null 2>&1 && break
      sleep 1
    done
    TS_IP="$(tailscale ip -4 2>/dev/null || true)"
    [[ -n "$TS_IP" ]] && echo "    Tailscale IPv4: $TS_IP"
  fi
else
  echo "    TS_AUTHKEY not set — leaving 'tailscale up' as a manual step."
fi

# ---------- 4. AnyDesk ------------------------------------------------------
echo "==> [4/11] Installing AnyDesk"
if ! command -v anydesk >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL --retry 5 --retry-delay 15 --retry-all-errors \
       https://keys.anydesk.com/repos/DEB-GPG-KEY \
       -o /etc/apt/keyrings/keys.anydesk.com.asc
  chmod a+r /etc/apt/keyrings/keys.anydesk.com.asc
  echo "deb [signed-by=/etc/apt/keyrings/keys.anydesk.com.asc] http://deb.anydesk.com/ all main" \
       > /etc/apt/sources.list.d/anydesk-stable.list
  apt_get update -y
  apt_get install -y anydesk
fi

# `anydesk --set-password` needs the service running AND initialized — wait
# for `--get-id` to answer (the ID exists only after first-start init), not
# merely for systemd to report "active".
systemctl enable --now anydesk
for _ in $(seq 1 30); do
  anydesk --get-id >/dev/null 2>&1 && break
  sleep 1
done

echo "==> Setting AnyDesk unattended password"
# ONE line on stdin — the CLI reads a single line, not a passwd-style
# confirmation pair (the old double-line form failed silently for weeks and
# only surfaced as a verify WARN). Success is judged by ground truth — a
# pwd_hash in system.conf — not the exit code, so the retry loop and
# kela-verify can never disagree about whether this worked.
ANYDESK_PASS_SET=0
for _ in 1 2 3; do
  printf '%s\n' "${ANYDESK_PASS}" | anydesk --set-password || true
  if grep -qi pwd_hash /etc/anydesk/system.conf 2>/dev/null; then
    ANYDESK_PASS_SET=1
    break
  fi
  sleep 2
done
if [[ "$ANYDESK_PASS_SET" != "1" ]]; then
  echo "WARN: unattended password did not take (no pwd_hash in /etc/anydesk/system.conf)."
  echo "      Set it manually:  printf '%s\\n' 'THEPASSWORD' | sudo anydesk --set-password"
fi

# ---------- 5. Google Chrome + telemetry suppression ------------------------
echo "==> [5/11] Installing Google Chrome"
# The vendor deb self-registers Google's apt repo in its postinst, so central
# patching covers Chrome afterwards.
if ! command -v google-chrome-stable >/dev/null 2>&1; then
  TMP_DEB="$(mktemp --suffix=.deb)"
  curl -fsSL --retry 5 --retry-delay 15 --retry-all-errors \
       -o "$TMP_DEB" https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  apt_get install -y "$TMP_DEB"
  rm -f "$TMP_DEB"
fi

# Chrome managed policy:
#  - telemetry / metrics off (prevents the /home disk accumulation we saw on
#    kela-fob-09-operator)
#  - kiosk hardening: DevTools off, incognito off, printing off, downloads blocked
#  - homepage + new-tab + startup pinned to kela.local
#  - managed bookmarks (live in a locked "Kela" folder on the bookmark bar)
# NOTE: --kiosk hides the bookmark bar, so those bookmarks are only reachable
# from the "Chrome (Regular)" launcher; in the kiosk, extra views exist only
# as the TAB2/TAB3 tabs (or via in-app navigation). No URL allow/blocklist
# here — confinement is via --kiosk + the §10b egress lock, which keeps the
# "Chrome (Regular)" launcher LAN-only too.
# Build the tab list as JSON fragments — only configured tabs appear in the
# startup URLs and managed bookmarks. Bookmark names come from TABn_NAME
# (default: the URL itself).
TABS_JSON="\"${TAB1}\""
BOOKMARKS_JSON="{ \"name\": \"${TAB1_NAME}\", \"url\": \"${TAB1}\" }"
if [[ -n "$TAB2" ]]; then
  TABS_JSON+=", \"${TAB2}\""
  BOOKMARKS_JSON+=", { \"name\": \"${TAB2_NAME}\", \"url\": \"${TAB2}\" }"
fi
if [[ -n "$TAB3" ]]; then
  TABS_JSON+=", \"${TAB3}\""
  BOOKMARKS_JSON+=", { \"name\": \"${TAB3_NAME}\", \"url\": \"${TAB3}\" }"
fi

mkdir -p /etc/opt/chrome/policies/managed
cat > /etc/opt/chrome/policies/managed/kela-policy.json <<JSON
{
  "MetricsReportingEnabled": false,
  "DefaultBrowserSettingEnabled": false,
  "BackgroundModeEnabled": false,
  "SafeBrowsingExtendedReportingEnabled": false,
  "ChromeCleanupEnabled": false,
  "ChromeCleanupReportingEnabled": false,
  "ComponentUpdatesEnabled": true,
  "PasswordManagerEnabled": false,
  "SearchSuggestEnabled": false,
  "BrowserSignin": 0,
  "SyncDisabled": true,

  "DeveloperToolsAvailability": 2,
  "IncognitoModeAvailability": 1,
  "PrintingEnabled": false,
  "DownloadRestrictions": 3,

  "HomepageLocation": "${KELA_LOCAL_URL}",
  "HomepageIsNewTabPage": false,
  "NewTabPageLocation": "${KELA_LOCAL_URL}",
  "ShowHomeButton": true,
  "RestoreOnStartup": 4,
  "RestoreOnStartupURLs": [ ${TABS_JSON} ],

  "BookmarkBarEnabled": true,
  "ManagedBookmarks": [
    { "toplevel_name": "Kela" },
    ${BOOKMARKS_JSON}
  ]
}
JSON

# ---------- 6. /etc/hosts entry for kela.local ------------------------------
# Done BEFORE cert fetch so `kela.local` resolves to the hub on the LAN.
# Match-and-replace (like the 127.0.1.1 handling in §1), not append-if-absent:
# a re-run after KELA_LOCAL_IP changed (e.g. a per-site station.conf override)
# must overwrite the stale mapping instead of leaving it in place. The regex
# only touches a real (non-comment) mapping for this exact host.
echo "==> [6/11] Adding ${KELA_LOCAL_HOST} -> ${KELA_LOCAL_IP}"
sed -i -E "/^[^#]*[[:space:]]${KELA_LOCAL_HOST}([[:space:]]|$)/d" /etc/hosts
printf '%s\t%s\n' "${KELA_LOCAL_IP}" "${KELA_LOCAL_HOST}" >> /etc/hosts

# ---------- 7. Fetch & install server certificate ---------------------------
# The fetch+install logic is installed as a standalone, re-runnable command:
#   sudo kela-install-cert [host] [port]
# This matters because a station is sometimes built BEFORE the site server
# exists — there's no cert to fetch yet, so the initial attempt below is
# non-fatal and the operator runs `sudo kela-install-cert` later once the
# server is up. Default source is the LAN server kela.local / 192.168.88.10.
echo "==> [7/11] Installing kela-install-cert helper + fetching cert"

install -m 0755 /dev/stdin /usr/local/sbin/kela-install-cert <<'KELACERT'
#!/usr/bin/env bash
# Fetch the Kela server's TLS cert and trust it system-wide (curl/wget) and
# in kela's NSS DB (Chrome on Linux). Idempotent — safe to re-run anytime.
#
# Use when the station was built before the site server existed:
#   sudo kela-install-cert                # pull from kela.local (192.168.88.10)
#   sudo kela-install-cert <host> [port]  # pull from a specific host/port
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: run with sudo: sudo kela-install-cert" >&2
  exit 1
fi

KELA_USER="kela"
KELA_HOME="$(getent passwd "$KELA_USER" | cut -d: -f6)"

# Per-site addressing from the single source of truth (written by setup.sh).
[ -r /etc/kela/station.conf ] && . /etc/kela/station.conf
KELA_LOCAL_HOST="${KELA_LOCAL_HOST:-kela.local}"

# Precedence: explicit arg > KELA_CERT_HOST env > station.conf CERT_FETCH_HOST
# > hub IP > baked default. Same for the port.
HOST="${1:-${KELA_CERT_HOST:-${CERT_FETCH_HOST:-${KELA_LOCAL_IP:-192.168.88.10}}}}"
PORT="${2:-${KELA_CERT_PORT:-${CERT_FETCH_PORT:-443}}}"
RETRIES="${KELA_CERT_RETRIES:-5}"
RETRY_DELAY="${KELA_CERT_RETRY_DELAY:-5}"
TIMEOUT="${KELA_CERT_TIMEOUT:-10}"
INSTALL_DIR="/usr/local/share/ca-certificates/kela"
NICKNAME="Kela Server"
TARGET="${INSTALL_DIR}/kela-server.crt"

echo "==> Fetching server certificate from ${HOST}:${PORT}"
mkdir -p "$INSTALL_DIR"

if ! timeout "$TIMEOUT" bash -c "echo >/dev/tcp/${HOST}/${PORT}" 2>/dev/null; then
  echo "ERROR: ${HOST}:${PORT} not reachable — the site server may not exist yet." >&2
  echo "       Re-run once it is online: sudo kela-install-cert" >&2
  exit 1
fi

FETCHED=0
for attempt in $(seq 1 "$RETRIES"); do
  echo "    Attempt ${attempt}/${RETRIES}"
  # SNI is the hostname (kela.local), NOT the -connect target (an IP): it must
  # match the SNI kela-cert-ensure probes with, or a future SNI-dependent hub
  # (per-host certs) would serve a different cert to probe vs. fetch and trip a
  # permanent 5-min re-pin/kiosk-restart loop. Same-SNI-by-construction.
  DATA="$(echo | timeout "$TIMEOUT" openssl s_client \
                 -connect "${HOST}:${PORT}" -servername "$KELA_LOCAL_HOST" </dev/null 2>/dev/null \
           | sed -n '/BEGIN CERTIFICATE/,/END CERTIFICATE/p')"
  if [[ -n "$DATA" ]] && echo "$DATA" | openssl x509 -noout 2>/dev/null; then
    echo "$DATA" > "$TARGET"
    chmod 644 "$TARGET"
    FETCHED=1
    break
  fi
  sleep "$RETRY_DELAY"
done

if [[ "$FETCHED" != "1" ]]; then
  echo "ERROR: cert fetch failed after ${RETRIES} attempts against ${HOST}:${PORT}." >&2
  exit 1
fi

echo "    Subject: $(openssl x509 -in "$TARGET" -noout -subject | sed 's/^subject=//')"
echo "    Issuer:  $(openssl x509 -in "$TARGET" -noout -issuer  | sed 's/^issuer=//')"
echo "    Expires: $(openssl x509 -in "$TARGET" -noout -enddate | sed 's/^notAfter=//')"

if openssl x509 -in "$TARGET" -noout -text 2>/dev/null | grep -q "CA:TRUE"; then
  NSS_TRUST="C,,"
  echo "    Cert is a CA — NSS trust flags: $NSS_TRUST"
else
  NSS_TRUST="P,,"
  echo "    Cert is a leaf — NSS trust flags: $NSS_TRUST"
fi

update-ca-certificates

# Chrome on Linux uses NSS, not the system store — register here too.
install -d -o "$KELA_USER" -g "$KELA_USER" -m 700 "$KELA_HOME/.pki/nssdb"
if ! sudo -u "$KELA_USER" certutil -L -d "sql:$KELA_HOME/.pki/nssdb" &>/dev/null; then
  sudo -u "$KELA_USER" certutil -N -d "sql:$KELA_HOME/.pki/nssdb" --empty-password
fi
sudo -u "$KELA_USER" certutil -D -d "sql:$KELA_HOME/.pki/nssdb" -n "$NICKNAME" 2>/dev/null || true
sudo -u "$KELA_USER" certutil -A -d "sql:$KELA_HOME/.pki/nssdb" -n "$NICKNAME" -t "$NSS_TRUST" -i "$TARGET"

# Chrome SSL override policy — fallback in case the NSS path drifts.
mkdir -p /etc/opt/chrome/policies/managed
cat > /etc/opt/chrome/policies/managed/kela-ssl-policy.json <<JSON
{
  "SSLErrorOverrideAllowedForOrigins": [
    "https://${KELA_LOCAL_HOST}"
  ]
}
JSON

echo "==> Certificate installed. Restart Chrome to pick it up."
KELACERT

# Initial attempt during setup. Non-fatal: if the server doesn't exist yet,
# the operator re-runs `sudo kela-install-cert` later.
if kela-install-cert "$CERT_FETCH_HOST" "$CERT_FETCH_PORT"; then
  :
else
  echo "WARN: certificate not installed (the site server may not exist yet)."
  echo "      kela-cert-ensure (§7b) will auto-install it within ~5 min of the"
  echo "      server at ${KELA_LOCAL_IP} (${KELA_LOCAL_HOST}) coming online — or run now:"
  echo "        sudo kela-install-cert"
fi

# ---------- 7b. Self-healing cert convergence (systemd timer) ---------------
# §7's fetch is one-shot: a station built before its site server exists gets no
# cert, and historically a human had to run `sudo kela-install-cert` later and
# restart Chrome. kela-cert-ensure automates that convergence — a 5-min timer
# probes the hub and, when the served cert first appears (or later CHANGES
# because the server was rebuilt with a new cert), it (re)pins the cert and
# bounces the kiosk so Chrome re-reads NSS. It is deliberately silent and
# non-fatal while the server is absent, since it runs forever.
echo "==> [7b/11] Installing kela-cert-ensure convergence timer"

install -m 0755 /dev/stdin /usr/local/sbin/kela-cert-ensure <<'KELACERTENSURE'
#!/usr/bin/env bash
# Converge the trusted Kela server cert with what the hub is actually serving.
# Runs every 5 min from kela-cert-ensure.timer:
#   - server absent/down    -> exit 0 silently (stations are often built first)
#   - served == installed   -> exit 0 (nothing to do)
#   - missing/mismatch       -> kela-install-cert, then restart the kiosk so
#                              Chrome re-reads NSS. Transient failures just wait
#                              for the next tick.
# This replaces the manual `sudo kela-install-cert` follow-up and also
# self-heals when the server is rebuilt with a fresh cert (fingerprint changes).
# NOTE: intentionally NOT `set -e` — a probe against an absent server must never
# fail this unit (it would spam the journal with failed timer runs).
set -uo pipefail

# Per-site addressing (single source of truth). Same env names kela-install-cert
# honors, so any override applies to both.
[ -r /etc/kela/station.conf ] && . /etc/kela/station.conf
HOST="${KELA_CERT_HOST:-${CERT_FETCH_HOST:-${KELA_LOCAL_IP:-192.168.88.10}}}"
PORT="${KELA_CERT_PORT:-${CERT_FETCH_PORT:-443}}"
# SNI must match the one kela-install-cert fetches with (also KELA_LOCAL_HOST):
# on an SNI-dependent hub, a differing SNI would probe cert A while the installer
# pins cert B, and every tick would re-detect a "mismatch" and restart the kiosk.
SNI="${KELA_LOCAL_HOST:-kela.local}"
TARGET="/usr/local/share/ca-certificates/kela/kela-server.crt"

# What the hub is serving right now. Empty => server not up yet: do nothing.
served="$(echo | timeout 10 openssl s_client -connect "${HOST}:${PORT}" \
            -servername "$SNI" 2>/dev/null \
          | openssl x509 -noout -fingerprint -sha256 2>/dev/null)"
[ -n "$served" ] || exit 0

# What we currently trust (missing file counts as "no match").
installed=""
[ -r "$TARGET" ] && installed="$(openssl x509 -in "$TARGET" -noout -fingerprint -sha256 2>/dev/null)"

[ "$served" = "$installed" ] && exit 0

# First pin or the server's cert changed: (re)fetch + trust. A transient failure
# is fine — the next tick retries.
if /usr/local/sbin/kela-install-cert "$HOST" "$PORT" >/dev/null 2>&1; then
  # Chrome reads NSS only at startup — bounce the kiosk so the new cert applies.
  # Only a RUNNING kiosk is restarted (a no-op mid-first-boot or after an
  # escape); log the restart only when it actually happened, so a journal
  # debugging a station isn't misled by a restart that never occurred.
  restarted=""
  kela_uid="$(id -u kela 2>/dev/null || true)"
  if [ -n "$kela_uid" ] \
     && sudo -u kela XDG_RUNTIME_DIR="/run/user/${kela_uid}" \
          systemctl --user is-active --quiet kela-kiosk.service 2>/dev/null; then
    sudo -u kela XDG_RUNTIME_DIR="/run/user/${kela_uid}" \
      systemctl --user try-restart kela-kiosk.service 2>/dev/null || true
    restarted=" (kiosk restarted)"
  fi
  echo "kela-cert-ensure: server cert installed/updated from ${HOST}:${PORT}${restarted}"
fi
exit 0
KELACERTENSURE

# oneshot service + 5-min timer. The TIMER is enabled (not the service): the
# service is just the work the timer triggers.
cat > /etc/systemd/system/kela-cert-ensure.service <<'EOF'
[Unit]
Description=Kela: converge trusted server cert with what the hub serves
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/kela-cert-ensure
EOF

cat > /etc/systemd/system/kela-cert-ensure.timer <<'EOF'
[Unit]
Description=Kela: periodic server-cert convergence (self-healing TLS pin)

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now kela-cert-ensure.timer

# ---------- 8. Kiosk: Chrome --kiosk as a self-restarting user service ------
# Replaces the old maximized-window autostart. The kiosk runs under a
# systemd --user service (Restart=always) so a close/crash relaunches it; a
# GNOME autostart entry starts the service at login. Escape is via
# kela-kiosk-escape (§9f), return via the "Kela Kiosk" launcher (§8b) or a
# reboot.
echo "==> [8/11] Kiosk launcher + user service for ${KELA_USER}"

# Drop the old maximized-window autostart if this is a re-run.
rm -f "$KELA_HOME/.config/autostart/chrome-kela.desktop"

# Kiosk launcher: scrub Chrome crash state (so no "restore pages" overlay
# blocks the kiosk), then run Chrome fullscreen. --touch-events keeps the
# CF-33 touchscreen working; --force-device-scale-factor makes the 3:2 HiDPI
# panel readable (tune on-device). No --disable-pinch: the hub map needs
# pinch-zoom (handled by the page, independent of viewport pinch).
#
# Configured tabs (TAB1..TAB3, station.conf) open in one kiosk window —
# command-line URLs are authoritative and override RestoreOnStartup. Only
# non-empty tabs open, so a one-tab site is just "TAB2/TAB3 unset". Docked,
# operators switch with Ctrl+Tab or Ctrl+1..3; in tablet mode the A2 bezel
# button cycles forward via Ctrl+Tab (kela-kiosk-next-tab, §9f). Ctrl+0 and
# Ctrl+4..9 are swallowed by the 10-kela-kiosk dconf (§9d) so stray number
# keys can't jump to a phantom tab or reset zoom. The §5 policy generates
# RestoreOnStartupURLs + ManagedBookmarks from the same TAB values.
cat > /usr/local/bin/kela-kiosk <<EOF
#!/usr/bin/env bash
set -euo pipefail

# Tab URLs come from the single source of truth. The values below are the
# build-time defaults; /etc/kela/station.conf (sourced next) overrides them, so
# re-pointing this station in the field is a station.conf edit + kiosk restart.
# KELA_LOCAL_URL stays separate from TAB1: it is the HUB (pre-flight wait +
# recovery watch key on it) even if TAB1 is overridden to something else.
KELA_LOCAL_URL='${KELA_LOCAL_URL}'
TAB1='${TAB1}'
TAB2='${TAB2}'
TAB3='${TAB3}'
[ -r /etc/kela/station.conf ] && . /etc/kela/station.conf

# Only configured tabs open (if/fi, not &&: set -e would abort on empty TAB2).
TABS=( "\$TAB1" )
if [ -n "\$TAB2" ]; then TABS+=( "\$TAB2" ); fi
if [ -n "\$TAB3" ]; then TABS+=( "\$TAB3" ); fi

PREFS="\$HOME/.config/google-chrome/Default/Preferences"
if [ -f "\$PREFS" ]; then
  sed -i 's/"exit_type":"[^"]*"/"exit_type":"Normal"/; s/"exited_cleanly":false/"exited_cleanly":true/' "\$PREFS" 2>/dev/null || true
fi
# Clear stale single-instance locks from an unclean exit (hard power-off):
# otherwise Chrome can refuse to start and every relaunch fails identically.
# Guard on "no Chrome already running": if "Chrome (Regular)" is open when the
# Kela Kiosk launcher fires, deleting its live lock would start a SECOND Chrome
# on the same Default profile — the exact corruption the lock exists to prevent.
if ! pgrep -u "\$(id -u)" -f google-chrome-stable >/dev/null 2>&1; then
  rm -f "\$HOME/.config/google-chrome/"Singleton* 2>/dev/null || true
fi

# Wait (bounded ~30s) for the hub to answer before launching. Chrome caches an
# ERR_CONNECTION_REFUSED page and never retries it, so launching into a slow
# DHCP lease or a hub that's still coming up would strand a keyboardless tablet
# on a dead error page. -k: the cert may not be installed yet. Launch anyway
# after the deadline — kela-kiosk-watch restarts us when the hub recovers.
deadline=\$(( SECONDS + 30 ))
while [ "\$SECONDS" -lt "\$deadline" ]; do
  curl -ks --max-time 2 -o /dev/null "\$KELA_LOCAL_URL" && break
  sleep 2
done

exec /usr/bin/google-chrome-stable \\
  --kiosk \\
  --no-first-run --no-default-browser-check \\
  --disable-features=TranslateUI \\
  --noerrdialogs --disable-session-crashed-bubble --disable-infobars \\
  --touch-events=enabled \\
  --force-device-scale-factor=1.25 \\
  --check-for-update-interval=31536000 \\
  "\${TABS[@]}"
EOF
chmod 0755 /usr/local/bin/kela-kiosk

# systemd --user service. PartOf graphical-session so it stops on logout;
# started by the autostart entry below (avoids linger / enable subtleties).
install -d -o "$KELA_USER" -g "$KELA_USER" "$KELA_HOME/.config/systemd/user"
cat > "$KELA_HOME/.config/systemd/user/kela-kiosk.service" <<'EOF'
[Unit]
Description=Kela kiosk (Chrome --kiosk on kela.local)
PartOf=graphical-session.target
After=graphical-session.target
# Never give up. With Restart=always, systemd's DEFAULT start limit (5 starts
# per 10s) would trip on an instant-crash loop — corrupt profile after a hard
# power-off (these boxes run until the battery dies), a stale SingletonLock, or
# the X display not yet imported into the user manager env — and drop the kiosk
# into failed state permanently: a black desktop until reboot, i.e. the exact
# opposite of self-healing. Disabling the limiter keeps it retrying forever.
StartLimitIntervalSec=0

[Service]
ExecStart=/usr/local/bin/kela-kiosk
Restart=always
# 5s (not 2s): a genuine crash-loop retries calmly instead of pegging the CPU,
# and transient causes (X not ready yet) get a moment to clear between tries.
RestartSec=5

[Install]
WantedBy=graphical-session.target
EOF
chown "$KELA_USER:$KELA_USER" "$KELA_HOME/.config/systemd/user/kela-kiosk.service"

# Hub-recovery watch: the launcher's pre-flight wait only helps at start, and
# Restart=always doesn't fire on an error page (Chrome didn't crash). So if the
# hub drops after launch, the kiosk keeps showing a stale ERR_CONNECTION_REFUSED
# page forever. This watcher restarts the kiosk on an unreachable->reachable
# transition, so error-page tabs get replaced with live content once the hub is
# back — no keyboard (Ctrl+R) or remote intervention needed on a tablet.
cat > /usr/local/bin/kela-kiosk-watch <<'EOF'
#!/usr/bin/env bash
set -uo pipefail

KELA_LOCAL_URL='https://kela.local/'
[ -r /etc/kela/station.conf ] && . /etc/kela/station.conf
: "${KELA_LOCAL_URL:=https://kela.local/}"

reachable() { curl -ks --max-time 2 -o /dev/null "$KELA_LOCAL_URL"; }

# Seed with the current state so we only act on a genuine recovery edge, not on
# the first poll.
if reachable; then last=1; else last=0; fi

while true; do
  sleep 20
  if reachable; then now=1; else now=0; fi
  if [ "$last" -eq 0 ] && [ "$now" -eq 1 ]; then
    # try-restart (not restart): only bounce a RUNNING kiosk stuck on an error
    # page. If a technician escaped to the desktop while the hub was down, a
    # hub recovery must NOT yank the kiosk back over their session — escape
    # stays escaped until explicitly re-entered.
    systemctl --user try-restart kela-kiosk.service || true
  fi
  last=$now
done
EOF
chmod 0755 /usr/local/bin/kela-kiosk-watch

cat > "$KELA_HOME/.config/systemd/user/kela-kiosk-watch.service" <<'EOF'
[Unit]
Description=Kela kiosk hub-recovery watch (restart kiosk when the hub comes back)
PartOf=graphical-session.target
After=graphical-session.target kela-kiosk.service
# Independent of kela-kiosk (not PartOf it), so restarting the kiosk doesn't
# restart the watcher. Never give up on the poll loop.
StartLimitIntervalSec=0

[Service]
ExecStart=/usr/local/bin/kela-kiosk-watch
Restart=always
RestartSec=10

[Install]
WantedBy=graphical-session.target
EOF
chown "$KELA_USER:$KELA_USER" "$KELA_HOME/.config/systemd/user/kela-kiosk-watch.service"

# Autostart entry: start the kiosk service + recovery watch at graphical login.
install -d -o "$KELA_USER" -g "$KELA_USER" "$KELA_HOME/.config/autostart"
cat > "$KELA_HOME/.config/autostart/kela-kiosk.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Kela kiosk
Exec=sh -c 'systemctl --user daemon-reload; systemctl --user start kela-kiosk.service kela-kiosk-watch.service'
X-GNOME-Autostart-enabled=true
NoDisplay=true
Terminal=false
EOF
chown "$KELA_USER:$KELA_USER" "$KELA_HOME/.config/autostart/kela-kiosk.desktop"

# --- 8b. App-grid launchers: leave / re-enter the kiosk --------------------
# After an escape (§9f) the operator is on plain GNOME. These two icons (in
# the app grid, reachable by touch via the dock's "Show Applications") let a
# technician open a normal browser or jump back into the kiosk. "Chrome
# (Regular)" is still LAN-only via the §10b per-user egress lock — not the
# open internet.
cat > /usr/share/applications/kela-chrome-regular.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Chrome (Regular)
Comment=Normal Chrome window (LAN-only)
Exec=/usr/bin/google-chrome-stable --no-first-run --no-default-browser-check %U
Icon=google-chrome
Categories=Network;WebBrowser;
Terminal=false
EOF

# Distinct icon so "Kela Kiosk" and "Chrome (Regular)" are tell-apart-able in
# the dock/app grid (both used to show the stock Chrome icon).
install -d /usr/share/icons/hicolor/scalable/apps
cat > /usr/share/icons/hicolor/scalable/apps/kela-kiosk.svg <<'EOF'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="14" fill="#1a5fb4"/>
  <rect x="10" y="14" width="44" height="30" rx="3" fill="#ffffff"/>
  <rect x="14" y="18" width="36" height="22" rx="1" fill="#1a5fb4"/>
  <rect x="26" y="46" width="12" height="4" fill="#ffffff"/>
  <text x="32" y="35.5" font-family="sans-serif" font-size="15" font-weight="bold"
        fill="#ffffff" text-anchor="middle">K</text>
</svg>
EOF
gtk-update-icon-cache -f /usr/share/icons/hicolor 2>/dev/null || true

cat > /usr/share/applications/kela-kiosk.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Kela Kiosk
Comment=Return to kiosk mode
Exec=sh -c 'systemctl --user start kela-kiosk.service'
Icon=kela-kiosk
Categories=Network;
Terminal=false
EOF

# ---------- 9. Operator session hardening -----------------------------------
# Auto-login + never sleep/suspend/lock + screen-never-blank + performance
# power mode + full volume + muted mic on login.
echo "==> [9/11] Operator session: auto-login (Xorg), kiosk lockdown, no-sleep, always-on, no-blank, performance, full-volume, mic-muted"

# --- 9a. GDM auto-login ----------------------------------------------------
mkdir -p /etc/gdm3
if [[ -f /etc/gdm3/custom.conf ]]; then
  sed -i '/^\[daemon\]/,/^\[/{/AutomaticLogin/d}' /etc/gdm3/custom.conf
  if grep -q '^\[daemon\]' /etc/gdm3/custom.conf; then
    sed -i "/^\[daemon\]/a AutomaticLoginEnable=true\nAutomaticLogin=${KELA_USER}" /etc/gdm3/custom.conf
  else
    cat >> /etc/gdm3/custom.conf <<EOF

[daemon]
AutomaticLoginEnable=true
AutomaticLogin=${KELA_USER}
EOF
  fi
else
  cat > /etc/gdm3/custom.conf <<EOF
[daemon]
AutomaticLoginEnable=true
AutomaticLogin=${KELA_USER}
EOF
fi

# --- 9a2. Force Xorg + close VT-switch escape + orientation lock -----------
# Xorg: GNOME's Wayland touchscreen shell-gestures (3-finger/edge swipes to
# overview) are a kiosk escape hole; Xorg has none and gives xinput /
# DontVTSwitch control. Ensure WaylandEnable=false lives under [daemon].
sed -i '/^\[daemon\]/,/^\[/{/WaylandEnable/d}' /etc/gdm3/custom.conf
sed -i '/^\[daemon\]/a WaylandEnable=false' /etc/gdm3/custom.conf

# Mask the spare text consoles so Ctrl+Alt+F2..F6 (kernel-level, not a GNOME
# keybinding) can't drop out of the kiosk to a login shell. tty1 (GDM) stays.
systemctl mask getty@tty2.service getty@tty3.service getty@tty4.service \
              getty@tty5.service getty@tty6.service 2>/dev/null || true

# Orientation lock (landscape): the CF-33 accelerometer would auto-rotate the
# slate; mask iio-sensor-proxy so the UI + touch mapping stay fixed. Touch
# input itself is unaffected.
systemctl mask iio-sensor-proxy.service 2>/dev/null || true

# --- 9b. systemd: mask every sleep/suspend/hibernate path ------------------
# Belt: nothing on the system can pull the box into a low-power state.
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target

# --- 9b2. UPower: never auto-shut-down on low battery ----------------------
# Always-on: run until the battery is physically depleted. GNOME's
# critical-battery-action is neutralized in the dconf db (9d); UPower is the
# other actor that can hibernate/power-off at the critical threshold — set it
# to Ignore too. (Revert both to restore normal low-battery behaviour.)
if [[ -f /etc/UPower/UPower.conf ]]; then
  if grep -q '^CriticalPowerAction=' /etc/UPower/UPower.conf; then
    sed -i 's/^CriticalPowerAction=.*/CriticalPowerAction=Ignore/' /etc/UPower/UPower.conf
  else
    echo 'CriticalPowerAction=Ignore' >> /etc/UPower/UPower.conf
  fi
  systemctl restart upower 2>/dev/null || true
fi

# --- 9c. systemd-logind: ignore lid close, power button, idle action -------
# Braces: even closing the lid, hitting the power button, or going idle
# without a GUI cannot suspend.
mkdir -p /etc/systemd/logind.conf.d
cat > /etc/systemd/logind.conf.d/kela-no-sleep.conf <<'EOF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
HandlePowerKey=ignore
HandleSuspendKey=ignore
HandleHibernateKey=ignore
IdleAction=ignore
EOF
systemctl restart systemd-logind || true

# --- 9d. GNOME power/idle defaults via system dconf db ---------------------
# Authoritative + reliable. `sudo -u kela dbus-launch gsettings set` (the old
# approach) frequently failed to persist — the throwaway D-Bus session didn't
# land the write in kela's dconf, which is why the screen still blanked. A
# system dconf db applies to the kela session deterministically, and the
# locks prevent the values from drifting back on a kiosk.
#  - idle-delay 0          = "Screen Blank: Never"
#  - sleep-inactive-*=nothing / timeout 0 = no automatic suspend
#  - screensaver lock off  = no lock screen
mkdir -p /etc/dconf/profile /etc/dconf/db/local.d/locks

cat > /etc/dconf/profile/user <<'EOF'
user-db:user
system-db:local
EOF

cat > /etc/dconf/db/local.d/00-kela-power <<'EOF'
[org/gnome/desktop/session]
idle-delay=uint32 0

[org/gnome/desktop/screensaver]
lock-enabled=false
idle-activation-enabled=false

[org/gnome/settings-daemon/plugins/power]
sleep-inactive-ac-type='nothing'
sleep-inactive-battery-type='nothing'
sleep-inactive-ac-timeout=0
sleep-inactive-battery-timeout=0
power-button-action='nothing'
idle-dim=false
# Always-on: never auto-shut-down on low battery — run until it physically
# dies. (Paired with CriticalPowerAction=Ignore in /etc/UPower/UPower.conf.)
critical-battery-action='nothing'
power-saver-profile-on-low-battery=false
EOF

cat > /etc/dconf/db/local.d/locks/00-kela-power <<'EOF'
/org/gnome/desktop/session/idle-delay
/org/gnome/desktop/screensaver/lock-enabled
/org/gnome/desktop/screensaver/idle-activation-enabled
/org/gnome/settings-daemon/plugins/power/sleep-inactive-ac-type
/org/gnome/settings-daemon/plugins/power/sleep-inactive-battery-type
/org/gnome/settings-daemon/plugins/power/power-button-action
/org/gnome/settings-daemon/plugins/power/idle-dim
/org/gnome/settings-daemon/plugins/power/critical-battery-action
/org/gnome/settings-daemon/plugins/power/power-saver-profile-on-low-battery
EOF

# --- 9d2. Kiosk input lockdown (dconf) -------------------------------------
# F2 (and A1 via the hwdb remap in 9f) is the ONLY way out of the kiosk. We
# swallow window-closers/new-tab to /bin/true and blank every GNOME shell
# key that could reveal the desktop (overview, app/window/workspace switch,
# close/minimize/show-desktop). Choosing Xorg (9a) already removes the
# touchscreen shell-gestures that Wayland would expose. Also: keep the GNOME
# OSK off (onboard is autostarted in 9f) and set a readable HiDPI scale.
cat > /etc/dconf/db/local.d/10-kela-kiosk <<'EOF'
[org/gnome/settings-daemon/plugins/media-keys]
custom-keybindings=['/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-escape/', '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-noop-cw/', '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-noop-csw/', '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-noop-cq/', '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-noop-ct/', '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-noop-cn/', '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-noop-c0/', '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-noop-c4/', '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-noop-c5/', '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-noop-c6/', '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-noop-c7/', '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-noop-c8/', '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-noop-c9/', '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-noop-csq/', '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-escape-a1/', '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-next-tab/']

[org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-escape]
name='Kela kiosk escape'
command='/usr/local/bin/kela-kiosk-escape'
binding='F2'

# A1 bezel button (tablet-mode escape). Shares the escape script with F2 — the
# stamp-file press counter means triple-press works from either. Fires when A1
# emits KEY_PROG1 natively (XF86Launch1); the 9f hwdb A1->f2 path covers the
# raw-scancode firmware instead.
[org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-escape-a1]
name='Kela kiosk escape (A1)'
command='/usr/local/bin/kela-kiosk-escape'
binding='XF86Launch1'

# A2 bezel button (tablet-mode next view). Injects Ctrl+Tab to cycle the three
# kiosk tabs forward. Fires when A2 emits KEY_PROG2 natively (XF86Launch2); the
# 9f hwdb A2->prog2 path covers the raw-scancode firmware instead.
[org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-next-tab]
name='Kela kiosk next view'
command='/usr/local/bin/kela-kiosk-next-tab'
binding='XF86Launch2'

[org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-noop-cw]
name='block close-tab'
command='/bin/true'
binding='<Ctrl>w'

[org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-noop-csw]
name='block close-window'
command='/bin/true'
binding='<Ctrl><Shift>w'

[org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-noop-cq]
name='block quit'
command='/bin/true'
binding='<Ctrl>q'

[org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-noop-csq]
name='block quit (Chrome Ctrl+Shift+Q)'
command='/bin/true'
binding='<Ctrl><Shift>q'

[org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-noop-ct]
name='block new-tab'
command='/bin/true'
binding='<Ctrl>t'

[org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-noop-cn]
name='block new-window'
command='/bin/true'
binding='<Ctrl>n'

# Kiosk tab nav: Ctrl+Tab and Ctrl+1/2/3 (the configured TAB1..TAB3) are
# left alone so they reach Chrome. Ctrl+0 (zoom reset) and Ctrl+4..9
# (jump-to-tab-N / last-tab) are grabbed to /bin/true so stray number keys
# can't reset zoom or land on a phantom tab. With fewer than three tabs the
# unused Ctrl+2/3 are harmless no-ops.
[org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-noop-c0]
name='block zoom-reset'
command='/bin/true'
binding='<Ctrl>0'

[org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-noop-c4]
name='block tab-4'
command='/bin/true'
binding='<Ctrl>4'

[org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-noop-c5]
name='block tab-5'
command='/bin/true'
binding='<Ctrl>5'

[org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-noop-c6]
name='block tab-6'
command='/bin/true'
binding='<Ctrl>6'

[org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-noop-c7]
name='block tab-7'
command='/bin/true'
binding='<Ctrl>7'

[org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-noop-c8]
name='block tab-8'
command='/bin/true'
binding='<Ctrl>8'

[org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/kela-noop-c9]
name='block tab-last'
command='/bin/true'
binding='<Ctrl>9'

[org/gnome/mutter]
overlay-key=''

[org/gnome/shell/keybindings]
toggle-overview=@as []
toggle-application-view=@as []
focus-active-notification=@as []

[org/gnome/desktop/wm/keybindings]
close=@as []
minimize=@as []
show-desktop=@as []
switch-applications=@as []
switch-applications-backward=@as []
switch-windows=@as []
switch-windows-backward=@as []
switch-group=@as []
switch-group-backward=@as []
switch-panels=@as []
switch-panels-backward=@as []
panel-main-menu=@as []
panel-run-dialog=@as []
switch-to-workspace-left=@as []
switch-to-workspace-right=@as []
switch-to-workspace-up=@as []
switch-to-workspace-down=@as []

[org/gnome/desktop/a11y/applications]
screen-keyboard-enabled=false

[org/onboard/auto-show]
enabled=true

# Dock pins (visible only after a kiosk escape): kiosk return, LAN-only
# Chrome, and a terminal — the three things a technician reaches for.
[org/gnome/shell]
favorite-apps=['kela-kiosk.desktop', 'kela-chrome-regular.desktop', 'org.gnome.Terminal.desktop']

# Hebrew input alongside US — the hub UI is Hebrew and operators type into
# its forms. Switch layouts with Super+Space (deliberately NOT blanked by the
# lockdown; the bare overlay-key is). onboard follows the active layout.
# LOCKED (see locks file): GNOME materializes a per-user input-sources value
# at first login, and an unlocked system default would lose to it.
[org/gnome/desktop/input-sources]
sources=[('xkb', 'us'), ('xkb', 'il')]

[org/gnome/desktop/interface]
text-scaling-factor=1.25
EOF

cat > /etc/dconf/db/local.d/locks/10-kela-kiosk <<'EOF'
/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings
/org/gnome/mutter/overlay-key
/org/gnome/shell/keybindings/toggle-overview
/org/gnome/shell/keybindings/toggle-application-view
/org/gnome/desktop/wm/keybindings/close
/org/gnome/desktop/wm/keybindings/minimize
/org/gnome/desktop/wm/keybindings/show-desktop
/org/gnome/desktop/wm/keybindings/switch-applications
/org/gnome/desktop/wm/keybindings/switch-applications-backward
/org/gnome/desktop/wm/keybindings/switch-windows
/org/gnome/desktop/wm/keybindings/switch-windows-backward
/org/gnome/desktop/wm/keybindings/switch-group
/org/gnome/desktop/wm/keybindings/switch-group-backward
/org/gnome/desktop/wm/keybindings/switch-panels
/org/gnome/desktop/wm/keybindings/switch-panels-backward
/org/gnome/desktop/wm/keybindings/switch-to-workspace-left
/org/gnome/desktop/wm/keybindings/switch-to-workspace-right
/org/gnome/desktop/wm/keybindings/switch-to-workspace-up
/org/gnome/desktop/wm/keybindings/switch-to-workspace-down
/org/gnome/desktop/a11y/applications/screen-keyboard-enabled
/org/gnome/desktop/input-sources/sources
EOF

dconf update

# --- 9e. Full output volume + muted microphone on every login --------------
# pactl talks to PipeWire-Pulse in the kela session, so it has to run inside
# that session — autostart entry is the cleanest hook.
cat > /usr/local/bin/kela-session-init <<'EOF'
#!/usr/bin/env bash
# Per-login session init, runs inside kela's graphical session.

# Power Mode: performance. power-profiles-daemon resets to 'balanced' on
# every boot, so it has to be re-applied each login from within the active
# session (polkit allows the active local user to switch profiles).
if command -v powerprofilesctl >/dev/null 2>&1; then
  powerprofilesctl set performance 2>/dev/null || true
fi

# Belt-and-braces "Screen Blank: Never" in case dconf lock didn't take —
# also kills X DPMS / screensaver blanking on Xorg sessions.
gsettings set org.gnome.desktop.session idle-delay 0 2>/dev/null || true
if command -v xset >/dev/null 2>&1 && [ -n "${DISPLAY:-}" ]; then
  xset s off 2>/dev/null || true
  xset -dpms 2>/dev/null || true
fi

# Wait briefly for the audio server, then: unmute output + max volume,
# and mute the microphone (every input source) by default.
for _ in 1 2 3 4 5 6 7 8 9 10; do
  pactl info >/dev/null 2>&1 && break
  sleep 1
done
if command -v pactl >/dev/null 2>&1; then
  # Output: unmute + full volume
  pactl set-sink-mute   @DEFAULT_SINK@ false 2>/dev/null || true
  pactl set-sink-volume @DEFAULT_SINK@ 100%  2>/dev/null || true
  # Input: mute + zero the default source and every other source
  pactl set-source-mute   @DEFAULT_SOURCE@ true 2>/dev/null || true
  pactl set-source-volume @DEFAULT_SOURCE@ 0%   2>/dev/null || true
  while read -r _src_id _; do
    [ -n "$_src_id" ] || continue
    pactl set-source-mute "$_src_id" true 2>/dev/null || true
  done < <(pactl list short sources 2>/dev/null)
fi
EOF
chmod 0755 /usr/local/bin/kela-session-init

install -d -o "$KELA_USER" -g "$KELA_USER" "$KELA_HOME/.config/autostart"
cat > "$KELA_HOME/.config/autostart/kela-session-init.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Kela session init (power, volume, mic)
Exec=/usr/local/bin/kela-session-init
X-GNOME-Autostart-enabled=true
NoDisplay=true
Terminal=false
EOF
chown "$KELA_USER:$KELA_USER" "$KELA_HOME/.config/autostart/kela-session-init.desktop"

# --- 9f. Kiosk bezel buttons (escape + next-view) + OSK --------------------
# Tablet mode has no keyboard, so the CF-33's two bezel buttons drive the
# kiosk:
#   A1 = escape  — triple-press within 1.5s stops the kiosk (drops to GNOME).
#   A2 = next view — injects Ctrl+Tab to cycle the three kiosk tabs.
# Both reach us as XF86Launch1/XF86Launch2 GNOME bindings (10-kela-kiosk
# dconf, 9d2). Depending on firmware the buttons either emit KEY_PROG1/PROG2
# natively (mapped to XF86Launch1/2 already — no hwdb needed) or raw scancodes
# that the hwdb file below remaps (A1->f2 reuses the F2 escape binding, A2->
# prog2 lands on XF86Launch2). The escape script counts presses in a stamp
# file, so the F2 and XF86Launch1 bindings share it. Runs as kela inside the
# session, so `systemctl --user` works.
cat > /usr/local/bin/kela-kiosk-escape <<'EOF'
#!/usr/bin/env bash
# Triple-press F2 (or A1, remapped to F2) within WINDOW_MS -> stop the kiosk.
set -euo pipefail
WINDOW_MS=1500
NEED=3
STAMP="${XDG_RUNTIME_DIR:-/tmp}/kela-f2-presses"

now=$(date +%s%3N)
echo "$now" >> "$STAMP"
recent="$(awk -v now="$now" -v w="$WINDOW_MS" '(now - $1) <= w' "$STAMP" 2>/dev/null || true)"
printf '%s\n' "$recent" > "$STAMP"
if [ "$(printf '%s\n' "$recent" | grep -c .)" -ge "$NEED" ]; then
  : > "$STAMP"
  systemctl --user stop kela-kiosk.service 2>/dev/null || true
fi
EOF
chmod 0755 /usr/local/bin/kela-kiosk-escape

# A2 = next kiosk view. hwdb can only map a button to a single key, never to a
# chord, so the Ctrl+Tab chord is injected here (Xorg session, so xdotool
# works). Two details validated on real hardware — do not simplify away:
#   - gsd holds an active keyboard grab while the shortcut key is physically
#     down; an XTest injection during the grab is delivered to gsd, NOT
#     Chrome, and silently vanishes. The sleep waits out the key release.
#   - the environment gsd spawns from can lack DISPLAY/XAUTHORITY, killing
#     xdotool instantly — export session defaults.
# No kiosk-active guard: after an escape, A2 just injects Ctrl+Tab into
# whatever is focused — harmless.
cat > /usr/local/bin/kela-kiosk-next-tab <<'EOF'
#!/usr/bin/env bash
# A2 bezel button -> cycle the three kiosk tabs forward (Ctrl+Tab).
set -uo pipefail
command -v xdotool >/dev/null 2>&1 || exit 0
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-/run/user/$(id -u)/gdm/Xauthority}"
# Wait out gsd's shortcut grab (released when the button is let go).
sleep 0.35
exec xdotool key --clearmodifiers ctrl+Tab
EOF
chmod 0755 /usr/local/bin/kela-kiosk-next-tab

# Bezel buttons -> keysyms. Captured on real CF-33 hardware (2026-07-06,
# device "Panasonic Laptop Support"): A1 = scan 09 (firmware default
# KEY_BATTERY), A2 = scan 0a (firmware default KEY_SUSPEND — which only did
# nothing because sleep is masked). The match is scoped to the Panasonic ACPI
# button device AND Panasonic DMI vendor, so it cannot touch the keyboard,
# touchscreen, or non-Panasonic hardware. If a different firmware revision
# leaves the buttons inert: re-capture with `sudo evtest` (press each button,
# read the MSC_SCAN value), update the scancodes here, and re-run setup.sh —
# or edit the file on-box and: sudo systemd-hwdb update && sudo udevadm trigger
# Drop the pre-rename stub so a re-run on an older station leaves no duplicate.
mkdir -p /etc/udev/hwdb.d
rm -f /etc/udev/hwdb.d/70-kela-cf33-a1.hwdb
cat > /etc/udev/hwdb.d/70-kela-cf33-buttons.hwdb <<'EOF'
# Kela CF-33: remap the A1/A2 bezel buttons for kiosk control.
#   A1 (scan 09) -> f2    = kiosk escape (triple-press, same binding as F2)
#   A2 (scan 0a) -> prog2 = next view (XF86Launch2 binding)
# Captured on-device 2026-07-06. If the buttons are inert on another firmware
# revision, re-capture with `sudo evtest` and update the scancodes, then:
#   sudo systemd-hwdb update && sudo udevadm trigger
evdev:name:Panasonic Laptop Support:dmi:bvn*:bvr*:bd*:svnPanasonic*:*
 KEYBOARD_KEY_09=f2
 KEYBOARD_KEY_0a=prog2
EOF
systemd-hwdb update 2>/dev/null || true
udevadm trigger 2>/dev/null || true

# On-screen keyboard (onboard) for tablet use — auto-shows on text-field
# focus (org.onboard/auto-show enabled in the 10-kela-kiosk dconf). More
# reliable over a fullscreen kiosk than the GNOME built-in OSK.
install -d -o "$KELA_USER" -g "$KELA_USER" "$KELA_HOME/.config/autostart"
cat > "$KELA_HOME/.config/autostart/onboard-kela.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Onboard on-screen keyboard
Exec=onboard
X-GNOME-Autostart-enabled=true
NoDisplay=true
Terminal=false
EOF
chown "$KELA_USER:$KELA_USER" "$KELA_HOME/.config/autostart/onboard-kela.desktop"

# ---------- 10. UFW (system-wide) + per-user egress lock for kela -----------
# UFW carves out 443/tcp + 53 system-wide so tailscaled (control plane),
# AnyDesk, and apt updates all work. WITHOUT a per-user restriction this
# also lets Chrome — running as the kela user — reach any HTTPS endpoint on
# the internet (ynet.co.il, google.com, etc.). The iptables OWNER-match
# block in 10b restricts the kela user to loopback / LAN / Tailscale only.
echo "==> [10/11] Configuring UFW + per-user egress lock for ${KELA_USER}"
ufw --force reset
ufw default deny incoming
ufw default deny outgoing

# Loopback
ufw allow in on lo
ufw allow out on lo

# Tailscale interface — full trust on the overlay
ufw allow in on tailscale0
ufw allow out on tailscale0

# LAN ingress/egress (RFC1918 + Tailscale CGNAT)
ufw allow out to 10.0.0.0/8
ufw allow out to 172.16.0.0/12
ufw allow out to 192.168.0.0/16
ufw allow out to 100.64.0.0/10
ufw allow from 10.0.0.0/8
ufw allow from 172.16.0.0/12
ufw allow from 192.168.0.0/16
ufw allow from 100.64.0.0/10

# System-wide outbound carve-outs (kela user is restricted further in 10b)
ufw allow out 53
ufw allow out 443/tcp
ufw allow out 41641/udp
ufw allow out 3478/udp
# 80/tcp: archive.ubuntu.com / security.ubuntu.com / deb.anydesk.com are
# plain http — without this, fleet patching over Tailscale dies at the apt
# step. Chrome (kela user) is still LAN-only via the OWNER-match chain (10b).
ufw allow out 80/tcp
# 123/udp: NTP fallback (ntp.ubuntu.com) when the hub is unreachable.
ufw allow out 123/udp

ufw --force enable

# --- 10b. Per-user egress firewall: kela → loopback/LAN/Tailscale only -----
# iptables OWNER-match: when the source UID is kela's, only allow traffic
# headed to loopback, the tailscale0 interface, RFC1918, or 100.64/10
# (Tailscale CGNAT). Everything else gets REJECT icmp-net-prohibited, which
# Chrome surfaces as ERR_NETWORK_ACCESS_DENIED. tailscaled, AnyDesk, apt,
# systemd-resolved etc. all run as root so they're unaffected.
KELA_UID="$(id -u "$KELA_USER")"

install -m 0755 /dev/stdin /usr/local/sbin/kela-egress-firewall <<EOF
#!/usr/bin/env bash
# Apply (up) or remove (down) the per-user egress restriction for kela.
set -e
KELA_UID=${KELA_UID}
CHAIN=KELA_EGRESS

case "\${1:-up}" in
  up)
    iptables -N "\$CHAIN" 2>/dev/null || iptables -F "\$CHAIN"
    iptables -A "\$CHAIN" -o lo                   -j ACCEPT
    iptables -A "\$CHAIN" -o tailscale0           -j ACCEPT
    iptables -A "\$CHAIN" -d 10.0.0.0/8           -j ACCEPT
    iptables -A "\$CHAIN" -d 172.16.0.0/12        -j ACCEPT
    iptables -A "\$CHAIN" -d 192.168.0.0/16       -j ACCEPT
    iptables -A "\$CHAIN" -d 100.64.0.0/10        -j ACCEPT
    iptables -A "\$CHAIN" -j REJECT --reject-with icmp-net-prohibited
    iptables -C OUTPUT -m owner --uid-owner "\$KELA_UID" -j "\$CHAIN" 2>/dev/null \
      || iptables -I OUTPUT -m owner --uid-owner "\$KELA_UID" -j "\$CHAIN"
    ;;
  down)
    iptables -D OUTPUT -m owner --uid-owner "\$KELA_UID" -j "\$CHAIN" 2>/dev/null || true
    iptables -F "\$CHAIN" 2>/dev/null || true
    iptables -X "\$CHAIN" 2>/dev/null || true
    ;;
  *)
    echo "usage: \$0 {up|down}" >&2; exit 2 ;;
esac
EOF

cat > /etc/systemd/system/kela-egress.service <<'EOF'
[Unit]
Description=Kela operator per-user egress firewall (kela -> LAN+Tailscale only)
After=network-online.target ufw.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/kela-egress-firewall up
ExecStop=/usr/local/sbin/kela-egress-firewall down

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now kela-egress.service

ufw status verbose
iptables -L KELA_EGRESS -n -v 2>/dev/null || true

# ---------- 11a. Strip games + bloat ----------------------------------------
# CASCADE WARNING: purging a package that sits in the desktop's Depends chain
# makes apt remove ubuntu-desktop-minimal / gnome-shell / gdm3 WITH it, -y and
# silently — a bench unit shipped desktop-less this way (the §12 tripwire
# below now catches it). Before adding anything here, dry-run on a built unit:
#   apt-get purge -s <pkg> | grep -E 'gdm3|gnome-shell|ubuntu-desktop'
# gnome-initial-setup is deliberately NOT purged (it can be a hard dep of
# ubuntu-desktop-minimal); its wizard is suppressed via the
# gnome-initial-setup-done file instead.
echo "==> [11/11] Removing games / bloat and blocking Bluetooth"
apt-get "${APT_LOCK[@]}" purge -y \
  aisleriot gnome-mahjongg gnome-mines gnome-sudoku gnome-2048 \
  gnome-chess gnome-klotski gnome-nibbles gnome-robots gnome-tetravex \
  gnome-taquin gnome-tali quadrapassel four-in-a-row five-or-more \
  swell-foop hitori iagno lightsoff \
  rhythmbox shotwell cheese \
  thunderbird transmission-gtk transmission-common \
  remmina remmina-common \
  simple-scan deja-dup \
  'libreoffice*' \
  gnome-todo gnome-weather gnome-clocks gnome-contacts gnome-maps \
  || true

# ---------- 11b. Block Bluetooth (rfkill + module blacklist + purge bluez) --
# Immediate soft block
rfkill block bluetooth || true

# Persistent rfkill via systemd one-shot
cat > /etc/systemd/system/rfkill-block-bluetooth.service <<'EOF'
[Unit]
Description=Persistently soft-block Bluetooth
After=systemd-rfkill.service

[Service]
Type=oneshot
ExecStart=/usr/sbin/rfkill block bluetooth
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now rfkill-block-bluetooth.service

# Disable Bluetooth daemon
systemctl disable --now bluetooth.service 2>/dev/null || true
systemctl mask bluetooth.service 2>/dev/null || true

# Module blacklist — definitive
cat > /etc/modprobe.d/blacklist-bluetooth.conf <<'EOF'
# Kela operator: Bluetooth must remain disabled
blacklist bluetooth
blacklist btusb
blacklist btbcm
blacklist btintel
blacklist btrtl
blacklist btmtk
blacklist hci_uart
install bluetooth /bin/true
install btusb /bin/true
EOF
update-initramfs -u

# Purge BlueZ stack so nothing tries to reload modules
# Daemon stack only. Do NOT purge 'gnome-bluetooth*': gnome-shell depends on
# the gnome-bluetooth library chain, and that glob cascaded into removing
# gnome-shell + gdm3 + ubuntu-desktop-minimal — silently (-y + discarded
# stderr) — leaving a text-console "kiosk". Bluetooth is already dead five
# other ways (rfkill, masked service, module blacklist, initramfs, BlueZ
# purge); the GNOME UI libs are harmless. stderr is kept visible so any
# future cascade shows in the first-boot log.
apt-get "${APT_LOCK[@]}" purge -y bluez bluez-cups bluez-obexd || true
apt-get "${APT_LOCK[@]}" autoremove -y --purge

# Tripwire: if any purge/autoremove above cascaded into the desktop, fail HERE
# with a named cause — not five minutes later as a bare kela-verify FAIL.
for p in ubuntu-desktop-minimal gdm3 gnome-shell; do
  if ! dpkg -s "$p" >/dev/null 2>&1; then
    echo "ERROR: bloat purge cascaded — '$p' was removed. A package in the" >&2
    echo "       §11 purge lists sits in the desktop's Depends chain." >&2
    echo "       Check: grep 'Remove:' /var/log/apt/history.log | tail -2" >&2
    exit 1
  fi
done
apt-get "${APT_LOCK[@]}" clean

# ---------- 12. kela-verify: machine-checked success ------------------------
# The §4.3 manual checklist, as code. kela-first-boot-run runs this before it
# cleans up (and refuses to clean up if it fails); ops can re-run it any time
# over SSH: sudo kela-verify
echo "==> [12/12] Installing kela-verify"

install -m 0755 /dev/stdin /usr/local/sbin/kela-verify <<'KELAVERIFY'
#!/usr/bin/env bash
# kela-verify — machine-check that this operator station is correctly built.
# PASS/FAIL per check; exit 0 only if every REQUIRED check passes.
# Optional checks (cert may legitimately not exist yet, etc.) only WARN.
set -uo pipefail

PASS=0; FAIL=0; WARN=0
req() { local d="$1"; shift; if "$@" >/dev/null 2>&1; then echo "PASS  $d"; PASS=$((PASS+1)); else echo "FAIL  $d"; FAIL=$((FAIL+1)); fi; }
opt() { local d="$1"; shift; if "$@" >/dev/null 2>&1; then echo "PASS  $d"; PASS=$((PASS+1)); else echo "WARN  $d"; WARN=$((WARN+1)); fi; }

if [[ $EUID -ne 0 ]]; then echo "ERROR: run as root: sudo kela-verify" >&2; exit 2; fi
KELA_HOME="$(getent passwd kela | cut -d: -f6)"

# Site addressing (single source of truth) so checks assert the CONFIGURED
# values, not hardcoded literals. Fallbacks keep `set -u` from crashing if the
# file is missing — the "station.conf present" check below then FAILs cleanly.
STATION_CONF=/etc/kela/station.conf
[ -r "$STATION_CONF" ] && . "$STATION_CONF"
KELA_LOCAL_IP="${KELA_LOCAL_IP:-}"
KELA_LOCAL_HOST="${KELA_LOCAL_HOST:-}"
KELA_LOCAL_URL="${KELA_LOCAL_URL:-}"
TAB1="${TAB1:-}"
TAB2="${TAB2:-}"
TAB3="${TAB3:-}"

echo "--- site addressing (single source of truth: /etc/kela/station.conf) ---"
req "station.conf present"               test -f "$STATION_CONF"
req "hub URL configured"                 test -n "$KELA_LOCAL_URL"
req "TAB1 (kiosk main view) configured"  test -n "$TAB1"
req "hub host -> hub IP in /etc/hosts"   grep -qE "^${KELA_LOCAL_IP}[[:space:]]+${KELA_LOCAL_HOST}([[:space:]]|$)" /etc/hosts
opt "hub reachable ($KELA_LOCAL_HOST)"   sh -c "curl -sk --max-time 5 -o /dev/null \"$KELA_LOCAL_URL\""
# TAB2/TAB3 are optional — checked only when configured.
[ -n "$TAB2" ] && opt "TAB2 reachable ($TAB2)" sh -c "curl -sk --max-time 5 -o /dev/null \"$TAB2\""
[ -n "$TAB3" ] && opt "TAB3 reachable ($TAB3)" sh -c "curl -sk --max-time 5 -o /dev/null \"$TAB3\""

echo "--- remote access (a FAIL here means a stranded station) ---"
req "tailscaled service active"          systemctl is-active --quiet tailscaled
req "tailscale connected (has IPv4)"     tailscale ip -4
req "anydesk service active"             systemctl is-active --quiet anydesk
# req, not opt: AnyDesk without the unattended password is not a working
# remote-access fallback — this silently WARNed for weeks while §4's
# double-line stdin bug made every set-password attempt fail.
req "anydesk unattended password set"    grep -qi pwd_hash /etc/anydesk/system.conf

echo "--- firewall ---"
req "ufw active"                         sh -c 'ufw status | grep -q "Status: active"'
req "kela egress chain loaded"           iptables -S KELA_EGRESS
req "kela-egress.service enabled"        systemctl is-enabled --quiet kela-egress.service

echo "--- updatability (vendor apt repos, so central patching can reach them) ---"
opt "tailscale apt repo configured"      test -f /etc/apt/sources.list.d/tailscale.list
opt "anydesk apt repo configured"        test -f /etc/apt/sources.list.d/anydesk-stable.list
opt "chrome apt repo configured"         test -f /etc/apt/sources.list.d/google-chrome.list

echo "--- kiosk ---"
# The desktop itself, not just its config files: a bench unit once passed
# every check below with NO display manager installed (the USB desktop
# install had silently not completed) — the gate vouched for a kiosk that
# could only boot to a text console. Any hard runtime dependency of the
# kiosk design belongs here as a req.
req "desktop metapackage installed"      dpkg -s ubuntu-desktop-minimal
req "gdm3 installed"                     dpkg -s gdm3
req "Xorg installed (WaylandEnable=false needs it)" dpkg -s xserver-xorg-core
req "default boot target is graphical"   sh -c 'systemctl get-default | grep -q graphical'
req "GDM auto-login configured"          grep -q "AutomaticLogin=kela" /etc/gdm3/custom.conf
req "GDM forced onto Xorg"               grep -q "^WaylandEnable=false" /etc/gdm3/custom.conf
req "Chrome installed"                   test -x /usr/bin/google-chrome-stable
req "Chrome policy present"              test -f /etc/opt/chrome/policies/managed/kela-policy.json
req "kiosk launcher present"             test -x /usr/local/bin/kela-kiosk
req "kiosk user service present"         test -f "$KELA_HOME/.config/systemd/user/kela-kiosk.service"
req "kiosk restart limit disabled"       grep -q '^StartLimitIntervalSec=0' "$KELA_HOME/.config/systemd/user/kela-kiosk.service"
req "kiosk waits for hub before launch"  grep -q 'curl -ks --max-time 2' /usr/local/bin/kela-kiosk
req "kiosk hub-recovery watch script"    test -x /usr/local/bin/kela-kiosk-watch
req "kiosk hub-recovery watch service"   test -f "$KELA_HOME/.config/systemd/user/kela-kiosk-watch.service"
req "kiosk autostart present"            test -f "$KELA_HOME/.config/autostart/kela-kiosk.desktop"
req "kiosk escape script present"        test -x /usr/local/bin/kela-kiosk-escape
req "kiosk next-view script present"     test -x /usr/local/bin/kela-kiosk-next-tab
req "xdotool installed (A2 injection)"   test -x /usr/bin/xdotool
req "kiosk keybinding dconf present"     test -f /etc/dconf/db/local.d/10-kela-kiosk
req "A2 next-view binding present"       grep -q "kela-next-tab" /etc/dconf/db/local.d/10-kela-kiosk
req "kiosk launcher sources station.conf" grep -q 'station.conf' /usr/local/bin/kela-kiosk
req "Ctrl+0/4..9 tab-jumps blocked"      grep -q "kela-noop-c9" /etc/dconf/db/local.d/10-kela-kiosk
req "Ctrl+Shift+Q (Chrome quit) blocked" grep -q "kela-noop-csq" /etc/dconf/db/local.d/10-kela-kiosk
req "'Chrome (Regular)' launcher"        test -f /usr/share/applications/kela-chrome-regular.desktop
req "'Kela Kiosk' launcher"              test -f /usr/share/applications/kela-kiosk.desktop
req "kiosk launcher has distinct icon"   test -f /usr/share/icons/hicolor/scalable/apps/kela-kiosk.svg
req "dock favorites pinned"              grep -q "favorite-apps=" /etc/dconf/db/local.d/10-kela-kiosk
req "Hebrew keyboard layout configured"  grep -q "'il'" /etc/dconf/db/local.d/10-kela-kiosk
req "onboard OSK installed"              test -x /usr/bin/onboard
req "session-init autostart present"     test -f "$KELA_HOME/.config/autostart/kela-session-init.desktop"

echo "--- kiosk runtime (WARNs during first-boot; meaningful on a re-run after reboot) ---"
# Files existing != kiosk running. A station can pass every file check, reboot,
# and sit on a dead session. These assert the kiosk is actually up. They're opt
# (WARN) because the kiosk starts at graphical login, so during the first-boot
# finalize run — before that session exists — they legitimately WARN; run
# `sudo kela-verify` after reboot for the real signal (also the exact health
# signal a future fleet endpoint would report).
KELA_UID="$(id -u kela 2>/dev/null || true)"
opt "kiosk service active"               sudo -u kela env XDG_RUNTIME_DIR="/run/user/${KELA_UID}" systemctl --user is-active --quiet kela-kiosk.service
opt "Chrome kiosk process present"       pgrep -u kela -f 'google-chrome-stable.*--kiosk'

echo "--- power / sleep / always-on ---"
for t in sleep.target suspend.target hibernate.target hybrid-sleep.target; do
  req "$t masked" sh -c "systemctl is-enabled $t 2>&1 | grep -q masked"
done
req "logind no-sleep drop-in present"    test -f /etc/systemd/logind.conf.d/kela-no-sleep.conf
req "dconf power db compiled"            test -f /etc/dconf/db/local
req "UPower CriticalPowerAction=Ignore"  sh -c "grep -q '^CriticalPowerAction=Ignore' /etc/UPower/UPower.conf"
req "dconf critical-battery=nothing"     sh -c "grep -q \"critical-battery-action='nothing'\" /etc/dconf/db/local.d/00-kela-power"

echo "--- kiosk session lockdown ---"
req "spare gettys masked"                sh -c "systemctl is-enabled getty@tty2.service 2>&1 | grep -q masked"
req "orientation lock (iio masked)"      sh -c "systemctl is-enabled iio-sensor-proxy.service 2>&1 | grep -q masked"
req "kiosk keybinding lock present"      test -f /etc/dconf/db/local.d/locks/10-kela-kiosk

echo "--- bluetooth ---"
req "bluetooth.service masked"           sh -c "systemctl is-enabled bluetooth.service 2>&1 | grep -q masked"
req "bluetooth module blacklist"         test -f /etc/modprobe.d/blacklist-bluetooth.conf
opt "no bluetooth modules loaded"        sh -c '! lsmod | grep -qiE "^(bluetooth|btusb) "'

echo "--- time ---"
req "NTP drop-in present"                test -f /etc/systemd/timesyncd.conf.d/kela-ntp.conf
opt "timesyncd active"                   systemctl is-active --quiet systemd-timesyncd

echo "--- server cert (optional: site server may not exist yet) ---"
opt "kela server cert installed"         test -f /usr/local/share/ca-certificates/kela/kela-server.crt
req "kela-cert-ensure script present"    test -x /usr/local/sbin/kela-cert-ensure
req "kela-cert-ensure timer enabled"     systemctl is-enabled --quiet kela-cert-ensure.timer

echo
echo "kela-verify: ${PASS} pass, ${FAIL} fail, ${WARN} warn"
[[ $FAIL -eq 0 ]]
KELAVERIFY

# ---------- 12b. build-info + optional check-in ------------------------------
# One machine-readable record per station: what was built, when, from which
# script version, and how to reach it (Tailscale IP + AnyDesk ID — no more
# "open AnyDesk and read the corner"). If KELA_CHECKIN_URL is set (e.g. in
# secrets.env), POST it to the hub/inventory endpoint, best-effort.
echo "==> Writing /etc/kela/build-info"
mkdir -p /etc/kela
TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
ANYDESK_ID="$(anydesk --get-id 2>/dev/null || true)"
cat > /etc/kela/build-info <<EOF
site=${SITE_NAME}
hostname=${HOSTNAME_NEW}
setup_version=${SETUP_VERSION}
built_at=$(date -Iseconds)
tailscale_ip=${TS_IP}
anydesk_id=${ANYDESK_ID}
EOF
chmod 644 /etc/kela/build-info
sed 's/^/    /' /etc/kela/build-info

if [[ -n "${KELA_CHECKIN_URL:-}" ]]; then
  if curl -m 10 -fsS -X POST --data-binary @/etc/kela/build-info \
       "$KELA_CHECKIN_URL" >/dev/null 2>&1; then
    echo "    Checked in to ${KELA_CHECKIN_URL}"
  else
    echo "WARN: check-in POST to ${KELA_CHECKIN_URL} failed (non-fatal)."
  fi
fi

# ---------- done ------------------------------------------------------------
cat <<EOF

============================================================================
  $HOSTNAME_NEW built.
EOF

if [[ -z "$TS_AUTHKEY" ]] || ! tailscale ip -4 >/dev/null 2>&1; then
  cat <<EOF

  TODO: Tailscale is not connected. Run:
    sudo tailscale up --hostname=$HOSTNAME_NEW --accept-routes
EOF
fi

cat <<EOF

  Verify:        sudo kela-verify
  Station info:  cat /etc/kela/build-info   (site, Tailscale IP, AnyDesk ID)

  Manual follow-ups:
    1. Server cert: if the site server didn't exist during build, it installs
       automatically within ~5 min of the server coming online (kela-cert-ensure
       timer, self-heals on cert rotation). To skip the wait: sudo kela-install-cert
    2. Reboot to confirm:
         - Bluetooth stays blocked (rfkill list / lsmod | grep -i blue)
         - WiFi available (rfkill list / nmcli dev)
         - Microphone muted (pactl get-source-mute @DEFAULT_SOURCE@ -> yes)
         - Auto-login -> Chrome -> ${KELA_LOCAL_URL} (no cert warning)
         - UFW active, LAN-only egress holds
============================================================================
EOF