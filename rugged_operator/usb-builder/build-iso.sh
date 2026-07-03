#!/usr/bin/env bash
# ============================================================================
#  build-iso.sh — bake a Kela operator install USB from an Ubuntu 24.04
#                 Desktop ISO.
#
#  Result: a hybrid bootable ISO that runs Subiquity unattended (autoinstall),
#  installs the kela user, drops operator-setup.sh + baked secrets into the
#  target system, and configures a first-boot prompt for the site name. The
#  ONLY operator interaction is typing the site name once after install.
#
#  Usage:
#    ./build-iso.sh \
#        --input   ~/Downloads/ubuntu-24.04.4-live-server-amd64.iso \
#        --output  ./kela-operator-24.04.iso \
#        --secrets ./secrets.env
#
#  IMPORTANT: must be the **Live Server** ISO, not the Desktop ISO.
#  Ubuntu 24.04 Desktop's new flutter installer does not reliably honor
#  autoinstall — even with the cmdline arg + cloud-init NoCloud datasource
#  loaded successfully, the installer launches its GUI wizard. The Server
#  ISO uses Subiquity, which honors autoinstall fully. The autoinstall
#  config installs `ubuntu-desktop-minimal` + `gdm3` so the end result is
#  still a desktop kiosk.
#
#  Optional flags:
#    --password '<plaintext>'   Override the kela password (default Kelasys123!)
#    --volid    'Kela Operator' ISO volume label (default same)
#    --offline-pool <dir>       Offline .deb pool from collect-offline-packages.sh.
#                               Auto-detected at ./offline-pool if present.
#                               When embedded, the install needs NO network:
#                               the desktop + apps install from /cdrom/extras.
#
#  Build host requirements:
#    All:    xorriso  (the only hard dep)
#            Linux:  sudo apt install xorriso
#            macOS:  brew install xorriso
#
#  Password hashing uses `openssl passwd -6` when available; if the local
#  openssl/LibreSSL is too old (common on macOS), the script falls back to
#  python3 + passlib:
#            python3 -m pip install --user passlib
#
#  Strategy: clone the source ISO via xorriso's `-boot_image any replay`,
#  which preserves the original hybrid BIOS+UEFI boot setup exactly, then
#  overlay /autoinstall/, /operator/, and a patched /boot/grub/grub.cfg.
#  This is layout-agnostic — works for any Ubuntu 24.04.x point release.
#
#  Write the resulting ISO to a USB stick with (replace /dev/sdX):
#    sudo dd if=./kela-operator-24.04.iso of=/dev/sdX bs=4M conv=fsync status=progress
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OVERLAY_DIR="$SCRIPT_DIR/overlay"

INPUT_ISO=""
OUTPUT_ISO=""
SECRETS_FILE=""
KELA_PASSWORD="Kelasys123!"
VOLID="Kela Operator"
OFFLINE_POOL=""

usage() {
  sed -n '/^# =\{5,\}/,/^# =\{5,\}/p' "$0" | sed 's/^# \{0,2\}//'
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --input)    INPUT_ISO="$2";     shift 2 ;;
    --output)   OUTPUT_ISO="$2";    shift 2 ;;
    --secrets)  SECRETS_FILE="$2";  shift 2 ;;
    --password) KELA_PASSWORD="$2"; shift 2 ;;
    --volid)    VOLID="$2";         shift 2 ;;
    --offline-pool) OFFLINE_POOL="$2"; shift 2 ;;
    -h|--help)  usage ;;
    *)          echo "unknown arg: $1" >&2; usage ;;
  esac
done

[[ -z "$INPUT_ISO"    ]] && { echo "ERROR: --input is required"    >&2; usage; }
[[ -z "$OUTPUT_ISO"   ]] && { echo "ERROR: --output is required"   >&2; usage; }
[[ -z "$SECRETS_FILE" ]] && { echo "ERROR: --secrets is required"  >&2; usage; }

for f in "$INPUT_ISO" "$SECRETS_FILE" "$REPO_DIR/setup.sh"; do
  [[ -r "$f" ]] || { echo "ERROR: cannot read $f" >&2; exit 1; }
done

command -v xorriso >/dev/null 2>&1 || {
  echo "ERROR: 'xorriso' not found." >&2
  echo "       Linux: sudo apt install xorriso" >&2
  echo "       macOS: brew install xorriso" >&2
  exit 1
}

