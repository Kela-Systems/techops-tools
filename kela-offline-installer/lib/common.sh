# shellcheck shell=bash
# Shared plumbing for make-usb-macos.sh and update-usb-macos.sh, so the seed
# and the GRUB block have exactly one definition. Sourced, not executed.

KITDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIBDIR="$KITDIR/lib"
TPLDIR="$KITDIR/templates"

die() { echo "error: $*" >&2; exit 1; }

require_root() { [ "$(id -u)" -eq 0 ] || die "run with sudo"; }

# Password for the 'kela' user on installed boxes. Single-quoted on purpose: the
# $ signs are literal crypt field separators, not expansions.
# shellcheck disable=SC2016
DEFAULT_HASH='$6$Ovc4PDuUv7OQDwBv$JUN9rYxo.KqFbqD5pCtSk4wlX4inRgopLc/8mCGGRqaqa51hKdgXqBjcWavJBl2cjLYgXyWSvnZNbT/EZmz4L1'
PASSWORD_HASH=""
PASSWORD_SOURCE=""

# Resolve the password before anything destructive happens. A set-but-invalid
# KELA_PASSWORD_HASH is fatal rather than a fallback: `openssl passwd -6` fails
# on macOS LibreSSL and yields an empty string, which would otherwise ship a
# production stick with the default password and no complaint.
resolve_password() {
  local pw="" pw2=""
  if [ -n "${KELA_PASSWORD_HASH+set}" ]; then
    # shellcheck disable=SC2016  # the pattern is a literal $6$salt$hash
    case "$KELA_PASSWORD_HASH" in
      '$6$'?*'$'?*)
        PASSWORD_HASH="$KELA_PASSWORD_HASH"
        PASSWORD_SOURCE="KELA_PASSWORD_HASH"
        return 0
        ;;
      *)
        die "KELA_PASSWORD_HASH is set but is not a SHA-512 crypt hash.
  got: '$KELA_PASSWORD_HASH'
  On macOS /usr/bin/openssl is LibreSSL and has no 'passwd -6', so that recipe
  produces an empty string. Generate a usable hash with:
      $LIBDIR/mkpasswd.sh"
        ;;
    esac
  fi
  printf 'Password for the kela user on the installed boxes.\n' >&2
  printf 'Press Enter alone to accept the built-in default (kela): ' >&2
  IFS= read -rs pw || true; echo >&2
  if [ -z "$pw" ]; then
    PASSWORD_HASH="$DEFAULT_HASH"
    PASSWORD_SOURCE="built-in default — plaintext 'kela', NOT for production"
    return 0
  fi
  printf 'Again: ' >&2
  IFS= read -rs pw2 || true; echo >&2
  [ "$pw" = "$pw2" ] || die "passwords do not match"
  PASSWORD_HASH=$(printf '%s\n' "$pw" | "$LIBDIR/mkpasswd.sh" --stdin) \
    || die "could not hash the password"
  PASSWORD_SOURCE="entered at the prompt"
}

human() { python3 -c 'import sys;n=int(sys.argv[1]);print(f"{n/1e9:.1f} GB")' "$1"; }

