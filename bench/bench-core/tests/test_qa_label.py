"""Tests for the QA label's content and ZPL (bench_core.qa_label, TEC-352).

Driven by the sample records in `label-records/` — one per face, realistic
enough to catch a mapping that reads the wrong field. See that folder's README
for the visual review loop.

Two classes of test here matter more than the field mapping:

* `test_no_face_leaks_*` — the design forbids printing a password, the
  firmware, the operator or the station id. Those are all present in a run
  record, one field away from the ones the label DOES print, so the ban is
  asserted rather than trusted.
* `test_*_stays_inside_the_label` — ZPL clips silently. A field that runs past
  the label edge, or a barcode too wide for its stock, still prints; it just
  comes out unscannable or cut in half, which nothing downstream would catch.
"""
import json
import re
from pathlib import Path

import pytest

from bench_core.qa_label import (
    BAR_EDGE,
    BAR_H,
    BAR_TEXT_DROP,
    FACES,
    FOOT_Y,
    LABEL_H,
    LABEL_W,
    MARGIN,
    MARGIN_Y,
    MAX_HEAD_DOTS,
    barcode_length,
    barcode_module_width,
    barcode_x,
    barcode_y,
    extra_contents,
    label_content,
    label_count,
    main,
    render_content,
    render_darkness_ladder,
    pick_face,
    render_zpl,
)

RECORDS = Path(__file__).parent / "label-records"


def record(name: str) -> dict:
    return json.loads((RECORDS / f"{name}.json").read_text(encoding="utf-8"))


ALL_RECORDS = sorted(p.stem for p in RECORDS.glob("*.json"))
# Formats, not records: the switch's record renders its QA label AND its port
# map, so a dump of everything is longer than the file count.
ALL_FORMATS = sum(1 + len(extra_contents(record(n))) for n in ALL_RECORDS)


# ── face selection ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,face", [
    ("otd", "hostname-imei"),
    ("rutm", "hostname-gateway"),
    ("tsw-static", "shared-ip"),
    ("tsw-dhcp", "dhcp-mac"),
    ("speaker", "shared-ip"),
    ("raythink-lan", "unit-ip"),
    ("raythink-dhcp", "dhcp-mac"),
    ("magos-radar", "channel"),
    ("magos-apu", "pairing"),
    ("planet", "shared-ip"),
])
def test_each_tool_gets_its_designed_face(name, face):
    assert pick_face(record(name)) == face


def test_every_sample_record_has_a_known_face():
    for name in ALL_RECORDS:
        assert pick_face(record(name)) in FACES


def test_unknown_tool_is_refused_rather_than_guessed():
    entry = record("otd")
    entry["tool"] = "toaster"
    with pytest.raises(ValueError):
        label_content(entry)


# ── the hero field is the one that tool actually wrote ────────────────────────

def test_otd_leads_with_the_hostname_not_an_address():
    content = label_content(record("otd"))
    assert content.hero == "otd-kela-fob-12"
    assert content.get("IMEI") == "864088064068679"
    # No LAN IP is written for a cellular gateway; nothing may invent one.
    assert "192.168" not in render_zpl(record("otd"))


def test_rutm_prints_the_shared_lan_address_in_its_band():
    content = label_content(record("rutm"))
    assert content.hero == "rut-haifa-port"
    assert content.hero_sub == "192.168.88.1"


def test_tsw_leans_on_the_barcode_for_its_serial():
    """A switch has no site and no hostname, so the serial is the only thing
    that tells two of them apart — and it is on the label once, under the
    barcode, rather than twice."""
    content = label_content(record("tsw-static"))
    assert content.hero == "192.168.88.2"
    assert content.serial == "6010620710"
    assert render_zpl(record("tsw-static")).count("6010620710") == 1


def test_raythink_carries_its_profile_on_the_header():
    content = label_content(record("raythink-lan"))
    assert content.hero == "192.168.88.35"
    assert "LAN" in content.mode
    assert content.get("HOST") == "raythink-35"


def test_radar_leads_with_the_channel_and_keeps_the_ip_beside_it():
    """The system diagram says "radar 1", not an address."""
    content = label_content(record("magos-radar"))
    assert content.hero == "1"
    assert content.hero_sub == "192.168.88.51"


def test_apu_names_the_radars_it_controls():
    content = label_content(record("magos-apu"))
    assert content.hero == "0"
    assert content.pairing == [("radar_0", "192.168.88.50"),
                               ("radar_1", "192.168.88.51")]


# ── DHCP: no address to print (TEC-848) ──────────────────────────────────────

def test_dhcp_makes_the_mac_the_hero_not_an_address():
    """There is no address to print, so the MAC takes the hero slot: it is how
    the unit is found again. The word DHCP is on the header, not repeated as a
    hero over the field that matters."""
    content = label_content(record("tsw-dhcp"))
    assert content.hero == "20:97:2B:2B:00:F7"
    assert content.mode == "DHCP"
    assert "192.168" not in render_zpl(record("tsw-dhcp"))


