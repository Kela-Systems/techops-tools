#!/bin/sh
# Arm the RTC to power the box back on after the poweroff that ends the install.
# A cold start is what reliably re-enumerates the USB stick; a warm reboot has
# been seen to leave it undetected until physically replugged, which is the one
# thing that needs a human in an otherwise hands-off flow.
#
# The awkward part is that the window is measured from here while the box powers
# off an unknown time later, and an alarm that fires before that is consumed for
# nothing. No run has actually been seen to lose that race: of two runs at 300s one
# woke itself 74s after its alarm, and the one reported as "didn't wake" was
# powered on by hand 69s before its alarm was even due. But the margin cannot be
# measured from the installed system (see the note in kela-activate.sh), and a
# window long enough to cover an unknown worst case makes every good run wait for
# the worst case too.
#
# So the alarm is refreshed on a loop rather than guessed at once. Each pass pushes
# it WINDOW seconds out; when the installer finally powers off the loop dies with
# it and the last alarm set is at most REFRESH seconds stale, so the box wakes
# WINDOW-ish seconds after the real poweroff no matter how long finalisation took.
# The duration stops being something we have to know.
#
# The loop is orphaned deliberately — subiquity waits for this script, not for its
# children, and all three streams are closed so nothing holds the pipe open. If it
# does get killed early the single alarm armed below still stands, which is exactly
# the old fixed-window behaviour: this can degrade to what it replaced, not below.
#
# If the firmware ignores RTC alarms entirely the box stays off and the operator
# presses power — same outcome, minus the automation. Either way the GRUB
# re-imaging guard sends the cold boot into the installed system rather than back
# into the installer.
WINDOW=300
REFRESH=60

mkdir -p /target/var/lib/kela
state=/target/var/lib/kela/rtc-armed

# Some firmware exposes the RTC as an ACPI wake device that is off by default,
# and then no alarm can wake the box from S5. Writing the name toggles the state,
# so act only on an explicit "*disabled" — flipping an already-enabled RTC would
# break the very thing this is meant to fix.
if [ -w /proc/acpi/wakeup ] &&
  awk '$1 == "RTC" { print $3 }' /proc/acpi/wakeup | grep -q '^\*disabled$'; then
  echo RTC >/proc/acpi/wakeup 2>/dev/null || true
  acpi="was disabled, enabled it"
elif [ -r /proc/acpi/wakeup ]; then
  acpi="$(awk '$1 == "RTC" { print $3 }' /proc/acpi/wakeup 2>/dev/null || true)"
  acpi="${acpi:-RTC not listed as a wake device}"
else
  acpi="no /proc/acpi/wakeup"
fi

if ! rtcwake -m no -s "$WINDOW" 2>/dev/null; then
  echo "FAILED to arm — the box stays off until the power button is pressed" >"$state"
  exit 0
fi
first=$(cat /sys/class/rtc/rtc0/wakealarm 2>/dev/null || echo unknown)

(
  while rtcwake -m no -s "$WINDOW" >/dev/null 2>&1; do
    sleep "$REFRESH"
  done
) </dev/null >/dev/null 2>&1 &
refresher=$!

# A loop that dies on its first pass is worth knowing about at first boot, since
# it turns the window back into a fixed one that can be outlasted.
sleep 2
if kill -0 "$refresher" 2>/dev/null; then
  refresh="running, re-arming +${WINDOW}s every ${REFRESH}s"
else
  refresh="NOT running — only the single ${WINDOW}s alarm below stands"
fi

{
  echo "armed=$(date -u +%FT%TZ) window=${WINDOW}s"
  echo "wakealarm=$first"
  echo "refresher=$refresh"
  echo "acpi-wake=$acpi"
} >"$state"
