#!/usr/bin/env bash
# /usr/local/sbin/kela-first-boot-run
# ----------------------------------------------------------------------------
# Runs as root via NOPASSWD sudo from kela-first-boot-ui. Sources the baked
# /etc/kela/operator-secrets.env, executes operator-setup.sh, then disables
# the first-boot wiring (autostart entry, sudoers drop-in, secrets file) and
# reboots so the next boot goes straight to Chrome at kela.local.
# ----------------------------------------------------------------------------
set -euo pipefail

SITE_NAME="${1:-}"
if [[ -z "$SITE_NAME" ]]; then
  echo "ERROR: SITE_NAME argument required" >&2
  exit 1
fi
if [[ ! "$SITE_NAME" =~ ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$ ]]; then
  echo "ERROR: SITE_NAME '$SITE_NAME' has illegal characters" >&2
  exit 1
fi

LOG="/var/log/kela-first-boot.log"
mkdir -p "$(dirname "$LOG")"
exec > >(tee -a "$LOG") 2>&1

echo "==================================================================="
echo " Kela first-boot at $(date -Iseconds)"
echo "   SITE_NAME=$SITE_NAME"
echo "==================================================================="

SECRETS="/etc/kela/operator-secrets.env"
if [[ ! -r "$SECRETS" ]]; then
  echo "ERROR: $SECRETS is missing or unreadable" >&2
  exit 1
fi

# Export baked secrets so operator-setup.sh sees TS_AUTHKEY/TS_TAGS/etc.
set -a
# shellcheck disable=SC1090
source "$SECRETS"
set +a
export SITE_NAME

# Record the site name so re-runs / audits don't depend on the operator's memory.
mkdir -p /etc/kela
printf '%s\n' "$SITE_NAME" > /etc/kela/site
chmod 644 /etc/kela/site

if ! bash /usr/local/sbin/operator-setup.sh; then
  echo
  echo "operator-setup.sh FAILED — see $LOG"
  echo "Once the issue is resolved, re-run:"
  echo "  sudo /usr/local/sbin/kela-first-boot-run $SITE_NAME"
  exit 1
fi

# ---- Gate on remote access BEFORE destroying the retry path ----------------
# operator-setup.sh treats a `tailscale up` failure as a warning (correct for
# manual runs), but an unattended station with no Tailscale is unreachable —
# never shred the auth key or remove the autostart/sudoers wiring in that case.
if ! tailscale ip -4 >/dev/null 2>&1; then
  echo
  echo "ERROR: Tailscale is NOT connected — refusing to clean up."
  echo "The auth key, autostart prompt and sudoers entry are left in place."
  echo "Check the key (expired? wrong tags?) and network, then re-run:"
  echo "  sudo /usr/local/sbin/kela-first-boot-run $SITE_NAME"
  exit 1
fi

echo
echo "==> Running kela-verify"
if ! /usr/local/sbin/kela-verify; then
  echo
  echo "ERROR: kela-verify reported failures — refusing to clean up."
  echo "Fix the failing checks (see above + $LOG), then re-run:"
  echo "  sudo /usr/local/sbin/kela-first-boot-run $SITE_NAME"
  exit 1
fi

# ---- Cleanup so we never re-prompt and never leak the auth key -------------
rm -f /home/kela/.config/autostart/kela-first-boot.desktop
rm -f /etc/sudoers.d/kela-first-boot
# Reclaim the offline package pool (~2 GB) now that everything is installed.
rm -rf /opt/kela-pool /etc/apt/kela-offline.list
# Shred the auth key from disk — operator-setup has already used it for `tailscale up`.
# /etc/kela itself stays: build-info + site live there.
if command -v shred >/dev/null 2>&1; then
  shred -u "$SECRETS" 2>/dev/null || rm -f "$SECRETS"
else
  rm -f "$SECRETS"
fi

echo
echo "==================================================================="
echo "  Build complete. Rebooting in 10s..."
echo "==================================================================="
sleep 10
systemctl reboot
