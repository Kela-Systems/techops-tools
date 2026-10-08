#!/usr/bin/env python3
"""The QA-pass label printed after a verified-OK run (TEC-352).

"No label, it doesn't ship" — the label is the physical done-signal, so a unit
that passed and a unit that failed stop being indistinguishable objects. That
makes this module's job narrow but unusually load-bearing: turn ONE run record
into the ZPL for ONE label, and never invent a value.

Named `qa_label` because `device_label.py` already exists and means the
opposite: that one READS the factory sticker a device arrives with, this one
WRITES the sticker it leaves with.

Eight faces, because the tools do genuinely different things to a device and
the useful hero field differs (design session 2026-08-29, mocked up in
`docs/qa-labels.md`). What they share is a family language: inverted header, a
hero, up to two supporting fields, and Code 128 of the serial along the bottom.
The hero is whichever identity that tool actually wrote:

    otd            hostname     no unique LAN IP is written
    rutm           hostname     every RUTM lands on the same LAN address
    tsw            ip           no site and no hostname to lead with
    speaker        ip           the FINAL static address (it arrives on DHCP)
    raythink       ip           per-unit octet is how ONVIF/the NVR find it
    magos-radar    channel      the system diagram says "radar 1", not an IP
    magos-apu      APU + pairs  the only face that must name other devices
    (any of tsw/speaker/raythink left on DHCP)  ->  the MAC

The eighth, `port-map`, is the exception to "one record, one label": the PLANET
switch also earns a second label saying which socket takes which device, which
goes on the switch itself rather than into the QA pile. See `extra_contents`.

No face prints the serial as a field: the barcode's human-readable line already
does, and on 58 mm stock that duplicate was worth a whole row.

The barcode is encoded by the printer (`^BC`), not here. That is the point: a
placeholder encoder would have shipped labels whose codes look right and scan
as nothing. Handing the payload to printer firmware means the symbol is either
real or absent.

NEVER on a label, however convenient: any password, `password_source`, the
firmware version, the operator name, the station id, or Tailscale details.
Firmware changes after the label is stuck on; the rest either identifies a
person or is a secret. `render_zpl` reads only the fields the faces below
name, so adding one is a deliberate act — see `test_qa_label.py`, which fails
if any of them appears in rendered output.

Everything here is pure: no printer, no I/O, no clock. `python -m
bench_core.qa_label <run.json>` dumps the ZPL for a real record to stdout,
which pasted into labelary.com renders the exact label — how all eight faces
get reviewed without hardware.
"""
from __future__ import annotations

import glob
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from bench_core import mac_with_colons
from bench_core.run_record import parse_run_record

# ── stock and geometry ───────────────────────────────────────────────────────
#
# Zebra ZD421, 203 dpi, 58 x 29 mm labels (TEC-351).
#
# The first design was 15 x 5 cm and could not be printed. The ZD421's head is
# 104 mm — 832 dots at 203 dpi — and the widest media it accepts is 118 mm, so
# no stock existed on which a 15 cm span across the head would work: the labels
# came off correct at the left edge and progressively absent to the right, the
# QR missing entirely. That is the constraint to check first for any future
# change of stock, and `MAX_HEAD_DOTS` below is it as an assertion.
#
# 58 mm is 464 dots, comfortably inside the head, so this design prints the
# long axis ACROSS the head with no rotation. What it costs is room: 464 x 232
# is under a quarter of the area the first design had, which is why the faces
# below carry a hero and at most two supporting fields, and why there is no QR.
DPI = 203
LABEL_W = 464         # 58 mm across the printhead
LABEL_H = 232         # 29 mm along the feed

# Nothing is drawn against a physical edge, and the two axes get different
# clearances because they go wrong in different ways. Small labels do not sit
# perfectly straight on the roll, so the media wanders from side to side under
# the head and the across-head margin has to absorb it. Along the feed the gap
# sensor keeps registration tight, so less is needed there — which is just as
# well, because the vertical budget has none to give.
MARGIN = 14           # across the head, where the media actually wanders
MARGIN_Y = 10         # along the feed
# The printhead itself, per Zebra's ZD421 spec sheet. Nothing may exceed it and
# no stock can raise it.
MAX_HEAD_DOTS = 832

HEAD_H = 26           # the inverted header band
BAR_H = 28
# How far below the bars the human-readable line reaches, per `^BY` module
# width. Measured off renders, because none of it is controllable from here:
# `^BC` draws that line itself and sizes it from the MODULE WIDTH. `^CF` has no
# effect on it at all — 10, 16 and 30 render identically — so the line grows
# whenever a shorter serial earns a wider module, and the label's bottom margin
# moves with the serial unless something accounts for it. `barcode_y` is that
# something.
BAR_TEXT_DROP = {2: 20, 3: 27}
# The barcode is the one element that needs more width than the body gets. The
# longest real serial encodes to 444 dots even at the narrowest module the
# scanner will take, which leaves exactly this much at each end — so it sets
# the floor, and everything else keeps MARGIN.
BAR_EDGE = 10
# Everything below this belongs to the barcode. It is the HIGHEST the bars can
# start — the widest module, hence the tallest interpretation line — so a body
# that clears it clears the barcode for every serial.
FOOT_Y = LABEL_H - MARGIN_Y - BAR_H - max(BAR_TEXT_DROP.values())
# Width available to the body — the whole label now that no QR column has to be
# kept clear. The APU face right-aligns its radar addresses to the end of this.
BODY_W = LABEL_W - 2 * MARGIN