# Normalize paths to absolute so xorriso isn't surprised by cwd shifts.
abs_path() {
  case "$1" in
    /*) printf '%s\n' "$1" ;;
    *)  printf '%s/%s\n' "$PWD" "$1" ;;
  esac
}
INPUT_ISO="$(abs_path "$INPUT_ISO")"
OUTPUT_ISO="$(abs_path "$OUTPUT_ISO")"
SECRETS_FILE="$(abs_path "$SECRETS_FILE")"

# Offline pool: explicit flag wins; otherwise auto-detect ./offline-pool.
if [[ -z "$OFFLINE_POOL" && -d "$SCRIPT_DIR/offline-pool" ]]; then
  OFFLINE_POOL="$SCRIPT_DIR/offline-pool"
fi
if [[ -n "$OFFLINE_POOL" ]]; then
  OFFLINE_POOL="$(abs_path "$OFFLINE_POOL")"
  if [[ ! -f "$OFFLINE_POOL/Packages" ]]; then
    echo "ERROR: --offline-pool '$OFFLINE_POOL' has no Packages index." >&2
    echo "       Run ./collect-offline-packages.sh first (or omit for an online install ISO)." >&2
    exit 1
  fi
fi

# ---- 1. validate secrets ---------------------------------------------------
echo "==> Validating secrets file"
(
  set -a
  # shellcheck disable=SC1090
  source "$SECRETS_FILE"
  set +a
  [[ -n "${TS_AUTHKEY:-}" ]] || { echo "ERROR: TS_AUTHKEY not set in $SECRETS_FILE" >&2; exit 1; }
  [[ "$TS_AUTHKEY" =~ ^tskey- ]] || { echo "ERROR: TS_AUTHKEY does not look like a Tailscale auth key" >&2; exit 1; }
  [[ -n "${TS_TAGS:-}"    ]] || echo "WARN: TS_TAGS not set in $SECRETS_FILE (Tailscale will come up untagged)"

  # Tailscale auth keys expire (90 days max). A stick built from an expired
  # key installs fine and then fails `tailscale up` in the field — guard at
  # build time if the admin recorded the expiry date in secrets.env.
  if [[ -n "${TS_KEY_EXPIRY:-}" ]]; then
    TODAY="$(date +%F)"
    SOON="$(date -v+14d +%F 2>/dev/null || date -d '+14 days' +%F)"
    if [[ "$TS_KEY_EXPIRY" < "$TODAY" ]]; then
      echo "ERROR: TS_AUTHKEY expired on $TS_KEY_EXPIRY (per TS_KEY_EXPIRY in $SECRETS_FILE)." >&2
      echo "       Generate a new key + update TS_KEY_EXPIRY, then rebuild." >&2
      exit 1
    elif [[ "$TS_KEY_EXPIRY" < "$SOON" ]]; then
      echo "WARN: TS_AUTHKEY expires $TS_KEY_EXPIRY (<14 days) — sticks built now have a short shelf life."
    else
      echo "    Auth key valid until $TS_KEY_EXPIRY"
    fi
  else
    echo "WARN: TS_KEY_EXPIRY not set in $SECRETS_FILE — Tailscale keys expire (90 days max)."
    echo "      Set TS_KEY_EXPIRY=YYYY-MM-DD to get build-time expiry guarding."
  fi
)

# ---- 2. work area + staging tree ------------------------------------------
WORK_DIR="$(mktemp -d -t kela-iso-XXXXXX)"
trap 'rm -rf "$WORK_DIR"' EXIT
STAGING="$WORK_DIR/staging"
mkdir -p "$STAGING/autoinstall" "$STAGING/operator" "$STAGING/boot/grub"

# ---- 3. hash the kela password --------------------------------------------
# Try `openssl passwd -6` first (works on Linux + modern openssl/libressl).
# Fall back to python3 + passlib (works on macOS, where the bundled
# LibreSSL is often too old to know about `-6`).
echo "==> Generating SHA-512 hash for 'kela' password"

PASSWORD_HASH=""
SHA512_RE='^\$6\$'   # in a variable to keep zsh/bash quoting quirks at bay

if h="$(openssl passwd -6 "$KELA_PASSWORD" 2>/dev/null)" && [[ "$h" =~ $SHA512_RE ]]; then
  PASSWORD_HASH="$h"
fi

if [[ -z "$PASSWORD_HASH" ]] && command -v python3 >/dev/null 2>&1; then
  if h="$(python3 - "$KELA_PASSWORD" <<'PY' 2>/dev/null
import sys
try:
    from passlib.hash import sha512_crypt
except ImportError:
    sys.exit(2)
print(sha512_crypt.using(rounds=5000).hash(sys.argv[1]))
PY
)"; then
    [[ "$h" == '$6$'* ]] && PASSWORD_HASH="$h"
  fi
fi

if [[ -z "$PASSWORD_HASH" ]]; then
  cat >&2 <<'ERR'
ERROR: could not generate a SHA-512 crypt hash for the kela password.

This build host lacks both:
  - an openssl new enough to support `openssl passwd -6`, and
  - python3 with the `passlib` library installed.

Pick whichever is easier:

  macOS (easiest):
      python3 -m pip install --user passlib
      ./build-iso.sh ...                  # re-run

  macOS (alternative):
      brew install openssl@3
      export PATH="$(brew --prefix openssl@3)/bin:$PATH"
      ./build-iso.sh ...                  # re-run

  Linux:
      sudo apt install python3-passlib    # or openssl >= 1.1.1
      ./build-iso.sh ...                  # re-run
ERR
  exit 1
fi

# ---- 4. stage autoinstall (NoCloud user-data + meta-data) -----------------
echo "==> Staging autoinstall config"
awk -v hash="$PASSWORD_HASH" '{ gsub(/__KELA_PASSWORD_HASH__/, hash); print }' \
    "$OVERLAY_DIR/autoinstall/user-data" > "$STAGING/autoinstall/user-data"
chmod 0644 "$STAGING/autoinstall/user-data"
install -m 0644 "$OVERLAY_DIR/autoinstall/meta-data" "$STAGING/autoinstall/meta-data"

# ---- 5. stage operator payload --------------------------------------------
echo "==> Staging operator payload"
install -m 0755 "$REPO_DIR/setup.sh"                                 "$STAGING/operator/operator-setup.sh"
install -m 0755 "$OVERLAY_DIR/operator/kela-first-boot-ui.sh"        "$STAGING/operator/kela-first-boot-ui.sh"
install -m 0755 "$OVERLAY_DIR/operator/kela-first-boot-run.sh"       "$STAGING/operator/kela-first-boot-run.sh"
install -m 0644 "$OVERLAY_DIR/operator/kela-first-boot.desktop"      "$STAGING/operator/kela-first-boot.desktop"
install -m 0440 "$OVERLAY_DIR/operator/kela-first-boot.sudoers"      "$STAGING/operator/kela-first-boot.sudoers"
install -m 0600 "$SECRETS_FILE"                                      "$STAGING/operator/secrets.env"

# ---- 6. extract + patch grub.cfg(s) from the source ISO -------------------
# Done with xorriso (-osirrox on -extract), so we don't need 7z and we don't
# care whether the ISO is laid out with i386-pc/boot_hybrid.img or any other
# specific boot-image variant — we never touch the boot setup directly.
echo "==> Extracting GRUB config from source ISO"
xorriso -osirrox on -indev "$INPUT_ISO" \
    -extract /boot/grub/grub.cfg "$WORK_DIR/grub.cfg" 2>/dev/null \
  || { echo "ERROR: source ISO has no /boot/grub/grub.cfg — is this an Ubuntu Desktop ISO?" >&2; exit 1; }

# loopback.cfg is optional — newer Ubuntu ISOs may not include it
xorriso -osirrox on -indev "$INPUT_ISO" \
    -extract /boot/grub/loopback.cfg "$WORK_DIR/loopback.cfg" 2>/dev/null \
  || true

KARGS='autoinstall ds=nocloud\;s=/cdrom/autoinstall/'

patch_grub() {
  local original="$1"
  local target="$2"
  [[ -f "$original" ]] || return 0
  {
    cat <<GRUB
set timeout=5
set default=0

menuentry "Kela Operator — Auto-install (WILL ERASE DISK)" {
    echo ""
    echo "Type the site name now (lowercase letters/digits/hyphens, NO spaces,"
    echo "e.g. fob-12) and the whole install + first-boot setup runs hands-off"
    echo "to completion. Or just press Enter to be prompted after the install."
    echo ""
    echo -n "Site name: "
    read kela_site
    set gfxpayload=keep
    linux   /casper/vmlinuz ${KARGS} kela.site=\${kela_site} quiet ---
    initrd  /casper/initrd
}

menuentry "Manual install (rescue / fallback)" {
    set gfxpayload=keep
    linux   /casper/vmlinuz maybe-ubiquity quiet splash ---
    initrd  /casper/initrd
}

GRUB
    # Strip the original's own `set timeout=` / `set default=` lines: this file
    # is concatenated AFTER ours, so they execute later and would win (the
    # stock Ubuntu cfg sets timeout=30, which silently overrode our 5).
    sed -E 's/^[[:space:]]*set[[:space:]]+(timeout|default)=.*$//' "$original"
  } > "$target"
  chmod 0644 "$target"
}

echo "==> Patching GRUB to default into the autoinstall entry"
patch_grub "$WORK_DIR/grub.cfg"     "$STAGING/boot/grub/grub.cfg"
patch_grub "$WORK_DIR/loopback.cfg" "$STAGING/boot/grub/loopback.cfg"

# ---- 7. clone source ISO + overlay our additions --------------------------
# `-boot_image any replay` tells xorriso to copy the source ISO's boot setup
# verbatim (MBR template, El Torito catalog, EFI partition image, GPT layout —
# everything). This works for any Ubuntu Desktop ISO layout without us having
# to know the path of any boot file.
echo "==> Building $(basename "$OUTPUT_ISO")"
rm -f "$OUTPUT_ISO"

XORRISO_CMD=(
  xorriso
  -indev  "$INPUT_ISO"
  -outdev "$OUTPUT_ISO"
  -boot_image any replay
  -volid "$VOLID"
  -overwrite on
  -map "$STAGING/autoinstall"           /autoinstall
  -map "$STAGING/operator"              /operator
  -map "$STAGING/boot/grub/grub.cfg"    /boot/grub/grub.cfg
)

if [[ -f "$STAGING/boot/grub/loopback.cfg" ]]; then
  XORRISO_CMD+=( -map "$STAGING/boot/grub/loopback.cfg" /boot/grub/loopback.cfg )
fi

if [[ -n "$OFFLINE_POOL" ]]; then
  POOL_MB="$(( $(du -sk "$OFFLINE_POOL" | cut -f1) / 1024 ))"
  echo "==> Embedding offline package pool (${POOL_MB} MB) → /extras (fully offline install)"
  XORRISO_CMD+=( -map "$OFFLINE_POOL" /extras )

  # Warn on a stale pool: package (incl. security) versions drift and skew vs the
  # ISO point release. POOL_INFO carries an ISO8601-UTC pool_built_at stamp.
  POOL_BUILT="$(sed -n 's/^pool_built_at=//p' "$OFFLINE_POOL/POOL_INFO" 2>/dev/null | head -1)"
  if [[ -n "$POOL_BUILT" ]]; then
    # Portable across GNU date (Linux) and BSD date (macOS build host).
    POOL_EPOCH="$(date -u -d "$POOL_BUILT" +%s 2>/dev/null \
      || date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$POOL_BUILT" +%s 2>/dev/null || true)"
    if [[ -n "$POOL_EPOCH" ]]; then
      POOL_AGE_DAYS=$(( ( $(date -u +%s) - POOL_EPOCH ) / 86400 ))
      if (( POOL_AGE_DAYS > 60 )); then
        echo "    WARN: offline pool is ${POOL_AGE_DAYS} days old (built ${POOL_BUILT})."
        echo "          Package/security versions may be stale and skewed vs the ISO."
        echo "          Consider re-running collect-offline-packages.sh before shipping."
      fi
    fi
  fi
else
  echo "==> No offline pool — building an ONLINE-install ISO (needs network during install)"
fi

XORRISO_CMD+=( -commit )

"${XORRISO_CMD[@]}"

OUT_SIZE_MB="$(( $(stat -c %s "$OUTPUT_ISO" 2>/dev/null || stat -f %z "$OUTPUT_ISO") / 1024 / 1024 ))"

cat <<EOF

============================================================================
  Built: $OUTPUT_ISO  (${OUT_SIZE_MB} MB)

  Write to USB:
    Linux:
      sudo dd if=$OUTPUT_ISO of=/dev/sdX bs=4M conv=fsync status=progress
      sync
    macOS:
      diskutil list                                       # find your USB
      diskutil unmountDisk /dev/diskN
      sudo dd if=$OUTPUT_ISO of=/dev/rdiskN bs=4m status=progress
      sync
      diskutil eject /dev/diskN

  Then boot the CF-33 from USB (hold F2 at the Panasonic logo -> Setup ->
  boot the USB; keyboard docked). Installation is
  unattended; on first boot the operator will be prompted for the site
  name once, and the build finishes automatically.
============================================================================
EOF
