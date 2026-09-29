"""The forwarding database, read as fan-out.

This is the only evidence a router can offer about a switch that holds no
address at all. The rules it licenses are deliberately narrow, and these
tests pin the narrowness as hard as the capability: the failure that matters
is not "we missed a switch", it is "we drew a cable that is not there",
because people wire to diagrams.

Every sample below is real output from the sites surveyed on 16 Sep 2026.
"""
import discover
import oui
import router
import topology

# fob-04: everything on one port, lan2/lan3 with no cable in them.
FDB = """\
20:97:27:81:0d:36 dev lan1 master br-lan
bc:74:d7:81:52:ea dev lan1 master br-lan
8c:1f:64:e7:49:e0 dev lan1 master br-lan
33:33:00:00:00:01 dev lan1 self permanent
20:97:27:4e:29:03 dev br-lan master br-lan permanent
"""

LINKS = """\
1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536
4: lan1@eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500
5: lan2@eth0: <NO-CARRIER,BROADCAST,MULTICAST,UP> mtu 1500
6: lan3@eth0: <NO-CARRIER,BROADCAST,MULTICAST,UP> mtu 1500
12: br-lan: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500
"""

UPSTREAM = """\
gateway 192.168.1.1
192.168.1.1 dev wan lladdr 20:97:27:4e:c5:59 REACHABLE
"""

SWITCH_MAC = "20:97:27:81:0d:36"
CAMERA_MAC = "bc:74:d7:81:52:ea"
RADAR_MAC = "8c:1f:64:e7:49:e0"


# -- reading the tables --------------------------------------------------

def test_permanent_and_self_rows_are_not_devices():
    # They are the bridge's own addresses, not things learned from traffic.
    # Counted, every port looks like it has more behind it than it does.
    fdb = router.parse_fdb(FDB)
    assert set(fdb) == {"lan1"}
    assert fdb["lan1"] == sorted([SWITCH_MAC, CAMERA_MAC, RADAR_MAC])


def test_a_dark_port_is_distinguishable_from_an_empty_one():
    links = router.parse_links(LINKS)
    assert links["lan1"] is True
    assert links["lan2"] is False and links["lan3"] is False


def test_the_upstream_gateway_is_read_with_its_mac():
    assert router.parse_upstream(UPSTREAM) == {
        "addr": "192.168.1.1", "mac": "20:97:27:4e:c5:59"}


def test_no_default_route_is_not_an_error():
    assert router.parse_upstream("") == {}


# -- fan-out -------------------------------------------------------------

def _found(pairs):
    out = []
    for name, mac, kind in pairs:
        item = discover.Discovered(entry=oui.Entry(mac=mac, ip=None))
        item.name, item.kind = name, kind
        out.append(item)
    return out


def _survey(fdb=FDB, links=LINKS):
    return router.Survey(host="h", hostname=None,
                         fdb=router.parse_fdb(fdb),
                         ports=router.parse_links(links))


def test_a_switch_among_many_macs_gets_an_uplink_edge_and_nothing_else():
    # The one edge the router's table licenses here: the switch's uplink.
    # The camera and the radar are behind that switch somewhere, and which
    # of ITS ports needs ITS table.
    found = _found([("switch", SWITCH_MAC, "switch"),
                    ("camera", CAMERA_MAC, "camera"),
                    ("radar", RADAR_MAC, "radar")])
    edges = router.fdb_edges(router.fan_out(found, _survey()))
    assert [(e["from"], e["to"], e["port"]) for e in edges] == \
        [("router", "switch", "lan1")]
    assert edges[0]["evidence"] == "switch-table"


def test_a_lone_mac_on_a_port_is_a_link():
    one = "20:97:27:81:0d:36 dev lan1 master br-lan\n"
    found = _found([("switch", SWITCH_MAC, "switch")])
    edges = router.fdb_edges(router.fan_out(found, _survey(fdb=one)))
    assert (edges[0]["from"], edges[0]["to"]) == ("router", "switch")
    # And it admits the one case no table can separate.
    assert "exactly one device behind it" in edges[0]["notes"]


