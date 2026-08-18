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
cp $KIT/extra-debs/*.deb /mnt/seed/ 2>/dev/null || \
  echo "no sideloaded debs (normal — the bundle supplies its own)"
python3 -c "import yaml,sys; yaml.safe_load(open('/mnt/seed/user-data'))" \
  && echo "seed parses"
```

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

The box then **powers itself off** and an RTC alarm cold-starts it about four
minutes later, because a cold start is what reliably re-enumerates the USB stick.
Leave the stick in. If the box has not come back roughly five minutes after the
screen goes dark, the firmware ignored the alarm — press the power button. Either
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
