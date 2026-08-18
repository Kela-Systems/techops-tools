#!/bin/bash
# Kela offline first-boot activation. Started by kela-firstboot.service, and by
# a udev rule when a FOBDATA drive appears.
#
# Waiting for the drive is retried freely, but activate.sh itself runs at most
# once: every run consumes a cluster-CA slot key on the stick, so a failure
# there needs a human reading the log, not a restart loop.
set -eu

LOG=/var/log/kela-firstboot.log
STATE=/var/lib/kela
DRIVE=/mnt/kela-drive
SEED=/mnt/kela-seed
PHASE=drive-wait

mkdir -p "$STATE" "$DRIVE" "$SEED" /etc/issue.d
exec >>"$LOG" 2>&1

say() {
  echo "$(date -u +%FT%TZ) $*"
  printf '\n[kela] %s\n' "$*" >/dev/tty1 2>/dev/null || true
}

# Surfaced at the console login prompt so the operator can read the outcome
# without logging in.
status() {
  printf 'Kela offline install: %s\n\n' "$*" >/etc/issue.d/10-kela.issue
}

on_exit() {
  rc=$?
  [ "$rc" -eq 0 ] && return 0
  [ "$PHASE" = activate ] || return 0
  say "FAILED (exit $rc) — see $LOG"
  status "FAILED — see $LOG"
}
trap on_exit EXIT

try_mount() {
  attempt=0
  while [ "$attempt" -lt "$1" ]; do
    mountpoint -q "$DRIVE" && return 0
    udevadm settle 2>/dev/null || true
    mount -L FOBDATA "$DRIVE" 2>/dev/null && return 0
    sleep 2
    attempt=$((attempt + 1))
  done
  mountpoint -q "$DRIVE"
}

# Recovery ladder, cheapest first. Everything here is a workaround for a stick
# that does not come back on its own; the install powers the box off and lets the
# RTC cold-start it precisely so this ladder is rarely needed.
retrigger() {
  say "re-triggering block device discovery"
  udevadm trigger --subsystem-match=block --action=add 2>/dev/null || true
  udevadm settle 2>/dev/null || true
  sleep 3
}