# The vertical budget, named rather than scattered through the faces, because
# on this stock there is no slack to absorb a face that drifts: 232 dots hold a
# header, a hero and two rows above the barcode and nothing else. Every face
# uses these, so a change here moves every face together and the geometry tests
# check the result against the media rather than against these numbers.
HERO_KEY_Y = 40
HERO_Y = 53
ROW1_Y = 89           # the first row under an FS_HERO hero
ROW1_BIG_Y = 99       # ...and under an FS_HERO_BIG one, which reaches lower
ROW2_Y = 127
PORT_Y = 46           # the port map's first row, clear of the header band
PORT_ROW_H = 30
PORT_NUM_W = 30       # the port-number column, wide enough for two digits

FS_HEAD = 20
FS_MODE = 15          # the header's right side, smaller than the family name
FS_KEY = 12           # the small grey-in-the-mockup field labels
FS_VAL = 19
FS_SUB = 22
FS_HERO = 32          # a hero with two rows under it
FS_HERO_BIG = 42      # a hero with one row, or none

FACES = ("hostname-imei", "hostname-gateway", "shared-ip", "unit-ip",
         "dhcp-mac", "channel", "pairing", "port-map")

# The port map is the one label that is not about identity: it says which
# socket takes which device, and it goes on the switch rather than in the QA
# pile. It is printed IN ADDITION to a QA label, never instead of one.
PORT_MAP_TOOLS = ("planet",)
PORT_MAP_ROWS = 5          # rows per column; 10 ports do not fit in one
PORT_MAP_MAX = 2 * PORT_MAP_ROWS

# Tools whose unit can legitimately leave the bench on DHCP (TEC-848), i.e.
# where an absent address is a decision rather than a gap. An OTD500 has no LAN
# address to begin with and the Magos pair is always static, so neither can
# reach the DHCP face by accident.
_DHCP_CAPABLE = ("tsw", "speaker", "raythink")


# ── text sanitising ──────────────────────────────────────────────────────────

def _ascii(value) -> str:
    """`value` folded to ASCII.

    The default ZPL character set is not UTF-8, and real records carry
    non-ASCII: the Magos tools write "—" into `radar_ip` when an APU controls
    nothing. Left alone those arrive as mojibake or a dropped glyph on a label
    nobody can re-print once the device is boxed.
    """
    if value is None:
        return ""
    text = str(value)
    for dash in ("\u2014", "\u2013", "\u2212"):    # em/en dash, minus
        text = text.replace(dash, "-")
    text = text.replace("\u00b7", "-")             # middot, used in the mockups
    text = unicodedata.normalize("NFKD", text)
    return text.encode("ascii", "ignore").decode("ascii").strip()


def _zpl(value) -> str:
    """`value` as ASCII, safe to drop inside a `^FD` field.

    `^` and `~` are ZPL's command prefixes: one in a field would end the field
    early and the rest of the value would be interpreted as commands. A
    hostname or site name is operator-supplied, so this is not hypothetical.
    """
    return _ascii(value).replace("^", " ").replace("~", " ").replace("\\", " ")


def _mac(value) -> str:
    """A MAC as the label shows it: colon-separated and upper-case. Records
    hold it lower-case; upper-case is what reads at arm's length."""
    return mac_with_colons(value).upper() if value else ""


# ── ZPL primitives ───────────────────────────────────────────────────────────
#
# Every one of these emits its position through `_fo`, so placement lives in
# exactly one place.

def _fo(x: int, y: int) -> str:
    """The `^FO` for a field the design places at (x, y).

    Design coordinates ARE printer coordinates here: 58 mm fits across the head,
    so nothing is rotated. Kept as its own function because it is the one place
    placement happens, and a stock that did not fit the head would have to
    rotate — see the note on `MAX_HEAD_DOTS`.
    """
    return f"^FO{x},{y}"


def _text(x: int, y: int, size: int, value, *, reverse: bool = False,
          width: Optional[int] = None, align: str = "L") -> str:
    """One text field, or "" when there is nothing to say — an empty `^FD`
    would still consume its place in the layout."""
    body = _zpl(value)
    if not body:
        return ""
    # The block runs from x to the end of the label unless the caller states
    # otherwise. It is a placement box, not a visible one, so a generous
    # default costs nothing: with `align="L"` the text still starts exactly at
    # x. What it must never be is *narrower* than the text, since `^FB` with a
    # one-line limit truncates rather than shrinking.
    block = LABEL_W - MARGIN - x if width is None else width
    # `^FB` rather than a bare field so a long hostname is bounded by the label
    # instead of running off it, and so `align` works. One line only: these are
    # single-value fields and a wrap would push into the row below.
    #
    # Height only, no width in `^A0N`: `^A0N,h,w` scales every glyph into a
    # w-wide cell, which pads the narrow ones — a hostname like
    # `otd-kela-fob-12` prints as `otd - kela - fob - 12`. Omitting w keeps
    # font 0 proportional.
    out = f"{_fo(x, y)}^A0N,{size}^FB{block},1,0,{align},0"
    if reverse:
        out += "^FR"                      # invert: white text on the black band
    return out + f"^FD{body}^FS"


def _box(x: int, y: int, w: int, h: int) -> str:
    """A filled black rectangle.

    The thickness is `min(w, h)`, not `h`. `^GB` raises either dimension to
    meet a thickness that exceeds it, so `^GB150,200,200` — the radar's tall
    channel index — silently became 200 wide and printed over the column
    beside it. At `min(w, h)` the border is thick enough to close over itself
    on both axes (2t >= the other side, for any aspect up to 2:1) while
    neither dimension gets inflated.
    """
    return f"{_fo(x, y)}^GB{w},{h},{min(w, h)}^FS"


def _rule(x: int, y: int, w: int, thickness: int = 3) -> str:
    return f"{_fo(x, y)}^GB{w},0,{thickness}^FS"


