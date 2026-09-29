"""ARP -> site model, and what each fact's evidence ends up as."""
import pytest
import yaml

import bench_central
import discover
from lint import coverage, lint
from model import load_site

ARP = """\
rut-kela-fob-03.lan (192.168.88.1) at 20:97:27:36:55:ec [ether] on enp1
? (192.168.88.132) at 8c:1f:64:e7:48:c7 [ether] on enp1
? (192.168.88.133) at 8c:1f:64:e7:48:d3 [ether] on enp1
kela-fob-03-operator.lan (192.168.88.29) at e8:cf:83:3f:f3:83 [ether] on enp1
KC0500PAZ00052.lan (192.168.88.30) at bc:74:d7:81:17:b1 [ether] on enp1
"""

DB = {
    "209727": "Teltonika Networks UAB",
    "8C1F64": "Ieee Registration Authority",
    "8C1F64E74": "Magosys Systems",
    "E8CF83": "Dell",
    "BC74D7": "HangZhou JuRu Technology",
}


class FakeClient:
    """Stands in for bench-central. `known` maps canonical MAC -> facts."""

    def __init__(self, known=None, fail=()):
        self.known = known or {}
        self.fail = set(fail)
        self.asked = []

    def facts_for_mac(self, mac):
        canon = bench_central.canonical_mac(mac)
        self.asked.append(canon)
        if canon in self.fail:
            raise bench_central.CentralError("boom")
        if canon in self.known:
            return self.known[canon]
        return bench_central.DeviceFacts(mac=canon, runs_seen=0)


ROUTER_MAC = "2097273655ec"

ROUTER_FACTS = bench_central.DeviceFacts(
    mac=ROUTER_MAC, model="RUTM08", firmware="RUTM_R_00.07.14.3",
    serial="6010212527", tool="rutm", last_run="2026-09-02T10:00:00Z",
    runs_seen=2,
)


# -- without bench-central ---------------------------------------------


def test_arp_alone_gives_vendor_and_no_model():
    found, warnings = discover.discover(ARP, DB, client=None)
    assert len(found) == 5
    assert all(d.model is None for d in found)
    assert any("vendor-only" in w for w in warnings)


def test_a_registrar_only_oui_is_not_reported_as_the_vendor():
    """8C:1F:64 without the MA-S entry would name IEEE as the manufacturer."""
    found, _ = discover.discover(ARP, {"8C1F64": "Ieee Registration Authority"}, None)
    magos = [d for d in found if d.entry.ip == "192.168.88.132"][0]
    assert magos.vendor is None
    assert any("registrar" in n for n in magos.notes)


def test_the_magos_ma_s_prefix_resolves_the_real_vendor():
    found, _ = discover.discover(ARP, DB, None)
    magos = [d for d in found if d.entry.ip == "192.168.88.132"][0]
    assert magos.vendor == "Magosys Systems"


def test_a_vendor_with_several_models_is_noted_as_ambiguous():
    found, _ = discover.discover(ARP, DB, None)
    router = [d for d in found if d.entry.ip == "192.168.88.1"][0]
    assert any("cannot tell them apart" in n for n in router.notes)


def test_devices_are_named_from_the_address_plan():
    found, _ = discover.discover(ARP, DB, None)
    names = {d.entry.ip: d.name for d in found}
    assert names["192.168.88.1"] == "router"
    assert names["192.168.88.29"] == "operator_station"
    assert names["192.168.88.30"] == "camera"


def test_devices_outside_the_plan_are_named_from_their_vendor():
    found, _ = discover.discover(ARP, DB, None)
    magos = [d for d in found if d.entry.ip == "192.168.88.132"][0]
    assert magos.name.startswith("magosys")


def test_several_devices_in_one_role_get_suffixed():
    found, _ = discover.discover(ARP, DB, None)
    magos = sorted(d.name for d in found if d.vendor == "Magosys Systems")
    assert len(magos) == 2 and len(set(magos)) == 2


def test_devices_come_out_in_address_order():
    found, _ = discover.discover(ARP, DB, None)
    octets = [int(d.entry.ip.split(".")[-1]) for d in found]
    assert octets == sorted(octets)


# -- with bench-central ------------------------------------------------


def test_a_model_from_bench_central_is_marked_as_a_bench_record():
    client = FakeClient({ROUTER_MAC: ROUTER_FACTS})
    found, _ = discover.discover(ARP, DB, client)
    router = [d for d in found if d.name == "router"][0]
    assert router.model == "RUTM08"
    assert router.firmware == "RUTM_R_00.07.14.3"
    # The crux: vendor from the sweep, model from the archive, on one node.
    assert router.evidence["*"] == "arp"
    assert router.evidence["model"] == "bench-record"
    assert router.evidence["firmware"] == "bench-record"


def test_the_run_history_is_recorded_in_the_notes():
    found, _ = discover.discover(ARP, DB, FakeClient({ROUTER_MAC: ROUTER_FACTS}))
    router = [d for d in found if d.name == "router"][0]
    note = " ".join(router.notes)
    assert "2 run(s)" in note and "rutm" in note and "6010212527" in note


def test_a_miss_leaves_the_model_unknown_and_explains_why():
    found, warnings = discover.discover(ARP, DB, FakeClient({}))
    router = [d for d in found if d.name == "router"][0]
    assert router.model is None
    assert "model" not in router.evidence
    assert any("opt-in" in n for n in router.notes)
    assert any("no bench-central record" in w for w in warnings)