# Reattach the mass-storage drivers without disturbing the rest of the bus.
storage_reset() {
  say "rebinding USB mass-storage drivers"
  for drv in usb-storage uas; do
    for dev in /sys/bus/usb/drivers/"$drv"/*-*; do
      [ -e "$dev" ] || continue
      iface=$(basename "$dev")
      echo "$iface" >/sys/bus/usb/drivers/"$drv"/unbind 2>/dev/null || true
      sleep 1
      echo "$iface" >/sys/bus/usb/drivers/"$drv"/bind 2>/dev/null || true
    done
  done
  sleep 5
}

# Software unplug/replug of the whole bus. Note this does not cut port power, so
# a stick wedged by the installer's SCSI eject may still need a real power cycle.
usb_reset() {
  say "rebinding USB host controllers"
  for dev in /sys/bus/pci/drivers/xhci_hcd/0000:*; do
    [ -e "$dev" ] || continue
    slot=$(basename "$dev")
    echo "$slot" >/sys/bus/pci/drivers/xhci_hcd/unbind 2>/dev/null || true
    sleep 2
    echo "$slot" >/sys/bus/pci/drivers/xhci_hcd/bind 2>/dev/null || true
  done
  sleep 8
}

mount_drive() {
  try_mount 15 && return 0
  retrigger
  try_mount 5 && return 0
  storage_reset
  try_mount 10 && return 0
  usb_reset
  try_mount 15 && return 0
  usb_reset
  try_mount 15 && return 0
  return 1
}

# Which failure this is matters: an absent device is an enumeration problem and
# only a replug or a power cycle fixes it, whereas a device that is present but
# will not mount is a filesystem or driver problem and no amount of rebinding
# will help. Say which, so the next box does not need guesswork.
diagnose() {
  local dev
  dev=$(blkid -L FOBDATA 2>/dev/null || true)
  if [ -n "$dev" ]; then
    say "diagnosis: FOBDATA exists at $dev but will not mount — filesystem or"
    say "driver problem, not USB enumeration."
    say "filesystems offering exfat: $(grep -c exfat /proc/filesystems || true)"
    say "recent kernel messages:"
    dmesg | tail -20 || true
  else
    say "diagnosis: no block device labelled FOBDATA — the stick is not"
    say "enumerated. A replug or a power cycle is the only way back."
    say "block devices currently present:"
    lsblk -o NAME,SIZE,LABEL,TRAN 2>/dev/null || true
  fi
}

say "first-boot activation starting on $(hostname)"
status "in progress — log at $LOG"
if [ -f "$STATE/rtc-armed" ]; then
  say "installer RTC alarm: $(cat "$STATE/rtc-armed")"
fi

modprobe exfat 2>/dev/null || true
if ! mount_drive; then
  diagnose
  say "giving up this cycle. systemd retries in 20s, and replugging the stick"
  say "starts activation immediately."
  status "waiting for the Kela USB drive — see $LOG"
  exit 1
fi
say "FOBDATA mounted at $DRIVE"

if [ -e "$STATE/.activation-attempted" ]; then
  say "activation was already attempted and did not finish. Refusing to retry:"
  say "each attempt consumes a CA slot key on the stick. Read $LOG, fix the"
  say "cause, then delete $STATE/.activation-attempted and reboot."
  status "FAILED — activation already attempted; see $LOG"
  exit 0
fi

PHASE=activate
: >"$STATE/.activation-attempted"

# Dependencies missing from the bundle ride along on the seed partition.
if mount -L CIDATA "$SEED" 2>/dev/null; then
  if ls "$SEED"/*.deb >/dev/null 2>&1; then
    say "installing sideloaded debs from CIDATA"
    dpkg -i "$SEED"/*.deb || true
  fi
  umount "$SEED" || true
fi

# Resolve the bundle debs' dependencies from the drive's own apt repo, if it has
# one. Not every bundle ships a repo — one that carries its whole closure in
# debs/ needs none — and a missing or unindexed apt/ must not abort activation:
# apt-get update fails hard under set -e, and the one-shot guard above means
# there would be no second chance.
REPO=/etc/apt/sources.list.d/kela-offline.list
if ls "$DRIVE"/02-kela/apt/Packages* >/dev/null 2>&1; then
  say "resolving dependencies from the drive's apt repo"
  echo "deb [trusted=yes] file:$DRIVE/02-kela/apt ./" >"$REPO"
  apt-get update || say "WARNING: apt-get update on the offline repo failed"
elif [ -d "$DRIVE/02-kela/apt/dists" ]; then
  say "WARNING: 02-kela/apt is a suite-based repo, not the flat one expected."
  say "Not wiring it up — dependencies must already be satisfied by the base"
  say "install or by debs on CIDATA. Suites present: $(ls "$DRIVE/02-kela/apt/dists")"
else
  say "no apt index under 02-kela/apt — installing debs without dependency resolution"
fi

if ls "$DRIVE"/02-kela/debs/*.deb >/dev/null 2>&1; then
  say "installing bundle debs"
  if [ -f "$REPO" ]; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y "$DRIVE"/02-kela/debs/*.deb
  else
    # No repo to pull from, so an unmet dependency surfaces here rather than
    # being fetched. Failing now is the good case: activate.sh has not run, so
    # no CA slot key is spent and a retry after fixing stays safe.
    dpkg -i "$DRIVE"/02-kela/debs/*.deb
  fi
else
  say "no debs under 02-kela/debs — leaving package installation to activate.sh"
fi

say "running activate.sh"
cd "$DRIVE/02-kela"
sh ./activate.sh

# The offline repo vanishes with the stick, and ubuntu.sources was parked
# during the install — leave the box with an apt config that works if it ever
# reaches a network.
rm -f "$REPO"
if [ -f /root/ubuntu.sources.bak ]; then
  mv /root/ubuntu.sources.bak /etc/apt/sources.list.d/ubuntu.sources
fi

: >"$STATE/.firstboot-activated"
say "activation complete"
status "COMPLETE on $(hostname)"

# Leave a copy of the log on the stick so the outcome can be read on any laptop
# after the drive is pulled.
if mkdir -p "$DRIVE/99-install-logs" 2>/dev/null; then
  cp "$LOG" "$DRIVE/99-install-logs/$(hostname)-$(date -u +%Y%m%dT%H%M%SZ).log" || true
fi
sync
