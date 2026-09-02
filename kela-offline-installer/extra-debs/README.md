# extra-debs — sideloaded packages

Drop `.deb` files here. The builder copies them onto the seed partition
(`CIDATA`) and `kela-activate.sh` installs them with `dpkg -i` before the bundle
debs and before `activate.sh` runs.

## usbguard (permanent resident)

This directory is **no longer expected to be empty**: it permanently carries
`usbguard` and its dependency closure. USBGuard is a required part of the
installed system — first boot installs it (masked), and activation enables it
with a policy that allows only HID devices (keyboards, mice, joysticks) and
hubs. See the USBGuard section in `OFFLINE-RUNBOOK.md` for the enablement flow
and its consequences for replugging FOB drives.

The current set was collected against **Ubuntu 24.04 (noble) amd64,
noble-updates as of 2026-09-02**, by resolving `apt-get install --download-only
usbguard` in a pristine `ubuntu:24.04` container. That closure is a superset of
what a stock server install actually misses; the extras are harmless — `dpkg -i`
reinstalls or upgrades, and the archive versions are never older than the
24.04.4 ISO squashfs. To refresh after an ISO/release bump:

```sh
docker run --rm --platform=linux/amd64 -v "$PWD":/out ubuntu:24.04 bash -c \
  'apt-get update -q && apt-get install --download-only -y usbguard \
   && cp /var/cache/apt/archives/*.deb /out/'
```

**Do not remove the usbguard debs when clearing bundle-specific sideloads.**

## Bundle dependency escape hatch

The directory's original purpose still applies: a bundle's own dependency
closure can be incomplete. The box is offline, the base install carries only
what the ISO's squashfs has, and anything missing from both makes `activate.sh`
fail after Ubuntu has installed perfectly well. Bundle `v2.6.0-rc.19` needed
the `tss2` packages this way (TEC-709/TEC-710).

A bundle that ships its own `02-kela/apt` repo, or whose `02-kela/debs` are
self-contained against a stock Ubuntu 24.04 server install, needs nothing here
beyond the usbguard set. The builder prints what the bundle carries during
preflight (`apt repo: flat, N packages` resolves dependencies off the drive;
`apt repo: none` means the debs are on their own), so you can see which case
you are in before committing to a build.

Reach for extra bundle-dependency sideloads only after an activation has
actually failed on an unmet dependency, which the first-boot log names
explicitly. To collect the missing packages, on a networked Ubuntu 24.04 box
matching the target release:

```sh
mkdir -p /tmp/closure && cd /tmp/closure
apt-get download <missing-package> $(apt-cache depends --recurse --no-recommends \
  --no-suggests --no-conflicts --no-breaks --no-replaces --no-enhances \
  <missing-package> | grep '^\w' | sort -u)
```

Copy the results here and record which bundle version needed them.
