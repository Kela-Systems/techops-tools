"""Evidence: what proves a fact, and what it does not."""
import textwrap

import evidence
from lint import ERROR, WARN, coverage, lint
from model import SiteModelError, load_site


def site_from(tmp_path, text):
    path = tmp_path / "site.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return load_site(path)


def codes(findings):
    return {f.code for f in findings}


def only(findings, code):
    return [f for f in findings if f.code == code]


# -- the vocabulary ----------------------------------------------------


def test_arp_does_not_prove_a_model():
    """The mistake this module exists to prevent: an OUI gives a vendor, and
    Teltonika alone makes the RUTM08, the RUTM51 and the RUTX50."""
    assert evidence.proves("arp", evidence.CLAIM_MODEL) is False
    assert evidence.proves("arp", evidence.CLAIM_PRESENT) is True
    assert evidence.proves("arp", evidence.CLAIM_ADDR) is True


def test_the_device_api_proves_a_model():
    assert evidence.proves("device-api", evidence.CLAIM_MODEL) is True


def test_a_bench_record_proves_the_model_but_not_presence():
    """It is the model the device reported when it was provisioned. That does
    not mean the box is still plugged in."""
    assert evidence.proves("bench-record", evidence.CLAIM_MODEL) is True
    assert evidence.proves("bench-record", evidence.CLAIM_PRESENT) is False


def test_config_and_doc_prove_nothing_about_reality():
    for name in ("config", "doc", "assumed"):
        assert evidence.get(name).proves == frozenset()
        assert evidence.get(name).live is False


def test_arp_does_not_prove_a_cable():
    assert evidence.proves("arp", evidence.CLAIM_LINK) is False
    assert evidence.proves("switch-table", evidence.CLAIM_LINK) is True


def test_poe_status_is_the_only_network_source_for_power():
    assert evidence.proves("poe-status", evidence.CLAIM_POWER) is True
    assert evidence.proves("poe-status", evidence.CLAIM_DRAW) is True
    for name in ("arp", "lldp", "switch-table", "dhcp-lease", "device-api"):
        assert evidence.proves(name, evidence.CLAIM_POWER) is False


def test_a_survey_proves_everything_because_a_person_looked():
    for claim in evidence.ALL_CLAIMS:
        assert evidence.proves("survey", claim) is True


def test_unmarked_defaults_to_assumed():
    assert evidence.get(None).name == "assumed"
    assert evidence.get("assumed").proves == frozenset()


def test_an_unknown_evidence_name_is_rejected(tmp_path):
    with pytest.raises(SiteModelError, match="evidence 'vibes'"):
        site_from(tmp_path, """
            version: 1
            site: t
            subnet: 192.168.88.0/24
            nodes:
              a: {kind: switch, evidence: vibes}
        """)


import pytest  # noqa: E402  (used by the test above)


# -- the checks --------------------------------------------------------

ARP_ONLY = """
version: 1
site: t
subnet: 192.168.88.0/24
nodes:
  router:
    kind: router
    model: Teltonika RUTM08
    addr: 192.168.88.1
    addr_source: static-bench
    evidence: arp
    roles: [gateway, dhcp-server]
"""


def test_a_model_backed_only_by_arp_is_flagged(tmp_path):
    findings = lint(site_from(tmp_path, ARP_ONLY))
    found = only(findings, "model-unverified")
    assert [f.where for f in found] == ["router"]
    assert "A vendor is not a model." in found[0].message
    assert "bench-central" in found[0].message


def test_an_address_backed_by_arp_is_accepted(tmp_path):
    """ARP genuinely does prove that something answered at that address."""
    assert "addr-unverified" not in codes(lint(site_from(tmp_path, ARP_ONLY)))


def test_a_model_from_the_device_api_is_accepted(tmp_path):
    findings = lint(site_from(tmp_path, ARP_ONLY.replace(
        "evidence: arp", "evidence: device-api")))
    assert "model-unverified" not in codes(findings)


def test_a_model_from_a_bench_record_is_accepted(tmp_path):
    findings = lint(site_from(tmp_path, ARP_ONLY.replace(
        "evidence: arp", "evidence: bench-record")))
    assert "model-unverified" not in codes(findings)


