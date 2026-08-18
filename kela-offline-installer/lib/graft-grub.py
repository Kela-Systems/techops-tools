#!/usr/bin/env python3
"""Graft the managed Kela block onto the Ubuntu ISO's own grub.cfg.

Idempotent: an existing managed block is replaced rather than stacked, so
running the updater twice leaves one copy. Also undoes the blanket `autoinstall`
injection that earlier builds applied to the ISO's own entries, keeping them
usable as a manual-install fallback.

Usage:
  graft-grub.py /Volumes/EFIBOOT/boot/grub/grub.cfg templates/grub-kela.cfg
"""

import pathlib
import sys

MARKER = "# >>> kela-offline-installer"


def main(argv):
    if len(argv) != 3:
        sys.exit(__doc__)
    cfg, block = pathlib.Path(argv[1]), pathlib.Path(argv[2])
    head = cfg.read_text(errors="surrogateescape").split(MARKER)[0]
    head = head.replace(" autoinstall ---", " ---")
    cfg.write_text(
        head.rstrip("\n") + "\n\n" + block.read_text(), errors="surrogateescape"
    )


if __name__ == "__main__":
    main(sys.argv)
