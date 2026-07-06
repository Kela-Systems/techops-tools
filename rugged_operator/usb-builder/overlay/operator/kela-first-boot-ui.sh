#!/usr/bin/env bash
# /usr/local/bin/kela-first-boot-ui
# ----------------------------------------------------------------------------
# Runs as user `kela` from a GNOME autostart entry on the very first graphical
# login. Prompts the operator for the site identifier, then hands off to the
# root runner (which has a one-shot NOPASSWD sudoers entry) to execute
# operator-setup.sh with the baked-in TS_AUTHKEY/TS_TAGS.
#
# This script intentionally does NO privileged work itself.
# ----------------------------------------------------------------------------
set -euo pipefail

# Re-launch in a visible terminal if we got invoked without one (autostart
# `.desktop` with Terminal=false drops us into the GNOME session, not a tty).
if [[ ! -t 0 ]] || [[ ! -t 1 ]]; then
  if command -v gnome-terminal >/dev/null 2>&1; then
    exec gnome-terminal --maximize --title="Kela first-boot setup" -- bash "$0"
  elif command -v xterm >/dev/null 2>&1; then
    exec xterm -fullscreen -title "Kela first-boot setup" -e bash "$0"
  else
    # Last resort: write a banner so the operator can find us via tty
    echo "kela-first-boot-ui: no terminal emulator found" >&2
    exit 1
  fi
fi

# The terminal we get exec'd into (xterm/gnome-terminal on first boot) can come
# up with a line-discipline `erase` char that doesn't match the Backspace key
# the keyboard sends (xterm often sends ^H while the tty expects ^?), which
# makes Backspace a no-op at the plain `read` prompts below. Reset to a sane
# state and force erase to DEL. Combined with `read -e` (readline, which binds
# both ^? and ^H to backward-delete-char), Backspace works either way.
if [[ -t 0 ]]; then
  stty sane 2>/dev/null || true
  stty erase '^?' 2>/dev/null || true
fi

clear
cat <<'BANNER'
============================================================================
                  Kela Operator Machine — First-Boot Setup
============================================================================
BANNER

# Zero-touch path: if the operator typed the site name at the GRUB menu, the
# installer recorded it in /etc/kela/site — use it and ask nothing.
SITE_NAME=""
if [[ -r /etc/kela/site ]]; then
  PRESET="$(head -n1 /etc/kela/site | tr -d '[:space:]')"
  if [[ "$PRESET" =~ ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$ && "$PRESET" != *-operator ]]; then
    SITE_NAME="$PRESET"
    echo
    echo "Site name was set at install time: ${SITE_NAME}"
    echo "Continuing automatically — no input needed."
  else
    echo
    echo "NOTE: ignoring invalid pre-set site name '${PRESET}' — please re-enter."
  fi
fi

if [[ -z "$SITE_NAME" ]]; then
  cat <<'BANNER'

Ubuntu has been installed. One question to finish the build:
the site identifier.

  - Becomes the hostname:   <site>-operator
  - Becomes a Tailscale tag suffix where applicable
  - Examples: fob-12   warehouse-3   yard-east

(Rules: start with letter or digit, only lowercase letters, digits, hyphens,
 max 32 chars. No spaces.)

BANNER

  while true; do
    read -erp "Site name: " SITE_NAME
    if [[ ! "$SITE_NAME" =~ ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$ ]]; then
      echo "  ! invalid — try again."
      continue
    fi
    if [[ "$SITE_NAME" == *-operator ]]; then
      echo "  ! don't include '-operator' — it's appended automatically (hostname becomes <site>-operator)."
      continue
    fi
    # The name is permanent (hostname, Tailscale name) — make typos cheap to
    # catch now, not after deployment.
    read -erp "  Build as '${SITE_NAME}-operator' — correct? [y/N] " CONFIRM
    [[ "$CONFIRM" =~ ^[Yy]$ ]] && break
    echo
  done
fi

echo
echo "==> Building as '${SITE_NAME}-operator'..."
echo "==> Handing off to root runner. This will take 10-20 minutes."
echo "==> The machine will reboot automatically when done."
echo

# NOPASSWD sudo is granted by /etc/sudoers.d/kela-first-boot (drop-in is
# self-removed by the runner on success).
#
# On success the runner reboots, so control never returns here. On failure it
# prints retry guidance and exits non-zero — but this UI runs in an exec'd
# gnome-terminal/xterm that closes the instant the script exits, so hold the
# window open until the operator has read the message (the retry wiring and the
# log survive regardless; the autostart prompt re-appears on the next boot).
if ! sudo /usr/local/sbin/kela-first-boot-run "$SITE_NAME"; then
  echo
  echo "============================================================================"
  echo "  SETUP FAILED — this machine is NOT finished."
  echo "  Details and the exact retry command are in:"
  echo "      /var/log/kela-first-boot.log"
  echo "  The setup will retry automatically on the next reboot."
  echo "============================================================================"
  echo
  read -rp "Press Enter to close this window..." _ || true
  exit 1
fi
