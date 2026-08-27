"""Tests for the device-label parser (bench_core.device_label, TEC-349).

No hardware: every case is a string a scanner could plausibly deliver. The two
`REAL_*` constants are verbatim scans of real units, so a regression that only
shows up on genuine labels cannot slip through.
"""
import pytest

from bench_core.device_label import (
    MIN_KEYS,
    DeviceLabel,
    parse_device_label,
)

# Actual scans, kept exactly as the scanner produced them.
REAL_OTD = ("SN:6008219573;I:864088065513384;M:2097272B00F7;"
            "U:admin;PW:zZ?40*kA;B:015;")
REAL_RUTM = "SN:6010212527;M:20972732F638;U:admin;PW:mL$7=b6N;B:039;"


# ── the real thing ───────────────────────────────────────────────────────────

def test_real_otd500_label():
    label = parse_device_label(REAL_OTD)
    assert label.serial == "6008219573"
    assert label.imei == "864088065513384"
    assert label.mac == "2097272B00F7"
    assert label.username == "admin"
    assert label.password == "zZ?40*kA"
    assert label.batch == "015"
    assert label.unknown_keys == []


def test_real_rutm08_label():
    label = parse_device_label(REAL_RUTM)
    assert label.serial == "6010212527"
    assert label.imei == ""          # no modem, so no IMEI on the sticker
    assert label.mac == "20972732F638"
    assert label.username == "admin"
    assert label.password == "mL$7=b6N"
    assert label.batch == "039"


def test_family_follows_the_imei():
    # Advisory only, but it is what separates the two tools' devices: an IMEI
    # means a modem, and only the OTD500 has one.
    assert parse_device_label(REAL_OTD).family() == "cellular"
    assert parse_device_label(REAL_RUTM).family() == "non-cellular"


# ── what a keyboard-wedge scanner adds or drops ──────────────────────────────

@pytest.mark.parametrize("raw", [
    "~" + REAL_OTD,                 # the configured scan-marker prefix
    REAL_OTD + "\r",                # CR suffix
    REAL_OTD + "\r\n",              # CR+LF
    "  " + REAL_OTD + "  ",         # stray whitespace
    "\x00" + REAL_OTD,              # a stray NUL
    "~" + REAL_OTD + "\r",          # prefix and suffix together
])
def test_scanner_decoration_is_ignored(raw):
    assert parse_device_label(raw) == parse_device_label(REAL_OTD)


def test_missing_trailing_semicolon():
    label = parse_device_label(REAL_RUTM.rstrip(";"))
    assert label.password == "mL$7=b6N"
    assert label.batch == "039"


def test_lowercase_keys_and_loose_spacing():
    label = parse_device_label("sn:1 ; m : AABBCCDDEEFF ; pw : secret ; b : 7 ;")
    assert (label.serial, label.mac, label.password, label.batch) == \
        ("1", "AABBCCDDEEFF", "secret", "7")


# ── the delimiter ambiguity ──────────────────────────────────────────────────
#
# The reason values are anchored on the next key rather than on the next `;`.
# A password that is *almost* right is the worst outcome available here: it
# looks like a device fault, not a parsing bug.

def test_password_containing_a_semicolon_survives():
    label = parse_device_label("SN:1;M:AABBCCDDEEFF;PW:ab;cd;B:015;")
    assert label.password == "ab;cd"
    assert label.batch == "015"


def test_password_containing_a_colon_survives():
    label = parse_device_label("SN:1;M:AABBCCDDEEFF;PW:a:b;B:015;")
    assert label.password == "a:b"
    assert label.batch == "015"


def test_password_of_pure_punctuation_survives():
    # The charset self-test barcode (docs/scanner/selftest-charset.svg): every
    # shift-dependent ASCII mark except the two delimiters.
    pw = "aZ09" + "!\"#$%&'()*+,-./<=>?@[\\]^_`{|}~"
    label = parse_device_label(f"SN:1;M:AABBCCDDEEFF;U:admin;PW:{pw};B:000;")
    assert label.password == pw


def test_empty_password_parses_as_empty_not_missing():
    # A real case: an already-provisioned device gets configured with an empty
    # label password, which the pipeline reads as "use the shared password".
    label = parse_device_label("SN:1;M:AABBCCDDEEFF;PW:;B:015;")
    assert label is not None
    assert label.password == ""
    assert label.batch == "015"


# ── forward compatibility ────────────────────────────────────────────────────

