#!/bin/sh
# Keep nouveau out of the installed system so the NVIDIA driver binds cleanly on
# first boot — otherwise the console fills with reset spam and the driver only
# takes over after another manual reboot.
printf 'blacklist nouveau\noptions nouveau modeset=0\n' \
  >/target/etc/modprobe.d/blacklist-nouveau.conf
curtin in-target -- update-initramfs -u