def test_a_fan_out_with_no_switch_draws_no_edge_at_all():
    # Three devices behind one cable and nothing that reads as a switch.
    # Something unmanaged is fanning out; guessing which device it hangs
    # off would be inventing the fact worth having.
    found = _found([("camera", CAMERA_MAC, "camera"),
                    ("radar", RADAR_MAC, "radar"),
                    ("other", SWITCH_MAC, "unknown")])
    fans = router.fan_out(found, _survey())
    assert router.fdb_edges(fans) == []
    assert [f.hidden_switch for f in fans] == [True]


def test_two_switches_on_one_port_draw_no_edge():
    found = _found([("switch_a", SWITCH_MAC, "switch"),
                    ("switch_b", CAMERA_MAC, "switch"),
                    ("radar", RADAR_MAC, "radar")])
    fans = router.fan_out(found, _survey())
    assert router.fdb_edges(fans) == []
    assert "not settled here" in " ".join(router.fan_out_notes(fans))


def test_a_dark_port_is_skipped_entirely():
    dark = FDB.replace("dev lan1", "dev lan2")
    found = _found([("switch", SWITCH_MAC, "switch")])
    assert router.fan_out(found, _survey(fdb=dark)) == []


def test_a_mac_that_answered_nothing_is_still_reported():
    # The bridge learned it, so it is present and talking - it just holds
    # no address on this subnet. That is a finding, not a gap.
    found = _found([("switch", SWITCH_MAC, "switch"),
                    ("camera", CAMERA_MAC, "camera")])
    fans = router.fan_out(found, _survey())
    assert fans[0].unknown == [RADAR_MAC]
    assert RADAR_MAC in " ".join(router.fan_out_notes(fans))


def test_the_routers_own_macs_are_not_counted_as_devices_behind_a_port():
    survey = _survey()
    survey.interfaces = [router.Iface(name="lan1", addr="192.168.88.2",
                                      prefix=24, scope="internal",
                                      mac=SWITCH_MAC)]
    found = _found([("camera", CAMERA_MAC, "camera"),
                    ("radar", RADAR_MAC, "radar")])
    fans = router.fan_out(found, survey)
    assert SWITCH_MAC not in fans[0].macs


# -- what the diagram does with it ---------------------------------------

class _Node:
    def __init__(self, kind, vendor=None, model=None):
        self.kind, self.vendor, self.model = kind, vendor, model
        self.candidates, self.roles, self.notes = [], [], []


class _Site:
    def __init__(self, nodes):
        self.nodes, self.net, self.uplink = nodes, [], None


def _site():
    return _Site({
        "router": _Node("router", "Teltonika Networks UAB", "Teltonika RUTM08"),
        "camera": _Node("camera", "HangZhou JuRu"),
        "radar": _Node("radar", "Magosys Systems"),
    })


def test_a_proven_switch_is_drawn_as_proven_not_as_an_expectation():
    fan = router.FanOut(port="lan1", macs=[CAMERA_MAC, RADAR_MAC],
                        known=["camera", "radar"], switches=[], unknown=[])
    top = topology.propose(_site(), [fan])
    ghost = [b for b in top.boxes if not b.real and b.kind == "switch"]
    assert len(ghost) == 1
    assert ghost[0].proven is True
    assert "PROVEN" in ghost[0].note
    # ... and the edge to it is read, not guessed.
    edge = [e for e in top.edges if e.child == ghost[0].name][0]
    assert (edge.source, edge.evidence) == ("read", "switch-table")


def test_the_leaves_hang_off_the_switch_the_port_proves_them_behind():
    fan = router.FanOut(port="lan1", macs=[CAMERA_MAC, RADAR_MAC],
                        known=["camera", "radar"], switches=[], unknown=[])
    top = topology.propose(_site(), [fan])
    ghost = [b for b in top.boxes if b.proven][0]
    behind = {e.child for e in top.edges if e.parent == ghost.name}
    assert behind == {"camera", "radar"}


def test_without_a_fan_out_nothing_changes():
    # A site loaded from YAML has no forwarding database, and must draw
    # exactly what it always drew.
    plain = topology.propose(_site()).as_payload()
    assert plain["counts"]["read"] == 0
    assert any("not seen" in b["label"] for b in plain["boxes"])


