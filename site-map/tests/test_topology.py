"""The FOB pattern, and the line between what it knows and what it expects.

The pattern is: SIM router high up, one cable down to a switch in the box,
everything else off the switch. Drawing it is useful before anyone reads a
switch port. Asserting it would be the same mistake `discover.to_yaml`
refuses to make, so every pattern edge stays labelled as an expectation and
never reaches a site file.
"""
import textwrap

import pytest
import yaml

import topology
from model import load_site

BASE = """
version: 1
site: test-site
subnet: 192.168.88.0/24
status: provisional
nodes:
  rut:
    kind: router
    vendor: Teltonika Networks
    model: RUTM08
    addr: 192.168.88.1
    evidence: {"*": device-api}
  sw:
    kind: switch
    vendor: MikroTik
    addr: 192.168.88.2
    evidence: {"*": arp}
  camera_1:
    kind: camera
    vendor: HangZhou JuRu Technology
    addr: 192.168.88.30
    evidence: {"*": arp}
  radar_1:
    kind: radar
    vendor: Magosys Systems
    addr: 192.168.88.50
    evidence: {"*": arp}
"""


def site_from(text, tmp_path):
    path = tmp_path / "site.yaml"
    path.write_text(textwrap.dedent(text))
    return load_site(path)


def edges_of(top, source):
    return {(e.parent, e.child) for e in top.edges if e.source == source}


# -- the shape the pattern produces --------------------------------------

def test_the_default_fob_comes_out_as_internet_router_switch_devices(tmp_path):
    top = topology.propose(site_from(BASE, tmp_path))
    assert top.router == "rut"
    assert top.switches == ["sw"]
    layers = {b.name: b.layer for b in top.boxes}
    assert layers["__internet__"] == topology.LAYER_INTERNET
    assert layers["rut"] == topology.LAYER_ROUTER
    assert layers["sw"] == topology.LAYER_SWITCH
    assert layers["camera_1"] == layers["radar_1"] == topology.LAYER_LEAF


def test_every_leaf_hangs_off_the_switch_and_not_off_the_router(tmp_path):
    top = topology.propose(site_from(BASE, tmp_path))
    assert ("sw", "camera_1") in edges_of(top, "pattern")
    assert ("sw", "radar_1") in edges_of(top, "pattern")
    assert ("rut", "camera_1") not in edges_of(top, "pattern")


def test_the_internet_arrives_at_the_router_and_nowhere_else(tmp_path):
    top = topology.propose(site_from(BASE, tmp_path))
    incoming = [e for e in top.edges if e.parent == topology.INTERNET]
    assert [e.child for e in incoming] == ["rut"]
    assert "SIM" in incoming[0].basis


def test_nothing_from_the_pattern_is_ever_called_established(tmp_path):
    top = topology.propose(site_from(BASE, tmp_path))
    assert all(e.source == "pattern" for e in top.edges), "this site declares none"
    assert all(e.evidence is None for e in top.edges)


# -- identification, and specifically not by address ---------------------

def test_the_router_is_found_by_its_model_not_by_being_dot_one(tmp_path):
    # `.1` is a bench convention. A site where it is untrue is exactly the
    # site where a diagram built on the convention misleads.
    moved = BASE.replace("addr: 192.168.88.1", "addr: 192.168.88.211")
    moved = moved.replace("addr: 192.168.88.2\n", "addr: 192.168.88.9\n")
    top = topology.propose(site_from(moved, tmp_path))
    assert top.router == "rut" and top.switches == ["sw"]


def test_an_evidence_backed_gateway_role_wins_over_the_vendor(tmp_path):
    # Roles in the wild read "default-gateway", so this is a substring test;
    # an exact match missed the only site that declares one.
    text = BASE.replace("""  sw:
    kind: switch""", """  sw:
    roles: [default-gateway]
    kind: switch""")
    top = topology.propose(site_from(text, tmp_path))
    assert top.router == "sw"


