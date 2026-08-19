#!/bin/sh
# Apply the hostname typed at the GRUB prompt, arriving as kela.hostname= on
# the installer's kernel cmdline. Normalised to a valid DNS label; if nothing
# usable survives, the seeded kela-fob stays in place.
raw=$(sed -n 's/.*kela\.hostname=\([^ ]*\).*/\1/p' /proc/cmdline)
h=$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' \
      | cut -c1-63 | sed 's/^-*//; s/-*$//')
[ -n "$h" ] || exit 0
echo "$h" >/target/etc/hostname
sed -i "s/kela-fob/$h/g" /target/etc/hosts