def test_an_absent_ip_is_read_as_dhcp_without_the_mode_field():
    """All three tools record `ip_mode` since TEC-848, so this is the fallback
    for a record written before that field existed. An empty `device.ip` is the
    signal: these tools write the address they assigned, so nothing written
    means nothing was assigned."""
    entry = record("tsw-static")
    entry["device"]["ip"] = ""
    assert pick_face(entry) == "dhcp-mac"


def test_an_explicit_mode_beats_an_empty_address():
    entry = record("raythink-lan")
    entry["device"]["ip_mode"] = "dhcp"
    assert pick_face(entry) == "dhcp-mac"


def test_a_static_mode_is_not_overridden_by_a_missing_address():
    """`ip_mode` is checked first, so a static run with an unrecorded address
    does not silently become a DHCP label."""
    entry = record("raythink-lan")
    entry["device"]["ip"] = ""
    assert pick_face(entry) == "unit-ip"


@pytest.mark.parametrize("name", ["speaker", "tsw-static"])
def test_the_speaker_and_switch_reach_the_dhcp_face_too(name):
    """TEC-848 gives both of these a DHCP option. The face is already shared,
    so this asserts the wiring rather than adding a face: the tool name has to
    be in `_DHCP_CAPABLE` for `_on_dhcp` to look at all, and a typo there fails
    silently by printing a stale default address."""
    entry = record(name)
    entry["device"]["ip_mode"] = "dhcp"
    content = label_content(entry)
    assert content.face == "dhcp-mac"
    assert content.mode.startswith("DHCP")
    assert "192.168" not in render_zpl(entry)


@pytest.mark.parametrize("name,expected_face", [("speaker", "shared-ip"),
                                                ("tsw-static", "shared-ip"),
                                                ("raythink-lan", "unit-ip")])
def test_a_manual_address_prints_as_typed(name, expected_face):
    """The other half of TEC-848: an operator can type the address. No face may
    print an assumed default, so the hero has to follow the record wherever it
    goes — including off the subnet these tools normally use."""
    entry = record(name)
    entry["device"]["ip"] = "10.20.30.40"
    content = label_content(entry)
    assert content.face == expected_face
    assert content.hero == "10.20.30.40"
    assert "10.20.30.40" in render_zpl(entry)


def test_a_dhcp_lease_is_never_printed_as_the_address():
    """A DHCP run may well record the address the bench found the unit on. That
    is a lease, not an assignment, and a lease on a sticker outlives its own
    truth — so `ip_mode` wins over a present address."""
    entry = record("speaker")
    entry["device"]["ip_mode"] = "dhcp"
    entry["device"]["ip"] = "192.168.1.57"
    assert label_content(entry).face == "dhcp-mac"
    assert "192.168.1.57" not in render_zpl(entry)


@pytest.mark.parametrize("name", ["speaker", "tsw-static", "raythink-lan"])
def test_where_the_unit_answered_is_not_where_it_lives(name):
    """The shape TEC-848 actually writes: on a DHCP run the tools leave
    `device.ip` empty and put the address the unit answered on in its own
    `device.reached_at`, precisely so it is not mistaken for an assignment.
    Nothing on the label may read that field — it is the site's lease, and the
    sticker outlives it."""
    entry = record(name)
    entry["device"]["ip_mode"] = "dhcp"
    entry["device"]["ip"] = ""
    entry["device"]["reached_at"] = "192.168.1.57"
    content = label_content(entry)
    assert content.face == "dhcp-mac"
    assert "192.168.1.57" not in render_zpl(entry)


def test_the_otd_can_never_reach_the_dhcp_face():
    """It has no LAN address at all, so "no address" is normal rather than a
    decision someone made."""
    entry = record("otd")
    entry["device"]["ip"] = ""
    assert pick_face(entry) == "hostname-imei"


# ── Magos verify runs, which assign nothing ──────────────────────────────────

def test_a_radar_verify_record_prints_the_address_not_an_invented_channel():
    """A verify pass assigns nothing, so `build_verify_entry` carries no
    `channel`, and it only carries `rf_channel` when the radar's own read
    confirmed one. With neither, the address is all the record can claim —
    deriving "channel N from .5N" would be wrong for a typed address, and wrong
    again for a radar that has no channel to be on."""
    entry = record("magos-radar")
    entry["kind"] = "verify"
    del entry["device"]["channel"]
    del entry["device"]["rf_channel"]
    assert pick_face(entry) == "shared-ip"
    assert label_content(entry).hero == "192.168.88.51"


def test_a_radar_verify_record_prints_a_channel_the_radar_confirmed():
    """The one case where a verify label leads with the channel: the unit itself
    reported it, so `rf_channel` is present even though nothing was assigned."""
    entry = record("magos-radar")
    entry["kind"] = "verify"
    del entry["device"]["channel"]
    assert pick_face(entry) == "channel"
    assert label_content(entry).hero == "1"


