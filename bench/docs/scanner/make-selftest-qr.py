#!/usr/bin/env python3
"""Regenerate the scanner self-test QR codes and their printable sheet.

The generated SVGs are committed, so a station never needs to run this — it
exists so the artifacts are reproducible rather than mystery binaries. `segno`
is deliberately NOT a bench dependency (nothing at runtime encodes QR codes);
install it in a throwaway venv when you need to regenerate:

    python3 -m venv /tmp/qrgen && /tmp/qrgen/bin/pip install segno
    /tmp/qrgen/bin/python bench/docs/scanner/make-selftest-qr.py

Two payloads, two different jobs:

* LABEL   — the realistic case. Same field order and shape as a genuine
            Teltonika sticker, so it exercises the whole path: transport,
            `parse_device_label()`, and the MAC cross-check (its MAC is
            deliberately one no real device has, so arming must refuse when a
            device is plugged in — that is the mismatch path, on demand).
* CHARSET — the layout case. Its PW field carries every shift-dependent ASCII
            punctuation mark a US-layout keyboard wedge can mangle on a
            non-US host. Teltonika only uses a handful of these, but a station
            that round-trips all of them cannot be silently mangling the few
            that matter.
"""
from __future__ import annotations

import html
from pathlib import Path

import segno

OUT_DIR = Path(__file__).resolve().parent

# Mirrors the default in bench/scripts/scan-probe.html — keep the two in step.
LABEL_PAYLOAD = ("SN:0000000000;I:000000000000000;M:AABBCCDDEEFF;"
                 "U:admin;PW:aZ?4*$=_-!;B:000;")

# Every printable ASCII punctuation mark except the two the label format uses
# as delimiters (`:` and `;`) — those are covered by the parser unit tests,
# where they can be asserted precisely instead of eyeballed on a page.
CHARSET_PW = "aZ09" + "!\"#$%&'()*+,-./<=>?@[\\]^_`{|}~"
CHARSET_PAYLOAD = f"SN:0000000001;M:AABBCCDDEEFF;U:admin;PW:{CHARSET_PW};B:000;"

SYMBOLS = [
    ("selftest-label.svg", LABEL_PAYLOAD, "Label self-test",
     "Realistic sticker shape. Expect an exact match, and a refused arm "
     "(MAC mismatch) when a device is plugged in."),
    ("selftest-charset.svg", CHARSET_PAYLOAD, "Charset self-test",
     "Every shift-dependent punctuation mark. Any mangled character here "
     "means the host keyboard layout is still being applied."),
]


def write_svg(name: str, payload: str) -> None:
    # Error correction M: these get printed, taped to a bench, and scanned for
    # years. Fixed version so a payload edit can't silently change the module
    # count and shrink the printed size.
    qr = segno.make(payload, error="m")
    qr.save(str(OUT_DIR / name), kind="svg", scale=8, border=2,
            dark="#000000", light="#ffffff", svgclass=None, lineclass=None)
    print(f"{name}: version {qr.version}, {len(payload)} chars")


def write_sheet() -> None:
    """One printable page: both symbols, their exact payloads, and what to do."""
    cards = "\n".join(f"""
    <section class="card">
      <img src="{name}" alt="{html.escape(title)} QR code">
      <div class="meta">
        <h2>{html.escape(title)}</h2>
        <p>{html.escape(note)}</p>
        <p class="label">Expected payload</p>
        <pre>{html.escape(payload)}</pre>
      </div>
    </section>""" for name, payload, title, note in SYMBOLS)

    (OUT_DIR / "selftest-sheet.html").write_text(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Bench scanner self-test sheet</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         color: #111; background: #fff; margin: 0; padding: 28px 32px; line-height: 1.5; }}
  h1 {{ font-size: 1.25rem; margin: 0 0 4px; }}
  h2 {{ font-size: 1rem; margin: 0 0 6px; }}
  .lead {{ color: #555; font-size: .88rem; max-width: 46em; margin-bottom: 22px; }}
  .card {{ display: flex; gap: 22px; align-items: flex-start; page-break-inside: avoid;
           border: 1px solid #ddd; border-radius: 10px; padding: 18px; margin-bottom: 18px; }}
  .card img {{ width: 190px; height: auto; flex: 0 0 auto; }}
  .meta {{ flex: 1; }}
  .meta p {{ font-size: .85rem; color: #444; margin: 0 0 8px; }}
  .label {{ font-size: .68rem !important; text-transform: uppercase; letter-spacing: .06em;
            color: #888 !important; margin: 10px 0 3px !important; }}
  pre {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
         font-size: .74rem; background: #f5f5f7; border: 1px solid #e3e3e8;
         border-radius: 6px; padding: 8px 10px; margin: 0; white-space: pre-wrap;
         word-break: break-all; }}
  ol {{ font-size: .85rem; color: #444; max-width: 46em; padding-left: 22px; }}
  li {{ margin-bottom: 4px; }}
  @page {{ margin: 14mm; }}
</style>
</head>
<body>
  <h1>Bench scanner self-test</h1>
  <p class="lead">Print this page and keep it at the station. Scanning these two
  codes proves the scanner and this host agree on every character a Teltonika
  label can contain — before a mangled password turns into a mystery login
  failure mid-run.</p>

  <ol>
    <li>Open <code>bench/scripts/scan-probe.html</code> in a browser on the host under test.</li>
    <li>Click the scan box, then scan each code below.</li>
    <li>Paste the expected payload into the probe's "Expected payload" field.</li>
    <li>Both codes must report <b>EXACT MATCH</b>. Anything else: re-do the
        scanner setup in <code>bench/docs/scanner-ds2278.md</code>, paying
        attention to keypad emulation.</li>
  </ol>
{cards}
</body>
</html>
""", encoding="utf-8")
    print("selftest-sheet.html")


if __name__ == "__main__":
    for name, payload, _title, _note in SYMBOLS:
        write_svg(name, payload)
    write_sheet()
