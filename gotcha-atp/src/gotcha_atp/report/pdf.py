"""PDF from the HTML report via WeasyPrint.

WeasyPrint needs pango from the OS (macOS: brew install pango). Without it the
HTML report still exists and this returns why the PDF is missing, so the
Result screen can say so instead of failing the run.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

# Homebrew's library directories (Apple silicon, Intel). WeasyPrint opens pango
# & co. by bare name; that lookup falls back to ctypes' find_library, which
# reads DYLD_FALLBACK_LIBRARY_PATH from os.environ at call time — so adding
# them here works without the launcher exporting it before Python starts.
HOMEBREW_LIB_DIRS = ("/opt/homebrew/lib", "/usr/local/lib")


def _homebrew_library_path() -> None:
    if sys.platform != "darwin":
        return
    current = [p for p in os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "").split(":") if p]
    extra = [d for d in HOMEBREW_LIB_DIRS if d not in current and os.path.isdir(d)]
    if extra:
        os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = ":".join(current + extra)


def write(html_text: str, path: Path) -> tuple[Optional[Path], str]:
    _homebrew_library_path()
    try:
        from weasyprint import HTML
    except (ImportError, OSError) as e:
        return None, (f"PDF not written: WeasyPrint unavailable ({type(e).__name__}). "
                      "Install pango (brew install pango) — the HTML report is next to the record.")
    try:
        HTML(string=html_text).write_pdf(str(path))
    except Exception as e:  # noqa: BLE001 — a rendering error must not lose the run
        return None, f"PDF not written: {type(e).__name__}: {e}"
    return path, ""