def test_a_switch_shortlisted_but_never_read_still_counts_as_the_switch(tmp_path):
    # kela-fob-03's .118 is a TSW202 by shortlist only - nobody could log in
    # to it. Missing it drew a phantom "switch (not seen)" beside the real one.
    text = BASE.replace("""  sw:
    kind: switch
    vendor: MikroTik""", """  sw:
    kind: network-device
    vendor: Teltonika Networks
    candidates: [TSW202]""")
    top = topology.propose(site_from(text, tmp_path))
    assert top.switches == ["sw"]
    assert not any(b.name == topology.UNSEEN_SWITCH for b in top.boxes)
    why = next(b.note for b in top.boxes if b.name == "sw")
    assert "shortlist" in why, "the basis has to say it is a lead, not a reading"


def test_mikrotik_teltonika_and_planet_all_read_as_switches(tmp_path):
    for vendor, model in (("MikroTik", None), ("Routerboard.com", None),
                          ("Teltonika Networks", "TSW202"),
                          ("Teltonika Networks", "PSW2"),
                          ("PLANET Technology", "IGS-4215-8UP2T2S")):
        text = BASE.replace("""  sw:
    kind: switch
    vendor: MikroTik""", f"""  sw:
    kind: unknown
    vendor: {vendor}
    model: {model or "~"}""")
        top = topology.propose(site_from(text, tmp_path))
        assert top.switches == ["sw"], (vendor, model)


# -- the cases where it refuses to guess ---------------------------------

def test_two_switches_means_no_leaf_is_assigned_to_either(tmp_path):
    # A FOB runs two when one has too few ports. Which device is on which is
    # the exact fact a MAC-address table exists to answer, so picking one
    # would invent the thing worth reading.
    #
    # This used to assert that the leaves had NO edge at all, which is a
    # different claim and the wrong one: a box with nothing reaching it reads
    # as a device with no network. They hang off an unnamed "one of these
    # switches" box instead - which says only what is true - and neither
    # switch is named as any device's parent.
    text = BASE + """  sw2:
    kind: switch
    vendor: MikroTik
    addr: 192.168.88.3
    evidence: {"*": arp}
"""
    top = topology.propose(site_from(text, tmp_path))
    assert sorted(top.switches) == ["sw", "sw2"]
    leaf_edges = [e for e in top.edges if e.child in ("camera_1", "radar_1")]
    assert leaf_edges, "a leaf with no cable claims it has no network"
    assert {e.parent for e in leaf_edges} == {topology.FABRIC}
    assert not [e for e in leaf_edges if e.parent in ("sw", "sw2")]
    assert all(e.source == "pattern" for e in leaf_edges)
    assert any("MAC-address table" in n for n in top.notes)


def test_no_device_is_ever_left_with_nothing_reaching_it(tmp_path):
    """The property the two-switch case broke, stated once for every shape.

    A device drawn with no cable is not a cautious diagram - it is a wrong
    one, claiming the thing has no network when all we failed to read is
    which switch it sits on.
    """
    shapes = {
        "one switch": BASE,
        "two switches": BASE + """  sw2:
    kind: switch
    vendor: MikroTik
    addr: 192.168.88.3
    evidence: {"*": arp}
""",
        "three switches": BASE + """  sw2:
    kind: switch
    vendor: MikroTik
    addr: 192.168.88.3
    evidence: {"*": arp}
  sw3:
    kind: switch
    vendor: MikroTik
    addr: 192.168.88.4
    evidence: {"*": arp}
""",
    }
    for label, text in shapes.items():
        top = topology.propose(site_from(text, tmp_path))
        reached = {e.child for e in top.edges}
        stranded = [b.name for b in top.boxes
                    if b.layer == topology.LAYER_LEAF and b.name not in reached]
        assert not stranded, f"{label}: nothing reaches {stranded}"


