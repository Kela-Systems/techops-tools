#!/usr/bin/env python3
"""Render a file from templates/ to stdout.

Two placeholder forms are supported:

  @@NAME@@                 replaced by the value of NAME=... on the argv
  @@INCLUDE:some-file@@    replaced by templates/some-file, with every line
                           prefixed by the placeholder's own indentation

The INCLUDE form is what keeps the shell scripts and systemd units that end up
inside cloud-init's `write_files` as real, lintable files instead of escaped
heredocs.

Usage:
  render.py user-data.tmpl PASSWORD_HASH='$6$...'
"""

import pathlib
import re
import sys

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "templates"
INCLUDE = re.compile(r"^([ \t]*)@@INCLUDE:([^@]+)@@[ \t]*$")


def render(name, subs):
    out = []
    for line in (TEMPLATES / name).read_text().splitlines():
        included = INCLUDE.match(line)
        if included:
            indent, target = included.groups()
            body = (TEMPLATES / target).read_text().rstrip("\n")
            out += [indent + l if l else "" for l in body.splitlines()]
            continue
        for key, value in subs.items():
            line = line.replace(f"@@{key}@@", value)
        out.append(line)
    return "\n".join(out) + "\n"


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__)
    subs = dict(arg.split("=", 1) for arg in argv[2:])
    text = render(argv[1], subs)
    left = re.search(r"@@[A-Z_]+@@", text)
    if left:
        sys.exit(f"render.py: unsubstituted placeholder {left.group(0)} in {argv[1]}")
    sys.stdout.write(text)


if __name__ == "__main__":
    main(sys.argv)
