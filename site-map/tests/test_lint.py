"""The offline checks. Each test pins one finding code."""
import textwrap

import pytest

from lint import ERROR, WARN, lint
from model import load_site


def site_from(tmp_path, text):
    path = tmp_path / "site.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return load_site(path)


RESERVATIONS_OVERLAPPING = (
    "reservations:\n"
    "  cameras: {range: 30-50, owner: cam, dynamic: true}\n"
    "  radars: {range: 50-53}\n"
)

RESERVATIONS_DISJOINT = (
    "reservations:\n"
    "  cameras: {range: 30-49, owner: cam, dynamic: true}\n"
    "  radars: {range: 50-53}\n"
)

RESERVATIONS_CAMERA_POOL = (
    "reservations:\n"
    "  cameras: {range: 30-50, owner: cam, dynamic: true}\n"
)

RESERVATIONS_RADARS_STATIC = (
    "reservations:\n"
    "  radars: {range: 50-53, owner: bench}\n"
)


def codes(findings):
    return {f.code for f in findings}


def only(findings, code):
    return [f for f in findings if f.code == code]


CLEAN = """
version: 1
site: t
subnet: 192.168.88.0/24
uplink: router
nodes:
  mains: {kind: mains, roles: [power-source]}
  router:
    kind: router
    addr: 192.168.88.1
    roles: [gateway, dhcp-server]
  sw:
    kind: poe-switch
    addr: 192.168.88.3
    poe_budget_w: 240
    poe_managed: true
  radar: {kind: radar, addr: 192.168.88.50}
net:
  - {from: router, to: sw, port: lan1}
  - {from: sw, to: radar, port: gi1}
power:
  - {from: mains, to: router, via: mains}
  - {from: mains, to: sw, via: mains}
  - {from: sw, to: radar, outlet: gi1, via: poe, draw_w: 35}
"""


def structural(findings):
    """Findings about the site being wrong, as opposed to unproven."""
    from lint import EVIDENCE_CODES
    return [f for f in findings if f.code not in EVIDENCE_CODES]


def test_a_correct_site_has_no_structural_problems(tmp_path):
    assert structural(lint(site_from(tmp_path, CLEAN))) == []


def test_an_unmarked_site_is_reported_as_unverified(tmp_path):
    """Silence must not read as confirmation: no evidence means unverified."""
    findings = lint(site_from(tmp_path, CLEAN))
    assert codes(findings) >= {"link-unverified", "power-unverified"}
    assert all(f.severity == WARN for f in findings)


def test_duplicate_address_is_an_error(tmp_path):
    findings = lint(site_from(tmp_path, CLEAN.replace(
        "radar: {kind: radar, addr: 192.168.88.50}",
        "radar: {kind: radar, addr: 192.168.88.3}")))
    found = only(findings, "addr-collision")
    assert len(found) == 1
    assert found[0].severity == ERROR
    assert "radar" in found[0].message and "sw" in found[0].message


def test_address_outside_the_subnet_is_an_error(tmp_path):
    findings = lint(site_from(tmp_path, CLEAN.replace(
        "addr: 192.168.88.50", "addr: 192.168.0.100")))
    found = only(findings, "addr-off-subnet")
    assert [f.severity for f in found] == [ERROR]


def test_a_factory_address_off_subnet_is_only_a_warning(tmp_path):
    """A fresh PLANET legitimately sits on 192.168.0.100 until provisioned."""
    findings = lint(site_from(tmp_path, CLEAN.replace(
        "radar: {kind: radar, addr: 192.168.88.50}",
        "radar: {kind: radar, addr: 192.168.0.100, addr_source: factory}")))
    found = only(findings, "addr-off-subnet")
    assert [f.severity for f in found] == [WARN]


def test_overlapping_reserved_ranges_are_an_error(tmp_path):
    """The real bug: the camera cycle ends on .50, where radar_0 lives."""
    findings = lint(site_from(tmp_path, CLEAN + RESERVATIONS_OVERLAPPING))
    found = only(findings, "pool-overlap")
    assert len(found) == 1
    assert "192.168.88.50" in found[0].message
    assert "dynamically" in found[0].message


