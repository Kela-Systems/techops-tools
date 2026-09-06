# extra-debs — sideloaded packages

Drop `.deb` files here. The builder copies them onto the seed partition
(`CIDATA`) and `kela-activate.sh` installs them with `dpkg -i` before the bundle
debs and before `activate.sh` runs.

## The one rule

**A deb in here may add a package. It must never change the version of one the
Ubuntu ISO already installs.**

`dpkg -i` has no solver. Hand it a newer copy of something the base install
already has and it installs it, whether or not the rest of that package's family
comes along — and members of one source package are routinely locked to each
other with `Depends: ... (= exact version)`. Break one of those pairs and every
`apt` invocation on the box fails with `E: Unmet dependencies` forever, because
first boot moves `ubuntu.sources` out of the way and the box has no network to
repair itself from. Activation dies at the bundle install with exit 100, and the
one-shot guard means it does not get a second try.

That is not hypothetical. It is what shipped, and it stranded a box.

## Collecting the set

Use the tool. It resolves against the ISO's own package manifest — the actual
list of what an `ubuntu-server` install puts on disk — so it can tell the
difference between "the target is missing this" and "the target already has
something new enough":

```sh
python3 ../lib/collect-extra-debs.py usbguard \
  --iso "/path/to/ubuntu-24.04.4-live-server-amd64.iso" \
  --out .
```

It refuses to write a set that would upgrade the base install or that has an
unsatisfied dependency, and it drops same-version reinstalls (which do nothing
except re-run maintainer scripts on a booting box). Add `--check-only` to see
the plan without downloading. To audit whatever is already sitting here — no
network needed, which is what the offline Linux runbook uses:

```sh
python3 ../lib/collect-extra-debs.py --iso "/path/to/....iso" --verify .
```

`make-usb-macos.sh` runs that same check during preflight and aborts the build
if it fails, so a bad set cannot reach a stick.

### Also check against the bundle

The debs here are only half of what first boot installs. `kela-activate.sh`
runs two package transactions in order: `dpkg -i` of this directory, then
`apt-get install` of `02-kela/debs` resolved against the bundle's own
`02-kela/apt` repo. Verifying against the ISO says nothing about the second
one, so add `--bundle` to check that the bundle still resolves *on top of* the
sideloads:

```sh
python3 ../lib/collect-extra-debs.py --baseline /Volumes/EFIBOOT/casper \
  --verify . --bundle /path/to/bundle/02-kela
```

That reports a bundle package the sideloads make unsatisfiable, a
`Conflicts`/`Breaks` between the two sets, an unreadable deb in the bundle, and
an upgrade that would strand a same-source sibling. Both builders pass
`--bundle` automatically. `--baseline` accepts a built stick's `EFIBOOT/casper`
directory as well as an ISO, so a stick already in hand can be checked as it
is — which is the useful thing to do before walking one to a box.

### Do not use the old container recipe

`apt-get install --download-only usbguard` inside `ubuntu:24.04` is how the
broken set was produced, and it will produce another one. The container is not
the target: it already had polkit and dbus installed, so the resolver quietly
downloaded a newer `libpolkit-gobject-1-0` and *none* of polkit's other
binaries. On the box that landed as a half-upgraded polkit — `polkitd` and
`libpolkit-agent-1-0` still pinned to `124-2ubuntu1.24.04.2`, the library moved
to `.3`. Recommends made it worse: they pulled in `usbguard-dbus`, and with it
the whole dbus/glib/polkit stack, 28 debs where 5 were needed. Eighteen of the
28 were same-version reinstalls of packages the base install already had, and
one of them was `dbus`, whose postinst re-ran on a live booting system.

## usbguard (permanent resident)

This directory is **not expected to be empty**: it permanently carries
`usbguard` and the packages the server install genuinely lacks. USBGuard is a
required part of the installed system — first boot installs it (masked), and
activation enables it with a policy that allows only HID devices (keyboards,
mice, joysticks) and hubs. See the USBGuard section in `OFFLINE-RUNBOOK.md` for
the enablement flow and its consequences for replugging FOB drives.

The current set, against **Ubuntu 24.04.4 server amd64**, is five packages:

| package            | why                                      |
| ------------------ | ---------------------------------------- |
| `usbguard`         | the daemon and CLI                       |
| `libusbguard1`     | its library                              |
| `libprotobuf32t64` | `libusbguard1` needs `libprotobuf32`     |
| `libqb100`         | `libusbguard1` needs it                  |
| `libumockdev0`     | `libusbguard1` needs it                  |

Everything else `usbguard` declares — `dbus`, `libglib2.0-0t64`,
`libpolkit-gobject-1-0`, `libaudit1`, `libcap-ng0`, `libseccomp2`, `libstdc++6`
— the server install already carries at a version that satisfies the constraint.
Shipping newer copies is what caused the incident above.

Re-run the collector after an ISO or release bump, and
**do not remove the usbguard debs when clearing bundle-specific sideloads.**

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
explicitly. Collect them with the same tool, which applies the same rule:

```sh
python3 ../lib/collect-extra-debs.py usbguard <missing-package> \
  --iso "/path/to/ubuntu-24.04.4-live-server-amd64.iso" --out .
```

Pass every package you want in one go — the whole directory is one `dpkg -i`,
so it has to be consistent as a set. Record which bundle version needed what.