def _kv(x: int, y: int, key: str, value, *, size: int = FS_VAL,
        width: Optional[int] = None) -> str:
    """A field label with its value underneath, the unit the faces are built
    from. Renders nothing at all when the value is empty, so a face does not
    print a heading over a blank."""
    if not _zpl(value):
        return ""
    return _text(x, y, FS_KEY, key, width=width) + \
        _text(x, y + FS_KEY + 3, size, value, width=width)


def barcode_module_width(value: str) -> int:
    """Dots per Code 128 module for `value` — as wide as will still fit.

    Adaptive because serial lengths differ by a factor of two (a Teltonika
    `6010212527` against a speaker's `TM-CS20-000001-XX`) and the printer
    clips rather than scales. A clipped Code 128 still looks like a barcode
    and scans as nothing, which is the failure this label exists to prevent.


    Two dots — 0.25 mm at 203 dpi — is the floor, and the longest serials land
    there. That is the practical lower limit for a handheld reader rather than a
    comfortable margin, so a serial materially longer than the speaker's would
    not so much overflow as quietly stop scanning.

    Three is the ceiling even where a very short serial would allow four. A
    wider module is no help to a scanner that already reads three comfortably,
    and it drags the interpretation line taller with it (see `BAR_TEXT_DROP`)
    until the barcode no longer fits under the body.
    """
    modules = barcode_modules(value)
    usable = LABEL_W - 2 * BAR_EDGE
    for width in (3, 2):
        if modules * width <= usable:
            return width
    return 2


def barcode_modules(value: str) -> int:
    """How many Code 128 modules `value` encodes to.

    Code 128: 11 modules per symbol, one symbol per character plus start,
    checksum and stop, and the stop pattern is 2 modules longer.
    """
    return (len(value) + 3) * 11 + 2


def barcode_length(value: str) -> int:
    """How wide the barcode will come out, in dots."""
    return barcode_modules(value) * barcode_module_width(value)


