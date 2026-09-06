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

# One file per run, not per call, so saving mid-convergence and again at the end
# refreshes the same log instead of leaving two partial copies to choose between.
RUN=$(date -u +%Y%m%dT%H%M%SZ)
save_log() {
  mountpoint -q "$DRIVE" || return 0
  mkdir -p "$DRIVE/99-install-logs" 2>/dev/null || return 0
  cp "$LOG" "$DRIVE/99-install-logs/$(hostname)-$RUN.log" 2>/dev/null || true
  sync
}

on_exit() {
  rc=$?
  [ "$rc" -eq 0 ] && return 0
  [ "$PHASE" = activate ] || return 0
  say "FAILED (exit $rc) — see $LOG"
  status "FAILED — see $LOG"
  save_log
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

# USBGuard ships on the seed partition and installs with the other sideloaded
# debs, but its unit was masked by the install seed: started any earlier, its
# block-by-default policy would deauthorize the FOBDATA stick this script is
# reading. Arming is split in two so a power cut during the long convergence
# wait cannot leave the box unguarded forever (this service never runs again
# once .firstboot-activated exists):
#   1. arm_usbguard, right after activation succeeds: policy in place, unit
#      unmasked and enabled — every boot from the next one is guarded.
#   2. start_usbguard, at the very end: this boot becomes guarded too, after
#      the last log write to the stick has been synced.
# Runs after the CA slot is spent, so nothing in here may fail the script:
# a box without USB filtering is a warning in the log, a box declared FAILED
# after a successful activation is a trip back to the bench.
arm_usbguard() {
  if ! dpkg -s usbguard >/dev/null 2>&1; then
    say "WARNING: usbguard is not installed — were its debs missing from the"
    say "seed partition? The box stays unguarded; USB devices are not filtered."
    return 0
  fi
  if ! install -m 0600 /usr/local/share/kela/usbguard-rules.conf /etc/usbguard/rules.conf; then
    say "WARNING: could not install the usbguard policy — leaving usbguard"
    say "masked rather than enabling it with whatever rules it happens to have."
    return 0
  fi
  # Ubuntu package defaults, but the whole policy rests on them, so pin both.
  sed -i -e 's/^ImplicitPolicyTarget=.*/ImplicitPolicyTarget=block/' \
         -e 's/^PresentDevicePolicy=.*/PresentDevicePolicy=apply-policy/' \
         /etc/usbguard/usbguard-daemon.conf 2>/dev/null \
    || say "WARNING: could not pin usbguard-daemon.conf — package defaults apply"
  systemctl unmask usbguard.service || true
  systemctl enable usbguard.service 2>/dev/null || true
  say "usbguard armed: HID-only policy installed, enabled from the next boot"
}

start_usbguard() {
  systemctl is-enabled usbguard.service >/dev/null 2>&1 || return 0
  umount "$DRIVE" 2>/dev/null || true
  if systemctl start usbguard.service; then
    say "usbguard active: only keyboards, mice, joysticks and hubs are allowed."
    say "The FOB stick is now blocked — replugging it will NOT restart this"
    say "service. To read it again: systemctl stop usbguard, then replug."
  else
    say "WARNING: usbguard failed to start — it is enabled and will be tried"
    say "again on the next boot. See: systemctl status usbguard"
  fi
}

# Whether the box woke on the alarm or a human pressed power is invisible after
# the fact unless it is worked out here, and "it didn't wake up" is impossible to
# act on without knowing which of the two failure modes it was: the alarm being
# consumed while the installer was still shutting down, or the firmware ignoring
# it. The installer's own last log write against the alarm time settles that.
report_wake() {
  local alarm now delta
  [ -f "$STATE/rtc-armed" ] || return 0
  while IFS= read -r line; do say "installer RTC: $line"; done <"$STATE/rtc-armed"

  alarm=$(sed -n 's/^wakealarm=\([0-9][0-9]*\)$/\1/p' "$STATE/rtc-armed")
  [ -n "$alarm" ] || return 0
  now=$(date +%s)
  delta=$((now - alarm))
  if [ "$delta" -lt 0 ]; then
    say "RTC: the alarm is still $((-delta))s in the future — this boot was not it,"
    say "so the power button was pressed early. Harmless."
  elif [ "$delta" -le 300 ]; then
    say "RTC: booted ${delta}s after the alarm — the box woke itself, as intended."
  else
    say "RTC: booted ${delta}s after the first alarm. If the refresher below was"
    say "running this is expected — the effective alarm was later than the one"
    say "recorded here, and the box may still have woken itself. Otherwise the"
    say "power button did the work."
    say "RTC: when the box does not wake at all, the cause is either the alarm"
    say "being consumed while the installer was still shutting down, or firmware"
    say "that ignores it. That cannot be told apart from here: subiquity snapshots"
    say "/var/log/installer into the target before the late-command runs, so"
    say "nothing on this disk records when the box actually powered off."
  fi
}

say "first-boot activation starting on $(hostname)"
status "in progress — log at $LOG"
report_wake

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

# Dependencies missing from the bundle ride along on the seed partition, and it
# carries the keep-slot-key opt-in too: CIDATA is FAT, so the marker can be created
# or deleted on any laptop without rebuilding the stick.
KEEP_SLOT=0
if mount -L CIDATA "$SEED" 2>/dev/null; then
  if ls "$SEED"/*.deb >/dev/null 2>&1; then
    say "installing sideloaded debs from CIDATA"
    dpkg -i "$SEED"/*.deb || true
  fi
  [ -e "$SEED/kela-keep-slot-key" ] && KEEP_SLOT=1
  umount "$SEED" || true
fi

# Resolve the bundle debs' dependencies from the drive's own apt repo, if it has
# one. Not every bundle ships a repo — one that carries its whole closure in
# debs/ needs none — and a missing or unindexed apt/ must not abort activation:
# apt-get update fails hard under set -e, and the one-shot guard above means
# there would be no second chance.
# Our own filename, deliberately NOT kela-offline.list: kela-node-controller
# writes that path itself during activation, pointing at the bundle it staged
# under /var/lib/kela/offline, and its daemon installs from it while converging
# (chrony for `time`, nvidia-container-toolkit). Sharing the name meant our
# post-activation cleanup deleted the controller's source, and convergence then
# failed on exactly those two components. Never create or remove that path here.
REPO=/etc/apt/sources.list.d/kela-installer.list
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

# activate.sh returns as soon as the bundle is staged and the CA slot is spent;
# the daemon then converges the box in the background, and that is where the real
# outcome is decided. Reporting COMPLETE on activate.sh's exit status told the
# operator the box was done while convergence was still pending — and failing.
# Journal markers, from the controller binary: "system converged" /
# "application converged" against "system converge failed for:" /
# "application converge failed:".
# A failure line is not a verdict. The daemon converges in passes and retries
# forever, and a component can fail on one pass and succeed on a later one: a box
# whose first pass failed on `time` nineteen seconds in was declared FAILED here
# nine seconds later, while the daemon was still working. An earlier box failed
# the same way and converged on the following attempt. So success is checked
# first, failures are only counted and reported as progress, and nothing is called
# failed until the timeout expires with no success. The count is what separates a
# box that stumbled once from one that is genuinely stuck in a retry loop.
CONVERGE_TIMEOUT=${KELA_CONVERGE_TIMEOUT:-5400}
FAILS=0
wait_for_convergence() {
  local since="$1" waited=0 log="" seen=0 last=""
  while :; do
    log=$(journalctl -u kela-node-controller --no-pager --since "$since" 2>/dev/null || true)
    case "$log" in
      *"system converged"*) return 0 ;;
    esac

    seen=$(printf '%s\n' "$log" |
      grep -c -e 'system converge failed for:' -e 'application converge failed:' ||
      true)
    if [ "${seen:-0}" -gt "$FAILS" ]; then
      FAILS=$seen
      last=$(printf '%s\n' "$log" |
        grep -e 'system converge failed for:' -e 'application converge failed:' |
        tail -1)
      say "converge attempt failed (${FAILS} so far): ${last##*failed*: }"
      say "the daemon retries on its own; this is a failure only if it never converges"
    fi

    [ "$waited" -lt "$CONVERGE_TIMEOUT" ] || break
    sleep 15
    waited=$((waited + 15))
  done
  [ "$FAILS" -eq 0 ] || return 2
  return 1
}

# The slot budget exists to bound how many clusters one drive can mint, so keeping
# the key is opt-in per stick and never a default: a drive that travels to a site
# must spend its slot. `kela-node-controller activate --help` documents the flag as
# "Leave the consumed slot's key on the drive (e.g. a read-only medium). The drive
# then still holds a usable cluster-CA key" — so a stick used this way stays
# sensitive, and two boxes activated from one slot share a cluster CA.
#
# The bundle may already pass the flag itself. v2.7.0-rc.5 ends activate.sh with
#   exec kela-node-controller activate "$dir" --keep-slot-key "$@"
# so appending our own gives the binary two, and it exits 2 on "the argument
# '--keep-slot-key' cannot be used multiple times" before the slot is touched.
# Read the bundle's script instead of assuming it forwards nothing.
#
# The reverse case matters more, and is checked against the slot count after
# activation rather than here: a bundle that hardcodes the flag keeps the slot
# key whether or not this stick asked it to, so the marker's absence no longer
# means the slot gets spent.
BUNDLE_KEEPS_SLOT=0
if grep -q -- '--keep-slot-key' "$DRIVE/02-kela/activate.sh" 2>/dev/null; then
  BUNDLE_KEEPS_SLOT=1
fi

if [ "$KEEP_SLOT" = 1 ]; then
  # Ask the binary rather than assume: an unrecognised argument would fail
  # activation outright, and the one-shot guard would then strand the box.
  if [ "$BUNDLE_KEEPS_SLOT" = 1 ]; then
    say "CIDATA/kela-keep-slot-key present, and the bundle's own activate.sh already"
    say "passes --keep-slot-key. Not passing it twice — the binary rejects that."
    say "The slot key stays on the drive, which keeps a usable cluster-CA key."
  elif kela-node-controller activate --help 2>&1 | grep -q -- '--keep-slot-key'; then
    say "CIDATA/kela-keep-slot-key present: activating with --keep-slot-key, so the"
    say "slot key stays on the drive. The drive keeps a usable cluster-CA key."
  else
    # Spending a slot is irreversible and the operator explicitly asked us not to,
    # so stop instead. activate.sh has not run, nothing is consumed, and clearing
    # the guard is provably safe for exactly that reason. Exit 0: Restart=on-failure
    # would otherwise spin on this every 20s, and no retry can fix it.
    rm -f "$STATE/.activation-attempted"
    say "CIDATA/kela-keep-slot-key is present, but this kela-node-controller has no"
    say "--keep-slot-key flag. Refusing to activate: a slot would be destroyed and"
    say "you asked for it to be kept. Delete the marker to activate normally."
    status "STOPPED — --keep-slot-key unsupported on this release; see $LOG"
    PHASE=stopped
    save_log
    exit 0
  fi
fi

# Counting the key files is the only honest check that the flag did what it says.
slot_keys() {
  find "$DRIVE/02-kela/pki/slots" -name cluster-ca.key 2>/dev/null | wc -l | tr -d ' '
}
SLOTS_BEFORE=$(slot_keys)
say "unspent CA slots on the drive: $SLOTS_BEFORE"

say "running activate.sh"
cd "$DRIVE/02-kela"
# Bound the journal read to this run, so a previous attempt's verdict cannot be
# mistaken for this one's.
SINCE=$(date '+%Y-%m-%d %H:%M:%S')
# Spelled out both ways rather than expanded from an array: "${arr[@]}" on an empty
# array is an unbound-variable error under set -u before bash 4.4, and this script
# must not depend on the target's bash being new enough to forgive that.
rc=0
if [ "$KEEP_SLOT" = 1 ] && [ "$BUNDLE_KEEPS_SLOT" = 0 ]; then
  sh ./activate.sh --keep-slot-key || rc=$?
else
  sh ./activate.sh || rc=$?
fi

SLOTS_AFTER=$(slot_keys)
say "unspent CA slots on the drive: $SLOTS_BEFORE before, $SLOTS_AFTER after"

# The guard is set before activation because a slot may be spent the moment
# activate.sh runs. But argument parsing and manifest verification both happen
# before the key is touched, so an unchanged count is proof this attempt cost
# nothing — and then holding the guard shut only strands a box that is free to try
# again. Exit 0 either way: Restart=on-failure would retry every 20s, and these
# are failures that need a human, not a loop.
if [ "$rc" -ne 0 ]; then
  if [ "$SLOTS_AFTER" = "$SLOTS_BEFORE" ]; then
    rm -f "$STATE/.activation-attempted"
    say "activate.sh failed (exit $rc) and no slot was consumed — $SLOTS_AFTER still"
    say "unspent. Guard cleared: fix the cause shown above and reboot to retry."
    status "FAILED, no slot spent — see $LOG"
  else
    say "activate.sh failed (exit $rc) AFTER consuming a slot ($SLOTS_BEFORE ->"
    say "$SLOTS_AFTER). The guard stays shut; another attempt would spend another"
    say "slot. Read the log before deciding to clear $STATE/.activation-attempted."
    status "FAILED, a slot was spent — see $LOG"
  fi
  PHASE=stopped
  save_log
  exit 0
fi

if [ "$KEEP_SLOT" = 1 ] && [ "$SLOTS_AFTER" -lt "$SLOTS_BEFORE" ]; then
  say "WARNING: --keep-slot-key was accepted but a slot key was destroyed anyway."
  say "Treat the flag as ineffective on this release and budget slots accordingly."
elif [ "$KEEP_SLOT" = 0 ] && [ "$SLOTS_AFTER" = "$SLOTS_BEFORE" ]; then
  # No marker means this stick was meant to spend its slot, and the count says it
  # did not. Reported, never fatal: activation succeeded and the daemon is already
  # converging, so failing here would send a working box back to the bench over a
  # custody problem that a human has to resolve on the drive either way.
  say "WARNING: there is no keep-slot-key marker on CIDATA, so this stick was meant"
  say "to spend its slot — but the count is unchanged at $SLOTS_AFTER. The drive still"
  say "holds a usable cluster-CA key for this cluster."
  if [ "$BUNDLE_KEEPS_SLOT" = 1 ]; then
    say "Cause: this bundle's activate.sh passes --keep-slot-key unconditionally, so"
    say "the marker cannot opt out of it."
  fi
  say "Treat the drive as being as sensitive as the original FOB drive: another box"
  say "activated from it would share this cluster's CA."
fi

# Our own list only. See the REPO comment: kela-offline.list is the controller's.
rm -f "$REPO"

: >"$STATE/.firstboot-activated"
say "bundle activated, CA slot spent. Waiting for the daemon to converge."
status "activated on $(hostname) — converging, see $LOG"

# Enabled (not started) now: if power is lost during the convergence wait below,
# the next boot still comes up guarded. This boot keeps the stick usable until
# start_usbguard at the end.
arm_usbguard

# Convergence can legitimately run for the better part of an hour, and the log was
# copied to the stick only after the verdict — so an operator who pulled the drive
# while it worked took away no record at all. Save it now and again at the end.
save_log

# The daemon retries forever on its own, so a bad verdict here is reported, not
# retried: re-running activate.sh would be the one action that risks a slot.
rc=0
wait_for_convergence "$SINCE" || rc=$?
case "$rc" in
  0) if [ "$FAILS" -gt 0 ]; then
       say "system converged, after $FAILS failed attempt(s) along the way"
     else
       say "system converged"
     fi
     status "COMPLETE on $(hostname)" ;;
  2) say "NOT CONVERGED after ${CONVERGE_TIMEOUT}s and $FAILS failed attempt(s)."
     say "The daemon is still retrying. The tail of its log:"
     journalctl -u kela-node-controller --no-pager --since "$SINCE" 2>/dev/null | tail -60
     status "FAILED to converge on $(hostname) — see $LOG" ;;
  *) say "still converging after ${CONVERGE_TIMEOUT}s — no verdict yet, not a failure"
     status "converging on $(hostname) — see $LOG" ;;
esac

# Restoring Ubuntu's online sources is deliberately last. They are unreachable on
# an air-gapped box, and putting them back before convergence pointed every
# apt-get the daemon runs at a host it cannot resolve.
if [ -f /root/ubuntu.sources.bak ]; then
  mv /root/ubuntu.sources.bak /etc/apt/sources.list.d/ubuntu.sources
fi

save_log
sync

# Deliberately last: starting usbguard deauthorizes the FOB stick, so every
# write to it (save_log above included) must already be on the medium.
start_usbguard