LINKED = """
version: 1
site: t
subnet: 192.168.88.0/24
nodes:
  sw: {kind: poe-switch, addr: 192.168.88.3, evidence: device-api}
  radar: {kind: radar, addr: 192.168.88.50, evidence: device-api}
net:
  - {from: sw, to: radar, port: gi1, evidence: EV}
power:
  - {from: sw, to: radar, outlet: gi1, via: poe, draw_w: 35, evidence: EV}
"""


def test_an_unproven_cable_is_flagged(tmp_path):
    findings = lint(site_from(tmp_path, LINKED.replace("EV", "config")))
    found = only(findings, "link-unverified")
    assert found and "MAC-address table" in found[0].message


def test_a_cable_proven_by_the_switch_table_is_accepted(tmp_path):
    findings = lint(site_from(tmp_path, LINKED.replace("EV", "switch-table")))
    assert "link-unverified" not in codes(findings)


def test_poe_power_and_draw_need_the_switch(tmp_path):
    findings = lint(site_from(tmp_path, LINKED.replace("EV", "config")))
    assert {"power-unverified", "draw-unverified"} <= codes(findings)


def test_poe_status_closes_power_and_draw(tmp_path):
    findings = lint(site_from(tmp_path, LINKED.replace("EV", "poe-status")))
    assert "power-unverified" not in codes(findings)
    assert "draw-unverified" not in codes(findings)


SURVEY_NEEDED = """
version: 1
site: t
subnet: 192.168.88.0/24
nodes:
  psu: {kind: psu, roles: [power-source]}
  sw: {kind: poe-switch, addr: 192.168.88.3, evidence: device-api}
power:
  - {from: psu, to: sw, inlet: PWR1, via: dc, evidence: EV}
"""


def test_a_dc_feed_is_reported_as_needing_a_survey(tmp_path):
    """No protocol can close this, and the tool must say so rather than imply
    a verifier will get to it."""
    findings = lint(site_from(tmp_path, SURVEY_NEEDED.replace("EV", "assumed")))
    found = only(findings, "power-needs-survey")
    assert found
    assert "cannot be proven by any protocol" in found[0].message
    assert "power-unverified" not in codes(findings)


def test_a_surveyed_dc_feed_is_accepted(tmp_path):
    findings = lint(site_from(tmp_path, SURVEY_NEEDED.replace("EV", "survey")))
    assert "power-needs-survey" not in codes(findings)


def test_poe_status_does_not_satisfy_a_dc_feed(tmp_path):
    """A switch reporting PoE says nothing about its own DC input."""
    findings = lint(site_from(tmp_path, SURVEY_NEEDED.replace("EV", "poe-status")))
    assert "power-needs-survey" not in codes(findings)


# -- gating ------------------------------------------------------------


def test_evidence_findings_are_warnings_by_default(tmp_path):
    from lint import EVIDENCE_CODES
    findings = lint(site_from(tmp_path, ARP_ONLY))
    ev = [f for f in findings if f.code in EVIDENCE_CODES]
    assert ev and all(f.severity == WARN for f in ev)


def test_require_evidence_promotes_them_to_errors(tmp_path):
    findings = lint(site_from(tmp_path, ARP_ONLY), require_evidence=True)
    found = only(findings, "model-unverified")
    assert [f.severity for f in found] == [ERROR]


def test_require_evidence_leaves_real_errors_as_errors(tmp_path):
    """The router in ARP_ONLY has no declared power — a genuine error, and it
    must stay an error under either mode."""
    findings = lint(site_from(tmp_path, ARP_ONLY), require_evidence=True)
    assert [f.severity for f in only(findings, "unpowered")] == [ERROR]


# -- coverage ----------------------------------------------------------


def test_coverage_reports_nothing_proven_for_an_unmarked_site(tmp_path):
    cov = coverage(site_from(tmp_path, LINKED.replace("EV", "config")))
    assert cov["link"] == {"proven": 0, "claimed": 1, "total": 1, "pct": 0.0}
    assert cov["power"]["pct"] == 0.0
    assert cov["draw"]["pct"] == 0.0