def test_an_apu_verify_record_does_not_print_a_placeholder_pairing():
    """A verify record sets `radar_ip` to a literal em dash. Printing that as a
    pairing row would put a dash where an address belongs."""
    entry = record("magos-apu")
    entry["kind"] = "verify"
    del entry["device"]["channel"]
    del entry["device"]["radars"]
    entry["device"]["radar_ip"] = "\u2014"
    content = label_content(entry)
    assert content.face == "shared-ip"
    assert content.pairing == []
    zpl = render_zpl(entry)
    assert "\u2014" not in zpl and "CONTROLS" not in zpl


def test_a_manual_ip_radar_does_not_print_a_channel_it_never_got():
    """`resolve_target` stores the literal "other" when the operator typed an
    address instead of picking a channel, and a manual-IP run deliberately
    leaves the radar's RF channel alone — so there is no `rf_channel`. Reading
    `channel` here printed a hero reading "CH other"; worse, on a unit whose
    frequency nobody set it would have been channel-shaped and wrong."""
    entry = record("magos-radar")
    entry["device"]["channel"] = "other"
    del entry["device"]["rf_channel"]
    entry["device"]["ip"] = "192.168.88.77"
    content = label_content(entry)
    assert content.face == "shared-ip"
    assert content.hero == "192.168.88.77"
    zpl = render_zpl(entry)
    assert "other" not in zpl.lower()
    assert "CH" not in zpl


def test_a_radar_with_no_rf_channel_prints_its_address():
    """Only the AR-300 line has an RF channel. `set_channel` skips the step
    without complaint on a radar that has none, so the record still carries the
    operator's pick — and the label must not print it as a frequency."""
    entry = record("magos-radar")
    entry["model"] = "AR-150"
    entry["device"]["channel"] = 1        # the pick survives; the setting never happened
    del entry["device"]["rf_channel"]
    content = label_content(entry)
    assert content.face == "shared-ip"
    assert content.hero == "192.168.88.51"
    assert "CH" not in render_zpl(entry)


def test_the_pick_alone_never_reaches_the_label():
    """`device.channel` is intent, kept so a later verify pass has something to
    check against. `device.rf_channel` is the confirmed reading. Only the second
    may be printed, and the two are allowed to disagree."""
    entry = record("magos-radar")
    entry["device"]["channel"] = 3
    entry["device"]["rf_channel"] = "1"
    content = label_content(entry)
    assert content.hero == "1"
    assert content.face == "channel"
    assert "3" not in content.hero


def test_a_manual_ip_apu_keeps_the_radars_it_really_assigned():
    """No APU index, because none was picked — but the radar addresses were
    genuinely written, so they still belong on the label."""
    entry = record("magos-apu")
    entry["device"]["channel"] = "other"
    content = label_content(entry)
    assert content.face == "pairing"
    assert content.hero == ""
    assert content.pairing == [("radar_0", "192.168.88.50"),
                               ("radar_1", "192.168.88.51")]
    zpl = render_zpl(entry)
    assert "other" not in zpl.lower()
    assert "192.168.88.50" in zpl


@pytest.mark.parametrize("channel", ["other", "", "  ", "none", "chan1"])
def test_only_a_plain_digit_counts_as_a_channel(channel):
    entry = record("magos-radar")
    entry["device"]["rf_channel"] = channel
    assert pick_face(entry) == "shared-ip"


@pytest.mark.parametrize("name,key", [("magos-radar", "rf_channel"),
                                      ("magos-apu", "channel")])
def test_channel_zero_is_a_channel(name, key):
    """Radar chan0 and APU 0 are both real. A falsy-looking value that is
    nonetheless a channel is exactly what a truthiness check gets wrong."""
    entry = record(name)
    entry["device"][key] = 0
    content = label_content(entry)
    assert content.hero == "0"


def test_a_radar_with_no_ip_in_a_pairing_list_is_skipped():
    entry = record("magos-apu")
    entry["device"]["radars"] = [{"radar_id": "radar_0", "ip": ""},
                                 {"radar_id": "radar_1", "ip": "192.168.88.51"}]
    assert label_content(entry).pairing == [("radar_1", "192.168.88.51")]


# ── what must never be printed ───────────────────────────────────────────────

@pytest.mark.parametrize("name", ALL_RECORDS)
def test_no_face_leaks_the_firmware_operator_or_station(name):
    """Firmware changes after the label is stuck on; the other two identify a
    person and a machine. All three sit in the record beside fields the label
    does print."""
    entry = record(name)
    zpl = render_zpl(entry)
    for field in ("firmware", "operator", "station_id", "bench_version",
                  "config_hash"):
        value = entry.get(field)
        if value:
            assert str(value) not in zpl, f"{name}: {field} reached the label"


