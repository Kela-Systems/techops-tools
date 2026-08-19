#!/usr/bin/env python3
"""Graft the managed Kela block onto the Ubuntu ISO's own grub.cfg.

Idempotent, including against blocks written by earlier versions of this kit.
Splitting on the marker alone was not enough: builds before the marker existed
appended an unmarked entry, which then survived into the head and was stacked
under a second copy of the managed block. The result was two identically named
"Install Ubuntu + Kela" entries sharing the id kela-auto — and because the
autoinstall un-injection below is applied to the head, the surviving legacy
entry lost its `autoinstall` argument and would have dropped the operator into
the interactive installer.

Everything this kit writes is appended at the tail, so truncating at the
earliest Kela sentinel reliably recovers the ISO's own config.

Also undoes the blanket `autoinstall` injection that earlier builds applied to
the ISO's own entries, keeping them usable as a manual-install fallback.

Usage:
  graft-grub.py /Volumes/EFIBOOT/boot/grub/grub.cfg templates/grub-kela.cfg
"""

import pathlib
import sys

MARKER = "# >>> kela-offline-installer"

# Anything this kit has ever appended starts at one of these. Ordered by nothing
# in particular; the earliest match in the file is the cut point.
SENTINELS = (
    MARKER,
    'menuentry "Install Ubuntu + Kela',
    "--id kela-auto",
    "--id kela-boot-installed",
    "set default=kela",
    "--set=kela_installed",
)

MENUENTRY = 'menuentry "Install Ubuntu + Kela'


def strip_managed(text):
    """The ISO's own config, with every generation of our block removed."""
    cuts = [text.index(s) for s in SENTINELS if s in text]
    return text[: min(cuts)] if cuts else text


def main(argv):
    if len(argv) != 3:
        sys.exit(__doc__)
    cfg, block = pathlib.Path(argv[1]), pathlib.Path(argv[2])
    original = cfg.read_text(errors="surrogateescape")

    head = strip_managed(original)
    legacy = original.count(MENUENTRY)
    if legacy:
        print(f"  removed {legacy} existing managed entr{'y' if legacy == 1 else 'ies'}")

    head = head.replace(" autoinstall ---", " ---")
    result = head.rstrip("\n") + "\n\n" + block.read_text()
    cfg.write_text(result, errors="surrogateescape")

    # A duplicate here is what this script exists to prevent, and it is cheap to
    # prove rather than assume.
    count = result.count(MENUENTRY)
    if count != 1:
        sys.exit(f"error: grub.cfg ended up with {count} managed entries, expected 1")
    if " autoinstall ---" not in result:
        sys.exit("error: the managed entry lost its autoinstall argument")


if __name__ == "__main__":
    main(sys.argv)
