"""Surveying from the router: the two things it fixes, and what it still cannot say.

The router is the vantage point because it is the site's DHCP server, DNS
resolver and default gateway, so everything has talked to it - and because it
is the one device a FOB certainly has. It is reached with root plus the shared
password over `bench_core.TeltonikaClient`, whose read-only mode refuses a
mutating command by construction. Raw ssh cannot reach these at all: they
answer `Permission denied (publickey,password)` with Tailscale SSH off.

No test here touches hardware.
"""
import ipaddress

import pytest

import router

# Real output from rut-kela-fob-03, trimmed.
ADDRS = """\
1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN
    inet 127.0.0.1/8 scope host lo
3: wan: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq state UP
    inet 192.168.1.164/24 brd 192.168.1.255 scope global wan
12: br-lan: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue state UP
    inet 192.168.88.1/24 brd 192.168.88.255 scope global br-lan
18: tailscale0: <POINTOPOINT,MULTICAST,NOARP,UP,LOWER_UP> mtu 1280 state UNKNOWN
    inet 100.64.242.104/32 scope global tailscale0
"""

LEASES = """\
43200 bc:74:d7:81:17:b1 192.168.88.30 KC0500PAZ00052 01:bc:74:d7:81:17:b1
43200 e8:cf:83:8d:fc:14 192.168.88.10 kela-fob-03 ff:d0:4c
43200 20:97:27:a0:ec:70 192.168.88.118 TSW202 01:20:97:27:a0:ec:70
43200 aa:bb:cc:dd:ee:ff 192.168.88.77 * 01:aa:bb:cc:dd:ee:ff
"""


# -- reading the router's own legs (the fix for point 2) -----------------

def test_all_three_interfaces_come_back_with_loopback_dropped():
    ifaces = router.parse_addrs(ADDRS)
    assert [i.name for i in ifaces] == ["wan", "br-lan", "tailscale0"]


def test_scope_comes_from_the_interface_name_not_the_address():
    # `wan` here holds 192.168.1.164 - a private address that is emphatically
    # not this site's LAN. Judging scope by "is it RFC1918" would call it
    # internal and put the site's uplink on the wrong side of the diagram.
    by_name = {i.name: i for i in router.parse_addrs(ADDRS)}
    assert by_name["wan"].scope == "external"
    assert by_name["wan"].addr == "192.168.1.164"
    assert by_name["br-lan"].scope == "internal"
    assert by_name["tailscale0"].scope == "external"
    assert by_name["tailscale0"].prefix == 32


def test_the_router_becomes_a_device_carrying_every_leg():
    # A host has no ARP entry for itself, so the vantage point is the one
    # device a sweep of it cannot see. It was the server before; it would be
    # the router now.
    result = router.Survey(host="100.64.242.104", hostname="rut-kela-fob-03",
                           interfaces=router.parse_addrs(ADDRS))
    item = router.as_device(result, ipaddress.ip_network("192.168.88.0/24"))
    assert item.name == "router" and item.kind == "router"
    assert {i["name"] for i in item.interfaces} == {"wan", "br-lan", "tailscale0"}
    # Its address on the map is the LAN one, not the tailnet one we came in on.
    assert item.entry.ip == "192.168.88.1"


def test_the_routers_presence_is_established_by_the_device_not_by_arp():
    result = router.Survey(host="100.64.242.104", hostname="rut-kela-fob-03",
                           interfaces=router.parse_addrs(ADDRS))
    item = router.as_device(result, ipaddress.ip_network("192.168.88.0/24"))
    assert item.evidence["*"] == "device-api", "read on the box, not inferred"
    assert item.addr_source == "static-manual"
    assert "default-gateway" in item.roles
    assert "never appears in its own neighbour table" in " ".join(item.notes)


def test_a_router_with_no_lan_leg_still_lands_somewhere():
    only_tailnet = router.Survey(
        host="100.64.242.104", hostname="rut-x",
        interfaces=[router.Iface("tailscale0", "100.64.242.104", 32, "external")])
    item = router.as_device(only_tailnet, ipaddress.ip_network("192.168.88.0/24"))
    assert item.entry.ip == "100.64.242.104"