def test_a_switch_a_fob_must_have_but_nobody_saw_is_drawn_as_absent(tmp_path):
    # ARP lists only what the host has recently exchanged traffic with, so a
    # quiet switch never appears. That is an expected absence, not a device
    # that is missing - and the leaves still have to hang off something.
    text = BASE.replace("""  sw:
    kind: switch
    vendor: MikroTik
    addr: 192.168.88.2
    evidence: {"*": arp}
""", "")
    top = topology.propose(site_from(text, tmp_path))
    ghost = next(b for b in top.boxes if b.name == topology.UNSEEN_SWITCH)
    assert ghost.real is False and "ARP" in ghost.note
    assert ("rut", topology.UNSEEN_SWITCH) in edges_of(top, "pattern")
    assert (topology.UNSEEN_SWITCH, "camera_1") in edges_of(top, "pattern")


def test_no_router_means_no_root_and_the_diagram_says_so(tmp_path):
    text = BASE.replace("""  rut:
    kind: router
    vendor: Teltonika Networks
    model: RUTM08
    addr: 192.168.88.1
    evidence: {"*": device-api}
""", "")
    top = topology.propose(site_from(text, tmp_path))
    assert top.router is None
    assert any("No router identified" in n for n in top.notes)


def test_two_unread_teltonikas_are_not_resolved_into_a_router(tmp_path):
    text = BASE.replace("""  rut:
    kind: router
    vendor: Teltonika Networks
    model: RUTM08""", """  rut:
    kind: unknown
    vendor: Teltonika Networks""")
    text = text + """  tel2:
    kind: unknown
    vendor: Teltonika Networks
    addr: 192.168.88.7
    evidence: {"*": arp}
"""
    top = topology.propose(site_from(text, tmp_path))
    assert top.router is None
    assert any("undetermined" in n or "No router" in n for n in top.notes)


# -- declared facts win --------------------------------------------------

def test_a_declared_cable_is_not_second_guessed_by_the_pattern(tmp_path):
    text = BASE + """net:
  - from: rut
    to: camera_1
    port: lan1
    evidence: {"*": switch-table}
"""
    top = topology.propose(site_from(text, tmp_path))
    assert ("rut", "camera_1") in edges_of(top, "declared")
    # And the pattern does not also propose it off the switch.
    assert ("sw", "camera_1") not in edges_of(top, "pattern")


def test_a_declared_edge_carries_its_own_evidence_not_the_parents(tmp_path):
    # Read off the parent node, kela-fob-03's one proven cable reported as
    # `arp` - overstating nothing, but crediting the wrong source entirely.
    text = BASE + """net:
  - from: rut
    to: sw
    port: lan3
    evidence: {"*": switch-table}
"""
    top = topology.propose(site_from(text, tmp_path))
    edge = next(e for e in top.edges if e.source == "declared")
    assert edge.evidence == "switch-table"
    assert edge.port == "lan3"


def test_the_real_site_comes_out_matching_what_its_file_says(tmp_path):
    # kela-fob-03: one proven cable router -> .118, and everything else
    # behind that switch as an expectation.
    site = load_site("sites/kela-fob-03.yaml")
    top = topology.propose(site)
    assert top.router == "router"
    assert top.switches == ["teltonika_118"]
    declared = edges_of(top, "declared")
    assert declared == {("router", "teltonika_118")}
    assert len(edges_of(top, "pattern")) == 9


def test_the_payload_keeps_the_two_kinds_of_edge_countable(tmp_path):
    payload = topology.propose(site_from(BASE, tmp_path)).as_payload()
    assert payload["counts"]["declared"] == 0
    assert payload["counts"]["pattern"] == len(payload["edges"])
    assert {b["name"] for b in payload["boxes"]} >= {"rut", "sw", "__internet__"}


# -- every site has a server ---------------------------------------------

def test_a_site_with_no_server_says_so(tmp_path):
    # Every site has at least one. When none turns up it is worth saying why
    # a MAC could not have found it.
    top = topology.propose(site_from(BASE, tmp_path))
    note = next((n for n in top.notes if "No server identified" in n), None)
    assert note, top.notes
    assert "same OUI" in note and "lease hostname" in note


def test_a_site_with_a_server_does_not_complain(tmp_path):
    text = BASE + """  server:
    kind: server
    vendor: Dell
    addr: 192.168.88.10
    evidence: {"*": arp}
"""
    top = topology.propose(site_from(text, tmp_path))
    assert not any("No server identified" in n for n in top.notes)
