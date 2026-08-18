#!/usr/bin/env python3
"""Verify the stick's copy of the bundle against the signed manifest.

Exit status is the point of this script: the builder relies on it to refuse to
ship a stick with a bad copy. A missing pki/slots/*/cluster-ca.key is expected
(those are consumed as boxes get provisioned) and only warned about; anything
else — a hash mismatch or any other missing file — fails.

Run with the bundle directory (the one holding MANIFEST.json) as the cwd.
"""

import hashlib
import json
import sys

CONSUMABLE = "pki/slots/"


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    with open("MANIFEST.json") as fh:
        files = json.load(fh)["files"]
    bad, consumed = [], []
    for entry in files:
        path = entry["path"]
        try:
            if sha256(path) != entry["sha256"]:
                bad.append(f"MISMATCH {path}")
        except FileNotFoundError:
            (consumed if path.startswith(CONSUMABLE) else bad).append(f"MISSING {path}")
    for line in consumed:
        print(f"  (expected) {line}")
    for line in bad:
        print(f"  {line}")
    print(f"checked {len(files)} files, {len(consumed)} consumed slot keys absent")
    if bad:
        print(f"FAILED: {len(bad)} file(s) do not match the manifest")
        return 1
    print("OK - copy is byte-identical to the manifest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