# -- the site's name -----------------------------------------------------

@pytest.mark.parametrize("hostname,expected", [
    ("rut-kela-fob-03", "kela-fob-03"),
    ("rutm-shomron-main-1", "shomron-main-1"),
    ("kela-kanaf-cuas", "kela-kanaf-cuas"),
    (None, "sweep-100-64-242-104"),
])
def test_the_site_is_named_from_the_router_minus_the_device_prefix(hostname, expected):
    assert router.site_name_from(hostname, "100.64.242.104") == expected


# -- the lease table -----------------------------------------------------

def test_leases_give_the_hostname_the_arp_table_cannot():
    leases = router.parse_leases(LEASES)
    assert leases["209727A0EC70"] == "TSW202"
    assert leases["E8CF838DFC14"] == "kela-fob-03"


def test_a_client_that_sent_no_hostname_is_not_given_one():
    # dnsmasq writes `*` when the client sent none. Recording that as a name
    # would be worse than keeping the vendor-derived one.
    assert "AABBCCDDEEFF" not in router.parse_leases(LEASES)


def test_a_lease_hostname_is_recorded_without_renaming_the_device():
    # Node names are referenced by `net:` edges in site files, so deriving
    # them differently would rewrite identities across every existing model.
    import discover
    import oui
    item = discover.Discovered(
        entry=oui.Entry(mac="20:97:27:a0:ec:70", ip="192.168.88.118"))
    item.name = "teltonika_118"
    router.lease_notes([item], router.parse_leases(LEASES))
    assert item.name == "teltonika_118", "the name must not move"
    assert any("TSW202" in n for n in item.notes)


def test_no_lease_leaves_the_device_untouched():
    import discover
    import oui
    item = discover.Discovered(entry=oui.Entry(mac="00:11:22:33:44:55", ip="1.2.3.4"))
    router.lease_notes([item], router.parse_leases(LEASES))
    assert item.notes == []


# -- the commands, which must stay reads ---------------------------------

def test_every_command_is_a_read():
    commands = [router.CMD_HOSTNAME, router.CMD_ADDRS,
                router.CMD_LEASES, router.CMD_NEIGH]
    for command in commands:
        for write in ("uci set", "uci commit", "ip addr add", "ip neigh add",
                      "restart", "reboot", "sysupgrade", "tee "):
            assert write not in command, command


def test_hostname_is_read_from_proc_because_rutos_has_no_hostname_binary():
    # BusyBox ash: `hostname` is rc 127, "not found". The first survey
    # attempt died on exactly this.
    assert router.CMD_HOSTNAME == "cat /proc/sys/kernel/hostname"


def test_a_missing_bench_core_is_a_clear_refusal(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "bench_core", None)
    with pytest.raises(router.RouterError) as exc:
        router.connect("100.64.242.104", "x")
    assert "bench_core" in str(exc.value)


# -- a factory hostname is a model number --------------------------------

def test_a_factory_dhcp_hostname_becomes_a_shortlist_with_a_basis():
    """How .118 gets pinned as a TSW202 without anyone logging in to it.

    An unprovisioned Teltonika still answers to the hostname it shipped
    with, and that hostname is a model number. The curated kela-fob-03 file
    reaches the same conclusion by hand. Without this a fresh survey has no
    shortlist at all, and the switch the whole site hangs off is drawn as a
    plain unknown device.
    """
    import discover
    import oui
    item = discover.Discovered(
        entry=oui.Entry(mac="20:97:27:a0:ec:70", ip="192.168.88.118"))
    router.lease_notes([item], router.parse_leases(LEASES))
    assert item.candidates == ["Teltonika TSW202"]
    assert "factory hostname" in item.candidate_basis
    assert "not a reading" in item.candidate_basis


