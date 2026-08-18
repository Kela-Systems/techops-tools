# Kela offline installer

Build a USB stick that takes a bare amd64 box to a converged Kela system with
**zero network and one prompt** (the hostname, at boot):

boot stick → GRUB asks hostname (Enter = `kela-fob`) → wipes the **smallest**
internal disk → installs Ubuntu Server 24.04 → powers off and cold-starts itself
about four minutes later → first boot installs the bundle debs and runs Kela
activation → daemon converges.

**Operator note:** leave the stick in, and if the box has not powered itself back
on roughly five minutes after the screen goes dark, press the power button. The
box shuts down on purpose (see below) and not every firmware honours the wake
alarm that is supposed to restart it.

The outcome is printed at the console login prompt, so the operator never has to
log in to find out whether it worked. Full log: `/var/log/kela-firstboot.log` on
the box, copied to `99-install-logs/` on the stick.

The FOB drive content lives byte-identical on its own exFAT partition
(`FOBDATA`) — MANIFEST.json verification passes unchanged, and all boot
machinery is on separate partitions.

## Which script do I run?

`make-usb-macos.sh` **erases its target disk** and copies the bundle onto it from
a source folder, so the source and the target cannot be the same stick.

**One stick, arriving with the bundle on it** (the common case). Stage the bundle
to local storage, verify it, then rebuild the same stick:

```bash
./stage-bundle.sh "/Volumes/<received drive>" ~/kela-staging
diskutil list external                       # find the stick, e.g. disk4
sudo ./make-usb-macos.sh disk4 ~/kela-staging/<bundle folder>
rm -rf ~/kela-staging                        # holds live CA slot keys
```

Staging only reads the drive, and it verifies the copy against `MANIFEST.json`
before you erase anything. Delete the staging copy once the stick verifies:
building a second stick from a stale copy would resurrect slot keys the first
stick already consumed and hand two boxes the same cluster CA.

**Two devices** — bundle on one drive, a separate blank stick to build. Skip
staging; the source drive is never written:

```bash
sudo ./make-usb-macos.sh disk4 "/Volumes/<received drive>"
```

**A stick this kit already built**, needing refreshed boot machinery.
`update-usb-macos.sh` rewrites the GRUB block, the ESP mirror and the seed in
place and never touches the data partition — the safe choice when the stick holds
the only copy of the bundle:

```bash
sudo ./update-usb-macos.sh
```

It cannot make a received drive bootable, only refresh a stick that already is;
it detects that mix-up and tells you which command you wanted.

## Tools

| Path | Purpose |
|---|---|
| `make-usb-macos.sh` | **Primary builder.** `sudo ./make-usb-macos.sh disk4 "/path/to/bundle"`. Erases the target; refuses to run if the source lives on that disk. Needs `brew install xorriso`; stick ≥ 24 GB. |
| `stage-bundle.sh` | Copy a received drive to local storage and verify it, so that same stick can then be erased and rebuilt. No sudo; reads the drive only. |
| `update-usb-macos.sh` | Re-apply the current boot machinery to an already-built stick **in place** (never touches the data partition — safe when the stick holds the only copy). |
| `templates/` | The single source of truth: autoinstall seed, GRUB block, and every script the target runs. Edit here, never on a stick. |
| `lib/` | Rendering and verification helpers, shared by both builders and the runbook. `lib/mkpasswd.sh` generates the password hash on hosts where `openssl passwd -6` is unavailable (i.e. macOS). |
| `extra-debs/` | Sideloaded `.deb`s, for the rare bundle whose dependency closure is incomplete. Normally empty — see `extra-debs/README.md`. |
| `OFFLINE-RUNBOOK.md` | The same build, by hand, on an offline Linux box. Uses the same templates. |
| `legacy/` | Superseded builder, kept for reference only. Do not use. |

## Before building for production

- Set a real password. The builder prompts for one; pressing Enter alone keeps
  the built-in default (`kela`/`kela`) and says so in the build output. To supply
  it non-interactively:

```bash
sudo KELA_PASSWORD_HASH="$(./lib/mkpasswd.sh)" ./make-usb-macos.sh disk4 "/path/to/FOB security drive"
```

  **`openssl passwd -6` does not work on macOS.** `/usr/bin/openssl` is LibreSSL,
  whose `passwd` supports only `-crypt`, `-1` and `-apr1`; the `-6` form errors
  out and leaves you with an empty string. `lib/mkpasswd.sh` probes for an
  OpenSSL that can do SHA-512 crypt (Homebrew's `openssl@3`, `$OPENSSL`, or one
  on `PATH`) and tells you how to get one if there is none. A
  `KELA_PASSWORD_HASH` that is set but not a `$6$` hash is now a hard error
  rather than a silent fall back to the default.
- Dependencies come from the bundle itself; `extra-debs/` is normally empty.
  Preflight prints which case the bundle is in — `apt repo: flat, N packages`
  means first boot resolves dependencies off the drive, `apt repo: none` means
  `02-kela/debs` must be self-contained against a stock Ubuntu 24.04 server
  install. Only if activation later fails on an unmet dependency do you need
  `extra-debs/`.