def test_static_ranges_that_do_not_overlap_are_fine(tmp_path):
    findings = lint(site_from(tmp_path, CLEAN + RESERVATIONS_DISJOINT))
    assert "pool-overlap" not in codes(findings)
    assert "addr-in-foreign-pool" not in codes(findings)


def test_fixed_address_inside_a_dynamic_pool_is_an_error(tmp_path):
    findings = lint(site_from(tmp_path, CLEAN + RESERVATIONS_CAMERA_POOL))
    found = only(findings, "addr-in-foreign-pool")
    assert [f.where for f in found] == ["radar"]


def test_a_static_range_over_an_address_is_not_a_finding(tmp_path):
    """A static plan documenting where the radar sits is the point of it."""
    findings = lint(site_from(tmp_path, CLEAN + RESERVATIONS_RADARS_STATIC))
    assert "addr-in-foreign-pool" not in codes(findings)


def test_the_pool_owner_may_sit_in_its_own_pool(tmp_path):
    findings = lint(site_from(tmp_path, CLEAN.replace(
        "radar: {kind: radar, addr: 192.168.88.50}",
        "radar: {kind: radar, addr: 192.168.88.30, addr_pool: cameras}")
        + "reservations:\n  cameras: {range: 30-50, dynamic: true}\n"))
    assert "addr-in-foreign-pool" not in codes(findings)


def test_undeclared_pool_is_an_error(tmp_path):
    findings = lint(site_from(tmp_path, CLEAN.replace(
        "radar: {kind: radar, addr: 192.168.88.50}",
        "radar: {kind: radar, addr: 192.168.88.50, addr_pool: nope}")))
    assert "unknown-pool" in codes(findings)


def test_a_lease_with_no_dhcp_server_is_an_error(tmp_path):
    findings = lint(site_from(tmp_path, CLEAN
        .replace("roles: [gateway, dhcp-server]", "roles: [gateway]")
        .replace("radar: {kind: radar, addr: 192.168.88.50}",
                 "radar: {kind: radar, addr_source: dhcp-lease}")))
    assert "dhcp-no-server" in codes(findings)


def test_two_dhcp_servers_on_one_subnet_is_an_error(tmp_path):
    findings = lint(site_from(tmp_path, CLEAN.replace(
        "    poe_budget_w: 240", "    roles: [dhcp-server]\n    poe_budget_w: 240")))
    assert "dhcp-multi-server" in codes(findings)


def test_a_lease_recorded_as_a_fixed_address_warns(tmp_path):
    findings = lint(site_from(tmp_path, CLEAN.replace(
        "radar: {kind: radar, addr: 192.168.88.50}",
        "radar: {kind: radar, addr: 192.168.88.50, addr_source: dhcp-lease}")))
    found = only(findings, "dhcp-pinned-addr")
    assert [f.severity for f in found] == [WARN]


def test_edges_to_undeclared_nodes_are_an_error(tmp_path):
    findings = lint(site_from(tmp_path, CLEAN.replace(
        "  - {from: sw, to: radar, port: gi1}",
        "  - {from: sw, to: radar, port: gi1}\n  - {from: sw, to: ghost, port: gi2}")))
    found = only(findings, "unknown-node")
    assert found and "ghost" in found[0].message


def test_one_port_carrying_two_devices_is_an_error(tmp_path):
    findings = lint(site_from(tmp_path, CLEAN.replace(
        "  radar: {kind: radar, addr: 192.168.88.50}",
        "  radar: {kind: radar, addr: 192.168.88.50}\n"
        "  radar2: {kind: radar, addr: 192.168.88.51}")
        .replace("  - {from: sw, to: radar, port: gi1}",
                 "  - {from: sw, to: radar, port: gi1}\n"
                 "  - {from: sw, to: radar2, port: gi1}")))
    found = only(findings, "port-reuse")
    assert found and "gi1" in found[0].message


def test_a_peer_port_fed_twice_is_an_error(tmp_path):
    findings = lint(site_from(tmp_path, CLEAN.replace(
        "  - {from: router, to: sw, port: lan1}",
        "  - {from: router, to: sw, port: lan1, peer_port: gi10}\n"
        "  - {from: radar, to: sw, peer_port: gi10}")))
    found = only(findings, "port-reuse")
    assert found and "gi10" in found[0].message


