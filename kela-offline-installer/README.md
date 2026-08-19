# Kela offline installer

Build a USB stick that takes a bare amd64 box to a converged Kela system with
**zero network and one prompt** (the hostname, at boot):

boot stick → GRUB asks hostname (Enter = `kela-fob`) → wipes the **smallest**
internal disk → installs Ubuntu Server 24.04 → powers off and cold-starts itself
about five minutes later → first boot installs the bundle debs and runs Kela
activation → daemon converges.

**Operator note:** leave the stick in. The box shuts down on purpose (see below)
and an RTC alarm is meant to restart it. **Pressing the power button once the
screen has gone dark is always safe** and skips the wait — it boots the system
just installed, not the installer. So there is no need to time anything: press it
whenever you are ready, or leave the box to wake itself. The first-boot log states
which of the two happened, and if the alarm failed, why.

The outcome is printed at the console login prompt, so the operator never has to
log in to find out whether it worked — and it reports the daemon's convergence,
not merely that activation returned. Full log: `/var/log/kela-firstboot.log` on
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


| Path                  | Purpose                                                                                                                                                                                    |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `make-usb-macos.sh`   | **Primary builder.** `sudo ./make-usb-macos.sh disk4 "/path/to/bundle"`. Erases the target; refuses to run if the source lives on that disk. Needs `brew install xorriso`; stick ≥ 24 GB.  |
| `stage-bundle.sh`     | Copy a received drive to local storage and verify it, so that same stick can then be erased and rebuilt. No sudo; reads the drive only.                                                    |
| `update-usb-macos.sh` | Re-apply the current boot machinery to an already-built stick **in place** (never touches the data partition — safe when the stick holds the only copy).                                   |
| `templates/`          | The single source of truth: autoinstall seed, GRUB block, and every script the target runs. Edit here, never on a stick.                                                                   |
| `lib/`                | Rendering and verification helpers, shared by both builders and the runbook. `lib/mkpasswd.sh` generates the password hash on hosts where `openssl passwd -6` is unavailable (i.e. macOS). |
| `extra-debs/`         | Sideloaded `.deb`s, for the rare bundle whose dependency closure is incomplete. Normally empty — see `extra-debs/README.md`.                                                               |
| `OFFLINE-RUNBOOK.md`  | The same build, by hand, on an offline Linux box. Uses the same templates.                                                                                                                 |




## Before building for production

- Set a real password. The builder prompts for one; pressing Enter alone keeps
the built-in default (`kela`/`kela`) and says so in the build output. To supply
it non-interactively:

```bash
sudo KELA_PASSWORD_HASH="$(./lib/mkpasswd.sh)" ./make-usb-macos.sh disk4 "/path/to/FOB security drive"
```

  `openssl passwd -6` **does not work on macOS.** `/usr/bin/openssl` is LibreSSL,
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

**A slot can be preserved, opt-in per stick.** Each `pki/slots/<n>/cluster-ca.key`
on the drive is a distinct pre-minted cluster CA, and activation destroys the key
it uses — that budget is what bounds how many clusters a lost drive could stand up,
and the leftover certificate keeps the spend auditable. Running out is terminal:
the controller's own message is "all *N* CA slots on this drive have been consumed
— build a new bundle to provision another cluster."

For lab boxes that get rebuilt, `kela-node-controller activate` accepts
`--keep-slot-key`, which it documents as "Leave the consumed slot's key on the
drive (e.g. a read-only medium). The drive then still holds a usable cluster-CA
key." Activation is otherwise completely normal — the bundle stages, the daemon
converges, the box is genuinely provisioned — so this is not a dry run, just one
that costs nothing.

Create an empty file named `kela-keep-slot-key` on the `CIDATA` partition to turn
it on. `CIDATA` is FAT, so it can be added or removed on any laptop without
rebuilding the stick. It is deliberately off by default: **a drive that travels to
a site must spend its slot**, because a drive kept this way still carries live CA
keys, and two boxes activated from one slot share a cluster CA — precisely what
one-time-use prevents.