def test_the_edge_is_parented_on_whatever_the_vantage_point_is_called():
    """`router` is the usual name and not a safe assumption.

    `merge_vantage_point` enriches an ARP-derived entry in place when one
    exists for the same MAC, and that entry was named by the address plan -
    which calls .2 `switch_mgmt`. An edge parented on the literal string
    `router` then names a node that does not exist, and the site fails to
    load rather than drawing something wrong.
    """
    survey = _survey()
    survey.interfaces = [router.Iface(name="br-lan", addr="192.168.88.2",
                                      prefix=24, scope="internal",
                                      mac="20:97:27:4e:29:03")]
    found = _found([("switch_mgmt", "20:97:27:4e:29:03", "router"),
                    ("switch", SWITCH_MAC, "switch"),
                    ("camera", CAMERA_MAC, "camera"),
                    ("radar", RADAR_MAC, "radar")])
    name = router.vantage_name(found, survey)
    assert name == "switch_mgmt"
    edges = router.fdb_edges(router.fan_out(found, survey, name), name)
    assert [e["from"] for e in edges] == ["switch_mgmt"]


def test_the_vantage_name_falls_back_when_nothing_matches():
    assert router.vantage_name(_found([("a", CAMERA_MAC, "camera")]),
                               _survey()) == "router"


def test_a_device_scoped_default_route_is_not_read_as_an_address():
    # `default dev usb0 proto static` - one site routes through a tethered
    # modem this way, and taking the third field called the interface an
    # upstream gateway.
    up = router.parse_upstream("device usb0")
    assert up == {"device": "usb0"}
    note = router.upstream_note(router.Survey(host="h", hostname=None,
                                              upstream=up))
    assert "point-to-point" in note and "usb0" in note


def test_several_switches_on_one_port_still_gives_the_devices_a_parent():
    """What fob-04 and fob-09 look like: two or three switches behind one
    cable. Nothing says which device is on which - but "behind that port" is
    read, and a device drawn with no cable claims something stronger and
    wronger than that."""
    site = _Site({
        "router": _Node("router", "Teltonika Networks UAB", "Teltonika RUTM08"),
        "sw_a": _Node("switch", "Routerboard.com"),
        "sw_b": _Node("switch", "Teltonika Networks UAB"),
        "camera": _Node("camera", "HangZhou JuRu"),
        "radar": _Node("radar", "Magosys Systems"),
    })
    fan = router.FanOut(
        port="lan1", macs=["a", "b", "c", "d"],
        known=["sw_a", "sw_b", "camera", "radar"],
        switches=["sw_a", "sw_b"], unknown=[])
    top = topology.propose(site, [fan])

    reached = {e.child for e in top.edges}
    for leaf in ("camera", "radar"):
        assert leaf in reached, f"nothing reaches {leaf}"
    # ... and neither switch is named as their parent, which is the fact
    # only a switch's own table can settle.
    leaf_parents = {e.parent for e in top.edges
                    if e.child in ("camera", "radar")}
    assert leaf_parents == {f"{topology.FABRIC}_lan1"}
    # both switches are drawn as behind the port, not as its far end
    for switch in ("sw_a", "sw_b"):
        edge = [e for e in top.edges if e.child == switch][0]
        assert edge.source == "pattern" and "lan1" in edge.basis


# -- the capture command has to work in a real shell ---------------------

def test_the_tcpdump_finder_does_not_lose_its_assignment_to_a_subshell():
    """The first version wrapped the search in `(...)`, which is a subshell,
    so $TD was set and immediately discarded - and every site reported
    NO_TCPDUMP from a router that has tcpdump installed. Run the real command
    through a real shell, with a binary that exists, and assert it finds it."""
    import subprocess
    import router as router_mod

    original = router_mod.TCPDUMP_PATHS
    try:
        # `true` exists and exits 0 on any POSIX box, so the command runs to
        # completion without needing an interface or root.
        router_mod.TCPDUMP_PATHS = ("/usr/bin/true", "/bin/true")
        cmd = router_mod.cmd_bpdu("lan1", seconds=1)
        done = subprocess.run(["/bin/sh", "-c", cmd],
                              capture_output=True, text=True, timeout=30)
    finally:
        router_mod.TCPDUMP_PATHS = original
    assert "NO_TCPDUMP" not in done.stdout, (
        "the finder could not see a binary that is definitely there:\n"
        f"{cmd}\n{done.stdout}{done.stderr}"
    )