@pytest.mark.parametrize("name", ALL_RECORDS)
def test_no_face_leaks_a_password_or_where_it_came_from(name):
    entry = record(name)
    entry["device"]["password"] = "Kelasys123!"
    entry["device"]["password_source"] = "scan"
    zpl = render_zpl(entry)
    assert "Kelasys123!" not in zpl
    assert "scan" not in zpl.lower()


@pytest.mark.parametrize("name", ALL_RECORDS)
def test_no_face_leaks_the_step_log(name):
    """The log names the admin user and quotes device replies. It is also the
    biggest field in a record, so a face that dumped it would be obvious — the
    point of the test is that nothing does it accidentally via a `**entry`."""
    entry = record(name)
    entry["log"] = "SENTINEL-LOG-TEXT"
    entry["steps"] = [{"time": "10:00:00", "level": "info", "sn": "-",
                       "msg": "SENTINEL-STEP-TEXT"}]
    zpl = render_zpl(entry)
    assert "SENTINEL" not in zpl


# ── the symbologies are real, and encoded by the printer ─────────────────────

@pytest.mark.parametrize("name", ALL_RECORDS)
def test_every_face_carries_a_code128_of_the_serial(name):
    entry = record(name)
    zpl = render_zpl(entry)
    assert "^BCN," in zpl, "no Code 128 of the serial"
    assert entry["serial"] in zpl


@pytest.mark.parametrize("name", ALL_RECORDS)
def test_no_face_prints_the_serial_twice(name):
    """The barcode's interpretation line already carries it. On 58 mm that
    freed a whole row per face, so a face that also printed an S/N field would
    be spending room the design does not have on a duplicate."""
    zpl = render_zpl(record(name))
    assert zpl.count(record(name)["serial"]) == 1
    assert "^FDS/N^FS" not in zpl


def test_a_field_never_holds_a_zpl_control_character():
    """`^` and `~` end a ZPL field. A hostname is operator-supplied, so a
    caret in one would truncate the label and run the rest as commands."""
    entry = record("otd")
    entry["device"]["hostname"] = "otd-^XZ~evil"
    zpl = render_zpl(entry)
    body = zpl.split("^FD", 1)[1]
    assert "^XZ~evil" not in body
    # exactly one label, still terminated properly
    assert zpl.count("^XA") == 1 and zpl.count("^XZ") == 1


def test_non_ascii_is_folded_rather_than_dropped_or_mangled():
    entry = record("magos-apu")
    entry["device"]["radar_ip"] = "\u2014"
    entry["serial"] = "GS\u2013123"          # en dash mid-serial
    zpl = render_zpl(entry)
    assert "GS-123" in zpl
    assert zpl.isascii()


@pytest.mark.parametrize("name", ALL_RECORDS)
def test_every_face_renders_pure_ascii_whatever_the_record_holds(name):
    """This invariant is load-bearing, not cosmetic: `label_printer` encodes
    the ZPL as ASCII on the way to the printer, so a face that let a non-ASCII
    field through would not print at all. Every string the label reads from a
    record is operator-supplied or device-reported, so all of them get one at
    once here."""
    entry = record(name)
    entry["serial"] = "SN\u2013001"
    entry["model"] = "mod\u00e8le"
    dev = entry.setdefault("device", {})
    for key in ("hostname", "site_name", "profile", "imei", "ip"):
        if key in dev:
            dev[key] = f"{dev[key]}\u2014\u00fc"
    zpl = render_zpl(entry)
    assert zpl.isascii()
    zpl.encode("ascii")            # the exact call the print path makes


# ── nothing runs off the stock ────────────────────────────────────────────────
#
# The first hardware run failed here: the design fitted its own 1199-dot width
# perfectly and was still unprintable, the printhead being 832 dots wide. On
# 58 mm stock the design fits across the head and nothing is rotated, so these
# read the emitted `^FO` directly.

_FO = re.compile(r"\^FO(\d+),(\d+)")


def _placed(zpl: str):
    """Every field in `zpl` as the (x, y) it prints at."""
    for x, y in _FO.findall(zpl):
        yield int(x), int(y)


def test_the_design_fits_the_printhead():
    """The bug that reached hardware, as an assertion. No stock can rescue a
    design wider than the head, so this is checked on the constants rather than
    on any one record."""
    assert LABEL_W <= MAX_HEAD_DOTS


@pytest.mark.parametrize("name", ALL_RECORDS)
def test_no_field_falls_off_the_media(name):
    for x, y in _placed(render_zpl(record(name))):
        assert 0 <= x <= LABEL_W, f"{name}: field {x} dots across the head"
        assert 0 <= y <= LABEL_H, f"{name}: field {y} dots along the feed"


@pytest.mark.parametrize("name", ALL_RECORDS)
def test_nothing_is_printed_against_a_physical_edge(name):
    """The other half of the first hardware report: the header band's edge came
    back shaved. Registration play is normal, so the design keeps clear of all
    four edges rather than trusting the media to be where it should be.

    Across the head the clearance is MARGIN for the body and the smaller
    BAR_EDGE for the barcode, which is the one element that needs the width.
    """
    for line in render_zpl(record(name)).splitlines():
        floor = BAR_EDGE if "^BC" in line else MARGIN
        for x, y in _placed(line):
            assert x >= floor, f"{name}: field at x={x} is on the edge"
            assert y >= MARGIN_Y, f"{name}: field at y={y} is on the edge"