def barcode_x(value: str) -> int:
    """Where the barcode starts, so that it sits CENTRED on the label.

    Centred rather than aligned to MARGIN with the body, because the module
    width is chosen greedily and so the barcode's width jumps around with the
    length of the serial — left-aligned, a short serial left a visibly heavier
    gap on the right and the whole label read as crooked. Centring also splits
    the slack evenly, which is what the media needs when it is sitting a little
    off under the head.
    """
    return max(BAR_EDGE, (LABEL_W - barcode_length(value)) // 2)


def barcode_y(value: str) -> int:
    """Where the bars start, so the label ends MARGIN_Y clear of the bottom.

    Anchored to the bottom rather than to `FOOT_Y`, because the interpretation
    line's height is set by the module width and so changes with the serial
    (see `BAR_TEXT_DROP`). Anchoring the top instead is what left a 3-dot gap
    under the wide-module faces and a 10-dot one under the speaker — the same
    print sitting at two different heights depending on whose label it was.
    """
    return LABEL_H - MARGIN_Y - BAR_H - BAR_TEXT_DROP[barcode_module_width(value)]


def _barcode(value: str) -> str:
    """Code 128 of the serial with its human-readable line, along the bottom.
    The DS2278 already on the bench reads it in the warehouse.

    The interpretation line is why no face prints a separate serial field: the
    serial is already here, in the one place a reader looks for it.
    """
    body = _zpl(value)
    if not body:
        return ""
    # ^CF is kept even though it demonstrably does nothing to the
    # interpretation line here: ^CF persists across labels in a printer
    # session, and this costs one command to not depend on what the last job
    # left behind.
    return (f"^CF0,16"
            f"{_fo(barcode_x(body), barcode_y(body))}"
            f"^BY{barcode_module_width(body)},3,{BAR_H}"
            f"^BCN,{BAR_H},Y,N,N^FD{body}^FS")


# ── what goes on one label ───────────────────────────────────────────────────

@dataclass
class LabelContent:
    """The label's content, resolved from a record and independent of ZPL.

    Split from rendering so face selection and field mapping — the part with
    the judgement calls in it — can be asserted on directly, without parsing
    printer commands.
    """

    face: str
    family: str                       # the header's left side, e.g. "MAGOS APU"
    mode: str                         # the header's right side, e.g. "DHCP"
    hero_key: str
    hero: str
    serial: str
    # Only what the face prints. On 58 mm the room ran out well before the
    # available data did, so `fields` is the shortlist that survived rather
    # than everything the record holds — a field here that no face draws is a
    # bug, not spare capacity.
    hero_sub: str = ""
    fields: list[tuple[str, str]] = field(default_factory=list)
    pairing: list[tuple[str, str]] = field(default_factory=list)

    def get(self, key: str) -> str:
        """The value for `key`, or "". Faces look their fields up by name
        rather than by position, so adding one cannot shift another."""
        for name, value in self.fields:
            if name == key:
                return value
        return ""


def _device(entry: dict) -> dict:
    return entry.get("device") or {}


def _ip(entry: dict) -> str:
    """The unit's address, without a prefix length. The Magos radar's configure
    run records the CIDR it wrote (`192.168.88.51/24`); an installer reading
    the label wants the address, and the mask is the subnet's, not the unit's."""
    return _ascii(_device(entry).get("ip")).split("/")[0]


def _digit(value) -> str:
    """`value` as a plain digit, or "" for anything else.

    The digit test is how the two Magos non-answers collapse to one: a missing
    key, and the literal `"other"` that `resolve_target` stores when the
    operator typed an address instead of picking from the list.
    `magos_verify.py` reads that string the same way ("no channel was assigned
    on this unit's configure run").
    """
    text = _ascii(value)
    return text if text.isdigit() else ""


def _rf_channel(dev: dict) -> str:
    """The RF channel a radar was confirmed to be transmitting on, or "".

    Deliberately NOT `device.channel`. That field is the operator's pick — the
    bench's intent, which a later verify pass checks the unit against — and on
    a radar it is not safe to print. Only the AR-300 line has an RF channel at
    all, and `set_channel` skips the step without complaint on a radar with no
    variants, so a run can record `channel: "1"` on a unit that has no such
    setting and never received one. Printing that would put a frequency on a
    sticker for a radar that has no frequency to be on.

    `device.rf_channel` is written by `confirmed_channel()` instead, and only
    when the radar's own read agreed with the intent — so its presence means
    "this unit really is on this channel", which is the only claim a label may
    make.
    """
    return _digit(dev.get("rf_channel"))


def _apu_index(dev: dict) -> str:
    """Which APU this is — 0 or 1 — or "" for a manual-IP unit.

    `device.channel` is safe to read here, unlike on the radar: an APU has no
    RF channel (and no `RF channel` verification row), so this number is purely
    the slot the operator picked, and the slot is what chose the IP. There is
    nothing on the device to confirm it against, and nothing being claimed
    about the hardware by printing it.
    """
    return _digit(dev.get("channel"))


def _on_dhcp(entry: dict) -> bool:
    """Whether this unit was left for someone else's DHCP server to address.

    Two signals. All three of these tools record `ip_mode` since TEC-848, and
    it is checked first as the one the tool states outright. The record spells
    it `"dhcp"` or `"static"` — the four internal mode names (fixed / cycle /
    manual / dhcp) collapse to those two before they reach a record, which is
    why this compares against `"dhcp"` rather than negating `"static"`.

    An empty `device.ip` is the fallback, and covers a record written before
    the mode field existed: on these three tools the bench writes the address
    it assigned, so nothing written means nothing was assigned. Note it is
    `device.ip` and never `device.reached_at` — under DHCP the latter holds
    where the unit happened to answer, which is the site's lease to change and
    not ours to print.
    """
    if entry.get("tool") not in _DHCP_CAPABLE:
        return False
    mode = _ascii(_device(entry).get("ip_mode")).lower()
    if mode:
        return mode == "dhcp"
    return not _ip(entry)


def _dhcp_face(entry: dict, family: str, kind: str, *,
               extra: Optional[list[tuple[str, str]]] = None,
               mode_suffix: str = "") -> LabelContent:
    """The shared DHCP face: the MAC as the hero.

    There is no address to print, and the MAC is how the bench — and later a
    technician — finds the unit again, so it takes the hero slot rather than
    sitting under the word DHCP. The header already says DHCP.
    """
    serial = _ascii(entry.get("serial"))
    return LabelContent(
        face="dhcp-mac",
        family=family,
        mode=_ascii("DHCP" + (f" - {mode_suffix}" if mode_suffix else "")),
        hero_key="FIND BY MAC", hero=_mac(entry.get("mac")),
        fields=list(extra or []),
        serial=serial,
    )


def _content_otd(entry: dict) -> LabelContent:
    dev = _device(entry)
    host = _ascii(dev.get("hostname"))
    imei = _ascii(dev.get("imei"))
    serial = _ascii(entry.get("serial"))
    return LabelContent(
        face="hostname-imei",
        family="OTD500", mode="CELLULAR",
        hero_key="HOSTNAME", hero=host,
        fields=[("IMEI", imei), ("MAC", _mac(entry.get("mac")))],
        serial=serial,
    )


def _content_rutm(entry: dict) -> LabelContent:
    dev = _device(entry)
    host = _ascii(dev.get("hostname"))
    serial = _ascii(entry.get("serial"))
    if dev.get("role") == "edge":
        # An edge router's LAN is inside its own box. What an installer types
        # is the WAN address, from the server box that sits in front of it.
        return LabelContent(
            face="hostname-gateway",
            family="RUTM08", mode="EDGE",
            hero_key="HOSTNAME", hero=host,
            hero_sub=_ascii(dev.get("wan_ip")) or "192.168.88.20",
            fields=[("MAC", _mac(entry.get("mac")))],
            serial=serial,
        )
    # Every RUTM lands on the same LAN address, so it is not an identity — but
    # it is still the thing an installer types after the box is in the rack,
    # which is why it gets the inverted band instead of a field slot.
    lan = _ip(entry) or "192.168.88.1"
    return LabelContent(
        face="hostname-gateway",
        family="RUTM08", mode="STATIC",
        hero_key="HOSTNAME", hero=host, hero_sub=lan,
        fields=[("MAC", _mac(entry.get("mac")))],
        serial=serial,
    )


def _content_tsw(entry: dict) -> LabelContent:
    serial = _ascii(entry.get("serial"))
    if _on_dhcp(entry):
        return _dhcp_face(entry, "TSW202", "TSW202")
    ip = _ip(entry)
    return LabelContent(
        face="shared-ip",
        family="TSW202", mode="STATIC",
        hero_key="MGMT IP", hero=ip,
        fields=[("MAC", _mac(entry.get("mac")))],
        serial=serial,
    )


def _content_planet(entry: dict) -> LabelContent:
    """PLANET IGS-4215 PoE switch.

    `shared-ip` like the TSW202 next to it: every switch lands on the same
    management address, which is not an identity but is what an installer
    types once the box is racked. The MAC does double duty here — it is the
    only per-unit identifier the device exposes (there is no serial), and it
    is what the factory password derives from.
    """
    serial = _ascii(entry.get("serial"))
    mac = _mac(entry.get("mac"))
    return LabelContent(
        face="shared-ip",
        family="IGS-4215", mode="STATIC",
        hero_key="MGMT IP", hero=_ip(entry),
        # The MAC row is dropped when the MAC *is* the serial, which on this
        # device is the normal case: the barcode's interpretation line already
        # prints it, and no face spends a row saying the same thing twice.
        fields=[] if _mac(serial) == mac else [("MAC", mac)],
        serial=serial,
    )


def _content_port_map(entry: dict) -> Optional[LabelContent]:
    """The switch's port map, from the plan the run recorded.

    Built from `device.port_map` — pairs of (port, what is plugged into it)
    that the tool wrote from its own config. Nothing is derived here: a label
    that guessed which socket a radar was on would be worse than no label.
    """
    rows = [(_ascii(port), _ascii(what))
            for port, what in (_device(entry).get("port_map") or [])
            if _ascii(what)]
    if not rows:
        return None
    if len(rows) > PORT_MAP_MAX:
        # Refused rather than truncated: a port map missing its last sockets
        # still looks complete, and the operator would have no way to tell.
        # print_run turns this into "no label printed", which they do see.
        raise ValueError(f"{len(rows)} ports named but the label holds "
                         f"{PORT_MAP_MAX}")
    return LabelContent(
        face="port-map",
        family="IGS-4215", mode="PORT MAP",
        hero_key="MGMT IP", hero=_ip(entry),
        # No barcode: this label is the same on every switch at a site, and the
        # unit's identity is on the QA label beside it.
        serial="",
        pairing=rows,
    )


def extra_contents(entry: dict) -> list[LabelContent]:
    """Labels this run earns BESIDES its QA label, in print order."""
    record = parse_run_record(entry)
    if record.get("tool") not in PORT_MAP_TOOLS:
        return []
    content = _content_port_map(record)
    return [content] if content else []


def _content_speaker(entry: dict) -> LabelContent:
    serial = _ascii(entry.get("serial"))
    if _on_dhcp(entry):
        return _dhcp_face(entry, "IP SPEAKER", "SPEAKER")
    ip = _ip(entry)
    return LabelContent(
        face="shared-ip",
        family="IP SPEAKER", mode="STATIC",
        hero_key="IP", hero=ip,
        fields=[("MAC", _mac(entry.get("mac")))],
        serial=serial,
    )


def _content_raythink(entry: dict) -> LabelContent:
    dev = _device(entry)
    profile = _ascii(dev.get("profile")).upper()
    serial = _ascii(entry.get("serial"))
    host = _ascii(dev.get("hostname"))
    if _on_dhcp(entry):
        return _dhcp_face(entry, "RAYTHINK", "RAYTHINK",
                          extra=[("HOST", host)], mode_suffix=profile)
    ip = _ip(entry)
    return LabelContent(
        face="unit-ip",
        family="RAYTHINK", mode=_ascii(f"STATIC - {profile}" if profile
                                       else "STATIC"),
        hero_key="IP", hero=ip,
        fields=[("HOST", host), ("MAC", _mac(entry.get("mac")))],
        serial=serial,
    )


def _content_magos_radar(entry: dict) -> LabelContent:
    dev = _device(entry)
    serial = _ascii(entry.get("serial"))
    ip = _ip(entry)
    channel = _rf_channel(dev)
    if not channel:
        # Nothing confirmed the channel: a non-AR-300 (or old firmware) that has
        # none, a manual-IP run that set none, or a read that came back amber.
        # The address face is the honest fallback — the alternative is deriving
        # "channel N from .5N", which claims an RF setting nobody verified.
        return LabelContent(
            face="shared-ip",
            family="MAGOS RADAR", mode="STATIC",
            hero_key="IP", hero=ip,
            fields=[("MAC", _mac(entry.get("mac")))],
            serial=serial,
        )
    return LabelContent(
        face="channel",
        family="MAGOS RADAR", mode="STATIC",
        hero_key="CHANNEL", hero=channel, hero_sub=ip,
        fields=[("MODEL", _ascii(entry.get("model"))),
                ("MAC", _mac(entry.get("mac")))],
        serial=serial,
    )


def _radar_pairs(dev: dict) -> list[tuple[str, str]]:
    """The APU's controlled radars as (id, ip) rows.

    Only the structured `radars` list is read. `radar_ip` is the same thing
    flattened for the UI's history column, and a verify record sets it to a
    literal "—" placeholder — printing that as a pairing row would put a dash
    where an address belongs.
    """
    pairs = []
    for index, radar in enumerate(dev.get("radars") or []):
        if not isinstance(radar, dict):
            continue
        ip = _ascii(radar.get("ip"))
        if not ip:
            continue
        pairs.append((_ascii(radar.get("radar_id")) or f"radar_{index}", ip))
    return pairs


def _content_magos_apu(entry: dict) -> LabelContent:
    dev = _device(entry)
    serial = _ascii(entry.get("serial"))
    ip = _ip(entry)
    channel = _apu_index(dev)
    pairs = _radar_pairs(dev)
    if not channel and not pairs:
        # As with the radar: a verify record has neither, so print the address.
        # A manual-IP APU keeps the pairing face when radars WERE supplied —
        # those addresses were really written, so they are worth printing even
        # though the unit has no APU index.
        return LabelContent(
            face="shared-ip",
            family="MAGOS APU", mode="STATIC",
            hero_key="IP", hero=ip,
            fields=[("MAC", _mac(entry.get("mac")))],
            serial=serial,
        )
    return LabelContent(
        face="pairing",
        family="MAGOS APU", mode="STATIC",
        hero_key="APU", hero=channel, hero_sub=ip,
        fields=[("MAC", _mac(entry.get("mac")))],
        pairing=pairs,
        serial=serial,
    )


_CONTENT_BY_TOOL = {
    "otd": _content_otd,
    "rutm": _content_rutm,
    "tsw": _content_tsw,
    "planet": _content_planet,
    "speaker": _content_speaker,
    "raythink": _content_raythink,
    "magos-radar": _content_magos_radar,
    "magos-apu": _content_magos_apu,
}


def label_content(entry: dict) -> LabelContent:
    """Resolve one run record — canonical or legacy — into label content."""
    record = parse_run_record(entry)
    builder = _CONTENT_BY_TOOL.get(record.get("tool"))
    if builder is None:
        raise ValueError(f"No label face for tool {record.get('tool')!r}.")
    return builder(record)


def pick_face(entry: dict) -> str:
    """Which of `FACES` this record prints on."""
    return label_content(entry).face


# ── faces ────────────────────────────────────────────────────────────────────
#
# None of these prints the serial as a field. It is already on every label, in
# the barcode's human-readable line — see `_barcode`. On 58 mm that redundancy
# was worth a whole row, which is what let the faces below keep a second real
# field instead.

def _face_hostname_imei(c: LabelContent) -> list[str]:
    # SITE is not printed: the hostname it would sit beside already ends in it
    # ("otd-kela-fob-12" against "kela-fob-12"), so the row went to the MAC.
    return [
        _text(MARGIN, HERO_KEY_Y, FS_KEY, c.hero_key),
        _text(MARGIN, HERO_Y, FS_HERO, c.hero),
        _kv(MARGIN, ROW1_Y, "IMEI", c.get("IMEI")),
        _kv(MARGIN, ROW2_Y, "MAC", c.get("MAC")),
    ]


def _face_hostname_gateway(c: LabelContent) -> list[str]:
    # The inverted LAN band survives the shrink because it is the field an
    # installer actually types once the box is racked.
    band = FOOT_Y - ROW2_Y - 8
    return [
        _text(MARGIN, HERO_KEY_Y, FS_KEY, c.hero_key),
        _text(MARGIN, HERO_Y, FS_HERO, c.hero),
        _kv(MARGIN, ROW1_Y, "MAC", c.get("MAC")),
        _box(MARGIN, ROW2_Y, BODY_W, band),
        _text(MARGIN + 6, ROW2_Y + 7, FS_KEY, "LAN", reverse=True),
        _text(MARGIN, ROW2_Y + 3, FS_SUB, c.hero_sub, reverse=True,
              width=BODY_W - 6, align="R"),
    ]


def _face_shared_ip(c: LabelContent) -> list[str]:
    return [
        _text(MARGIN, HERO_KEY_Y, FS_KEY, c.hero_key),
        _text(MARGIN, HERO_Y, FS_HERO_BIG, c.hero),
        _kv(MARGIN, ROW1_BIG_Y, "MAC", c.get("MAC"), size=FS_SUB),
    ]


def _face_unit_ip(c: LabelContent) -> list[str]:
    return [
        _text(MARGIN, HERO_KEY_Y, FS_KEY, c.hero_key),
        _text(MARGIN, HERO_Y, FS_HERO, c.hero),
        _kv(MARGIN, ROW1_Y, "HOST", c.get("HOST")),
        _kv(MARGIN, ROW2_Y, "MAC", c.get("MAC")),
    ]


def _face_dhcp_mac(c: LabelContent) -> list[str]:
    # The MAC IS the hero here, not a field under one. There is no address to
    # print, and the MAC is the whole reason the label is worth reading: it is
    # how the bench, and later a technician, finds the unit on the network.
    # "DHCP" is not repeated as a hero — the header already says it.
    return [
        _text(MARGIN, HERO_KEY_Y, FS_KEY, c.hero_key),
        _text(MARGIN, HERO_Y, FS_HERO_BIG, c.hero),
        _kv(MARGIN, ROW1_BIG_Y, "HOST", c.get("HOST")),
    ]


def _face_channel(c: LabelContent) -> list[str]:
    # The index block is readable from a few metres, which is the point: the
    # system diagram says "radar 1" and someone standing at the rack has to be
    # able to tell which box that is.
    box = 68
    right = MARGIN + box + 12
    return [
        _box(MARGIN, HERO_KEY_Y, box, box),
        _text(MARGIN, HERO_KEY_Y + 5, FS_KEY, "CH", reverse=True,
              width=box, align="C"),
        _text(MARGIN, HERO_KEY_Y + 19, 44, c.hero, reverse=True,
              width=box, align="C"),
        _text(right, HERO_KEY_Y, FS_KEY, "IP"),
        _text(right, HERO_KEY_Y + 15, FS_HERO, c.hero_sub),
        _kv(MARGIN, ROW2_Y, "MODEL", c.get("MODEL"), width=120),
        _kv(MARGIN + 130, ROW2_Y, "MAC", c.get("MAC")),
    ]


def _face_pairing(c: LabelContent) -> list[str]:
    # The radar list is the reason this face exists, so it gets the room and
    # the MAC does not. Two radars is the cap the APU tool itself allows.
    out = []
    if c.hero:
        out += [_text(MARGIN, HERO_KEY_Y, FS_KEY, c.hero_key),
                _text(MARGIN, HERO_Y, FS_HERO, c.hero),
                _text(MARGIN + 60, HERO_KEY_Y, FS_KEY, "IP"),
                _text(MARGIN + 60, HERO_Y, FS_HERO, c.hero_sub)]
    else:
        out += [_text(MARGIN, HERO_KEY_Y, FS_KEY, "IP"),
                _text(MARGIN, HERO_Y, FS_HERO, c.hero_sub)]
    if c.pairing:
        out += [_rule(MARGIN, ROW1_Y + 2, BODY_W),
                _text(MARGIN, ROW1_Y + 6, FS_KEY, "CONTROLS")]
        for index, (radar_id, radar_ip) in enumerate(c.pairing[:2]):
            y = ROW1_Y + 22 + index * 24
            out += [_text(MARGIN + 8, y, FS_VAL, radar_id, width=150),
                    _text(MARGIN, y, FS_VAL, radar_ip,
                          width=BODY_W, align="R")]
    else:
        out.append(_kv(MARGIN, ROW1_BIG_Y, "MAC", c.get("MAC")))
    return out


def _face_port_map(c: LabelContent) -> list[str]:
    """Ten sockets down a 29 mm label: two columns of five, port then device.

    The management address sits on the last line rather than as a hero — on
    this label the sockets are the content, and the address is the one thing
    an installer needs once the box is racked.
    """
    out = []
    col_w = BODY_W // 2
    for index, (port, what) in enumerate(c.pairing[:PORT_MAP_MAX]):
        x = MARGIN + (index // PORT_MAP_ROWS) * col_w
        y = PORT_Y + (index % PORT_MAP_ROWS) * PORT_ROW_H
        out += [_text(x, y, FS_VAL, port, width=PORT_NUM_W),
                _text(x + PORT_NUM_W, y, FS_VAL, what,
                      width=col_w - PORT_NUM_W - 6)]
    if c.hero:
        y = PORT_Y + PORT_MAP_ROWS * PORT_ROW_H + 4
        out += [_rule(MARGIN, y, BODY_W),
                _text(MARGIN, y + 6, FS_VAL, f"{c.hero_key} {c.hero}",
                      width=BODY_W)]
    return out


_FACE_RENDERERS = {
    "hostname-imei": _face_hostname_imei,
    "hostname-gateway": _face_hostname_gateway,
    "shared-ip": _face_shared_ip,
    "unit-ip": _face_unit_ip,
    "dhcp-mac": _face_dhcp_mac,
    "channel": _face_channel,
    "pairing": _face_pairing,
    "port-map": _face_port_map,
}


# ── print quality ────────────────────────────────────────────────────────────
#
# How hard the head burns, how fast the media moves, and whether there is a
# ribbon in the way. None of it changes the layout, and all of it changes
# whether the label is readable.
#
# These are sent with the label rather than left to the printer because a
# printer's stored settings are invisible: a station whose darkness had drifted
# low printed pale labels for as long as it took someone to notice, and nothing
# in the bench could see that it had. Sent per job, print quality is a property
# of the config, and a swapped printer behaves like the one it replaced.
#
# All three are optional and omitted when unset, so a station that has never
# configured them keeps whatever its printer does today.

MEDIA_COMMAND = {"direct": "^MTD", "transfer": "^MTT"}
DARKNESS_RANGE = (0, 30)
SPEED_RANGE = (2, 6)          # inches/sec, per the ZD421 spec sheet


def _darkness(darkness: Optional[int] = None) -> list[str]:
    """The control command that goes in front of `^XA`.

    ~SD, not ^MD. ^MD is an adjustment RELATIVE to whatever the printer is
    already set to, so it inherits the drift it is meant to remove; ~SD is
    absolute and makes the value in the config the value that prints. It sets
    the running darkness only — saving to flash needs ^JUS, which is
    deliberately not sent.
    """
    if darkness is None:
        return []
    return [f"~SD{_clamp(darkness, *DARKNESS_RANGE)}"]


def _format_quality(media: str = "",
                    speed: Optional[int] = None) -> list[str]:
    """The quality commands that go INSIDE the format, after `^XA`.

    Both are caret commands, and a caret command the printer receives outside
    `^XA`/`^XZ` belongs to no label and is dropped — the station's speed and
    media type would silently never reach the head. Only tilde commands like
    `~SD` act wherever they land.
    """
    out = []
    if media:
        # ^MT tells the printer whether a ribbon is in the path. Getting it
        # wrong is not a subtle difference: thermal-transfer mode on direct
        # thermal stock puts a ribbon between the head and heat-sensitive
        # paper, which insulates it, and every label comes out uniformly pale.
        out.append(MEDIA_COMMAND[media])
    if speed is not None:
        # Slower is darker, and gentler on the head than the equivalent
        # darkness increase — worth reaching for first when print is pale.
        out.append(f"^PR{_clamp(speed, *SPEED_RANGE)}")
    return out


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


# ── how many ─────────────────────────────────────────────────────────────────
#
# Two per run: one goes on the unit and one on its box. The unit's label
# identifies it for the rest of its service life; the box's is what the
# receiving end reads without unpacking anything, and reprinting it later means
# finding the run record and a working printer at the same time.
#
# `^PQ` rather than sending the format twice. The printer replicates from a
# single job, so there is no window in which the first label prints and the
# second does not — either the job is accepted whole or the run reports an
# unprinted label and the operator is told to write one. Sending the format
# twice would make a half-labelled unit possible, and a unit that looks
# labelled but isn't is the failure this module exists to prevent.
DEFAULT_COPIES = 2
COPIES_RANGE = (1, 5)


def _copies(n: int) -> str:
    """`^PQ`, or nothing at all when a single label is wanted.

    Omitted rather than sent as `^PQ1` so that asking for one label produces
    byte-identical ZPL to what the bench emitted before copies existed. A
    station that wants one label should not be exercising a new code path.
    """
    n = _clamp(n, *COPIES_RANGE)
    return f"^PQ{n}" if n > 1 else ""


def label_count(zpl: str) -> int:
    """How many labels `zpl` actually puts on the floor.

    Not the same as the number of formats once `^PQ` is in play, and the
    difference matters when the count is being used to check a hardware run
    against what was expected.
    """
    return sum(int(found.group(1)) if (found := re.search(r"\^PQ(\d+)", fmt))
               else 1
               for fmt in zpl.split("^XA")[1:])


# ── rendering ────────────────────────────────────────────────────────────────

def render_content(c: LabelContent, *, media: str = "",
                   darkness: Optional[int] = None,
                   speed: Optional[int] = None,
                   copies: int = DEFAULT_COPIES) -> str:
    """`c` as one ZPL label format, printed `copies` times."""
    parts = [
        *_darkness(darkness),
        "^XA",
        *_format_quality(media, speed),
        "^CI28",                                  # UTF-8 in, though _ascii folds
        # `^LH0,0` keeps the home position at the label corner, so the margins
        # the design applies are the margins that print — they cannot be
        # cancelled out by a printer whose home position was left shifted.
        f"^PW{LABEL_W}", f"^LL{LABEL_H}", "^LH0,0",
        # Inset rather than bled to the edge: the first hardware run came back
        # with the top of this band shaved off by registration play.
        _box(MARGIN, MARGIN_Y, BODY_W, HEAD_H),
        _text(MARGIN + 5, MARGIN_Y + 3, FS_HEAD, c.family, reverse=True),
        _text(MARGIN, MARGIN_Y + 6, FS_MODE, c.mode, reverse=True,
              width=BODY_W - 5, align="R"),
        *_FACE_RENDERERS[c.face](c),
        _barcode(c.serial),
        _copies(copies),
        "^XZ",
    ]
    return "\n".join(p for p in parts if p) + "\n"


def render_zpl(entry: dict, **quality) -> str:
    """One run record as the ZPL for its QA label."""
    return render_content(label_content(entry), **quality)


def render_darkness_ladder(entry: dict, *, media: str = "",
                           speed: Optional[int] = None,
                           steps: tuple[int, ...] = (12, 16, 19, 22, 25, 28, 30),
                           ) -> str:
    """`entry`'s real label, once per darkness in `steps`.

    The way the right darkness gets chosen: print the strip, find the darkest
    label whose barcode still scans, and put that number in the station config.
    The alternative is guessing one value at a time against a printer that
    takes a roll of labels to answer.

    It prints the REAL face rather than a test pattern on purpose. Too little
    darkness and the bars are too faint to read; too much and they bleed into
    each other and stop scanning just as completely, so the thing being judged
    has to be the actual barcode with the actual serial. The setting replaces
    the mode on the header, which is the one place on a full label with room
    for it.
    """
    out = []
    for darkness in steps:
        content = label_content(entry)
        content.mode = f"D{darkness}" + (f" S{speed}" if speed else "")
        # One per rung, whatever a run prints. The strip is read by comparing
        # rungs, and a duplicate of each is a longer strip that says no more.
        out.append(render_content(content, media=media, darkness=darkness,
                                  speed=speed, copies=1))
    return "".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    """Dump the ZPL for one or more run-record JSON files.

    The review loop for the faces: paste the output into labelary.com and the
    label renders exactly as the ZD421 will print it, with no printer, no
    device and no bench.

    Wildcards are expanded here rather than left to the shell, because the
    bench station is Windows and neither cmd nor PowerShell expands one before
    handing it over — a pattern arrives as a literal filename.

    `-o FILE` writes the ZPL itself instead of relying on a redirect, for the
    same reason: PowerShell 5.1's `>` produces UTF-16, and a printer fed that
    prints a page of nothing recognisable. ZPL is ASCII by construction here
    (see `_ascii`), so the file this writes is the bytes the printer wants.

    `--ladder` turns one record into a strip of the same label at a range of
    darkness settings, for choosing the value that goes in the station config.
    `--media`, `--darkness` and `--speed` set the print quality of an ordinary
    dump, for trying a setting before committing it. `--copies` overrides how
    many of each label comes out; a ladder ignores it and prints one per rung.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    out_path = ""
    ladder = False
    quality: dict = {}
    if "--ladder" in args:
        args.remove("--ladder")
        ladder = True
    for flag, key, cast in (("--media", "media", str),
                            ("--darkness", "darkness", int),
                            ("--speed", "speed", int),
                            ("--copies", "copies", int)):
        if flag not in args:
            continue
        i = args.index(flag)
        if i + 1 >= len(args):
            print(f"{flag} needs a value", file=sys.stderr)
            return 2
        try:
            quality[key] = cast(args[i + 1])
        except ValueError:
            print(f"{flag}: {args[i + 1]!r} is not a number", file=sys.stderr)
            return 2
        del args[i:i + 2]
    if quality.get("media", "") and quality["media"] not in MEDIA_COMMAND:
        print(f"--media must be one of {', '.join(MEDIA_COMMAND)}",
              file=sys.stderr)
        return 2
    if "-o" in args:
        i = args.index("-o")
        if i + 1 >= len(args):
            print("-o needs a filename", file=sys.stderr)
            return 2
        out_path = args[i + 1]
        del args[i:i + 2]
    if not args:
        print("usage: python -m bench_core.qa_label [-o FILE] [--ladder] "
              "[--media direct|transfer] [--darkness 0-30] [--speed 2-6] "
              "[--copies 1-5] <run-record.json>...", file=sys.stderr)
        return 2
    if ladder:
        quality.pop("darkness", None)       # the ladder is what sets it
        quality.pop("copies", None)         # a rung is a rung
    paths: list[str] = []
    for arg in args:
        matched = sorted(glob.glob(arg))
        if matched:
            paths.extend(matched)
        elif any(ch in arg for ch in "*?["):
            print(f"{arg}: no files match", file=sys.stderr)
            return 2
        else:
            paths.append(arg)   # not a pattern — let open() name it in the error
    chunks = []
    for path in paths:
        with open(path, "r", encoding="utf-8") as handle:
            record = json.load(handle)
        if ladder:
            chunks.append(render_darkness_ladder(record, **quality))
            continue
        chunks.append(render_zpl(record, **quality))
        # The switch's port map is a real label this record produces, so the
        # review loop has to show it too — a ladder is about darkness, and one
        # face is enough for that.
        chunks += [render_content(extra, copies=1, **quality)
                   for extra in extra_contents(record)]
    zpl = "".join(chunks)
    if out_path:
        with open(out_path, "w", encoding="ascii", newline="\n") as handle:
            handle.write(zpl)
        print(f"{label_count(zpl)} label(s) -> {out_path}", file=sys.stderr)
    else:
        sys.stdout.write(zpl)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
