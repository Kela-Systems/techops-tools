#!/usr/bin/env bash
# ============================================================================
#  Kela operator machine setup — Ubuntu 24.04 on Dell Latitude 5420 Rugged
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
SETUP_VERSION="2026-06-10.1"

# ---------- config ----------------------------------------------------------
SITE_NAME="${SITE_NAME:-CHANGE-ME}"
KELA_USER="kela"
ANYDESK_PASS="Kelasys123!"
KELA_LOCAL_IP="192.168.88.10"
KELA_LOCAL_HOST="kela.local"
KELA_LOCAL_URL="https://${KELA_LOCAL_HOST}/"

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

# ---------- 0. wait for network ----------------------------------------------
# A slow DHCP lease at first boot fails the Tailscale/cert steps. Even
# offline-pool installs need the network for `tailscale up`, so wait up to
# 45s for a default route — then warn-and-continue (the Tailscale gate in
# kela-first-boot-run keeps the retry path if connectivity never comes).
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
# Timezone Asia/Jerusalem; sync time from the site server (192.168.88.10).
# UFW (§10) is LAN-only egress, so the NTP server must be on the LAN.
echo "==> [1b/11] Timezone Asia/Jerusalem + NTP ${KELA_LOCAL_IP}"
timedatectl set-timezone Asia/Jerusalem 2>/dev/null || \
  ln -sf /usr/share/zoneinfo/Asia/Jerusalem /etc/localtime
mkdir -p /etc/systemd/timesyncd.conf.d
# Hub is the primary time source. Fallback to ntp.ubuntu.com so a station
# whose RTC drifted while the hub is down can still get sane time (TLS
# breaks with "cert not yet valid" otherwise). UFW (§10) allows 123/udp out.
cat > /etc/systemd/timesyncd.conf.d/kela-ntp.conf <<EOF
[Time]
NTP=${KELA_LOCAL_IP}
FallbackNTP=ntp.ubuntu.com
EOF
timedatectl set-ntp true 2>/dev/null || true
systemctl restart systemd-timesyncd 2>/dev/null || true

# ---------- 2. base packages + updates --------------------------------------
echo "==> [2/11] apt update + base tools"
export DEBIAN_FRONTEND=noninteractive

# Offline mode: when the ISO baked an offline pool into /opt/kela-pool, install
# everything from there (no network). The build-iso/late-commands flow leaves
# the pool in place; kela-first-boot-run removes it after first boot succeeds.
KELA_POOL_DIR="${KELA_POOL_DIR:-/opt/kela-pool}"
APT_OPTS=()
if [[ -f "$KELA_POOL_DIR/Packages" ]]; then
  OFFLINE_APT=1
  printf 'deb [trusted=yes] file:%s ./\n' "$KELA_POOL_DIR" > /etc/apt/kela-offline.list
  APT_OPTS=( -o Dir::Etc::SourceList=/etc/apt/kela-offline.list -o Dir::Etc::SourceParts=/dev/null )
  echo "    Offline pool detected at $KELA_POOL_DIR — installing from local debs."
else
  OFFLINE_APT=0
fi

# apt_get: routes through the offline pool when OFFLINE_APT=1, else normal apt.
apt_get() { apt-get "${APT_OPTS[@]}" "$@"; }

apt_get update -y

# apt upgrade is intentionally skipped (it was a redundant re-download). Patch
# centrally over Tailscale instead. Set KELA_APT_UPGRADE=1 to force it online.
if [[ "$OFFLINE_APT" == "0" && "${KELA_APT_UPGRADE:-0}" == "1" ]]; then
  apt-get upgrade -y
fi

apt_get install -y curl wget ca-certificates apt-transport-https \
                   gnupg lsb-release ufw rfkill openssh-server \
                   libnss3-tools power-profiles-daemon dconf-cli

# ---------- 3. Tailscale ----------------------------------------------------
echo "==> [3/11] Installing Tailscale"
if ! command -v tailscale >/dev/null 2>&1; then
  if [[ "$OFFLINE_APT" == "1" ]]; then
    apt_get install -y tailscale
  else
    curl -fsSL https://tailscale.com/install.sh | sh
  fi
fi
systemctl enable --now tailscaled

# Wait for tailscaled to come up before calling `tailscale up`
for _ in 1 2 3 4 5 6 7 8 9 10; do
  systemctl is-active --quiet tailscaled && break
  sleep 1
done

