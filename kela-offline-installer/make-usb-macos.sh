#!/usr/bin/env bash
# ============================================================================
#  make-usb-macos.sh — build the Kela FOB bootable install USB on macOS
#                      (Apple Silicon or Intel).
#
#  Result: a stick that boots the Ubuntu 24.04 installer unattended on the
#  target box (amd64 PC — it cannot boot the Mac itself), installs onto the
#  SMALLEST internal disk, then runs Kela activation on first boot. The FOB
#  drive content is copied byte-identical to its own partition.
#
#  Usage:
#    sudo ./make-usb-macos.sh disk4 "/Users/you/.../FOB security drive"
#  Find the disk id with:  diskutil list external
#
#  The script prompts for the kela user's password (Enter alone keeps the
#  built-in default, plaintext 'kela'). To supply it non-interactively:
#    sudo KELA_PASSWORD_HASH="$(./lib/mkpasswd.sh)" ./make-usb-macos.sh disk4 ...
#  Note that `openssl passwd -6` does NOT work on macOS — /usr/bin/openssl is
#  LibreSSL and has no -6. lib/mkpasswd.sh finds one that does.
#
#  Requirements: xorriso (brew install xorriso); everything else is stock
#  macOS (diskutil, cp, awk, python3).
#
#  ./extra-debs/ rides the seed partition and installs before Kela activation.
#  It permanently carries usbguard (the USB device policy enabled after
#  activation) and optionally covers dependencies missing from both the base
#  install and the bundle. See extra-debs/README.md.
#
#  At target power-on the default GRUB entry prompts once for a hostname
#  (empty = kela-fob), then everything runs hands-off:
#    wipe smallest disk -> install Ubuntu -> reboot (leave the stick in) ->
#    first boot: install debs, activate Kela, converge.
#  Outcome is shown at the console login prompt; log on the target is
#  /var/log/kela-firstboot.log, copied to 99-install-logs/ on the stick.
#
#  Layout (GPT):
#    s1  (ESP, auto)  bootloader mirror + hand-off config for strict firmware
#    s2  4G    FAT32  EFIBOOT — extracted Ubuntu ISO (its own bootloader)
#    s3  64M   FAT    CIDATA  — autoinstall seed + extra debs
#    s4  rest  exFAT  FOBDATA — exact copy of the FOB drive folder
#
#  The seed, the GRUB block and the first-boot scripts all live in templates/;
#  this script only places them. update-usb-macos.sh applies the same set to a
#  stick that already exists.
# ============================================================================

set -euo pipefail

DISK="${1:?usage: sudo $0 diskN \"/path/to/FOB security drive\"}"
SRC="${2:?usage: sudo $0 diskN \"/path/to/FOB security drive\"}"
DISK="${DISK#/dev/}"

# shellcheck source=lib/common.sh
source "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

ISO="$SRC/01-ubuntu-image/ubuntu-24.04.4-live-server-amd64.iso"
BOOT_BYTES=4000000000          # the 4g EFIBOOT partition, as diskutil sizes it
SEED_BYTES=64000000

require_root
[ -r "$ISO" ] || die "ISO not found at $ISO"
[ -d "$SRC/02-kela" ] || die "$SRC doesn't look like the FOB drive folder"
command -v xorriso >/dev/null 2>&1 || die "xorriso missing: brew install xorriso"
diskutil info "$DISK" >/dev/null || die "no such disk: $DISK"

echo "== preflight =="
# Read the disk's attributes from the plist rather than scraping the human
# output, where several lines carry a "(nnn Bytes)" group. plistlib.loads, not
# load: load() seeks, and a pipe is not seekable.
read -r WHERE DISK_BYTES <<<"$(diskutil info -plist "$DISK" | python3 -c '
import plistlib, sys
d = plistlib.loads(sys.stdin.buffer.read())
print("internal" if d.get("Internal") else "external", d.get("TotalSize") or d.get("Size"))
')"
# An unreadable plist must not masquerade as an internal-disk refusal.
case "$WHERE" in
  external) ;;
  internal) die "REFUSING: $DISK is an internal disk" ;;
  *)        die "could not read the attributes of $DISK from diskutil" ;;
esac
[ -n "$DISK_BYTES" ] && [ "$DISK_BYTES" != None ] || die "could not read the size of $DISK"

# The common case is now a single stick that arrives carrying the bundle. Reading
# the source off the very disk we are about to partition would destroy it.
SRC_DISK=$(whole_disk_of "$SRC")
if [ -n "$SRC_DISK" ] && [ "$SRC_DISK" = "$DISK" ]; then
  die "REFUSING: the source folder lives on $SRC_DISK, the disk about to be erased.
  Building onto the same stick that carries the bundle would destroy the bundle.
  Either target a different stick, or stage the bundle to local storage first:
      ./stage-bundle.sh \"$SRC\" ~/kela-staging
  then rerun with the staged path as the source."