def test_coverage_reports_full_when_everything_is_proven(tmp_path):
    cov = coverage(site_from(tmp_path, LINKED.replace("EV", "poe-status")))
    assert cov["link"]["pct"] == 100.0
    assert cov["power"]["pct"] == 100.0
    assert cov["draw"]["pct"] == 100.0


def test_devices_with_no_model_recorded_score_as_zero_not_as_absent(tmp_path):
    """Two devices, neither with a model: 0% of the models are known. Scoring
    that as "nothing claimed, so 100%" is the trap."""
    cov = coverage(site_from(tmp_path, LINKED.replace("EV", "poe-status")))
    assert cov["model"]["claimed"] == 0
    assert cov["model"]["total"] == 2
    assert cov["model"]["pct"] == 0.0


def test_coverage_breaks_down_by_source(tmp_path):
    cov = coverage(site_from(tmp_path, LINKED.replace("EV", "switch-table")))
    assert cov["by_source"]["device-api"] == 2
    assert cov["by_source"]["switch-table"] == 2


def test_coverage_counts_feeds_that_only_a_survey_can_close(tmp_path):
    cov = coverage(site_from(tmp_path, SURVEY_NEEDED.replace("EV", "assumed")))
    assert cov["survey_only_feeds"] == 1


EMPTY_POWER = """
version: 1
site: t
subnet: 192.168.88.0/24
nodes:
  a: {kind: switch, vendor: PLANET, addr: 192.168.88.3, evidence: arp}
"""


def test_a_site_with_no_power_graph_does_not_report_power_as_verified(tmp_path):
    cov = coverage(site_from(tmp_path, EMPTY_POWER))
    assert cov["power"]["pct"] is None
    assert cov["link"]["pct"] is None


def test_an_oui_proves_the_vendor_but_not_the_model(tmp_path):
    """The exact shape of the fob-03 data: vendor fully proven, model at 0%."""
    cov = coverage(site_from(tmp_path, EMPTY_POWER))
    assert cov["vendor"] == {"proven": 1, "claimed": 1, "total": 1,
                             "pct": 100.0}
    assert cov["model"]["pct"] == 0.0
    assert cov["model"]["claimed"] == 0


def test_a_vendor_claim_from_config_is_flagged(tmp_path):
    findings = lint(site_from(tmp_path, EMPTY_POWER.replace(
        "evidence: arp", "evidence: config")))
    assert "vendor-unverified" in codes(findings)


PROVISIONAL = """
version: 1
site: t
status: provisional
subnet: 192.168.88.0/24
nodes:
  a: {kind: radar, addr: 192.168.88.50, evidence: arp}
  b: {kind: radar, addr: 192.168.88.50, evidence: arp}
"""


def test_a_provisional_site_downgrades_missing_power_to_a_warning(tmp_path):
    findings = lint(site_from(tmp_path, PROVISIONAL))
    found = only(findings, "unpowered")
    assert found and all(f.severity == WARN for f in found)
    assert "provisional" in found[0].message


def test_a_provisional_site_still_errors_on_a_contradiction(tmp_path):
    """Incomplete is forgivable; two devices on one address never is."""
    findings = lint(site_from(tmp_path, PROVISIONAL))
    assert [f.severity for f in only(findings, "addr-collision")] == [ERROR]


def test_an_active_site_errors_on_missing_power(tmp_path):
    findings = lint(site_from(tmp_path, PROVISIONAL.replace(
        "status: provisional\n", "")))
    assert [f.severity for f in only(findings, "unpowered")] == [ERROR] * 2


def test_an_unknown_status_is_rejected(tmp_path):
    with pytest.raises(SiteModelError, match="status 'maybe'"):
        site_from(tmp_path, PROVISIONAL.replace("provisional", "maybe"))


# -- per-claim evidence ------------------------------------------------

MIXED = """
version: 1
site: t
subnet: 192.168.88.0/24
nodes:
  router:
    kind: router
    vendor: Teltonika Networks
    model: Teltonika RUTM08
    firmware: RUTM_R_00.07.14
    mac: 20:97:27:36:55:ec
    addr: 192.168.88.1
    evidence:
      "*": arp
      model: bench-record
      firmware: bench-record
"""