def test_unknown_key_is_recorded_not_fatal():
    label = parse_device_label("SN:1;M:AABBCCDDEEFF;XY:zzz;PW:pw;")
    assert label.serial == "1"
    assert label.password == "pw"
    assert label.unknown_keys == ["XY"]


def test_unknown_key_does_not_corrupt_the_value_before_it():
    # The value ahead of an unknown key must end at it, not swallow it.
    label = parse_device_label("SN:1;M:AABBCCDDEEFF;XY:zzz;PW:pw;B:9;")
    assert label.mac == "AABBCCDDEEFF"
    assert label.batch == "9"


def test_duplicate_key_keeps_the_first_value():
    label = parse_device_label("SN:1;M:AABBCCDDEEFF;PW:first;PW:second;")
    assert label.password == "first"


# ── rejection ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "", None, "   ", "hello world",
    "https://example.com/thing",     # a URL QR on some other sticker
    "1234567890",                    # a plain barcode
    "M:AABBCCDDEEFF;",               # one key is not enough structure
    "PW:lonely",
])
def test_non_labels_are_rejected(raw):
    assert parse_device_label(raw) is None


def test_min_keys_is_the_boundary():
    assert MIN_KEYS == 2
    assert parse_device_label("SN:1;") is None
    assert parse_device_label("SN:1;M:AABBCCDDEEFF;") is not None


def test_a_badge_scan_is_not_a_device_label():
    # The operator badge box shares the page with this; a badge must never be
    # mistaken for a label (or vice versa).
    assert parse_device_label("EMP-00417") is None
    assert parse_device_label("Dana K") is None


# ── MAC cross-check ──────────────────────────────────────────────────────────

def test_matches_mac_across_separator_styles():
    label = parse_device_label(REAL_OTD)
    for form in ("20:97:27:2b:00:f7", "20-97-27-2B-00-F7",
                 "2097272B00F7", "20:97:27:2B:00:F7"):
        assert label.matches_mac(form) is True, form


def test_matches_mac_rejects_a_different_device():
    # The whole point: the operator scanned the box next to the plugged-in one.
    assert parse_device_label(REAL_OTD).matches_mac("20:97:27:32:f6:38") is False


def test_matches_mac_is_none_when_either_side_is_unknown():
    # "Could not tell" must be distinguishable from "they disagree" — the
    # caller arms with a warning in the first case and refuses in the second.
    label = parse_device_label(REAL_OTD)
    assert label.matches_mac(None) is None
    assert label.matches_mac("") is None
    no_mac = parse_device_label("SN:1;PW:pw;")
    assert no_mac.matches_mac("20:97:27:2b:00:f7") is None


# ── the password must not leak ────────────────────────────────────────────────

def test_redacted_omits_the_password_but_keeps_its_shape():
    label = parse_device_label(REAL_OTD)
    safe = label.redacted()
    assert "zZ?40*kA" not in repr(safe)
    assert "password" not in safe          # not even an empty/masked key
    assert safe["has_password"] is True
    assert safe["password_length"] == 8
    assert safe["serial"] == "6008219573"
    assert safe["mac"] == "2097272B00F7"
    assert safe["batch"] == "015"
    assert safe["family"] == "cellular"


def test_redacted_reports_an_absent_password():
    safe = parse_device_label("SN:1;M:AABBCCDDEEFF;PW:;").redacted()
    assert safe["has_password"] is False
    assert safe["password_length"] == 0


@pytest.mark.parametrize("render", [repr, str, "{}".format, lambda o: f"{o}"])
def test_password_never_renders(render):
    # A traceback, a log line, an f-string or a debugger dump must not be able
    # to spill the password just by stringifying the object.
    label = parse_device_label(REAL_OTD)
    text = render(label)
    assert "zZ?40*kA" not in text
    assert "<redacted 8 chars>" in text
    assert "6008219573" in text            # the non-secret fields still help


def test_password_not_in_a_container_repr():
    # Dataclasses inherit repr into container reprs; make sure ours is used.
    label = parse_device_label(REAL_OTD)
    assert "zZ?40*kA" not in repr([label])
    assert "zZ?40*kA" not in repr({"label": label})


def test_the_password_is_still_reachable_deliberately():
    # Redaction must not have made the parser useless.
    assert parse_device_label(REAL_OTD).password == "zZ?40*kA"


# ── equality / construction ──────────────────────────────────────────────────

def test_empty_label_is_falsy_on_every_field():
    label = DeviceLabel()
    assert label.redacted()["has_password"] is False
    assert label.family() == "non-cellular"
    assert label.matches_mac("20:97:27:2b:00:f7") is None