@pytest.mark.parametrize("name", ALL_RECORDS)
def test_the_barcode_is_centred_on_the_label(name):
    """The module width is chosen greedily, so the barcode's width moves with
    the serial. Left-aligned, that showed up as an uneven right-hand gap and
    made the whole label look crooked — and on media that is already sitting a
    little off under the head, the slack is worth splitting evenly."""
    serial = record(name)["serial"]
    left = barcode_x(serial)
    right = LABEL_W - left - barcode_length(serial)
    assert abs(left - right) <= 1, f"{name}: {left} left against {right} right"
    assert left >= BAR_EDGE


@pytest.mark.parametrize("name", ALL_RECORDS)
def test_the_body_never_starts_under_the_barcode(name):
    """Everything below FOOT_Y belongs to the Code 128 and its text. A body
    field down there would print through the bars."""
    for line in render_zpl(record(name)).splitlines():
        if "^BC" in line:                    # the barcode itself lives there
            continue
        for _, y in _placed(line):
            assert y < FOOT_Y, \
                f"{name}: body field at y={y} is under the barcode"


@pytest.mark.parametrize("name", ALL_RECORDS)
def test_every_label_ends_the_same_distance_from_the_bottom(name):
    """`^BC` sizes its interpretation line from the module width, so a face
    whose serial earned a wider module used to finish lower down the label than
    one that did not — the same print sitting at two heights. Anchoring the
    barcode to the bottom edge is what makes the margin the same on all seven,
    and equal to the top, which is what stops the label reading as offset when
    the media is not sitting square."""
    serial = record(name)["serial"]
    bottom = barcode_y(serial) + BAR_H + BAR_TEXT_DROP[barcode_module_width(serial)]
    assert bottom == LABEL_H - MARGIN_Y


def test_the_body_clears_the_barcode_however_wide_its_module():
    """FOOT_Y has to be the highest the bars can ever start, not where they
    happen to start for one serial."""
    assert FOOT_Y == min(barcode_y("X" * n) for n in range(4, 30))


@pytest.mark.parametrize("name", ALL_RECORDS)
def test_the_barcode_fits_the_stock(name):
    """Code 128 is clipped, not scaled, when it is too wide — and a clipped
    barcode still looks like a barcode."""
    serial = record(name)["serial"]
    modules = (len(serial) + 3) * 11 + 2
    assert modules * barcode_module_width(serial) <= LABEL_W - 2 * BAR_EDGE


def test_a_long_serial_narrows_the_barcode_instead_of_overflowing():
    """58 mm is tight enough that the real serials already span the range: a
    Teltonika gets 3 dots per module, a speaker's longer one drops to 2."""
    assert barcode_module_width("6010212527") == 3
    assert barcode_module_width("TM-CS20-000001-XX") == 2
    assert barcode_module_width("X" * 40) == 2


@pytest.mark.parametrize("name", ALL_RECORDS)
def test_the_label_declares_the_58x29_stock(name):
    """`^PW`/`^LL` are what the printer believes the media to be. Declaring a
    label wider than the head is what produced the first hardware failure."""
    zpl = render_zpl(record(name))
    assert f"^PW{LABEL_W}" in zpl and f"^LL{LABEL_H}" in zpl


# ── print quality ────────────────────────────────────────────────────────────

def test_a_station_that_sets_nothing_sends_nothing():
    """The default has to be "leave the printer alone". Every bench printing
    today has its quality set on the printer, and a default that overrode that
    would change what they all produce the moment this shipped."""
    zpl = render_zpl(record("tsw-static"))
    assert zpl.startswith("^XA")
    for command in ("^MT", "~SD", "^PR"):
        assert command not in zpl


def test_darkness_comes_before_the_label():
    """`~SD` is a control command: inside a format it is not part of it, so it
    lands before `^XA` to apply to the label that follows."""
    zpl = render_zpl(record("tsw-static"), media="direct", darkness=22, speed=3)
    head, body = zpl.split("^XA", 1)
    assert "~SD22" in head and "~SD" not in body


def test_speed_and_media_are_inside_the_format():
    """`^PR` and `^MT` are format commands. Sent ahead of `^XA` they belong to
    no label and the printer drops them — which is how a station configured
    for speed 2 went on printing at the printer's own speed."""
    zpl = render_zpl(record("tsw-static"), media="direct", darkness=22, speed=3)
    head, rest = zpl.split("^XA", 1)
    body = rest.split("^XZ", 1)[0]
    assert "^PR" not in head and "^MT" not in head
    assert "^PR3" in body and "^MTD" in body


def test_thermal_transfer_is_a_different_command():
    assert "^MTT" in render_zpl(record("tsw-static"), media="transfer")


