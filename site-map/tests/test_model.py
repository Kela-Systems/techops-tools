"""Loading and structural validation."""
import textwrap

import pytest

from model import SCHEMA_VERSION, SiteModelError, load_site

MINIMAL = """
version: 1
site: t
subnet: 192.168.88.0/24
nodes:
  a:
    kind: switch
    addr: 192.168.88.2
"""


def write(tmp_path, text):
    path = tmp_path / "site.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def test_loads_minimal_site(tmp_path):
    site = load_site(write(tmp_path, MINIMAL))
    assert site.name == "t"
    assert str(site.subnet) == "192.168.88.0/24"
    assert site.nodes["a"].addr == "192.168.88.2"
    # An address with no stated source is one the bench assigned; that is the
    # overwhelmingly common case and spelling it out on every node is noise.
    assert site.nodes["a"].addr_source == "static-bench"


def test_node_without_addr_defaults_to_no_source(tmp_path):
    site = load_site(write(tmp_path, """
        version: 1
        site: t
        subnet: 192.168.88.0/24
        nodes:
          a: {kind: psu}
    """))
    assert site.nodes["a"].addr_source == "none"


@pytest.mark.parametrize("body,expected", [
    ("", "empty"),
    ("version: 99\nsite: t\nsubnet: 192.168.88.0/24\nnodes: {a: {kind: x}}", "schema"),
    ("version: 1\nsubnet: 192.168.88.0/24\nnodes: {a: {kind: x}}", "no 'site'"),
    ("version: 1\nsite: t\nnodes: {a: {kind: x}}", "no 'subnet'"),
    ("version: 1\nsite: t\nsubnet: nope\nnodes: {a: {kind: x}}", "not an IPv4 subnet"),
    ("version: 1\nsite: t\nsubnet: 192.168.88.0/24\nnodes: {}", "no nodes"),
    ("version: 1\nsite: t\nsubnet: 192.168.88.0/24\nnodes: {a: {}}", "no kind"),
])
def test_unusable_files_are_rejected(tmp_path, body, expected):
    with pytest.raises(SiteModelError, match=expected):
        load_site(write(tmp_path, body))


def test_bad_address_is_rejected(tmp_path):
    with pytest.raises(SiteModelError, match="not an IPv4 address"):
        load_site(write(tmp_path, """
            version: 1
            site: t
            subnet: 192.168.88.0/24
            nodes:
              a: {kind: switch, addr: 192.168.88.999}
        """))


def test_bad_addr_source_is_rejected(tmp_path):
    with pytest.raises(SiteModelError, match="addr_source"):
        load_site(write(tmp_path, """
            version: 1
            site: t
            subnet: 192.168.88.0/24
            nodes:
              a: {kind: switch, addr: 192.168.88.2, addr_source: magic}
        """))


def test_invalid_yaml_is_rejected(tmp_path):
    with pytest.raises(SiteModelError, match="invalid YAML"):
        load_site(write(tmp_path, "version: 1\n  bad: [indent"))


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(SiteModelError, match="cannot read"):
        load_site(tmp_path / "nope.yaml")


def test_reservation_accepts_octets_and_full_addresses(tmp_path):
    site = load_site(write(tmp_path, """
        version: 1
        site: t
        subnet: 192.168.88.0/24
        reservations:
          octets: 30-50
          single: 70
          full: 192.168.88.10-192.168.88.20
          owned: {range: 1-3, owner: bench, dynamic: true}
        nodes:
          a: {kind: switch, addr: 192.168.88.2}
    """))
    by_name = {r.name: r for r in site.reservations}
    assert (str(by_name["octets"].start), str(by_name["octets"].end)) == (
        "192.168.88.30", "192.168.88.50")
    assert by_name["single"].start == by_name["single"].end
    assert str(by_name["full"].end) == "192.168.88.20"
    assert by_name["owned"].owner == "bench"
    assert by_name["owned"].dynamic is True
    # Everything else defaults to a documented static plan, not a pool.
    assert by_name["octets"].dynamic is False


def test_backwards_reservation_is_rejected(tmp_path):
    with pytest.raises(SiteModelError, match="backwards"):
        load_site(write(tmp_path, """
            version: 1
            site: t
            subnet: 192.168.88.0/24
            reservations: {bad: 50-30}
            nodes: {a: {kind: switch}}
        """))


def test_edges_require_both_ends(tmp_path):
    with pytest.raises(SiteModelError, match="no 'to'"):
        load_site(write(tmp_path, """
            version: 1
            site: t
            subnet: 192.168.88.0/24
            nodes: {a: {kind: switch}}
            net:
              - {from: a}
        """))


def test_bad_power_via_is_rejected(tmp_path):
    with pytest.raises(SiteModelError, match="via 'steam'"):
        load_site(write(tmp_path, """
            version: 1
            site: t
            subnet: 192.168.88.0/24
            nodes: {a: {kind: switch}, b: {kind: radar}}
            power:
              - {from: a, to: b, via: steam}
        """))


def test_walk_net_survives_a_cycle(tmp_path):
    """A malformed site must still render; the linter reports the loop."""
    site = load_site(write(tmp_path, """
        version: 1
        site: t
        subnet: 192.168.88.0/24
        uplink: a
        nodes:
          a: {kind: switch}
          b: {kind: switch}
        net:
          - {from: a, to: b}
          - {from: b, to: a}
    """))
    walked = list(site.walk_net("a"))
    assert len(walked) < 10


def test_net_edge_label_shows_both_ends(tmp_path):
    site = load_site(write(tmp_path, """
        version: 1
        site: t
        subnet: 192.168.88.0/24
        nodes: {a: {kind: switch}, b: {kind: switch}}
        net:
          - {from: a, to: b, port: lan1, peer_port: gi10}
    """))
    assert site.net[0].label() == "lan1 -> gi10"


def test_power_edge_label_shows_both_sockets(tmp_path):
    site = load_site(write(tmp_path, """
        version: 1
        site: t
        subnet: 192.168.88.0/24
        nodes: {psu: {kind: psu}, sw: {kind: switch}}
        power:
          - {from: psu, to: sw, outlet: a1, inlet: PWR1, via: dc, draw_w: 12}
    """))
    assert site.power[0].label() == "a1 -> PWR1 dc 12 W"
