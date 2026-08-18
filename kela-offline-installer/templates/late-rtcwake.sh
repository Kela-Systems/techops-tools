#!/bin/sh
# Arm the RTC to power the box back on a few minutes after the poweroff that
# ends the install. A cold start is what reliably re-enumerates the USB stick;
# a warm reboot has been seen to leave it undetected until physically replugged,
# which is the one thing that needs a human in an otherwise hands-off flow.
#
# The 300s is measured from here and only curtin's finalisation stands between
# this and the actual poweroff, so the alarm cannot expire while the box is
# still running. If the firmware ignores RTC alarms the box simply stays off and
# the operator presses power — same outcome, minus the automation. Either way
# the GRUB re-imaging guard sends the cold boot into the installed system rather
# than back into the installer.
mkdir -p /target/var/lib/kela
if rtcwake -m no -s 300 2>/dev/null; then
  echo "armed, wakealarm=$(cat /sys/class/rtc/rtc0/wakealarm 2>/dev/null || echo unknown)" \
    >/target/var/lib/kela/rtc-armed
else
  echo "FAILED to arm — the box stays off until the power button is pressed" \
    >/target/var/lib/kela/rtc-armed
fi