@pytest.mark.parametrize("given,expected", [
    (0, "~SD0"), (30, "~SD30"),
    (99, "~SD30"),          # clamped, not passed through
    (-5, "~SD0"),
])
def test_darkness_is_clamped_to_what_the_printer_accepts(given, expected):
    """A value out of range is a typo in a config file, and ZPL's response to
    one is undefined. Clamping keeps a fat-fingered 300 printing labels."""
    assert expected in render_zpl(record("tsw-static"), darkness=given)


@pytest.mark.parametrize("given,expected", [(1, "^PR2"), (9, "^PR6")])
def test_speed_is_clamped_to_what_the_printer_accepts(given, expected):
    assert expected in render_zpl(record("tsw-static"), speed=given)


def test_darkness_zero_is_a_setting_not_an_absence():
    """0 is the lightest darkness, and `if darkness:` would silently drop it."""
    assert "~SD0" in render_zpl(record("tsw-static"), darkness=0)


def test_quality_does_not_disturb_the_layout():
    entry = record("magos-apu")
    plain = render_zpl(entry)
    tuned = render_zpl(entry, media="direct", darkness=25, speed=2)
    tuned_body = tuned.split("^XA", 1)[1].replace("^MTD\n^PR2\n", "", 1)
    assert tuned_body == plain.split("^XA", 1)[1]


# ── how many ─────────────────────────────────────────────────────────────────

def test_a_run_prints_one_label_for_the_unit_and_one_for_the_box():
    assert "^PQ2" in render_zpl(record("tsw-static"))


@pytest.mark.parametrize("name", ALL_RECORDS)
def test_every_face_prints_the_pair(name):
    """Not a property of one face. A device whose label came out single would
    ship with nothing on its box, and which device that is would depend on
    which renderer someone last touched."""
    assert "^PQ2" in render_zpl(record(name))


def test_the_count_is_inside_the_format():
    """`^PQ` is a format command. Outside `^XA`/`^XZ` it belongs to no label
    and the printer has nothing to apply it to."""
    zpl = render_zpl(record("tsw-static"))
    body = zpl.split("^XA", 1)[1]
    assert "^PQ2" in body.split("^XZ", 1)[0]


def test_one_copy_is_the_zpl_the_bench_sent_before_copies_existed():
    """`^PQ1` and no `^PQ` print the same single label, but only one of them
    leaves the emitted ZPL unchanged — and an unchanged format is one fewer
    thing to have broken for a station that wants a single label."""
    assert "^PQ" not in render_zpl(record("tsw-static"), copies=1)


@pytest.mark.parametrize("given,expected", [
    (5, "^PQ5"),
    (40, "^PQ5"),       # a typo in a station file, not a request for a roll
    (0, ""),            # clamped up to one, which prints no ^PQ at all
    (-3, ""),
])
def test_the_count_is_clamped_to_something_a_bench_meant(given, expected):
    zpl = render_zpl(record("tsw-static"), copies=given)
    assert (expected in zpl) if expected else ("^PQ" not in zpl)


def test_the_count_does_not_disturb_the_layout():
    """Everything but the `^PQ` has to be the label that was approved."""
    entry = record("magos-apu")
    one = render_zpl(entry, copies=1)
    pair = render_zpl(entry, copies=2)
    assert pair.replace("^PQ2\n", "") == one


def test_the_count_is_one_command_not_a_second_format():
    """Sent twice, a dropped connection halfway leaves a unit labelled and its
    box not. One format that the printer replicates cannot half-succeed."""
    zpl = render_zpl(record("tsw-static"))
    assert zpl.count("^XA") == 1 and zpl.count("^XZ") == 1


def test_the_label_count_counts_labels_rather_than_formats():
    """The number the dump reports is the number that comes off the roll —
    otherwise a hardware run gets checked against the wrong expectation."""
    assert label_count(render_zpl(record("tsw-static"))) == 2
    assert label_count(render_zpl(record("tsw-static"), copies=1)) == 1
    assert label_count(render_zpl(record("otd"), copies=4)
                       + render_zpl(record("rutm"), copies=1)) == 5


# ── the darkness ladder ──────────────────────────────────────────────────────

def test_the_ladder_prints_one_label_per_step():
    zpl = render_darkness_ladder(record("tsw-static"), steps=(10, 20, 30))
    assert zpl.count("^XA") == 3
    assert zpl.count("^XZ") == 3
    for step in (10, 20, 30):
        assert f"~SD{step}" in zpl


def test_the_ladder_prints_one_of_each_rung_not_two():
    """A run prints a pair; the strip is read by comparing rungs to each other,
    and a duplicate of every rung is twice the labels saying the same thing."""
    zpl = render_darkness_ladder(record("tsw-static"), steps=(10, 20, 30))
    assert "^PQ" not in zpl
    assert label_count(zpl) == 3