Two guards around it. First boot asks the binary whether it really supports the
flag (`activate --help`) rather than assuming; if it doesn't, activation *stops*
before `activate.sh` runs — nothing is consumed, the one-shot marker is cleared so
a retry stays possible, and the console says `STOPPED`, because silently spending a
slot you asked to keep is the one unrecoverable outcome. Afterwards the key files
are counted again, and the log reports `N before, M after`, so a flag that is
accepted but ineffective shows up as a warning instead of a surprise.

`kela-offline.list` **belongs to the node controller, not to us.**
`kela-node-controller activate` stages the bundle under `/var/lib/kela/offline`
and writes `/etc/apt/sources.list.d/kela-offline.list` pointing at it; its daemon
installs from that source while converging (chrony for `time`, the NVIDIA
container toolkit). This kit uses `kela-installer.list` for its own temporary
source and must never create or delete the controller's. Sharing the name once
meant our post-activation cleanup deleted it, and convergence then failed on
exactly those two components while the console still read `COMPLETE`.

**The console reports convergence, not activation.** `activate.sh` returns as
soon as the bundle is staged and the slot is spent; the daemon converges
afterwards, and that is where the outcome is really decided. `kela-activate.sh`
therefore waits on the daemon's journal and reports what it finds there.
`COMPLETE` means converged.

**A `converge failed` line is not a verdict.** The daemon works in passes and
retries forever, and a component can fail on one pass and succeed on the next —
`time` did exactly that on two separate boxes. So only `system converged` ends the
wait. Failures are counted and printed as progress (`converge attempt failed
(1 so far)`), and nothing is called `FAILED` until `KELA_CONVERGE_TIMEOUT`
(default 90 min) expires with no success; a run that stumbled and recovered
reports `system converged, after 1 failed attempt(s)`. An earlier version returned
on the first failure line and declared `FAILED` nine seconds into a convergence
that was still running.

Because that wait can legitimately last the better part of an hour, the log is
copied to `99-install-logs/` on the stick as soon as activation finishes, again at
the verdict, and once more if activation dies outright — pulling the drive early
no longer takes away the only record.

**Cold power cycle, then a recovery ladder.** The stick not being detected after
the install is the failure mode this project has actually hit in the field, so it
gets two independent answers. The install ends in `poweroff` with an RTC alarm
armed as the very last late-command, because a cold start is what reliably
re-enumerates the stick — a warm reboot has been seen to leave it invisible until
physically replugged.

That alarm is *refreshed*, not set once. The window is measured from the
late-command while the box powers off an unknown time later, and an alarm that
fires before that is consumed for nothing. So `late-rtcwake.sh` leaves an orphaned
loop pushing the alarm +300 s every 60 s, which dies with the poweroff — the box
then wakes about five minutes after the *real* shutdown however long finalisation
took, and the duration stops being something anyone has to know. If that loop is
killed early the single alarm still stands, so the worst case is the fixed window
it replaced, never less. `/var/lib/kela/rtc-armed` records which one you got.

**Wait the full five minutes before deciding it failed.** Both wake failures
reported so far were misreadings. This hardware honours the alarm: one run woke
itself 74 s after it. The run reported as "didn't wake" had been powered on by hand
69 s *before* its alarm was due, so the alarm never got the chance. Pressing power
early is safe and skips the wait, but it also destroys the evidence — the log can
tell you the button was pressed early (`rtc-armed` versus boot time) and does.

What the log cannot tell you is why a box that genuinely never wakes did not:
subiquity snapshots `/var/log/installer` into the target *before* the late-command
runs, so nothing on the disk records when the box actually powered off, and an
expired alarm cannot be told apart from firmware that ignores alarms.

Behind all that, `kela-activate.sh` escalates through
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

- `read` **under Secure Boot.** The hostname prompt needs GRUB's `read` module.
If a signed monolithic grub lacks it the command errors, `KELA_HOSTNAME` stays
empty, and every box silently installs as `kela-fob`. Check the prompt actually
appears on a Secure Boot machine; if not, swap the prompt for a set of
pre-defined hostname menu entries, which needs no module.
- **NVIDIA under Secure Boot.** If the bundle builds its driver via DKMS, the
module is unsigned and MOK enrolment is interactive — which breaks the
unattended flow. Confirm how the bundle ships the driver.
- **ESP selection.** Confirm which of the stick's two FAT partitions the target
firmware actually boots, so the ESP hand-off path gets exercised at least once.

