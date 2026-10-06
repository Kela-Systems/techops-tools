"""Gotcha ATP — the Techops acceptance test of an assembled Gotcha unit."""
from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path

__version__ = "0.1.0"

PACKAGE_DIR = Path(__file__).resolve().parent
# src/gotcha_atp -> gotcha-atp/. The tool runs from its checkout (like the bench
# tools), so release.yaml, proto/ and runs/ are found relative to it.
PROJECT_DIR = PACKAGE_DIR.parent.parent


def git_revision(path: Path = PROJECT_DIR) -> str:
    """Short git revision of the checkout, '+dirty' when gotcha-atp/ has local
    changes, or 'unknown' outside a checkout."""
    with contextlib.suppress(Exception):
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=path,
                             capture_output=True, text=True, timeout=3)
        if rev.returncode == 0 and rev.stdout.strip():
            dirty = subprocess.run(["git", "status", "--porcelain", "--", "."],
                                   cwd=path, capture_output=True, text=True, timeout=3)
            return rev.stdout.strip() + ("+dirty" if dirty.stdout.strip() else "")
    return "unknown"