if [[ -n "$TS_AUTHKEY" ]]; then
  echo "==> Bringing Tailscale up as $HOSTNAME_NEW"
  TS_UP_ARGS=( --reset \
               --authkey="$TS_AUTHKEY" \
               --hostname="$HOSTNAME_NEW" \
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
if [[ "$OFFLINE_APT" == "1" ]]; then
  apt_get install -y anydesk
else
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://keys.anydesk.com/repos/DEB-GPG-KEY \
       -o /etc/apt/keyrings/keys.anydesk.com.asc
  chmod a+r /etc/apt/keyrings/keys.anydesk.com.asc
  echo "deb [signed-by=/etc/apt/keyrings/keys.anydesk.com.asc] http://deb.anydesk.com/ all main" \
       > /etc/apt/sources.list.d/anydesk-stable.list
  apt-get update -y
  apt-get install -y anydesk
fi

# `anydesk --set-password` needs the service running — enable it first and
# wait for it, instead of relying on the deb postinst having started it.
systemctl enable --now anydesk
for _ in $(seq 1 15); do
  systemctl is-active --quiet anydesk && break
  sleep 1
done

echo "==> Setting AnyDesk unattended password"
ANYDESK_PASS_SET=0
for _ in 1 2 3; do
  if echo -e "${ANYDESK_PASS}\n${ANYDESK_PASS}" | anydesk --set-password; then
    ANYDESK_PASS_SET=1
    break
  fi
  sleep 2
done
if [[ "$ANYDESK_PASS_SET" != "1" ]]; then
  echo "WARN: anydesk --set-password failed — set it manually:"
  echo "      echo -e 'PASS\\nPASS' | sudo anydesk --set-password"
fi

# ---------- 5. Google Chrome + telemetry suppression ------------------------
echo "==> [5/11] Installing Google Chrome"
if [[ "$OFFLINE_APT" == "1" ]]; then
  apt_get install -y google-chrome-stable
else
  TMP_DEB="$(mktemp --suffix=.deb)"
  wget -qO "$TMP_DEB" https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  apt-get install -y "$TMP_DEB"
  rm -f "$TMP_DEB"
fi

# Chrome managed policy:
#  - telemetry / metrics off (prevents the /home disk accumulation we saw on
#    kela-fob-09-operator)
#  - homepage + new-tab + startup pinned to kela.local
#  - managed bookmarks (live in a locked "Kela" folder on the bookmark bar)
mkdir -p /etc/opt/chrome/policies/managed
cat > /etc/opt/chrome/policies/managed/kela-policy.json <<'JSON'
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

  "HomepageLocation": "https://kela.local/",
  "HomepageIsNewTabPage": false,
  "NewTabPageLocation": "https://kela.local/",
  "ShowHomeButton": true,
  "RestoreOnStartup": 4,
  "RestoreOnStartupURLs": ["https://kela.local/"],

  "BookmarkBarEnabled": true,
  "ManagedBookmarks": [
    { "toplevel_name": "Kela" },
    { "name": "מערכת קלע", "url": "https://kela.local/" },
    { "name": "שינוי מיקום אתר", "url": "https://kela.local/location-updater" },
    { "name": "ממשק מצלמה", "url": "http://192.168.88.210:6010" }
  ]
}
JSON

# ---------- 6. /etc/hosts entry for kela.local ------------------------------
# Done BEFORE cert fetch so `kela.local` resolves to the hub on the LAN.
echo "==> [6/11] Adding ${KELA_LOCAL_HOST} -> ${KELA_LOCAL_IP}"
if ! grep -qE "^[^#]*\s${KELA_LOCAL_HOST}(\s|$)" /etc/hosts; then
  echo "${KELA_LOCAL_IP}	${KELA_LOCAL_HOST}" >> /etc/hosts
fi

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
KELA_LOCAL_HOST="kela.local"

HOST="${1:-${KELA_CERT_HOST:-192.168.88.10}}"
PORT="${2:-${KELA_CERT_PORT:-443}}"
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
  DATA="$(echo | timeout "$TIMEOUT" openssl s_client \
                 -connect "${HOST}:${PORT}" -servername "$HOST" </dev/null 2>/dev/null \
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
  echo "      Once the server at ${KELA_LOCAL_IP} (${KELA_LOCAL_HOST}) is up, run:"
  echo "        sudo kela-install-cert"
fi

# ---------- 8. Chrome autostart for kela user -------------------------------
echo "==> [8/11] Chrome autostart for ${KELA_USER}"
install -d -o "$KELA_USER" -g "$KELA_USER" "$KELA_HOME/.config/autostart"

cat > "$KELA_HOME/.config/autostart/chrome-kela.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Chrome - kela.local
Exec=/usr/bin/google-chrome-stable --no-first-run --no-default-browser-check --start-maximized --disable-features=TranslateUI ${KELA_LOCAL_URL}
X-GNOME-Autostart-enabled=true
NoDisplay=false
Hidden=false
Terminal=false
EOF
chown "$KELA_USER:$KELA_USER" "$KELA_HOME/.config/autostart/chrome-kela.desktop"