fi
ISO_BYTES=$(stat -f%z "$ISO")
echo "  measuring the bundle (du over the whole folder — this takes a minute)"
SRC_BYTES=$(( $(du -sk "$SRC" | cut -f1) * 1024 ))

# The extracted ISO is a little larger than the image itself once FAT cluster
# slack is counted; 10% covers it comfortably.
[ "$(( ISO_BYTES * 11 / 10 ))" -lt "$BOOT_BYTES" ] \
  || die "ISO is $(human "$ISO_BYTES") and will not fit the $(human "$BOOT_BYTES") EFIBOOT partition — raise BOOT_BYTES and the matching 4g in the partitioning step"
NEED=$(( BOOT_BYTES + SEED_BYTES + SRC_BYTES + 1000000000 ))
[ "$DISK_BYTES" -ge "$NEED" ] \
  || die "stick is $(human "$DISK_BYTES"); need at least $(human "$NEED") for a $(human "$SRC_BYTES") bundle"
echo "  ISO $(human "$ISO_BYTES"), bundle $(human "$SRC_BYTES"), stick $(human "$DISK_BYTES") — ok"
check_bundle "$SRC"
check_extra_debs "$ISO" "$KITDIR/extra-debs" "$SRC/02-kela"

# Before the disk is touched, so a bad KELA_PASSWORD_HASH costs nothing.
resolve_password

echo "About to ERASE $DISK:"
diskutil info "$DISK" | grep -E 'Device Node|Media Name|Disk Size'
printf 'Type the disk id (%s) to confirm: ' "$DISK"; read -r CONFIRM
[ "$CONFIRM" = "$DISK" ] || die "aborted"

export COPYFILE_DISABLE=1   # no AppleDouble ._ junk

echo "== partitioning =="
diskutil partitionDisk "$DISK" GPT \
  "MS-DOS FAT32" EFIBOOT 4g \
  "MS-DOS"       CIDATA  64m \
  ExFAT          FOBDATA R

echo "== boot partition: extracting ISO =="
xorriso -abort_on NEVER -osirrox on -indev "$ISO" -extract / /Volumes/EFIBOOT
chmod -R +w /Volumes/EFIBOOT 2>/dev/null || true
[ -f /Volumes/EFIBOOT/EFI/boot/bootx64.efi ] || [ -f /Volumes/EFIBOOT/EFI/BOOT/BOOTX64.EFI ] \
  || die "EFI bootloader missing after extraction"

echo "== grub: unattended entry with hostname prompt + re-imaging guard =="
write_grub_block /Volumes/EFIBOOT/boot/grub/grub.cfg

echo "== esp: bootloader mirror for strict firmware =="
write_esp_shim "$DISK" /Volumes/EFIBOOT

echo "== seed partition: autoinstall =="
write_seed /Volumes/CIDATA "$ISO" "$SRC/02-kela"

echo "== data partition: copying FOB drive content (unmodified) =="
# cp reports failures on macOS junk files (.DS_Store, ._* AppleDouble sidecars)
# and on anything exFAT will not take; the manifest verification below is the
# real gate, so note the status and carry on.
if ! cp -RX "$SRC/." /Volumes/FOBDATA/; then
  echo "  note: cp reported errors (usually macOS sidecar files) — verifying below"
fi
find /Volumes/FOBDATA \( -name '.DS_Store' -o -name '._*' \) -delete 2>/dev/null || true

echo "== repairing scanner-mangled names on the stick copy =="
cd /Volumes/FOBDATA/02-kela
python3 "$LIBDIR/repair-names.py"

echo "== verifying copy against the signed manifest =="
python3 "$LIBDIR/verify-manifest.py" || die "the copy on the stick does not match the manifest — do not ship this stick"
cd /

echo "== ejecting =="
sync
diskutil eject "$DISK"

cat <<'DONE'

Done. Boot a FOB box from this stick over UEFI, ideally through the firmware's
one-time boot menu rather than by reordering the boot devices.

The default entry asks for a hostname, then WIPES the smallest internal disk and
installs Ubuntu. The box POWERS ITSELF OFF on purpose and an RTC alarm cold-starts
it a minute or two later. Leave the stick in. Pressing the power button
once the screen has gone dark is always safe and skips the wait — either way the
box boots the system it just installed, not the installer, and runs Kela
activation.

A box that already has an Ubuntu ESP boots that instead of being re-imaged — to
re-image deliberately, pick the install entry from the GRUB menu.

The console login prompt reports progress and the final outcome. Full log:
/var/log/kela-firstboot.log on the box, and 99-install-logs/ on the stick.
DONE
