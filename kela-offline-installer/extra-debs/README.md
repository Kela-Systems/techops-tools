# extra-debs — sideloaded dependencies

Drop `.deb` files here. The builder copies them onto the seed partition
(`CIDATA`) and `kela-activate.sh` installs them with `dpkg -i` before the bundle
debs and before `activate.sh` runs.

This exists because a bundle's own dependency closure can be incomplete: the
box is offline, the base install carries only what the ISO's squashfs has, and
anything missing from both makes `activate.sh` fail after Ubuntu has installed
perfectly well. Bundle `v2.6.0-rc.19` needed the `tss2` packages this way
(TEC-709/TEC-710).

**Empty is the normal state.** A bundle that ships its own `02-kela/apt` repo,
or whose `02-kela/debs` are self-contained against a stock Ubuntu 24.04 server
install, needs nothing here — leave the directory empty and build. The builder
prints what the bundle carries during preflight (`apt repo: flat, N packages`
resolves dependencies off the drive; `apt repo: none` means the debs are on
their own), so you can see which case you are in before committing to a build.

Reach for this directory only after an activation has actually failed on an
unmet dependency, which the first-boot log names explicitly. To collect the
missing packages, on a networked Ubuntu 24.04 box matching the target release:

```sh
mkdir -p /tmp/closure && cd /tmp/closure
apt-get download <missing-package> $(apt-cache depends --recurse --no-recommends \
  --no-suggests --no-conflicts --no-breaks --no-replaces --no-enhances \
  <missing-package> | grep '^\w' | sort -u)
```

Copy the results here and record which bundle version needed them.