def test_two_parents_for_one_node_is_an_error(tmp_path):
    findings = lint(site_from(tmp_path, CLEAN.replace(
        "  - {from: sw, to: radar, port: gi1}",
        "  - {from: sw, to: radar, port: gi1}\n"
        "  - {from: router, to: radar, port: lan2}")))
    assert "net-multi-parent" in codes(findings)


def test_a_data_loop_is_an_error(tmp_path):
    findings = lint(site_from(tmp_path, CLEAN.replace(
        "  - {from: sw, to: radar, port: gi1}",
        "  - {from: sw, to: radar, port: gi1}\n  - {from: radar, to: router}")))
    found = only(findings, "net-cycle")
    assert found and found[0].severity == ERROR


def test_a_node_off_the_data_path_warns(tmp_path):
    findings = lint(site_from(tmp_path, CLEAN.replace(
        "  radar: {kind: radar, addr: 192.168.88.50}",
        "  radar: {kind: radar, addr: 192.168.88.50}\n"
        "  stray: {kind: camera, addr: 192.168.88.31}")
        .replace("  - {from: mains, to: sw, via: mains}",
                 "  - {from: mains, to: sw, via: mains}\n"
                 "  - {from: mains, to: stray, via: mains}")))
    found = only(findings, "net-orphan")
    assert [f.where for f in found] == ["stray"]


def test_an_unknown_uplink_is_an_error(tmp_path):
    findings = lint(site_from(tmp_path, CLEAN.replace("uplink: router", "uplink: ghost")))
    assert "unknown-node" in codes(findings)


def test_a_device_with_no_power_is_an_error(tmp_path):
    findings = lint(site_from(tmp_path, CLEAN.replace(
        "  - {from: sw, to: radar, outlet: gi1, via: poe, draw_w: 35}", "")))
    found = only(findings, "unpowered")
    assert [f.where for f in found] == ["radar"]


def test_power_sources_need_no_power_themselves(tmp_path):
    assert "unpowered" not in codes(lint(site_from(tmp_path, CLEAN)))


def test_two_power_feeds_warns_rather_than_errors(tmp_path):
    """PWR1/PWR2 is redundancy; a duplicate edge is a mistake. Can't tell."""
    findings = lint(site_from(tmp_path, CLEAN.replace(
        "  - {from: mains, to: sw, via: mains}",
        "  - {from: mains, to: sw, outlet: PWR1, via: mains}\n"
        "  - {from: mains, to: sw, outlet: PWR2, via: mains}")))
    found = only(findings, "power-multi-source")
    assert [f.severity for f in found] == [WARN]


def test_one_outlet_feeding_two_devices_is_an_error(tmp_path):
    findings = lint(site_from(tmp_path, CLEAN.replace(
        "  - {from: mains, to: router, via: mains}",
        "  - {from: mains, to: router, outlet: a1, via: mains}\n"
        "  - {from: mains, to: radar, outlet: a1, via: mains}")))
    found = only(findings, "outlet-reuse")
    assert found and "a1" in found[0].message


def test_a_power_loop_is_an_error(tmp_path):
    findings = lint(site_from(tmp_path, CLEAN.replace(
        "  - {from: mains, to: router, via: mains}",
        "  - {from: mains, to: router, via: mains}\n"
        "  - {from: radar, to: mains, via: dc}")))
    assert "power-cycle" in codes(findings)


def test_poe_overcommit_is_an_error_when_the_switch_is_managed(tmp_path):
    findings = lint(site_from(tmp_path, CLEAN
        .replace("poe_budget_w: 240", "poe_budget_w: 30")))
    found = only(findings, "poe-overcommit")
    assert [f.severity for f in found] == [ERROR]
    assert "35" in found[0].message


def test_poe_overcommit_only_warns_when_the_switch_manages_itself(tmp_path):
    """poe_managed false is the live PLANET state — nothing enforces a budget,
    so the model cannot call an overcommit a fact."""
    findings = lint(site_from(tmp_path, CLEAN
        .replace("poe_budget_w: 240", "poe_budget_w: 30")
        .replace("poe_managed: true", "poe_managed: false")))
    found = only(findings, "poe-overcommit")
    assert [f.severity for f in found] == [WARN]
    assert "negotiates its own limits" in found[0].message