def test_one_node_can_carry_different_sources_per_claim(tmp_path):
    """The shape the ARP + bench-central join produces: vendor and address
    from the sweep, model and firmware from the run records."""
    site = site_from(tmp_path, MIXED)
    node = site.nodes["router"]
    assert node.source_for("vendor") == "arp"
    assert node.source_for("addr") == "arp"
    assert node.source_for("model") == "bench-record"
    assert node.source_for("firmware") == "bench-record"
    assert node.proves("vendor") and node.proves("model")
    assert node.proves("firmware")


def test_a_mixed_node_raises_no_evidence_findings(tmp_path):
    findings = lint(site_from(tmp_path, MIXED))
    assert "model-unverified" not in codes(findings)
    assert "vendor-unverified" not in codes(findings)
    assert "firmware-unverified" not in codes(findings)


def test_firmware_from_arp_is_flagged(tmp_path):
    """ARP cannot possibly know a firmware version."""
    findings = lint(site_from(tmp_path, MIXED.replace(
        "      firmware: bench-record\n", "")))
    found = only(findings, "firmware-unverified")
    assert found and "only the device or a bench record" in found[0].message.lower()


def test_coverage_counts_each_claim_independently(tmp_path):
    cov = coverage(site_from(tmp_path, MIXED))
    assert cov["vendor"]["pct"] == 100.0
    assert cov["model"]["pct"] == 100.0
    assert cov["firmware"]["pct"] == 100.0


def test_coverage_by_source_counts_every_source_a_node_uses(tmp_path):
    cov = coverage(site_from(tmp_path, MIXED))
    assert cov["by_source"]["arp"] == 1
    assert cov["by_source"]["bench-record"] == 1


def test_an_unknown_claim_key_is_rejected(tmp_path):
    with pytest.raises(SiteModelError, match="'colour' is not a claim"):
        site_from(tmp_path, MIXED.replace("model: bench-record",
                                          "colour: bench-record"))


def test_an_unknown_source_in_a_mapping_is_rejected(tmp_path):
    with pytest.raises(SiteModelError, match="evidence 'hunch'"):
        site_from(tmp_path, MIXED.replace("model: bench-record",
                                          "model: hunch"))


def test_a_bare_source_name_still_works(tmp_path):
    """The short form has to keep working; most items have one source."""
    site = site_from(tmp_path, MIXED.replace(
        '''    evidence:
      "*": arp
      model: bench-record
      firmware: bench-record''', "    evidence: device-api"))
    node = site.nodes["router"]
    assert node.evidence.is_uniform()
    assert node.proves("model") and node.proves("firmware")


PARTIAL = """
version: 1
site: t
subnet: 192.168.88.0/24
nodes:
  known:
    kind: router
    model: RUTM08
    addr: 192.168.88.1
    evidence: {"*": arp, model: bench-record}
  unknown_a: {kind: radar, addr: 192.168.88.50, evidence: arp}
  unknown_b: {kind: radar, addr: 192.168.88.51, evidence: arp}
  mains: {kind: mains, roles: [power-source]}
"""


def test_coverage_scores_models_against_every_real_device(tmp_path):
    """One model proven, on a site of three real devices, is 33% knowledge of
    the models — not 100% because only one device claimed one."""
    cov = coverage(site_from(tmp_path, PARTIAL))
    assert cov["model"]["proven"] == 1
    assert cov["model"]["claimed"] == 1
    assert cov["model"]["total"] == 3
    assert cov["model"]["pct"] == 33.3


def test_coverage_ignores_external_nodes_that_cannot_have_a_model(tmp_path):
    """The mains feed has no model, so it is not counted as a gap."""
    cov = coverage(site_from(tmp_path, PARTIAL))
    assert cov["model"]["total"] == 3  # not 4 — `mains` excluded
    assert cov["addr"]["total"] == 3


def test_coverage_still_reports_full_when_every_device_is_known(tmp_path):
    cov = coverage(site_from(tmp_path, PARTIAL.replace(
        "  unknown_a: {kind: radar, addr: 192.168.88.50, evidence: arp}\n"
        "  unknown_b: {kind: radar, addr: 192.168.88.51, evidence: arp}\n", "")))
    assert cov["model"] == {"proven": 1, "claimed": 1, "total": 1, "pct": 100.0}
