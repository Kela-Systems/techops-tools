# Offline runbook — build the Kela bootable USB on a Linux box

For a standalone Linux machine with **no internet and no extra packages**. Uses
only what a stock Ubuntu/Debian install has: `sfdisk`, `mkfs.vfat`, `mkfs.ext4`,
`mount`, `cp`, `python3`, `openssl`.

This is the Linux equivalent of `make-usb-macos.sh` and it uses the **same**
`templates/` and `lib/` files, so the resulting stick behaves identically. Never
hand-type the seed: render it, or the two paths drift apart.

Two deliberate differences from the macOS build:

- The boot partition is typed as an EFI System Partition directly (`type=U`), so
  there is no separate ESP to mirror the bootloader onto and
  `templates/grub-esp-shim.cfg` is not needed.
- `FOBDATA` is ext4 rather than exFAT, because `mkfs.exfat` is not on a stock
  install. The first-boot script mounts by label and does not care, but a stick
  built this way cannot be read on macOS or Windows.

Everything below runs as root (`sudo -i`). Replace `/dev/sdX` with the USB
stick, `SRC` with the mounted FOB drive, and `KIT` with this directory.

## 0. Sanity check tools + identify the stick

```sh
command -v sfdisk mkfs.vfat mkfs.ext4 python3 openssl || echo MISSING
lsblk -o NAME,SIZE,MODEL,TRAN        # find the USB stick — triple-check!
KIT=/path/to/kela-offline-installer
SRC="/media/wherever/FOB security drive"
USB=/dev/sdX
ISO="$SRC/01-ubuntu-image/ubuntu-24.04.4-live-server-amd64.iso"
```

## 1. Partition (GPT: 4G ESP, 64M seed, rest data)

```sh
printf 'size=4GiB,type=U\nsize=64MiB\n,\n' | sfdisk --label gpt --wipe always $USB
mkfs.vfat -F32 -n EFIBOOT ${USB}1
mkfs.vfat -n CIDATA ${USB}2
mkfs.ext4 -L FOBDATA ${USB}3
```

(NVMe-style device? Partitions are `p1 p2 p3`, e.g. `${USB}p1`.)

## 2. Boot partition: extract the ISO onto it

```sh
mkdir -p /mnt/iso /mnt/boot /mnt/seed /mnt/data
mount -o loop,ro "$ISO" /mnt/iso
mount ${USB}1 /mnt/boot
cp -a /mnt/iso/. /mnt/boot/
```

`cp` prints a few "cannot create symbolic link" errors (FAT cannot hold
symlinks, e.g. under `dists/`). **Ignore them** — the installer does not use
those files; it installs from the squashfs.

## 3. Boot partition: graft the managed GRUB block

Adds the unattended entry with the hostname prompt and the re-imaging guard, and
reverts the ISO's own entries to manual installs as a fallback. Safe to re-run.

```sh
python3 $KIT/lib/graft-grub.py /mnt/boot/boot/grub/grub.cfg $KIT/templates/grub-kela.cfg
tail -40 /mnt/boot/boot/grub/grub.cfg     # eyeball the result
```

## 4. Seed partition: render the autoinstall answers

Pick a password for the `kela` user. On Linux `openssl passwd -6` prints the hash
directly (unlike macOS, where LibreSSL has no `-6` and `$KIT/lib/mkpasswd.sh` is
needed instead).

```sh
mount ${USB}2 /mnt/seed
echo 'instance-id: kela-fob' > /mnt/seed/meta-data
python3 $KIT/lib/render.py user-data.tmpl \
  PASSWORD_HASH="$(openssl passwd -6)" > /mnt/seed/user-data
python3 $KIT/lib/collect-extra-debs.py --iso "$ISO" --verify $KIT/extra-debs \
  --bundle "$SRC/02-kela" \
  || { echo "REFUSING to ship extra-debs — see extra-debs/README.md"; false; }
cp $KIT/extra-debs/*.deb /mnt/seed/
ls /mnt/seed/usbguard_*.deb >/dev/null \
  || echo "MISSING usbguard debs — required; see extra-debs/README.md"
python3 -c "import yaml,sys; yaml.safe_load(open('/mnt/seed/user-data'))" \
  && echo "seed parses"
```

`extra-debs/` permanently carries `usbguard` and its dependencies (plus any
bundle-specific sideloads); an empty copy is a broken kit checkout, not a
normal state.

