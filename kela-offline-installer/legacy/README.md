# Legacy builder — do not use

Kept only as a reference for how the drive used to be built. Everything here
predates the reliability work in `../templates/` and will produce a stick that
misbehaves in the field:

- `make-bootable.sh` + `grub.cfg` — GRUB-loopback variant: it installs its own
  GRUB and loopback-boots the ISO from the data partition instead of extracting
  it to a FAT partition. Needs a Linux host with `grub-install`, and has no
  hostname prompt, no re-imaging guard and no ESP hand-off config.
- `autoinstall/user-data` — the original seed. Its first-boot script does not
  install the bundle debs, does not survive a stick that is slow to
  re-enumerate, has no guard against re-running activation (which burns a CA
  slot key per attempt), and blacklists nothing, so nouveau fights the NVIDIA
  driver.

Use `../make-usb-macos.sh` to build, `../update-usb-macos.sh` to refresh an
existing stick, or `../OFFLINE-RUNBOOK.md` if you have nothing but an offline
Linux box.