def test_every_rung_says_which_setting_it_is():
    """A strip of labels that all look slightly different and none of which
    says what it was printed at is not a test, it is a pile of labels."""
    zpl = render_darkness_ladder(record("tsw-static"), steps=(17,), speed=4)
    assert "^FDD17 S4^FS" in zpl


def test_the_ladder_prints_the_real_barcode():
    """Too dark stops a barcode scanning just as surely as too light — the bars
    bleed together — so the thing being judged has to be the real one."""
    zpl = render_darkness_ladder(record("speaker"), steps=(20,))
    assert "^BCN," in zpl
    assert record("speaker")["serial"] in zpl


def test_the_ladder_carries_the_media_setting_through():
    zpl = render_darkness_ladder(record("tsw-static"), media="direct",
                                 steps=(20,))
    assert "^MTD" in zpl


# ── missing fields degrade instead of printing a heading over a blank ─────────

def test_an_empty_value_prints_no_heading_at_all():
    entry = record("otd")
    entry["device"]["site_name"] = ""
    zpl = render_zpl(entry)
    assert "SITE" not in zpl
    assert "HOSTNAME" in zpl       # the rest of the face is unaffected


def test_a_record_with_no_device_block_still_renders():
    """A legacy or truncated record must not take a bench down mid-batch."""
    entry = record("tsw-static")
    entry["device"] = {}
    zpl = render_zpl(entry)
    assert entry["serial"] in zpl
    assert zpl.startswith("^XA")


# ── the review CLI ───────────────────────────────────────────────────────────
# It runs on the bench station, which is Windows, where the shell does NOT
# expand a wildcard and `>` does not write ASCII. Both of those are the CLI's
# problem to solve, so both are asserted here.

def test_a_wildcard_is_expanded_by_the_tool_not_the_shell(capsys):
    """cmd and PowerShell hand a pattern over as a literal filename, so a glob
    that works on a mac would fail on the only machine that has the printer."""
    assert main([str(RECORDS / "*.json")]) == 0
    assert capsys.readouterr().out.count("^XA") == ALL_FORMATS


def test_the_records_are_dumped_in_a_stable_order(capsys):
    """Two runs have to be diffable — the review loop is a visual comparison
    against the previous output."""
    main([str(RECORDS / "*.json")])
    first = capsys.readouterr().out
    main([str(RECORDS / "*.json")])
    assert capsys.readouterr().out == first


def test_a_pattern_matching_nothing_says_so(capsys):
    """Rather than reporting a file literally named `*.json` as missing."""
    assert main([str(RECORDS / "*.xml")]) == 2
    assert "no files match" in capsys.readouterr().err


def test_a_named_file_that_is_missing_still_raises(tmp_path):
    """Only patterns are the CLI's business; a plain name it cannot find should
    fail the way it always has, naming the file."""
    with pytest.raises(FileNotFoundError):
        main([str(tmp_path / "nope.json")])


def test_the_written_file_is_the_bytes_the_printer_wants(tmp_path):
    """`-o` exists because PowerShell 5.1's `>` writes UTF-16 with a BOM, and a
    ZD421 fed that prints a page of nothing. Read back as ASCII: a byte over
    127 anywhere in here would mean a label that prints wrong on the bench and
    right in every test that renders to a string."""
    out = tmp_path / "all.zpl"
    assert main(["-o", str(out), str(RECORDS / "*.json")]) == 0
    raw = out.read_bytes()
    assert raw.decode("ascii").count("^XA") == ALL_FORMATS
    assert not raw.startswith(b"\xff\xfe") and not raw.startswith(b"\xef\xbb\xbf")


def test_o_without_a_filename_is_refused(capsys):
    assert main(["-o"]) == 2
    assert "needs a filename" in capsys.readouterr().err


def test_no_arguments_explains_itself(capsys):
    assert main([]) == 2
    assert "usage:" in capsys.readouterr().err


def test_the_cli_can_set_the_quality_for_a_one_off(capsys):
    """For trying a setting against the printer before writing it into the
    station file."""
    assert main(["--darkness", "24", "--speed", "3", "--media", "direct",
                 str(RECORDS / "tsw-static.json")]) == 0
    out = capsys.readouterr().out
    assert "~SD24" in out and "^PR3" in out and "^MTD" in out


def test_the_cli_prints_a_ladder(capsys):
    assert main(["--ladder", str(RECORDS / "tsw-static.json")]) == 0
    out = capsys.readouterr().out
    assert out.count("^XA") > 1
    assert len({line for line in out.splitlines()
                if line.startswith("~SD")}) == out.count("^XA")


def test_the_ladder_reports_labels_not_files(tmp_path, capsys):
    """One record in, seven labels out — a count of files would tell the
    operator to expect one label and leave six on the roll."""
    out = tmp_path / "ladder.zpl"
    assert main(["--ladder", "-o", str(out),
                 str(RECORDS / "tsw-static.json")]) == 0
    written = out.read_text(encoding="ascii").count("^XA")
    assert f"{written} label(s)" in capsys.readouterr().err
    assert written > 1