def test_a_model_without_firmware_says_so():
    facts = bench_central.DeviceFacts(
        mac=ROUTER_MAC, model="RUTM08", runs_seen=1)
    found, _ = discover.discover(ARP, DB, FakeClient({ROUTER_MAC: facts}))
    router = [d for d in found if d.name == "router"][0]
    assert router.evidence.get("firmware") is None
    assert any("No firmware recorded" in n for n in router.notes)


def test_a_lookup_failure_is_reported_and_does_not_abort_the_rest():
    client = FakeClient({ROUTER_MAC: ROUTER_FACTS}, fail={"e8cf833ff383"})
    found, warnings = discover.discover(ARP, DB, client)
    assert len(found) == 5
    assert any("boom" in w for w in warnings)
    assert [d for d in found if d.name == "router"][0].model == "RUTM08"


def test_every_mac_is_looked_up_exactly_once():
    client = FakeClient({})
    discover.discover(ARP, DB, client)
    assert len(client.asked) == len(set(client.asked)) == 5


# -- the emitted YAML ---------------------------------------------------


def test_the_emitted_yaml_loads_as_a_site_model(tmp_path):
    found, warnings = discover.discover(ARP, DB, FakeClient({ROUTER_MAC: ROUTER_FACTS}))
    text = discover.to_yaml("kela-fob-03", "192.168.88.0/24", found, warnings)
    path = tmp_path / "s.yaml"
    path.write_text(text, encoding="utf-8")

    site = load_site(path)
    assert site.name == "kela-fob-03"
    assert site.status == "provisional"
    assert len(site.nodes) == 5
    assert site.nodes["router"].model == "RUTM08"
    assert site.nodes["router"].source_for("model") == "bench-record"
    assert site.nodes["router"].source_for("vendor") == "arp"


def test_the_emitted_yaml_declares_no_cables_or_power():
    """An ARP sweep proves neither. Emitting a plausible guess would be the
    most damaging thing this could do."""
    found, warnings = discover.discover(ARP, DB, FakeClient({}))
    text = discover.to_yaml("s", "192.168.88.0/24", found, warnings)
    doc = yaml.safe_load(text)
    assert "net" not in doc and "power" not in doc
    assert "NO net: or power: block" in text


def test_the_discovered_model_lints_without_contradictions(tmp_path):
    found, warnings = discover.discover(ARP, DB, FakeClient({ROUTER_MAC: ROUTER_FACTS}))
    text = discover.to_yaml("s", "192.168.88.0/24", found, warnings)
    path = tmp_path / "s.yaml"
    path.write_text(text, encoding="utf-8")

    findings = lint(load_site(path))
    # Provisional, so gaps are warnings; nothing here should be an error.
    assert [f for f in findings if f.severity == "error"] == []


def test_coverage_after_the_join_shows_vendor_full_and_model_partial(tmp_path):
    found, warnings = discover.discover(ARP, DB, FakeClient({ROUTER_MAC: ROUTER_FACTS}))
    text = discover.to_yaml("s", "192.168.88.0/24", found, warnings)
    path = tmp_path / "s.yaml"
    path.write_text(text, encoding="utf-8")

    cov = coverage(load_site(path))
    assert cov["vendor"]["pct"] == 100.0
    # Only the router was in the archive, and only it claims a model.
    # One of five real devices has a proven model: 20%, not 100%.
    assert cov["model"]["proven"] == 1
    assert cov["model"]["total"] == 5
    assert cov["model"]["pct"] == 20.0
    assert cov["link"]["pct"] is None
    assert cov["power"]["pct"] is None


def test_hostnames_survive_as_comments(tmp_path):
    found, warnings = discover.discover(ARP, DB, FakeClient({}))
    text = discover.to_yaml("s", "192.168.88.0/24", found, warnings)
    assert "# hostname: rut-kela-fob-03.lan" in text


def test_values_needing_quotes_are_quoted(tmp_path):
    """A MAC starting with a digit and containing colons must not be read as
    a YAML sexagesimal or a mapping."""
    found, warnings = discover.discover(ARP, DB, FakeClient({}))
    text = discover.to_yaml("s", "192.168.88.0/24", found, warnings)
    doc = yaml.safe_load(text)
    assert doc["nodes"]["router"]["mac"] == "20:97:27:36:55:ec"


# -- what a vendor settles, and what it does not -------------------------

def test_a_magosys_oui_is_a_radar_and_the_model_stays_unknown():
    """Magos makes radars and APUs, but an APU is an NVIDIA board carrying
    an NVIDIA MAC - so a Magosys OUI is a radar. Which radar it is is a
    different question, and the vendor cannot answer it."""
    arp = "? (192.168.88.130) at 8c:1f:64:e7:4c:46 [ether] on br-lan\n"
    found, _ = discover.discover(arp, {"8C1F64E74": "Magosys Systems"}, None)
    assert found[0].kind == "radar"
    assert found[0].model is None, "a vendor never settles a model"


def test_a_hangzhou_oui_is_a_camera():
    arp = "? (192.168.88.111) at bc:74:d7:81:17:b1 [ether] on br-lan\n"
    found, _ = discover.discover(
        arp, {"BC74D7": "HangZhou JuRu Technology"}, None)
    assert found[0].kind == "camera"


def test_a_dell_oui_settles_nothing_between_a_server_and_a_station():
    # Both are Dell on e8:cf:83. Only the hostname tells them apart, which
    # is router.kind_from_hostname's job, not the vendor table's.
    arp = "? (192.168.88.77) at e8:cf:83:8d:fc:14 [ether] on br-lan\n"
    found, _ = discover.discover(arp, {"E8CF83": "Dell"}, None)
    assert found[0].kind == "operator-station", "the weak fallback, by design"
