"""MAC -> vendor resolution. The database is faked so tests stay offline."""
import pytest

import oui

# Shaped like nmap-mac-prefixes: prefix, a space, then the vendor name. The
# 8C1F64 / 8C1F64E74 pair is the case that matters — a 24-bit hit that is only
# the registrar, with the real vendor at 36 bits.
FAKE_DB = {
    "209727": "Teltonika Networks UAB",
    "8C1F64": "Ieee Registration Authority",
    "8C1F64E74": "Magosys Systems",
    "E8CF83": "Dell",
    "BC74D7": "HangZhou JuRu Technology",
    "A8F7E0": "Planet Technology",
}

ARP = """\
rut-kela-fob-03.lan (192.168.88.1) at 20:97:27:36:55:ec [ether] on enp128s31f6
? (192.168.88.132) at 8c:1f:64:e7:48:c7 [ether] on enp128s31f6
kela-fob-03-operator.lan (192.168.88.29) at e8:cf:83:3f:f3:83 [ether] on enp128s31f6
KC0500PAZ00052.lan (192.168.88.30) at bc:74:d7:81:17:b1 [ether] on enp128s31f6
some noise that is not an arp row at all
"""


def test_longest_prefix_wins_over_the_registrar():
    """The whole point: 24 bits says 'IEEE', 36 bits says who actually made it."""
    vendor = oui.lookup("8c:1f:64:e7:48:c7", FAKE_DB)
    assert vendor.name == "Magosys Systems"
    assert vendor.prefix_bits == 36
    assert vendor.matched == "8C1F64E74"
    assert vendor.is_registrar is False


def test_a_registrar_only_hit_is_flagged():
    """A MAC in the same block but outside the MA-S assignment resolves only
    to the registrar, and must say so rather than imply IEEE built it."""
    vendor = oui.lookup("8c:1f:64:00:00:01", FAKE_DB)
    assert vendor.name == "Ieee Registration Authority"
    assert vendor.prefix_bits == 24
    assert vendor.is_registrar is True


def test_plain_24_bit_lookup():
    vendor = oui.lookup("20:97:27:36:55:ec", FAKE_DB)
    assert vendor.name == "Teltonika Networks UAB"
    assert vendor.prefix_bits == 24


def test_unknown_prefix_returns_nothing():
    assert oui.lookup("00:00:00:00:00:01", FAKE_DB) is None


@pytest.mark.parametrize("raw", [
    "8c:1f:64:e7:48:c7", "8C1F64E748C7", "8c-1f-64-e7-48-c7", "8c1f.64e7.48c7",
])
def test_separators_do_not_matter(raw):
    assert oui.normalise(raw) == "8C1F64E748C7"


def test_too_short_to_be_a_mac_is_rejected():
    with pytest.raises(oui.OuiError, match="not a MAC"):
        oui.normalise("8c:1f")


def test_parse_arp_reads_ip_host_and_mac():
    entries = oui.parse_arp(ARP)
    assert len(entries) == 4
    first = entries[0]
    assert first.ip == "192.168.88.1"
    assert first.hostname == "rut-kela-fob-03.lan"
    assert first.mac == "20:97:27:36:55:ec"


def test_parse_arp_treats_a_question_mark_as_no_hostname():
    entries = {e.ip: e for e in oui.parse_arp(ARP)}
    assert entries["192.168.88.132"].hostname is None


def test_parse_arp_ignores_lines_that_are_not_arp_rows():
    assert all(e.ip for e in oui.parse_arp(ARP))


def test_a_raythink_serial_hostname_names_the_model():
    entries = oui.resolve(oui.parse_arp(ARP), FAKE_DB)
    cam = [e for e in entries if e.ip == "192.168.88.30"][0]
    assert cam.model == "Raythink PC464A1"
    assert any("serial" in r for r in cam.because)


def test_a_rut_hostname_names_the_router():
    entries = oui.resolve(oui.parse_arp(ARP), FAKE_DB)
    router = [e for e in entries if e.ip == "192.168.88.1"][0]
    assert router.model == "Teltonika RUTM08"


def test_a_vendor_with_several_catalogue_entries_is_not_guessed():
    """Magos makes both the radar and the APU, and a MAC cannot tell them
    apart — so the tool must refuse to pick one."""
    entries = oui.resolve(oui.parse_arp(ARP), FAKE_DB)
    magos = [e for e in entries if e.ip == "192.168.88.132"][0]
    assert magos.vendor.name == "Magosys Systems"
    assert magos.model is None
    assert any("several devices" in r for r in magos.because)


def test_resolve_leaves_an_unknown_vendor_empty():
    entry = oui.Entry(mac="00:00:00:00:00:01")
    [out] = oui.resolve([entry], FAKE_DB)
    assert out.vendor is None and out.model is None


def test_load_db_skips_comments_and_blanks(tmp_path):
    path = tmp_path / "prefixes"
    path.write_text("# a comment\n\n209727 Teltonika Networks UAB\n", encoding="utf-8")
    assert oui.load_db(path) == {"209727": "Teltonika Networks UAB"}


def test_an_empty_db_is_an_error(tmp_path):
    path = tmp_path / "prefixes"
    path.write_text("# nothing but a comment\n", encoding="utf-8")
    with pytest.raises(oui.OuiError, match="no usable entries"):
        oui.load_db(path)


def test_a_missing_db_path_is_an_error(tmp_path):
    with pytest.raises(oui.OuiError, match="no vendor database"):
        oui.find_db(str(tmp_path / "nope"))
