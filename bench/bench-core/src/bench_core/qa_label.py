#!/usr/bin/env python3
"""The QA-pass label printed after a verified-OK run (TEC-352).

"No label, it doesn't ship" — the label is the physical done-signal, so a unit
that passed and a unit that failed stop being indistinguishable objects. That
makes this module's job narrow but unusually load-bearing: turn ONE run record
into the ZPL for ONE label, and never invent a value.

Named `qa_label` because `device_label.py` already exists and means the
opposite: that one READS the factory sticker a device arrives with, this one
WRITES the sticker it leaves with.

Eight faces, one per tool, because the tools do genuinely different things to a
device and the useful hero field differs (design session 2026-08-29, mocked up
in `docs/qa-labels.md`). What they share is a family
language: inverted header, QR on the right, Code 128 of the serial along the
bottom. The hero is whichever identity that tool actually wrote:

    otd            hostname     no unique LAN IP is written; IMEI beside the site
    rutm           hostname     every RUTM lands on the same LAN address
    tsw            ip           no site, no hostname — serial is the identity
    speaker        ip           the FINAL static address (it arrives on DHCP)
    raythink       ip           per-unit octet is how ONVIF/the NVR find it
    magos-radar    channel      the system diagram says "radar 1", not an IP
    magos-apu      APU + pairs  the only face that must name other devices
    (any of tsw/speaker/raythink left on DHCP)  ->  the word DHCP + the MAC

The QR and the barcode are encoded by the printer (`^BQ`, `^BC`), not here.
That is the point: the design mockups drew QR modules as a visual stand-in, and
a placeholder encoder would have shipped labels whose codes look right and
scan as nothing. Handing the payload to printer firmware means the symbol is
either real or absent.

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

import json
import sys
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from bench_core import mac_with_colons
from bench_core.run_record import parse_run_record

# ── stock and geometry ───────────────────────────────────────────────────────
#
# Zebra ZD421, 203 dpi, 15 x 5 cm (TEC-351). 15 cm is the chosen stock: the APU
# pairing table is the densest face and still fits, and 15 x 10 cm is the same
# content with more air rather than room for more fields.
DPI = 203
LABEL_W = 1199        # 15 cm at 203 dpi
LABEL_H = 400         # 5 cm

MARGIN = 14
HEAD_H = 46           # the inverted header band
# Everything below FOOT_Y belongs to the barcode: BAR_H of bars, then the
# human-readable line under them. Both have to fit above LABEL_H or the serial
# is clipped off the bottom of the label.
FOOT_Y = 312
BAR_H = 42
BAR_TEXT_H = 20
QR_X = 975            # the QR column; the body's left column ends before it
QR_Y = 54
# Width available to the body. The gap to QR_X is clearance, not decoration:
# the APU face right-aligns its radar addresses to the end of this and they
# would otherwise touch the QR's quiet zone.
BODY_W = QR_X - MARGIN - 34

FS_HEAD = 28
FS_KEY = 18           # the small grey-in-the-mockup field labels
FS_VAL = 28
FS_SUB = 40
FS_HERO = 58
FS_HERO_BIG = 72

FACES = ("hostname-imei", "hostname-gateway", "shared-ip", "unit-ip",
         "dhcp-mac", "channel", "pairing")

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

def _text(x: int, y: int, size: int, value, *, reverse: bool = False,
          width: Optional[int] = None, align: str = "L") -> str:
    """One text field, or "" when there is nothing to say — an empty `^FD`
    would still consume its place in the layout."""
    body = _zpl(value)
    if not body:
        return ""
    # Height only, no width: `^A0N,h,w` scales every glyph into a w-wide cell,
    # which pads the narrow ones — a hostname like `otd-kela-fob-12` prints as
    # `otd - kela - fob - 12`. Omitting w keeps font 0 proportional.
    out = f"^FO{x},{y}^A0N,{size}"
    if width is not None:
        out += f"^FB{width},1,0,{align},0"
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
    return f"^FO{x},{y}^GB{w},{h},{min(w, h)}^FS"


def _rule(x: int, y: int, w: int, thickness: int = 3) -> str:
    return f"^FO{x},{y}^GB{w},0,{thickness}^FS"


def _kv(x: int, y: int, key: str, value, *, size: int = FS_VAL) -> str:
    """A field label with its value underneath, the unit the faces are built
    from. Renders nothing at all when the value is empty, so a face does not
    print a heading over a blank."""
    if not _zpl(value):
        return ""
    return _text(x, y, FS_KEY, key) + _text(x, y + FS_KEY + 4, size, value)


def qr_magnification(payload: str) -> int:
    """Dots per QR module for `payload`.

    Bounded on purpose. The QR sits in the band between the header and the
    `hostname-gateway` face's black LAN strip at y=248, so a symbol that grew
    with the payload would silently print over it. 4 keeps the longest real
    payload (the APU's, with two radar addresses) inside that band, and is
    still comfortably above the ~3 dots/module a DS2278 needs.
    """
    return 5 if len(payload) <= 40 else 4


def _qr(payload: str) -> str:
    # ^FD for ^BQ is "<error correction><input mode>,<data>". Q = 25% recovery,
    # chosen over the usual M because these labels live on equipment that gets
    # handled, racked and occasionally scuffed before anyone scans them.
    return (f"^FO{QR_X},{QR_Y}^BQN,2,{qr_magnification(payload)}"
            f"^FDQA,{_zpl(payload)}^FS")


def barcode_module_width(value: str) -> int:
    """Dots per Code 128 module for `value` — as wide as will still fit.

    Adaptive because serial lengths differ by a factor of two (a Teltonika
    `6010212527` against a speaker's `TM-CS20-000001-XX`) and the printer
    clips rather than scales. A clipped Code 128 still looks like a barcode
    and scans as nothing, which is the failure this label exists to prevent.
    """
    # Code 128: 11 modules per symbol, one symbol per character plus start,
    # checksum and stop, and the stop pattern is 2 modules longer.
    modules = (len(value) + 3) * 11 + 2
    usable = LABEL_W - 2 * MARGIN
    for width in (4, 3, 2):
        if modules * width <= usable:
            return width
    return 2


def _barcode(value: str) -> str:
    """Code 128 of the serial with its human-readable line, along the bottom.
    The DS2278 already on the bench reads it in the warehouse."""
    body = _zpl(value)
    if not body:
        return ""
    # ^CF sets the font of the interpretation line, which ^BC draws itself.
    # Left at the printer default it comes out in a bitmapped font at whatever
    # size the last job left behind.
    return (f"^CF0,{BAR_TEXT_H}"
            f"^FO{MARGIN},{FOOT_Y}^BY{barcode_module_width(body)},3,{BAR_H}"
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
    qr: str
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
    return _ascii(_device(entry).get("ip"))


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
    """The shared DHCP face: the word DHCP as the hero and the MAC printed
    large. There is no address to print, and the MAC is how the bench — and
    later a technician — finds the unit again."""
    serial = _ascii(entry.get("serial"))
    return LabelContent(
        face="dhcp-mac",
        family=family,
        mode=_ascii("DHCP" + (f" - {mode_suffix}" if mode_suffix else "")),
        hero_key="ADDRESS", hero="DHCP",
        fields=([("MAC", _mac(entry.get("mac"))), ("S/N", serial)]
                + list(extra or [])),
        serial=serial,
        qr=f"KELA|{kind}|{serial}|DHCP" + (f"|{mode_suffix.lower()}"
                                           if mode_suffix else ""),
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
        fields=[("SITE", _ascii(dev.get("site_name"))), ("IMEI", imei),
                ("S/N", serial), ("MAC", _mac(entry.get("mac")))],
        serial=serial,
        qr=f"KELA|OTD500|{serial}|{host}|{imei}",
    )


def _content_rutm(entry: dict) -> LabelContent:
    dev = _device(entry)
    host = _ascii(dev.get("hostname"))
    serial = _ascii(entry.get("serial"))
    # Every RUTM lands on the same LAN address, so it is not an identity — but
    # it is still the thing an installer types after the box is in the rack,
    # which is why it gets the inverted band instead of a field slot.
    lan = _ip(entry) or "192.168.88.1"
    return LabelContent(
        face="hostname-gateway",
        family="RUTM08", mode="STATIC",
        hero_key="HOSTNAME", hero=host, hero_sub=lan,
        fields=[("SITE", _ascii(dev.get("site_name"))), ("S/N", serial),
                ("MAC", _mac(entry.get("mac")))],
        serial=serial,
        qr=f"KELA|RUTM08|{serial}|{host}|{lan}",
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
        fields=[("SERIAL", serial), ("MAC", _mac(entry.get("mac")))],
        serial=serial,
        qr=f"KELA|TSW202|{serial}|{ip}",
    )


def _content_speaker(entry: dict) -> LabelContent:
    serial = _ascii(entry.get("serial"))
    if _on_dhcp(entry):
        return _dhcp_face(entry, "IP SPEAKER", "SPEAKER")
    ip = _ip(entry)
    return LabelContent(
        face="shared-ip",
        family="IP SPEAKER", mode="STATIC",
        hero_key="IP", hero=ip,
        fields=[("SERIAL", serial), ("MAC", _mac(entry.get("mac")))],
        serial=serial,
        qr=f"KELA|SPEAKER|{serial}|{ip}",
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
        fields=[("HOST", host), ("S/N", serial),
                ("MAC", _mac(entry.get("mac")))],
        serial=serial,
        qr=f"KELA|RAYTHINK|{serial}|{ip}" + (f"|{profile.lower()}"
                                             if profile else ""),
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
            fields=[("SERIAL", serial), ("MAC", _mac(entry.get("mac")))],
            serial=serial,
            qr=f"KELA|RADAR|{serial}|{ip}",
        )
    return LabelContent(
        face="channel",
        family="MAGOS RADAR", mode="STATIC",
        hero_key="CHANNEL", hero=channel, hero_sub=ip,
        fields=[("MODEL", _ascii(entry.get("model"))), ("S/N", serial),
                ("MAC", _mac(entry.get("mac")))],
        serial=serial,
        qr=f"KELA|RADAR|{serial}|CH{channel}|{ip}",
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
            fields=[("SERIAL", serial), ("MAC", _mac(entry.get("mac")))],
            serial=serial,
            qr=f"KELA|APU|{serial}|{ip}",
        )
    pair_qr = "".join(f"|r{index}={pair_ip}"
                      for index, (_, pair_ip) in enumerate(pairs))
    return LabelContent(
        face="pairing",
        family="MAGOS APU", mode="STATIC",
        hero_key="APU", hero=channel, hero_sub=ip,
        fields=[("S/N", serial), ("MAC", _mac(entry.get("mac")))],
        pairing=pairs,
        serial=serial,
        qr=(f"KELA|APU|{serial}"
            + (f"|APU{channel}" if channel else "") + f"|{ip}" + pair_qr),
    )


_CONTENT_BY_TOOL = {
    "otd": _content_otd,
    "rutm": _content_rutm,
    "tsw": _content_tsw,
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

def _face_hostname_imei(c: LabelContent) -> list[str]:
    return [
        _text(MARGIN, 58, FS_KEY, c.hero_key),
        _text(MARGIN, 80, FS_HERO, c.hero),
        _kv(MARGIN, 152, "SITE", c.get("SITE")),
        _kv(MARGIN + 430, 152, "IMEI", c.get("IMEI")),
        _kv(MARGIN, 228, "S/N", c.get("S/N")),
        _kv(MARGIN + 430, 228, "MAC", c.get("MAC")),
    ]


def _face_hostname_gateway(c: LabelContent) -> list[str]:
    return [
        _text(MARGIN, 58, FS_KEY, c.hero_key),
        _text(MARGIN, 80, FS_HERO, c.hero),
        _kv(MARGIN, 152, "SITE", c.get("SITE")),
        _kv(MARGIN + 330, 152, "S/N", c.get("S/N")),
        _kv(MARGIN + 650, 152, "MAC", c.get("MAC")),
        _box(0, 248, LABEL_W, 56),
        _text(MARGIN, 264, FS_KEY, "LAN", reverse=True),
        _text(0, 254, FS_SUB, c.hero_sub, reverse=True,
              width=LABEL_W - MARGIN, align="R"),
    ]


def _face_shared_ip(c: LabelContent) -> list[str]:
    return [
        _text(MARGIN, 58, FS_KEY, c.hero_key),
        _text(MARGIN, 76, FS_HERO_BIG, c.hero),
        _kv(MARGIN, 168, "SERIAL", c.get("SERIAL"), size=FS_SUB),
        _kv(MARGIN, 248, "MAC", c.get("MAC")),
    ]


def _face_unit_ip(c: LabelContent) -> list[str]:
    return [
        _text(MARGIN, 58, FS_KEY, c.hero_key),
        _text(MARGIN, 76, FS_HERO_BIG, c.hero),
        _kv(MARGIN, 182, "HOST", c.get("HOST")),
        _kv(MARGIN + 330, 182, "S/N", c.get("S/N")),
        _kv(MARGIN + 650, 182, "MAC", c.get("MAC")),
    ]


def _face_dhcp_mac(c: LabelContent) -> list[str]:
    return [
        _text(MARGIN, 58, FS_KEY, c.hero_key),
        _text(MARGIN, 74, 80, c.hero),
        _kv(MARGIN, 176, "FIND BY MAC", c.get("MAC"), size=44),
        _kv(MARGIN, 250, "S/N", c.get("S/N")),
        _kv(MARGIN + 430, 250, "HOST", c.get("HOST")),
    ]


def _face_channel(c: LabelContent) -> list[str]:
    # The index block is readable from a few metres, which is the point: the
    # system diagram says "radar 1" and someone standing at the rack has to be
    # able to tell which box that is.
    right = MARGIN + 180
    return [
        _box(MARGIN, 52, 150, 200),
        _text(MARGIN, 64, 22, "CH", reverse=True, width=150, align="C"),
        _text(MARGIN, 94, 100, c.hero, reverse=True, width=150, align="C"),
        _text(right, 58, FS_KEY, "IP"),
        _text(right, 78, 56, c.hero_sub),
        _kv(right, 182, "MODEL", c.get("MODEL")),
        _kv(right + 260, 182, "S/N", c.get("S/N")),
        _kv(right + 520, 182, "MAC", c.get("MAC")),
    ]


def _face_pairing(c: LabelContent) -> list[str]:
    out = []
    if c.hero:
        out += [_text(MARGIN, 58, FS_KEY, c.hero_key),
                _text(MARGIN, 76, 60, c.hero),
                _text(MARGIN + 170, 58, FS_KEY, "IP"),
                _text(MARGIN + 170, 78, 44, c.hero_sub)]
    else:
        out += [_text(MARGIN, 58, FS_KEY, "IP"),
                _text(MARGIN, 78, 44, c.hero_sub)]
    if c.pairing:
        out += [_rule(MARGIN, 148, BODY_W),
                _text(MARGIN, 154, FS_KEY, "CONTROLS")]
        for index, (radar_id, radar_ip) in enumerate(c.pairing[:2]):
            y = 176 + index * 30
            out += [_text(MARGIN + 10, y, FS_VAL, radar_id),
                    _text(MARGIN, y, FS_VAL, radar_ip,
                          width=BODY_W, align="R")]
        out.append(_rule(MARGIN, 236, BODY_W))
    out += [_kv(MARGIN, 244, "S/N", c.get("S/N")),
            _kv(MARGIN + 430, 244, "MAC", c.get("MAC"))]
    return out


_FACE_RENDERERS = {
    "hostname-imei": _face_hostname_imei,
    "hostname-gateway": _face_hostname_gateway,
    "shared-ip": _face_shared_ip,
    "unit-ip": _face_unit_ip,
    "dhcp-mac": _face_dhcp_mac,
    "channel": _face_channel,
    "pairing": _face_pairing,
}


# ── rendering ────────────────────────────────────────────────────────────────

def render_content(c: LabelContent) -> str:
    """`c` as one ZPL label."""
    parts = [
        "^XA",
        "^CI28",                                  # UTF-8 in, though _ascii folds
        f"^PW{LABEL_W}", f"^LL{LABEL_H}", "^LH0,0",
        _box(0, 0, LABEL_W, HEAD_H),
        _text(MARGIN, 10, FS_HEAD, c.family, reverse=True),
        _text(0, 12, 24, c.mode, reverse=True,
              width=LABEL_W - MARGIN, align="R"),
        *_FACE_RENDERERS[c.face](c),
        _qr(c.qr),
        _barcode(c.serial),
        "^XZ",
    ]
    return "\n".join(p for p in parts if p) + "\n"


def render_zpl(entry: dict) -> str:
    """One run record as the ZPL for its QA label."""
    return render_content(label_content(entry))


def main(argv: Optional[list[str]] = None) -> int:
    """Dump the ZPL for one or more run-record JSON files.

    The review loop for the faces: paste the output into labelary.com and the
    label renders exactly as the ZD421 will print it, with no printer, no
    device and no bench.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: python -m bench_core.qa_label <run-record.json>...",
              file=sys.stderr)
        return 2
    for path in args:
        with open(path, "r", encoding="utf-8") as handle:
            sys.stdout.write(render_zpl(json.load(handle)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
