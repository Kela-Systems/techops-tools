#!/usr/bin/env bash
# ============================================================================
#  update-usb-macos.sh — re-apply the current boot machinery to an EXISTING
#  Kela FOB stick, in place. Does NOT repartition and does NOT touch the
#  FOBDATA data partition — safe when the stick holds the only copy of the
#  bundle.
#
#  Refreshed from templates/ on every run:
#    - the managed GRUB block (hostname prompt, re-imaging guard); the ISO's
#      own entries are restored to manual installs as a fallback
#    - the ESP bootloader mirror and its hand-off config
#    - CIDATA/user-data (the autoinstall seed and everything the installed
#      system runs on first boot)
#    - CIDATA's sideloaded .debs, mirrored from extra-debs/. Debs from an
#      earlier build are removed, not kept: a stick refreshed to correct a bad
#      sideload set would otherwise still be carrying the bad debs alongside
#      the good ones, and first boot installs every .deb it finds.
#
#  Usage: plug the stick in (EFIBOOT + CIDATA mount automatically), then:
#    sudo ./update-usb-macos.sh
#  It prompts for the kela user's password (Enter alone keeps the built-in
#  default). Non-interactively:
#    sudo KELA_PASSWORD_HASH="$(./lib/mkpasswd.sh)" ./update-usb-macos.sh
# ============================================================================

set -euo pipefail

# shellcheck source=lib/common.sh
source "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

require_root

# A stick that arrived carrying the bundle has none of these partitions, and it
# is a common enough mix-up to be worth naming explicitly rather than reporting
# an absent EFIBOOT.
if [ ! -f /Volumes/EFIBOOT/boot/grub/grub.cfg ] || [ ! -d /Volumes/CIDATA ]; then
  received=""
  for vol in /Volumes/*/; do
    [ -d "${vol}02-kela" ] || continue
    received="${vol%/}"
    break
  done
  if [ -n "$received" ]; then
    die "this is a received Kela drive, not a stick built by make-usb-macos.sh:
  found the bundle at $received, but there are no EFIBOOT/CIDATA partitions.
  update-usb-macos.sh only refreshes boot machinery that already exists; it
  cannot create it. To make this drive bootable, stage the bundle and rebuild:
      ./stage-bundle.sh \"$received\" ~/kela-staging
      sudo ./make-usb-macos.sh <diskN> ~/kela-staging/$(basename "$received")
  Or, if you have a second blank stick, skip staging and build onto that with
  $received as the source."
  fi
  die "EFIBOOT/CIDATA not mounted — is a stick built by make-usb-macos.sh plugged in?"
fi

DISK=$(diskutil info /Volumes/CIDATA | awk -F': *' '/Part of Whole/{print $2}')
[ -n "$DISK" ] || die "could not work out which disk CIDATA belongs to"
echo "== updating $DISK =="

resolve_password

echo "== grub: managed block (replaces any earlier copy) =="
write_grub_block /Volumes/EFIBOOT/boot/grub/grub.cfg

echo "== esp: bootloader mirror for strict firmware =="
write_esp_shim "$DISK" /Volumes/EFIBOOT

echo "== seed: rewriting user-data and syncing extra-debs/ =="
# The stick carries the ISO extracted onto EFIBOOT, so the sideloaded debs can
# be verified against the exact install the box will get, with no ISO file and
# no network.
write_seed /Volumes/CIDATA /Volumes/EFIBOOT/casper

echo "== ejecting =="
sync
diskutil eject "$DISK"
echo "Done. Stick updated in place; the data partition was not touched."