# ---------- 9. Operator session hardening -----------------------------------
# Auto-login + never sleep/suspend/lock + screen-never-blank + performance
# power mode + full volume + muted mic on login.
echo "==> [9/11] Operator session: auto-login, no-sleep, no-blank, performance, full-volume, mic-muted"

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

# --- 9b. systemd: mask every sleep/suspend/hibernate path ------------------
# Belt: nothing on the system can pull the box into a low-power state.
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target

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
EOF

cat > /etc/dconf/db/local.d/locks/00-kela-power <<'EOF'
/org/gnome/desktop/session/idle-delay
/org/gnome/desktop/screensaver/lock-enabled
/org/gnome/desktop/screensaver/idle-activation-enabled
/org/gnome/settings-daemon/plugins/power/sleep-inactive-ac-type
/org/gnome/settings-daemon/plugins/power/sleep-inactive-battery-type
/org/gnome/settings-daemon/plugins/power/power-button-action
/org/gnome/settings-daemon/plugins/power/idle-dim
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
echo "==> [11/11] Removing games / bloat and blocking Bluetooth"
apt-get purge -y \
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
  gnome-initial-setup \
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
apt-get purge -y bluez bluez-cups bluez-obexd 'gnome-bluetooth*' 2>/dev/null || true
apt-get autoremove -y --purge
apt-get clean

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

echo "--- remote access (a FAIL here means a stranded station) ---"
req "tailscaled service active"          systemctl is-active --quiet tailscaled
req "tailscale connected (has IPv4)"     tailscale ip -4
req "anydesk service active"             systemctl is-active --quiet anydesk
opt "anydesk unattended password set"    grep -qi pwd_hash /etc/anydesk/system.conf

echo "--- firewall ---"
req "ufw active"                         sh -c 'ufw status | grep -q "Status: active"'
req "kela egress chain loaded"           iptables -S KELA_EGRESS
req "kela-egress.service enabled"        systemctl is-enabled --quiet kela-egress.service

echo "--- kiosk ---"
req "GDM auto-login configured"          grep -q "AutomaticLogin=kela" /etc/gdm3/custom.conf
req "Chrome installed"                   test -x /usr/bin/google-chrome-stable
req "Chrome policy present"              test -f /etc/opt/chrome/policies/managed/kela-policy.json
req "Chrome autostart present"           test -f "$KELA_HOME/.config/autostart/chrome-kela.desktop"
req "session-init autostart present"     test -f "$KELA_HOME/.config/autostart/kela-session-init.desktop"
req "kela.local in /etc/hosts"           grep -qE "^[^#]*[[:space:]]kela\.local([[:space:]]|$)" /etc/hosts

echo "--- power / sleep ---"
for t in sleep.target suspend.target hibernate.target hybrid-sleep.target; do
  req "$t masked" sh -c "systemctl is-enabled $t 2>&1 | grep -q masked"
done
req "logind no-sleep drop-in present"    test -f /etc/systemd/logind.conf.d/kela-no-sleep.conf
req "dconf power db compiled"            test -f /etc/dconf/db/local

echo "--- bluetooth ---"
req "bluetooth.service masked"           sh -c "systemctl is-enabled bluetooth.service 2>&1 | grep -q masked"
req "bluetooth module blacklist"         test -f /etc/modprobe.d/blacklist-bluetooth.conf
opt "no bluetooth modules loaded"        sh -c '! lsmod | grep -qiE "^(bluetooth|btusb) "'

echo "--- time ---"
req "NTP drop-in present"                test -f /etc/systemd/timesyncd.conf.d/kela-ntp.conf
opt "timesyncd active"                   systemctl is-active --quiet systemd-timesyncd

echo "--- server cert (optional: site server may not exist yet) ---"
opt "kela server cert installed"         test -f /usr/local/share/ca-certificates/kela/kela-server.crt

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
POOL_INFO_LINE="$(tr '\n' ' ' < "${KELA_POOL_DIR}/POOL_INFO" 2>/dev/null || true)"
cat > /etc/kela/build-info <<EOF
site=${SITE_NAME}
hostname=${HOSTNAME_NEW}
setup_version=${SETUP_VERSION}
built_at=$(date -Iseconds)
tailscale_ip=${TS_IP}
anydesk_id=${ANYDESK_ID}
offline_pool=${OFFLINE_APT}
pool_info=${POOL_INFO_LINE}
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
    1. If the site server didn't exist during build, install its cert once
       it's online:  sudo kela-install-cert
    2. Reboot to confirm:
         - Bluetooth stays blocked (rfkill list / lsmod | grep -i blue)
         - WiFi available (rfkill list / nmcli dev)
         - Microphone muted (pactl get-source-mute @DEFAULT_SOURCE@ -> yes)
         - Auto-login -> Chrome -> ${KELA_LOCAL_URL} (no cert warning)
         - UFW active, LAN-only egress holds
============================================================================
EOF