#!/usr/bin/env python3
"""Undo the security scanner's filename mangling on the stick's copy.

The scan appends .gz to files whose magic is gzip (OCI blobs genuinely are
gzip — the bytes are untouched) and rewrites % as _ in apt pool filenames.
This is a rename-only repair applied to the copy; the source drive is never
written to.

Run with the bundle directory (the one holding MANIFEST.json) as the cwd.
"""

import json
import os
import sys


def main():
    with open("MANIFEST.json") as fh:
        files = json.load(fh)["files"]
    repaired = 0
    for entry in files:
        path = entry["path"]
        if os.path.exists(path):
            continue
        for candidate in (path + ".gz", path.replace("%", "_")):
            if os.path.exists(candidate):
                os.rename(candidate, path)
                repaired += 1
                break
    print(f"repaired names: {repaired}")


if __name__ == "__main__":
    sys.exit(main())