def test_a_router_without_tcpdump_says_so_rather_than_failing():
    import subprocess
    import router as router_mod

    original = router_mod.TCPDUMP_PATHS
    try:
        router_mod.TCPDUMP_PATHS = ("/nonexistent/tcpdump",)
        cmd = router_mod.cmd_bpdu("lan1", seconds=1)
        done = subprocess.run(["/bin/sh", "-c", cmd],
                              capture_output=True, text=True, timeout=30)
    finally:
        router_mod.TCPDUMP_PATHS = original
    assert "NO_TCPDUMP" in done.stdout
    assert router_mod.parse_bpdu(done.stdout) == {}


# -- what a BPDU establishes ---------------------------------------------

BPDU_ROOT = """\
12:45:46.582663 04:f4:1c:9f:dd:5b > 01:80:c2:00:00:00, 802.3, length 39: LLC, \
dsap STP (0x42) Individual, ssap STP (0x42) Command, ctrl 0x03: STP 802.1w, \
Rapid STP, Flags [Proposal, Learn, Forward], bridge-id 8000.04:f4:1c:9f:dd:5b.8001, \
length 36
	root-id 8000.04:f4:1c:9f:dd:5b, root-pathcost 0, port-role Designated
"""

# fob-05 lan3, verbatim: the nearest bridge is NOT the root, and the cost
# says the link between them is fast ethernet.
BPDU_BEHIND = """\
12:45:49.923646 20:97:27:a0:e7:e5 > 01:80:c2:00:00:00, 802.3, length 39: LLC, \
dsap STP (0x42) Individual, ssap STP (0x42) Command, ctrl 0x03: STP 802.1w, \
Rapid STP, Flags [Learn, Forward, Agreement], bridge-id 8000.20:97:27:a0:e7:df.8006, \
length 36
	root-id 8000.20:97:27:33:23:48, root-pathcost 200000, port-role Designated
"""


def test_the_bridge_mac_not_the_frame_source_is_the_identity():
    # The frame comes from the sending PORT's own MAC, which differs from the
    # bridge's by a digit or two - and the bridge MAC is what matches an ARP
    # entry. Matching on the source would find nothing.
    frame = router.parse_bpdu(BPDU_BEHIND)
    assert frame["src_mac"] == "20:97:27:a0:e7:e5"
    assert frame["bridge_mac"] == "20:97:27:a0:e7:df"
    assert frame["port_id"] == 6
    assert frame["protocol"] == "rstp"


def test_a_hundred_megabit_hop_is_read_off_the_path_cost():
    frame = router.parse_bpdu(BPDU_BEHIND)
    assert frame["root_cost"] == 200000
    assert frame["root_hop"] == "100 Mb"
    assert router.parse_bpdu(BPDU_ROOT)["root_cost"] == 0


def _stp_found():
    return _found([("router", "20:97:27:4e:29:03", "router"),
                   ("near", "20:97:27:a0:e7:df", "network-device"),
                   ("far", "20:97:27:33:23:48", "network-device")])


def test_speaking_stp_makes_switch_a_reading_not_a_vendor_hunch():
    found = _stp_found()
    survey = _survey()
    survey.bpdu = {"lan3": router.parse_bpdu(BPDU_BEHIND)}
    edges, notes = router.stp_facts(found, survey)
    near = [i for i in found if i.name == "near"][0]
    assert near.kind == "switch"
    assert "only a switch participates in spanning tree" in " ".join(near.notes)
    assert edges[0]["evidence"] == "stp"
    assert edges[0]["peer_port"] == "port 6"


def test_a_non_zero_cost_to_root_is_proof_of_a_bridge_further_out():
    found = _stp_found()
    survey = _survey()
    survey.bpdu = {"lan3": router.parse_bpdu(BPDU_BEHIND)}
    _, notes = router.stp_facts(found, survey)
    joined = " ".join(notes)
    assert "proof of a further bridge" in joined
    assert "DEFECT" in joined and "100 Mb" in joined


def test_the_root_being_the_nearest_bridge_says_everything_is_below_it():
    found = _found([("router", "20:97:27:4e:29:03", "router"),
                    ("mikrotik", "04:f4:1c:9f:dd:5b", "switch")])
    survey = _survey()
    survey.bpdu = {"lan1": router.parse_bpdu(BPDU_ROOT)}
    edges, notes = router.stp_facts(found, survey)
    assert edges[0]["to"] == "mikrotik"
    assert "spanning-tree root" in " ".join(notes)
    assert "DEFECT" not in " ".join(notes)