- The disk-selection rule is `size: smallest`. Subiquity's `size` and `ssd`
  matchers never return the install media, so the stick itself is safe, but any
  **other** idle USB/SD card in the box is a candidate — boot with no other
  removable media attached, or pin the disk by serial for known hardware.
- The stick carries live cluster-CA slot keys: same custody rules as the
  original FOB drive. Each activation consumes one slot key on the stick.

## How the unattended path is kept safe

**Re-imaging guard.** The GRUB block checks for an Ubuntu ESP
(`/EFI/ubuntu/shimx64.efi`) on any disk in the box. If one exists it boots that
instead of the installer, so a stick left in a machine whose firmware prefers USB
cannot wipe a freshly built box. A fresh box has no such ESP and installs
automatically. To re-image deliberately, pick the install entry from the menu
within 10 s. Prefer the firmware's one-time boot menu over reordering the boot
devices.

**Bootloader on both partitions.** The extracted ISO lives on a FAT32 partition
typed as basic data, which some firmware refuses to boot from, so the bootloader
is mirrored onto the stick's real ESP. That mirror gets its own
`boot/grub/grub.cfg` (`templates/grub-esp-shim.cfg`) which hands control to
`EFIBOOT` — without it, firmware that picks the ESP lands at a grub rescue
prompt.

**Activation runs at most once.** `kela-firstboot.service` retries every 20 s
while waiting for `FOBDATA` to appear. But `activate.sh` itself is fenced behind
`/var/lib/kela/.activation-attempted`, because every run consumes a CA slot key
on the stick. A failure there stops and says so rather than looping and burning
slots; clear the marker to retry.

**Cold power cycle, then a recovery ladder.** The stick not being detected after
the install is the failure mode this project has actually hit in the field, so it
gets two independent answers. The install ends in `poweroff` with an RTC alarm
armed as the very last late-command, because a cold start is what reliably
re-enumerates the stick — a warm reboot has been seen to leave it invisible until
physically replugged. Behind that, `kela-activate.sh` escalates through
`udevadm trigger`, rebinding the `usb-storage`/`uas` drivers, and finally
rebinding the xHCI host controllers, then keeps retrying every 20 s while a udev
rule starts activation the instant a `FOBDATA` device appears. So: firmware
honours the alarm and it is fully unattended; firmware ignores it and it costs one
power press with no prompt to answer; stick still wedged and the ladder gets it;
everything fails and a replug starts activation immediately.

Note that rebinding a host controller does not cut port power, so a stick wedged
by the installer's SCSI eject may need the real power cycle. That is the reason
the poweroff is the primary mechanism and the ladder is the backup, not the other
way round.

**Failures say which failure they are.** When the drive never turns up, the log
distinguishes "no block device labelled FOBDATA" (an enumeration problem — only a
replug or power cycle helps) from "present but will not mount" (a filesystem or
driver problem — rebinding will never help), and dumps `lsblk` or the tail of
`dmesg` accordingly. Whether the RTC alarm was armed is recorded during the
install and echoed into the first-boot log, so a box that never woke explains
itself.

**Other reliability details.** Hostname arrives as a `kela.hostname=` kernel arg
and is normalised to a valid DNS label before use. nouveau is blacklisted at
install time so NVIDIA binds cleanly on first boot with no console spam and no
extra reboot. A bench fallback default route lets k3s start with no LAN (any real
LAN route wins on metric). apt is left in a working state afterwards: the offline
file repo is removed and `ubuntu.sources` restored once activation succeeds.
Scanner-mangled filenames (`.gz` appended, `%`→`_`) are repaired on the copy and
re-verified against the signed manifest, and the builder refuses to finish if
verification fails.

## Known open design question

Activation needs the stick at first boot because `activate.sh` hands the bundle
directory to `kela-node-controller activate`, and consuming a slot means deleting
`pki/slots/<n>/cluster-ca.key` from the directory it was given. Staging the bundle
onto the target disk during the install — while the stick is provably healthy,
which would remove the USB dependency at first boot entirely — is therefore only
safe if exactly one slot key is staged and that key is deleted from the stick in
the same step. That depends on how `kela-node-controller` picks a slot: safe if it
takes the lowest-numbered key present, broken if it expects the full set. Worth
confirming with whoever owns that binary, because it is the only route to a first
boot that touches no USB device at all.

## Still to verify on target hardware

- **`read` under Secure Boot.** The hostname prompt needs GRUB's `read` module.
  If a signed monolithic grub lacks it the command errors, `KELA_HOSTNAME` stays
  empty, and every box silently installs as `kela-fob`. Check the prompt actually
  appears on a Secure Boot machine; if not, swap the prompt for a set of
  pre-defined hostname menu entries, which needs no module.
- **NVIDIA under Secure Boot.** If the bundle builds its driver via DKMS, the
  module is unsigned and MOK enrolment is interactive — which breaks the
  unattended flow. Confirm how the bundle ships the driver.
- **ESP selection.** Confirm which of the stick's two FAT partitions the target
  firmware actually boots, so the ESP hand-off path gets exercised at least once.