def test_a_poe_switch_with_no_budget_warns(tmp_path):
    findings = lint(site_from(tmp_path, CLEAN.replace("    poe_budget_w: 240\n", "")))
    assert "poe-no-budget" in codes(findings)


def test_a_poe_port_with_no_draw_warns(tmp_path):
    findings = lint(site_from(tmp_path, CLEAN.replace(", draw_w: 35", "")))
    found = only(findings, "poe-unknown-draw")
    assert found and "floor" in found[0].message


SPOF_SITE = """
version: 1
site: t
subnet: 192.168.88.0/24
uplink: sw
nodes:
  mains: {kind: mains, roles: [power-source]}
  psu: {kind: psu, roles: [power-source]}
  sw: {kind: poe-switch, addr: 192.168.88.3, critical: true}
  radar_a: {kind: radar, addr: 192.168.88.50, critical: true}
  radar_b: {kind: radar, addr: 192.168.88.51, critical: true}
net:
  - {from: sw, to: radar_a, port: gi1}
  - {from: sw, to: radar_b, port: gi2}
power:
  - {from: mains, to: psu, via: mains}
  - {from: psu, to: sw, via: dc}
  - {from: sw, to: radar_a, outlet: gi1, via: poe}
  - {from: sw, to: radar_b, outlet: gi2, via: poe}
"""


def test_a_single_feed_under_several_critical_devices_warns(tmp_path):
    findings = lint(site_from(tmp_path, SPOF_SITE))
    found = only(findings, "power-spof")
    assert found and all(f.severity == WARN for f in found)
    # The PSU and the switch each sit above both radars on a single feed.
    assert {"psu", "sw"} <= {f.where for f in found}


def test_the_mains_feed_is_not_reported_as_a_single_point_of_failure(tmp_path):
    """True at every site and fixable at none, so reporting it is pure noise."""
    findings = lint(site_from(tmp_path, SPOF_SITE))
    assert "mains" not in {f.where for f in only(findings, "power-spof")}


def test_a_dual_fed_node_is_not_a_single_point_of_failure(tmp_path):
    """PWR1/PWR2 both wired is exactly the fix this warning asks for."""
    findings = lint(site_from(tmp_path, SPOF_SITE.replace(
        "  - {from: psu, to: sw, via: dc}",
        "  - {from: psu, to: sw, outlet: PWR1, via: dc}\n"
        "  - {from: mains, to: sw, outlet: PWR2, via: dc}")))
    assert "sw" not in {f.where for f in only(findings, "power-spof")}


def test_errors_sort_before_warnings(tmp_path):
    findings = lint(site_from(tmp_path, CLEAN.replace(
        "radar: {kind: radar, addr: 192.168.88.50}",
        "radar: {kind: radar, addr: 192.168.88.3, addr_source: dhcp-lease}")))
    severities = [f.severity for f in findings]
    assert severities == sorted(severities, key=lambda s: 0 if s == ERROR else 1)


def test_one_input_fed_twice_is_an_error(tmp_path):
    """PWR1 cannot be wired to two sources; PWR1 and PWR2 is the valid shape."""
    findings = lint(site_from(tmp_path, CLEAN.replace(
        "  - {from: mains, to: sw, via: mains}",
        "  - {from: mains, to: sw, inlet: PWR1, via: mains}\n"
        "  - {from: router, to: sw, inlet: PWR1, via: dc}")))
    found = only(findings, "inlet-reuse")
    assert found and "PWR1" in found[0].message


def test_dual_inputs_on_one_device_are_fine(tmp_path):
    findings = lint(site_from(tmp_path, CLEAN.replace(
        "  - {from: mains, to: sw, via: mains}",
        "  - {from: mains, to: sw, inlet: PWR1, via: mains}\n"
        "  - {from: mains, to: sw, inlet: PWR2, via: mains}")))
    assert "inlet-reuse" not in codes(findings)