def test_a_hostname_that_names_no_catalogue_model_adds_no_shortlist():
    import discover
    import oui
    item = discover.Discovered(
        entry=oui.Entry(mac="e8:cf:83:8d:fc:14", ip="192.168.88.10"))
    router.lease_notes([item], router.parse_leases(LEASES))
    assert item.candidates == [], "`kela-fob-03` is a hostname, not a model"
    assert any("Hostname `kela-fob-03`" in n for n in item.notes)


def test_a_shortlist_never_overwrites_a_model_that_was_read():
    import discover
    import oui
    item = discover.Discovered(
        entry=oui.Entry(mac="20:97:27:a0:ec:70", ip="192.168.88.118"))
    item.facts = None
    object.__setattr__(item, "model", "Teltonika TSW202") if False else None
    # `model` on Discovered comes off the bench facts; simulate one being set.
    class Facts:
        model = "Teltonika TSW202"
        firmware = None
        serial = None
        runs_seen = 1
        tool = None
        last_run = None
    item.facts = Facts()
    router.lease_notes([item], router.parse_leases(LEASES))
    assert item.candidates == [], "a reading beats a lease, so no shortlist"


def test_the_shortlist_makes_the_switch_visible_to_the_diagram(tmp_path):
    # The end of the chain: lease -> shortlist -> `_is_switch` -> layer 2.
    import discover
    import oui
    import topology
    from model import load_site

    item = discover.Discovered(
        entry=oui.Entry(mac="20:97:27:a0:ec:70", ip="192.168.88.118"))
    item.name = "teltonika_118"
    item.kind = "network-device"
    item.evidence = {"*": "arp"}
    router.lease_notes([item], router.parse_leases(LEASES))

    path = tmp_path / "s.yaml"
    path.write_text(discover.to_yaml("t", "192.168.88.0/24", [item], []))
    site = load_site(path)
    assert site.nodes["teltonika_118"].candidates == ["Teltonika TSW202"]
    assert topology.propose(site).switches == ["teltonika_118"]


# -- the vantage point is sometimes already in the list ------------------

def _survey():
    ifaces = router.parse_addrs(ADDRS)
    for iface in ifaces:
        iface.mac = {"br-lan": "20:97:27:36:55:ec"}.get(iface.name)
    return router.Survey(host="100.64.242.104", hostname="rut-kela-fob-03",
                         interfaces=ifaces)


def test_the_router_is_enriched_not_duplicated_when_it_answered_the_sweep():
    """The sweep pings the router's own address, so it lands in its own
    neighbour table. Inserting blindly emitted two `router:` keys and the
    ARP-derived one silently won, taking all three interfaces with it."""
    import discover
    import oui
    seen = discover.Discovered(
        entry=oui.Entry(mac="20:97:27:36:55:ec", ip="192.168.88.1"))
    seen.name = "router"
    seen.kind = "router"
    seen.evidence = {"*": "arp"}
    seen.notes = ["seen in the neighbour table"]

    out = router.merge_vantage_point([seen], _survey(),
                                     ipaddress.ip_network("192.168.88.0/24"))
    assert len(out) == 1, "one router, not two"
    assert {i["name"] for i in out[0].interfaces} == {"wan", "br-lan", "tailscale0"}
    assert out[0].evidence["*"] == "device-api", "the device outranks its ARP entry"
    assert "seen in the neighbour table" in out[0].notes, "nothing is discarded"


def test_the_router_is_added_when_it_did_not_answer():
    out = router.merge_vantage_point([], _survey(),
                                     ipaddress.ip_network("192.168.88.0/24"))
    assert [i.name for i in out] == ["router"]


def test_matching_falls_back_to_the_address_when_there_is_no_mac():
    import discover
    import oui
    seen = discover.Discovered(entry=oui.Entry(mac="", ip="192.168.88.1"))
    seen.name = "router"
    result = _survey()
    for iface in result.interfaces:
        iface.mac = None
    out = router.merge_vantage_point([seen], result,
                                     ipaddress.ip_network("192.168.88.0/24"))
    assert len(out) == 1


# -- server vs operator station, which a MAC cannot settle ---------------

