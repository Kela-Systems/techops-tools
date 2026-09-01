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
    FACES,
    FOOT_Y,
    LABEL_H,
    LABEL_W,
    MARGIN,
    QR_X,
    barcode_module_width,
    label_content,
    main,
    pick_face,
    render_zpl,
)

RECORDS = Path(__file__).parent / "label-records"


def record(name: str) -> dict:
    return json.loads((RECORDS / f"{name}.json").read_text(encoding="utf-8"))


ALL_RECORDS = sorted(p.stem for p in RECORDS.glob("*.json"))


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


def test_tsw_makes_the_serial_the_second_loudest_line():
    """A switch has no site and no hostname, so the serial is the only thing
    that tells two of them apart."""
    content = label_content(record("tsw-static"))
    assert content.hero == "192.168.88.2"
    assert content.get("SERIAL") == "6010620710"


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

def test_dhcp_prints_the_word_and_the_mac_not_an_address():
    content = label_content(record("tsw-dhcp"))
    assert content.hero == "DHCP"
    assert content.get("MAC") == "20:97:2B:2B:00:F7"
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
    assert content.hero == "DHCP"
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
    assert label_content(entry).hero == "DHCP"
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
    assert content.hero == "DHCP"
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
    assert "CH1" in content.qr


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
def test_every_face_carries_a_qr_and_a_code128_of_the_serial(name):
    entry = record(name)
    zpl = render_zpl(entry)
    assert "^BQN,2," in zpl, "no QR — the scanner has nothing to match back"
    assert "^BCN," in zpl, "no Code 128 of the serial"
    assert entry["serial"] in zpl


@pytest.mark.parametrize("name", ALL_RECORDS)
def test_the_qr_payload_is_the_kela_format(name):
    payload = label_content(record(name)).qr
    assert payload.startswith("KELA|")
    assert record(name)["serial"] in payload


def test_the_qr_payload_never_holds_a_zpl_control_character():
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

_FO = re.compile(r"\^FO(\d+),(\d+)")


@pytest.mark.parametrize("name", ALL_RECORDS)
def test_no_field_starts_outside_the_label(name):
    for x, y in _FO.findall(render_zpl(record(name))):
        assert 0 <= int(x) <= LABEL_W, f"{name}: field at x={x}"
        assert 0 <= int(y) <= LABEL_H, f"{name}: field at y={y}"


@pytest.mark.parametrize("name", ALL_RECORDS)
def test_the_body_never_starts_under_the_barcode(name):
    """Everything below FOOT_Y belongs to the Code 128 and its text. A body
    field down there would print through the bars."""
    for line in render_zpl(record(name)).splitlines():
        if "^BC" in line:                    # the barcode itself lives there
            continue
        for x, y in _FO.findall(line):
            if int(x) >= QR_X:               # the QR column is its own thing
                continue
            assert int(y) < FOOT_Y, \
                f"{name}: body field at y={y} is under the barcode"


@pytest.mark.parametrize("name", ALL_RECORDS)
def test_the_barcode_fits_the_stock(name):
    """Code 128 is clipped, not scaled, when it is too wide — and a clipped
    barcode still looks like a barcode."""
    serial = record(name)["serial"]
    modules = (len(serial) + 3) * 11 + 2
    assert modules * barcode_module_width(serial) <= LABEL_W - 2 * MARGIN


def test_a_very_long_serial_narrows_the_barcode_instead_of_overflowing():
    assert barcode_module_width("6010212527") == 4
    assert barcode_module_width("X" * 40) < 4


@pytest.mark.parametrize("name", ALL_RECORDS)
def test_the_label_declares_the_15x5_stock(name):
    zpl = render_zpl(record(name))
    assert f"^PW{LABEL_W}" in zpl and f"^LL{LABEL_H}" in zpl


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
    assert capsys.readouterr().out.count("^XA") == len(ALL_RECORDS)


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
    assert raw.decode("ascii").count("^XA") == len(ALL_RECORDS)
    assert not raw.startswith(b"\xff\xfe") and not raw.startswith(b"\xef\xbb\xbf")


def test_o_without_a_filename_is_refused(capsys):
    assert main(["-o"]) == 2
    assert "needs a filename" in capsys.readouterr().err


def test_no_arguments_explains_itself(capsys):
    assert main([]) == 2
    assert "usage:" in capsys.readouterr().err