The verify step is not optional paranoia. First boot installs these with
`dpkg -i`, which has no solver: one deb that is *newer* than what the ISO
installs half-upgrades that package's family, the siblings keep their
`Depends: ... (= old version)`, and every `apt` call on the box fails from then
on — unfixable, because the box has no network. That shipped once and stranded a
box. The check reads the ISO's own package manifest and needs no network, so it
works here. `make-usb-macos.sh` runs the same check in preflight.

## 5. Data partition: exact copy of the folder

```sh
mount ${USB}3 /mnt/data
cp -a "$SRC/." /mnt/data/
```

## 6. Repair scanner-mangled names, then verify against the manifest

The security scan appends `.gz` to gzip-magic files and rewrites `%` as `_` in
apt pool filenames. Repair is rename-only and touches the copy, never the
source.

```sh
cd /mnt/data/02-kela
python3 $KIT/lib/repair-names.py
python3 $KIT/lib/verify-manifest.py; echo "exit=$?"
```

Exit 0 is the gate. A missing `pki/slots/<n>/cluster-ca.key` is reported as
expected (those get consumed as boxes are provisioned); anything else means a
bad copy and the stick must not be shipped.

## 7. Finish

```sh
cd /
sync
umount /mnt/iso /mnt/boot /mnt/seed /mnt/data
```

## Using it

Boot the target box from the stick over UEFI, ideally through the firmware's
one-time boot menu rather than by reordering the boot devices. The default entry
asks for a hostname, then **wipes the smallest internal disk with no further
prompt** and installs Ubuntu.

The box then **powers itself off** and an RTC alarm cold-starts it within about
five minutes, because a cold start is what reliably re-enumerates the USB stick.
Leave the stick in. Pressing the power button once the screen has gone dark is
always safe and skips the wait; the first-boot log records which happened. Either
way the box boots the system it just installed, not the installer, and runs Kela
activation.

A box that already carries an Ubuntu ESP boots that instead of being re-imaged;
to re-image deliberately, pick the install entry from the GRUB menu.

Progress and the final outcome appear at the console login prompt. Full log:
`/var/log/kela-firstboot.log` on the box, copied to `99-install-logs/` on the
stick. Then remove the stick.

Notes:

- Runs entirely offline end to end — the installer uses its own squashfs, the
  seed disables geoip and falls back to an offline install if no mirror is
  reachable, network is optional, and activation is offline by design.
- Each activation consumes one CA slot key on the **stick** (by design); the
  external HD is never written to.
- The stick carries live cluster-CA keys — same custody rules as the HD.

## USBGuard (USB device policy)

Boxes built from this stick run [USBGuard](https://usbguard.github.io/) with a
strict policy: **only HID devices (keyboards, mice, joysticks/gamepads) and USB
hubs are allowed; everything else — including USB storage — is blocked**, both
on hotplug and if already inserted at boot.

How it gets there: the `usbguard` debs ride the seed partition and install at
first boot with the unit **masked**, so the policy cannot block the FOBDATA
stick mid-activation. `kela-activate.sh` arms it the moment activation succeeds
(policy installed, unit enabled — any later boot comes up guarded even after a
power cut during convergence) and starts it as its very last step, after the
final log copy to the stick has been synced.

Consequences to know in the field:

- **Replugging a FOB stick into an activated box does not work.** The udev
  rule that used to restart activation never fires because the stick is not
  authorized. To read a stick on an activated box:
  `sudo systemctl stop usbguard`, plug the stick, do the work, unplug,
  `sudo systemctl start usbguard`. (A one-off alternative:
  `usbguard list-devices`, then `usbguard allow-device <id>`.)
- A box whose activation **failed** is left unguarded on purpose — keyboard
  and stick keep working for debugging, and usbguard stays masked until an
  activation succeeds.
- Keyboards that expose non-HID functions (a separate audio interface,
  a memory-card reader) are blocked by the every-interface-must-be-HID rule;
  plain keyboards, mice, wireless HID dongles and HID+hub combos work.
- The policy lives at `/etc/usbguard/rules.conf` on the box, staged from
  `templates/user-data.tmpl` (`/usr/local/share/kela/usbguard-rules.conf`).
  Verify with `usbguard list-devices` (blocked devices show `block`).
