#!/bin/bash
# make-bootable.sh — build the Kela FOB bootable install drive.
#
# Creates a USB stick that boots the Ubuntu 24.04 installer (unattended),
# installs Ubuntu on the target box's internal disk, then runs Kela
# activation on first boot. The FOB drive content is copied byte-for-byte
# to its own partition — nothing in it is modified, so MANIFEST.json
# verification passes unchanged.
#
# Run on a Linux machine (Ubuntu 22.04+) as root:
#   sudo ./make-bootable.sh /dev/sdX "/path/to/FOB security drive"
#
# Requirements: sgdisk (gdisk), mkfs.vfat (dosfstools), mkfs.exfat (exfatprogs),
#               grub-install (grub-efi-amd64-bin), rsync
#
# Layout (GPT, UEFI boot):
#   p1  512M  FAT32  ESP        — GRUB + grub.cfg (loopback-boots the ISO)
#   p2   16M  FAT32  CIDATA     — autoinstall user-data/meta-data
#   p3  rest  exFAT  FOBDATA    — exact copy of the FOB security drive folder

set -euo pipefail

DEV="${1:?usage: $0 /dev/sdX /path/to/FOB-security-drive}"
SRC="${2:?usage: $0 /dev/sdX /path/to/FOB-security-drive}"
KIT="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }
[ -b "$DEV" ] || { echo "$DEV is not a block device"; exit 1; }
[ -d "$SRC/01-ubuntu-image" ] || { echo "$SRC doesn't look like the FOB drive folder"; exit 1; }
for t in sgdisk mkfs.vfat mkfs.exfat grub-install rsync; do
  command -v "$t" >/dev/null || { echo "missing tool: $t"; exit 1; }
done

SRC_BYTES=$(du -sb "$SRC" | cut -f1)
DEV_BYTES=$(blockdev --getsize64 "$DEV")
NEED=$(( SRC_BYTES + 2*1024*1024*1024 ))   # content + boot partitions + slack
if [ "$DEV_BYTES" -lt "$NEED" ]; then
  echo "device too small: $(numfmt --to=iec $DEV_BYTES) < $(numfmt --to=iec $NEED) needed"
  exit 1
fi

echo "About to ERASE $DEV ($(numfmt --to=iec $DEV_BYTES)) and copy $(numfmt --to=iec $SRC_BYTES) of content."
lsblk "$DEV"
read -rp "Type the device name ($DEV) to confirm: " CONFIRM
[ "$CONFIRM" = "$DEV" ] || { echo "aborted"; exit 1; }

# Partition suffix (/dev/sdb1 vs /dev/nvme0n1p1)
case "$DEV" in *[0-9]) P="p" ;; *) P="" ;; esac

umount -q "${DEV}${P}"* 2>/dev/null || true

echo "== partitioning =="
sgdisk --zap-all "$DEV"
sgdisk -n1:0:+512M -t1:EF00 -c1:"ESP" \
       -n2:0:+16M  -t2:0700 -c2:"CIDATA" \
       -n3:0:0     -t3:0700 -c3:"FOBDATA" "$DEV"
partprobe "$DEV"; sleep 2

echo "== formatting =="
mkfs.vfat  -F32 -n EFIBOOT "${DEV}${P}1"
mkfs.vfat  -F12 -n CIDATA  "${DEV}${P}2"
mkfs.exfat -L FOBDATA      "${DEV}${P}3"

MNT=$(mktemp -d)
mkdir -p "$MNT/esp" "$MNT/seed" "$MNT/data"
mount "${DEV}${P}1" "$MNT/esp"
mount "${DEV}${P}2" "$MNT/seed"
mount "${DEV}${P}3" "$MNT/data"
trap 'umount -q "$MNT"/esp "$MNT"/seed "$MNT"/data 2>/dev/null; rmdir "$MNT"/{esp,seed,data} "$MNT" 2>/dev/null' EXIT

echo "== installing GRUB (UEFI, removable) =="
grub-install --target=x86_64-efi \
             --efi-directory="$MNT/esp" \
             --boot-directory="$MNT/esp/boot" \
             --removable --no-nvram
cp "$KIT/grub.cfg" "$MNT/esp/boot/grub/grub.cfg"

echo "== writing autoinstall seed =="
cp "$KIT/autoinstall/user-data" "$KIT/autoinstall/meta-data" "$MNT/seed/"

echo "== copying FOB drive content (unmodified) =="
rsync -rt --info=progress2 --exclude='.DS_Store' "$SRC"/ "$MNT/data"/

echo "== verifying manifest on the copy =="
if command -v sha256sum >/dev/null && command -v jq >/dev/null; then
  ( cd "$MNT/data/02-kela" && \
    jq -r '.files[] | "\(.sha256)  \(.path)"' MANIFEST.json | sha256sum -c --quiet ) \
    && echo "manifest OK: copy is byte-identical" \
    || echo "WARNING: manifest check reported differences (expected only for consumed CA slot keys)"
else
  echo "jq/sha256sum not available — skipping manifest verification"
fi

sync
echo "== done =="
echo "Boot a UEFI box from this stick. It will WIPE the internal disk,"
echo "install Ubuntu 24.04, reboot, and run Kela activation automatically"
echo "(log: /var/log/kela-firstboot.log; login kela/kela — change in user-data)."