def test_the_cli_can_override_the_count(capsys):
    assert main(["--copies", "1", str(RECORDS / "tsw-static.json")]) == 0
    assert "^PQ" not in capsys.readouterr().out


def test_the_cli_ladder_ignores_the_count(capsys):
    """`--ladder --copies 2` is a request for two of every rung, which is a
    longer strip that says exactly what the short one did."""
    assert main(["--ladder", "--copies", "2",
                 str(RECORDS / "tsw-static.json")]) == 0
    assert "^PQ" not in capsys.readouterr().out


@pytest.mark.parametrize("flag", ["--darkness", "--speed", "--media",
                                  "--copies"])
def test_a_quality_flag_without_a_value_is_refused(capsys, flag):
    assert main([flag]) == 2
    assert "needs a value" in capsys.readouterr().err


@pytest.mark.parametrize("flag,complaint", [("--darkness", "not a number"),
                                            ("--speed", "not a number"),
                                            ("--copies", "not a number"),
                                            ("--media", "must be one of")])
def test_a_quality_flag_that_swallows_the_filename_says_so(capsys, flag,
                                                           complaint):
    """`--darkness record.json` takes the record as the value. That is how flag
    parsing works, but the operator typing it needs to be told, not handed an
    empty dump."""
    assert main([flag, str(RECORDS / "tsw-static.json")]) == 2
    err = capsys.readouterr().err
    assert complaint in err
    assert "tsw-static.json" in err or "must be one of" in err


def test_a_darkness_that_is_not_a_number_is_refused(capsys):
    assert main(["--darkness", "dark", str(RECORDS / "tsw-static.json")]) == 2
    assert "not a number" in capsys.readouterr().err


def test_an_unknown_media_type_is_refused(capsys):
    """`^MT` takes exactly two values and the wrong one is the difference
    between a readable label and a pale one, so a typo stops here."""
    assert main(["--media", "thermal", str(RECORDS / "tsw-static.json")]) == 2
    assert "--media must be one of" in capsys.readouterr().err


# ── the switch's second label: which socket takes what ───────────────────────

def test_only_the_poe_switch_earns_a_second_label():
    """Every other tool provisions one device with one identity. The switch is
    the only one whose label has to say something about the things plugged
    into it."""
    for name in ALL_RECORDS:
        extras = extra_contents(record(name))
        assert len(extras) == (1 if name == "planet" else 0), name


def test_the_port_map_says_what_the_run_recorded_and_nothing_else():
    """The rows come from the plan the tool wrote into the record. A label that
    guessed which socket a radar was on would be worse than no label at all."""
    entry = record("planet")
    content = extra_contents(entry)[0]
    assert content.face == "port-map"
    assert content.pairing == [tuple(row) for row in entry["device"]["port_map"]]
    assert content.hero == "192.168.88.3"


def test_a_switch_with_no_recorded_plan_prints_no_port_map():
    """A record from before the plan was recorded — or one whose config had no
    named ports — gets its QA label and nothing invented beside it."""
    entry = record("planet")
    entry["device"].pop("port_map")
    assert extra_contents(entry) == []


def test_the_port_map_carries_no_barcode():
    """It is the same label on every switch at a site; the unit's identity is
    on the QA label beside it, and the room went to the tenth socket."""
    zpl = render_content(extra_contents(record("planet"))[0])
    assert "^BC" not in zpl
    assert record("planet")["serial"] not in zpl


def test_every_socket_reaches_the_label():
    zpl = render_content(extra_contents(record("planet"))[0])
    for port, what in record("planet")["device"]["port_map"]:
        assert f"^FD{what}^FS" in zpl, f"gi{port} is not on the label"


def test_no_row_of_the_port_map_falls_off_the_media():
    """Ten rows on a 29 mm label is the tightest face there is: it runs in two
    columns and has no barcode, so the usual bottom anchor does not guard it."""
    zpl = render_content(extra_contents(record("planet"))[0])
    for x, y in _placed(zpl):
        assert MARGIN <= x <= LABEL_W - MARGIN, f"field {x} dots across the head"
        assert MARGIN_Y <= y <= LABEL_H - MARGIN_Y, f"field {y} dots along the feed"


def test_more_sockets_than_the_label_holds_is_refused_not_truncated():
    """A truncated port map still looks complete. Refusing turns it into the
    missing label the operator is warned about."""
    entry = record("planet")
    entry["device"]["port_map"] += [["11", "SFP 1"], ["12", "SFP 2"]]
    with pytest.raises(ValueError, match="the label holds"):
        extra_contents(entry)


def test_the_cli_dumps_the_port_map_beside_the_qa_label(capsys):
    """The review loop is "dump it and paste it into labelary" — a face it
    never printed is a face nobody looks at."""
    assert main([str(RECORDS / "planet.json")]) == 0
    out = capsys.readouterr().out
    assert out.count("^XA") == 2
    assert "PORT MAP" in out