# Report what the bundle actually contains, so a shape the first-boot script
# cannot work with is caught here rather than on the box — where the one-shot
# activation guard turns any surprise into a trip back to the bench. Fatal only
# for what is genuinely unusable; the rest is reported so the operator can
# judge, since bundle layout varies between Kela releases.
check_bundle() {
  local src="$1" k="$1/02-kela" nrepo="" restore_nullglob
  [ -d "$k" ] || die "no 02-kela directory under $src — is this a Kela bundle?"
  [ -f "$k/activate.sh" ] || die "$k/activate.sh missing — nothing to activate"

  restore_nullglob=$(shopt -p nullglob)
  shopt -s nullglob
  local debs=("$k"/debs/*.deb) indexes=("$k"/apt/Packages*) suites=() d
  echo "  bundle debs: ${#debs[@]}"

  if [ "${#indexes[@]}" -gt 0 ]; then
    # One 'Package:' line per stanza. grep exits 1 on zero matches, which is
    # information, not an error, hence the || true.
    nrepo=$(grep -ch '^Package:' "$k"/apt/Packages 2>/dev/null || true)
    [ -n "$nrepo" ] || nrepo="?"
    echo "  apt repo: flat, $nrepo packages — dependencies resolve from the drive"
  elif [ -d "$k/apt/dists" ]; then
    for d in "$k"/apt/dists/*/; do suites+=("$(basename "$d")"); done
    echo "  apt repo: suite-based (${suites[*]}) — NOT wired up; first boot will"
    echo "    install the debs with dpkg and no dependency resolution"
  else
    echo "  apt repo: none — the debs must be self-contained against a stock"
    echo "    Ubuntu 24.04 server install, or activation stops at the first unmet"
    echo "    dependency. That is the failure extra-debs/ exists to patch."
  fi
  [ "${#debs[@]}" -gt 0 ] \
    || echo "  note: 02-kela/debs is empty — activate.sh must install Kela itself"
  eval "$restore_nullglob"
}

# extra-debs/ rides the seed partition and first boot installs it with `dpkg -i`,
# which will cheerfully half-upgrade the base system. A set collected the obvious
# way — `apt-get install --download-only usbguard` in an `ubuntu:24.04` container
# — did exactly that: it shipped a newer libpolkit-gobject-1-0 without polkit's
# other binaries, whose `Depends: ... (= old version)` then could not be met, and
# every apt call on the box failed from that point on. The box has no network to
# repair itself with, so this has to be caught here.
# `baseline` is either the ISO file (make-usb-macos.sh has one) or a casper
# directory (update-usb-macos.sh has the ISO already extracted onto EFIBOOT).
# `dir` is the directory whose .debs get checked: extra-debs/ during preflight,
# and the seed partition after the copy, because the partition is what actually
# ships and may be carrying debs from an earlier build.
check_extra_debs() {
  local baseline="$1" dir="${2:-$KITDIR/extra-debs}" flag="--baseline"
  if ! ls "$dir"/*.deb >/dev/null 2>&1; then
    echo "  no sideloaded debs in $dir — boxes get NO USB device policy"
    return 0
  fi
  [ -f "$baseline" ] && flag="--iso"
  if [ ! -e "$baseline" ]; then
    echo "  WARNING: no $baseline, cannot verify sideloaded debs against the install"
    return 0
  fi
  # Captured rather than piped: the exit status is the whole point, and in a
  # pipeline it would be sed's.
  local out rc=0
  out=$(python3 "$LIBDIR/collect-extra-debs.py" "$flag" "$baseline" \
          --verify "$dir" 2>&1) || rc=$?
  printf '%s\n' "$out" | sed 's/^/  /'
  [ "$rc" -eq 0 ] || die "the sideloaded debs in $dir would break apt on every box
  built from this stick. Recollect the set with:
      python3 $LIBDIR/collect-extra-debs.py usbguard --iso <ubuntu ISO> --out '$KITDIR/extra-debs'"
}

# Whole-disk identifier (e.g. disk4) backing any path, empty if it is not on a
# diskutil-managed volume. Goes via df because diskutil only accepts mount points
# and device nodes, not arbitrary paths, and the device node from df is the one
# field guaranteed free of spaces. plistlib.loads, not load: load() seeks, and a
# pipe is not seekable.
whole_disk_of() {
  local node
  node=$(df -P "$1" 2>/dev/null | awk 'NR==2{print $1}')
  case "$node" in
    /dev/*) ;;
    *) return 0 ;;
  esac
  diskutil info -plist "$node" 2>/dev/null | python3 -c '
import plistlib, sys
raw = sys.stdin.buffer.read()
print(plistlib.loads(raw).get("ParentWholeDisk") or "" if raw.strip() else "")
'
}

# Render the autoinstall seed onto a mounted CIDATA partition, and carry any
# extra .deb files that the bundle's own dependency closure is missing.
write_seed() {
  local dest="$1" baseline="${2:-}"
  [ -d "$dest" ] || die "seed partition not mounted at $dest"
  [ -n "$PASSWORD_HASH" ] || die "resolve_password must run before write_seed"
  echo "  kela password from: $PASSWORD_SOURCE"
  echo 'instance-id: kela-fob' >"$dest/meta-data"
  python3 "$LIBDIR/render.py" user-data.tmpl "PASSWORD_HASH=$PASSWORD_HASH" \
    >"$dest/user-data" || die "rendering user-data failed"

  # CIDATA is FAT and cannot hold extended attributes, so a plain `cp` from a
  # macOS filesystem leaves an AppleDouble stub named `._<file>` beside every
  # deb. Those stubs are not packages, and they match `*.deb`: first boot's
  # `dpkg -i "$SEED"/*.deb` picks them up and errors on each one. Copy with -X
  # so they are never created, and sweep any left by an earlier build.
  local junk
  for junk in "$dest"/._*; do
    [ -e "$junk" ] || continue
    rm -f "$junk"
  done

  # Mirror extra-debs/, do not merge into it. Keeping whatever a previous build
  # left behind meant a refreshed stick shipped the union of both sets: the
  # correction landed next to the debs it was correcting, and first boot's
  # `dpkg -i *.deb` installed the lot. That is how a stick "updated" to fix a
  # half-upgraded polkit would still have carried the deb that caused it.
  local deb keep=0
  for deb in "$dest"/*.deb; do
    [ -e "$deb" ] || continue
    if [ -e "$KITDIR/extra-debs/$(basename "$deb")" ]; then
      keep=$((keep + 1))
    else
      echo "  removing stale deb from the seed partition: $(basename "$deb")"
      rm -f "$deb"
    fi
  done
  [ "$keep" -eq 0 ] || echo "  $keep deb(s) already current on the seed partition"
  if ls "$KITDIR"/extra-debs/*.deb >/dev/null 2>&1; then
    cp -X "$KITDIR"/extra-debs/*.deb "$dest/"
  fi

  if ls "$dest"/*.deb >/dev/null 2>&1; then
    echo "  sideloaded debs on the seed partition:"
    for deb in "$dest"/*.deb; do echo "    $(basename "$deb")"; done
  else
    echo "  sideloaded debs: none"
  fi
  # usbguard is a permanent resident of extra-debs/, not a bundle dependency:
  # first boot installs it and activation enables the USB device policy.
  if ! ls "$dest"/usbguard_*.deb >/dev/null 2>&1; then
    echo "  WARNING: no usbguard deb on the seed partition. Boxes built from this"
    echo "  stick will have NO USB device policy. The debs belong permanently in"
    echo "  extra-debs/ — see extra-debs/README.md for how to collect the set."
  fi
  # The partition, not the kit directory: this is the set the box will install.
  # An `if`, not a `&&` tail: a false test as the last statement would return
  # non-zero and abort the caller under `set -e`.
  if [ -n "$baseline" ]; then
    check_extra_debs "$baseline" "$dest"
  fi
}

# Append (or replace) the managed Kela block in the ISO's own grub.cfg.
write_grub_block() {
  local grubcfg="$1"
  [ -f "$grubcfg" ] || die "grub.cfg not found at $grubcfg"
  python3 "$LIBDIR/graft-grub.py" "$grubcfg" "$TPLDIR/grub-kela.cfg"
}

# Mirror the bootloader onto the stick's real ESP for firmware that only looks
# there, together with a config that hands control back to EFIBOOT.
write_esp_shim() {
  local disk="$1" bootmp="$2" espmp=""
  diskutil mount "${disk}s1" >/dev/null 2>&1 || {
    echo "  note: could not mount ${disk}s1 — skipping the ESP mirror"
    return 0
  }
  espmp=$(diskutil info "${disk}s1" | awk -F': *' '/Mount Point/{print $2}')
  if [ -z "$espmp" ] || [ ! -d "$espmp" ]; then
    echo "  note: ${disk}s1 has no mount point — skipping the ESP mirror"
    return 0
  fi
  rm -rf "${espmp:?}/EFI" "${espmp:?}/boot"
  cp -RX "$bootmp/EFI" "$espmp/" || die "copying EFI/ to the ESP failed"
  mkdir -p "$espmp/boot/grub"
  cp "$TPLDIR/grub-esp-shim.cfg" "$espmp/boot/grub/grub.cfg"
  diskutil unmount "${disk}s1" >/dev/null 2>&1 || true
  echo "  ESP mirror written (bootloader + hand-off config)"
}