# Both real, both from kela-fob-03, both Dell, both on e8:cf:83. The
# remaining three bytes are Dell's own allocation sequence, so nothing in
# either MAC says which is which.
SERVER_MAC = "e8:cf:83:8d:fc:14"
OPERATOR_MAC = "e8:cf:83:3f:f3:83"
REAL_LEASES = """\
43200 e8:cf:83:8d:fc:14 192.168.88.10 kela-fob-03 ff:d0
43200 e8:cf:83:3f:f3:83 192.168.88.29 kela-fob-03-operator ff:d0
"""


def test_two_dells_on_the_same_oui_are_told_apart_by_hostname():
    import discover
    import oui
    items = []
    for mac, addr in ((SERVER_MAC, "192.168.88.10"),
                      (OPERATOR_MAC, "192.168.88.29")):
        item = discover.Discovered(entry=oui.Entry(mac=mac, ip=addr))
        item.kind = "operator-station"   # what the vendor table guesses
        items.append(item)

    router.lease_notes(items, router.parse_leases(REAL_LEASES), "kela-fob-03")
    assert items[0].kind == "server"
    assert items[1].kind == "operator-station"


def test_the_suffix_is_checked_before_the_bare_site_name():
    # `kela-fob-03-operator` starts with the site name. Checking the bare
    # name first would call the operator station a server.
    assert router.kind_from_hostname("kela-fob-03-operator", "kela-fob-03")[0] \
        == "operator-station"
    assert router.kind_from_hostname("kela-fob-03", "kela-fob-03")[0] == "server"


def test_the_reason_says_the_mac_could_not_have_answered():
    _, why = router.kind_from_hostname("kela-fob-03-operator", "kela-fob-03")
    assert "MAC cannot" in why and "same OUI" in why


@pytest.mark.parametrize("hostname,kind", [
    ("afbp-haruvit-operator", "operator-station"),
    ("afb8-oc-station", "operator-station"),
    ("fob-91-hamamis-host", "server"),
    ("fob-91-hamamis-mediaserver", "server"),
    ("haruvit-1-udp-gateway", "server"),
    ("KC0500PAZ00052", None),
    # A camera's serial says nothing; a switch's factory hostname says
    # switch. `TSW202` used to map to None on the reasoning that a model
    # number is not a kind - but it is one here, and the cost of the old
    # answer was real: five of the seven sites surveyed name their switch
    # this way in the lease table, and every one of them drew the switch as
    # kind `network-device` with the diagram guessing from the vendor.
    ("TSW202", "switch"),
    ("Teltonika-TSW202", "switch"),
])
def test_hostnames_from_the_real_tailnet_map_to_kinds(hostname, kind):
    assert router.kind_from_hostname(hostname, "somewhere")[0] == kind


def test_a_hostname_that_says_nothing_leaves_the_kind_alone():
    import discover
    import oui
    item = discover.Discovered(
        entry=oui.Entry(mac="bc:74:d7:81:17:b1", ip="192.168.88.30"))
    item.kind = "camera"
    router.lease_notes(item and [item], router.parse_leases(LEASES), "kela-fob-03")
    assert item.kind == "camera"


# -- reading a PC's identity off the PC ----------------------------------

DMI = """\
dmi.sys_vendor=Dell Inc.
dmi.product_name=Dell Pro Max Tower T2 FCT2250
dmi.board_name=0D8XDK
dmi.bios_version=1.6.1
os=Ubuntu 24.04.4 LTS
kernel=6.8.0-139-generic
host=kela-fob-03
link.enp128s31f6=e8:cf:83:8d:fc:14
link.cni0=6e:9b:67:2f:d5:d0
link.tailscale0=
addr.enp128s31f6=192.168.88.10/24
addr.cni0=10.42.0.1/24
addr.flannel.1=10.42.0.0/32
addr.tailscale0=100.101.8.89/32
"""


def _reader(text, error=None):
    import hosts
    def read(host, user="kela", **kwargs):
        if error:
            return hosts.HostReading(host=host, error=error)
        return hosts.parse_reading(host, text)
    return read