def test_a_bridge_that_holds_no_address_is_still_reported():
    found = _found([("router", "20:97:27:4e:29:03", "router")])
    survey = _survey()
    survey.bpdu = {"lan1": router.parse_bpdu(BPDU_ROOT)}
    edges, notes = router.stp_facts(found, survey)
    assert edges == []
    assert "answered nothing in the sweep" in " ".join(notes)
    assert "only bridges speak STP" in " ".join(notes)


def test_the_root_named_in_the_frame_is_also_proven_a_switch():
    """It sent the router nothing - it sits behind the bridge that did - so a
    BPDU someone else sent is the only evidence a survey can get about it.
    fob-05's `.234` sat as `network-device` while being the root of the
    fabric on lan3."""
    found = _stp_found()
    survey = _survey()
    survey.bpdu = {"lan3": router.parse_bpdu(BPDU_BEHIND)}
    router.stp_facts(found, survey)
    far = [i for i in found if i.name == "far"][0]
    assert far.kind == "switch"
    assert "only a bridge can be elected root" in " ".join(far.notes)


# -- what a device says about itself, unprompted --------------------------
#
# Verbatim capture from fob-04's MikroTik, 17 Sep 2026. No credential was
# involved: the switch broadcasts this to nobody in particular every 30s.

MNDP = """\
13:09:31.993068 IP 192.168.88.1.5678 > 255.255.255.255.5678: UDP, length 164
	0x0000:  4500 00c0 0000 0000 4011 6184 c0a8 5801
	0x0010:  ffff ffff 162e 162e 00ac 23b9 0000 0b15
	0x0020:  0001 0006 04f4 1c9f dd5b 0005 0008 4d69
	0x0030:  6b72 6f54 696b 0007 0023 372e 3138 2e32
	0x0040:  2028 7374 6162 6c65 2920 3230 3235 2d30
	0x0050:  332d 3131 2031 313a 3539 3a30 3400 0800
	0x0060:  084d 696b 726f 5469 6b00 0a00 04a3 4c01
	0x0070:  0000 0b00 094e 3131 422d 5739 4d37 000c
	0x0080:  000c 4352 5331 3132 2d38 502d 3453 000e
	0x0090:  0001 0100 0f00 10fe 8000 0000 0000 0006
	0x00a0:  f41c fffe 9fdd 5b00 1000 0d62 7269 6467
	0x00b0:  652f 6574 6865 7231 0011 0004 c0a8 5801
"""


def test_the_broadcast_is_parsed_as_tlvs_not_scraped_as_text():
    heard = router.parse_mndp(MNDP)
    assert heard["mac"] == "04:f4:1c:9f:dd:5b"
    assert heard["board"] == "CRS112-8P-4S"
    assert heard["version"].startswith("7.18.2 (stable)")
    assert heard["serial"] == "N11B-W9M7"
    assert heard["interface"] == "bridge/ether1"


def test_a_model_nobody_has_credentials_for_becomes_a_reading():
    found = _found([("routerboard_com", "04:f4:1c:9f:dd:5b", "switch")])
    survey = _survey()
    survey.mndp = {"lan1": router.parse_mndp(MNDP)}
    router.mndp_facts(found, survey)
    item = found[0]
    assert item.model == "CRS112-8P-4S"
    assert item.firmware.startswith("7.18.2")
    assert item.evidence["model"] == "discovery"
    assert "N11B-W9M7" in " ".join(item.notes)


def test_a_broadcast_from_something_that_answered_nothing_is_reported():
    found = _found([("other", "aa:bb:cc:dd:ee:ff", "camera")])
    survey = _survey()
    survey.mndp = {"lan1": router.parse_mndp(MNDP)}
    notes = router.mndp_facts(found, survey)
    assert "holding no address" in " ".join(notes)
    assert "CRS112-8P-4S" in " ".join(notes)


def test_a_capture_that_heard_nothing_changes_nothing():
    assert router.parse_mndp("") == {}
    assert router.parse_mndp("NO_TCPDUMP") == {}
    found = _found([("routerboard_com", "04:f4:1c:9f:dd:5b", "switch")])
    router.mndp_facts(found, _survey())
    assert found[0].model is None