def test_the_model_comes_from_dmi_which_needs_no_sudo():
    # /sys/class/dmi/id is world-readable, so an ordinary ssh session is
    # enough. This is the reading the curated site file records by hand.
    import hosts
    reading = hosts.parse_reading("100.101.8.89", DMI)
    assert reading.model == "Dell Pro Max Tower T2 FCT2250"
    assert reading.firmware == "1.6.1", "the BIOS is a PC's firmware"
    assert reading.os == "Ubuntu 24.04.4 LTS"


def test_container_plumbing_is_not_a_network_leg():
    # The site server runs k3s, so it carries cni0 and flannel.1. Both
    # arrived on the diagram as external interfaces of the server.
    import hosts
    legs = hosts.interfaces_from(hosts.parse_reading("h", DMI))
    assert {leg["name"] for leg in legs} == {"enp128s31f6", "tailscale0"}


def test_the_lan_leg_is_internal_and_tailscale_is_external():
    import hosts
    import ipaddress
    legs = {leg["name"]: leg for leg in hosts.interfaces_from(
        hosts.parse_reading("h", DMI),
        ipaddress.ip_network("192.168.88.0/24"))}
    assert legs["enp128s31f6"]["scope"] == "internal"
    assert legs["tailscale0"]["scope"] == "external"


def test_a_reading_is_discarded_when_the_mac_does_not_match():
    """The bench-collision guard. 192.168.88.0/24 is the subnet at every site
    AND on the bench, and a bench station carries a 192.168.88.10 alias - so
    a session opened to the wrong machine answers entirely convincingly."""
    import discover
    import hosts
    import oui
    item = discover.Discovered(
        entry=oui.Entry(mac="aa:bb:cc:dd:ee:ff", ip="192.168.88.10"))
    warnings = hosts.enrich(
        [item], {"AABBCCDDEEFF": "kela-fob-03"},
        nodes={"kela-fob-03": "100.101.8.89"}, reader=_reader(DMI))
    assert item.model is None, "a mismatched host must teach us nothing"
    assert any("DISCARDED" in w for w in warnings)
    assert any("bench-collision guard" in n for n in item.notes)


def test_a_matching_mac_is_accepted_and_carries_device_api_evidence():
    import discover
    import hosts
    import oui
    item = discover.Discovered(
        entry=oui.Entry(mac="e8:cf:83:8d:fc:14", ip="192.168.88.10"))
    hosts.enrich([item], {"E8CF838DFC14": "kela-fob-03"},
                 nodes={"kela-fob-03": "100.101.8.89"}, reader=_reader(DMI))
    assert item.model == "Dell Pro Max Tower T2 FCT2250"
    assert item.evidence["model"] == "device-api"
    assert item.evidence["firmware"] == "device-api"
    assert {i["name"] for i in item.interfaces} == {"enp128s31f6", "tailscale0"}


def test_a_host_that_refuses_a_key_is_a_finding_not_a_silence():
    # The operator station advertises password auth only, so a key is never
    # offered. Fixing that is an sshd change - a write to production.
    import discover
    import hosts
    import oui
    item = discover.Discovered(
        entry=oui.Entry(mac="e8:cf:83:3f:f3:83", ip="192.168.88.29"))
    warnings = hosts.enrich(
        [item], {"E8CF833FF383": "kela-fob-03-operator"},
        nodes={"kela-fob-03-operator": "100.120.150.95"},
        reader=_reader("", error="Permission denied (password)."))
    assert item.model is None
    assert any("Permission denied" in w for w in warnings)
    assert any("could not be read" in n for n in item.notes)


def test_a_device_with_no_tailnet_node_is_left_alone():
    import discover
    import hosts
    import oui
    item = discover.Discovered(
        entry=oui.Entry(mac="8c:1f:64:e7:4c:46", ip="192.168.88.130"))
    hosts.enrich([item], {}, nodes={"kela-fob-03": "100.101.8.89"},
                 reader=_reader(DMI))
    assert item.model is None and item.notes == []


def test_a_reading_off_the_device_outranks_a_bench_record():
    import discover
    import oui
    item = discover.Discovered(entry=oui.Entry(mac="e8:cf:83:8d:fc:14"))
    class Facts:
        model = "something a bench tool saw once"
        firmware = "old"
    item.facts = Facts()
    item.read_model = "Dell Pro Max Tower T2 FCT2250"
    assert item.model == "Dell Pro Max Tower T2 FCT2250"


# -- the name the vantage point needs ------------------------------------
#
# At three of the seven sites surveyed on 16 Sep 2026 the router was missing
# from its own map. `discover.name_devices` names 192.168.88.1 `router`
# straight from the address plan; at those sites .1 is a MikroTik switch and
# the Teltonika's br-lan is .2, so both wanted the same node name, the YAML
# got two `router:` keys and the later one won. The device the survey ran
# FROM was the one it lost - with its model, its firmware and its three
# interfaces.

def _plan_router_at(ip, mac, vendor_name="Routerboard.com"):
    import discover
    import oui
    item = discover.Discovered(entry=oui.Entry(
        mac=mac, ip=ip, vendor=oui.Vendor(name=vendor_name, prefix_bits=24,
                          matched="04F41C")))
    item.name, item.kind = "router", "router"
    return item


def _vantage(addr="192.168.88.2", mac="20:97:27:4e:29:03"):
    return router.Survey(
        host="100.0.0.1", hostname="kela-fob-04",
        interfaces=[router.Iface(name="br-lan", addr=addr, prefix=24,
                                 scope="internal", mac=mac)],
        subnet="192.168.88.0/24")


def test_the_vantage_point_survives_a_name_the_address_plan_took():
    import ipaddress
    found = [_plan_router_at("192.168.88.1", "04:f4:1c:9f:dd:5b")]
    out = router.merge_vantage_point(
        found, _vantage(), ipaddress.ip_network("192.168.88.0/24"))
    names = [i.name for i in out]
    assert len(names) == len(set(names)), f"duplicate node names: {names}"
    router_node = [i for i in out if i.name == "router"][0]
    assert router_node.entry.ip == "192.168.88.2", (
        "the node called `router` is not the device the survey ran from")


def test_the_device_moved_aside_is_renamed_from_what_it_is():
    import ipaddress
    found = [_plan_router_at("192.168.88.1", "04:f4:1c:9f:dd:5b")]
    out = router.merge_vantage_point(
        found, _vantage(), ipaddress.ip_network("192.168.88.0/24"))
    moved = [i for i in out if i.entry.ip == "192.168.88.1"][0]
    assert "routerboard" in moved.name
    # and it stops claiming to be a router, which drew two of them
    assert moved.kind != "router"
    assert "a reading, and the reading wins" in " ".join(moved.notes)


def test_a_router_that_holds_the_planned_address_still_merges_in_place():
    # The ordinary case must not regress: when the Teltonika IS .1, the
    # sweep sees its own ARP entry and the two have to become one node.
    import ipaddress
    import discover
    import oui
    existing = discover.Discovered(entry=oui.Entry(
        mac="20:97:27:3d:af:c0", ip="192.168.88.1"))
    existing.name, existing.kind = "router", "router"
    out = router.merge_vantage_point(
        [existing], _vantage(addr="192.168.88.1", mac="20:97:27:3d:af:c0"),
        ipaddress.ip_network("192.168.88.0/24"))
    assert len(out) == 1 and out[0].name == "router"


def test_a_hyphenated_factory_hostname_still_names_its_model():
    # `Teltonika-TSW202` is how three of the five sites that name their
    # switch spell it. The catalogue spells it `Teltonika TSW202`, and a
    # plain substring test said no to the hyphen - so the switch lost its
    # model shortlist over one character.
    import discover
    import oui
    item = discover.Discovered(entry=oui.Entry(
        mac="20:97:27:28:16:44", ip="192.168.88.191"))
    item.name, item.kind = "teltonika", "network-device"
    router.lease_notes([item], {"20972728164 4".replace(" ", ""):
                                "Teltonika-TSW202"})
    assert item.candidates == ["Teltonika TSW202"]
    assert item.kind == "switch"
