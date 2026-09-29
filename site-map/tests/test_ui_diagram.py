"""The page actually renders, not just parses.

`node --check` in test_ui_page.py proves the inline script is syntactically
valid. It cannot prove the script runs: a diagram that throws on a payload
shape, or lays a box out at `NaN,NaN`, parses perfectly and renders nothing.
The blanked dashboard that reached production was caught by a person opening
the page, and this is the cheapest thing that stands in for that person.

The stub in tests/support/render_page.js is a few lines of fake DOM, so it
exercises the real script against the real exported payload.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui"
RENDERER = Path(__file__).resolve().parent / "support" / "render_page.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed; CI always has it"
)


def render(ui_dir):
    done = subprocess.run(
        ["node", str(RENDERER), str(ui_dir)],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert done.returncode == 0, (
        f"the page threw while rendering:\n{done.stderr}"
    )
    return json.loads(done.stdout)


@pytest.fixture(scope="module")
def rendered():
    return render(UI)


def stage(tmp_path, site):
    """The real page, against a payload of our choosing."""
    import shutil
    ui = tmp_path / "ui"
    ui.mkdir(exist_ok=True)
    shutil.copy(UI / "index.html", ui / "index.html")
    (ui / "site-data.js").write_text(
        "window.SITE_DATA = "
        + json.dumps({"version": 1, "sites": [site], "devices": {}}) + ";\n")
    (ui / "archive-data.js").write_text("window.ARCHIVE_CONTEXT = null;\n")
    return ui


def site_payload_for(text, tmp_path):
    import sys
    sys.path.insert(0, str(ROOT))
    import export
    from model import load_site
    path = tmp_path / "s.yaml"
    path.write_text(text)
    return export.site_payload(load_site(path))


ROUTERLESS = """\
version: 1
site: two-switch-site
subnet: 10.11.10.0/24
status: provisional
nodes:
  sw_a:
    kind: switch
    vendor: Routerboard.com
    addr: 10.11.10.155
    evidence: {"*": arp}
  sw_b:
    kind: switch
    vendor: Routerboard.com
    addr: 10.11.10.161
    evidence: {"*": arp}
  radar_1:
    kind: radar
    vendor: Magosys Systems
    addr: 10.11.10.113
    evidence: {"*": arp}
"""


def test_a_site_with_no_router_and_no_edges_still_draws(tmp_path):
    """The layout skips empty layers rather than collapsing.

    A swept site can have no router identified and no cabling at all, which
    leaves layers 0 and 1 empty. Every box then has to land somewhere real:
    an empty row that still advances the cursor puts the rest at NaN.
    """
    site = site_payload_for(ROUTERLESS, tmp_path)
    assert site["topology"]["router"] is None
    out = render(stage(tmp_path, site))
    assert out["has_svg"] and out["bad_coords"] is None
    assert out["boxes"] == 3, "no internet box without a router to attach it to"
    assert out["proven_edges"] == 0 and out["pattern_edges"] == 0
    assert out["notes"] >= 2, "it has to say why there is nothing drawn"


def test_a_swept_site_does_not_inherit_the_other_site_s_findings(tmp_path):
    site = site_payload_for(ROUTERLESS, tmp_path)
    site["live"] = True
    out = render(stage(tmp_path, site))
    assert out["banner_chars"] > 300
    assert out["site_name"] == "two-switch-site"


def test_the_page_runs_end_to_end_and_draws_something(rendered):
    assert rendered["has_svg"], "no <svg> reached the diagram panel"
    assert rendered["svg_chars"] > 1000
    assert rendered["site_name"], "the masthead never got a site name"
    assert rendered["devices_rendered"] > 0, "the device list is empty"
    assert rendered["banner_chars"] > 500, "the banner did not render"


def test_no_box_is_laid_out_at_nan(rendered):
    # An arithmetic slip in the layout puts every coordinate at NaN, which
    # SVG renders as an empty canvas with no error anywhere.
    assert rendered["bad_coords"] is None, rendered["bad_coords"]


def test_every_device_in_the_model_is_on_the_diagram(rendered):
    # 10 devices at kela-fob-03, plus the internet box.
    assert rendered["boxes"] == rendered["devices_rendered"] + 1


def test_established_and_expected_cables_are_drawn_differently(rendered):
    # The whole point of the diagram. If these ever collapse into one class
    # the page starts asserting cabling nobody checked.
    assert rendered["proven_edges"] >= 1
    assert rendered["pattern_edges"] >= 1
    assert "established" in rendered["hint"] and "expected" in rendered["hint"]


def test_the_diagram_says_how_much_of_it_is_unchecked(rendered):
    assert rendered["notes"] >= 1


def test_every_line_and_box_carries_a_title_for_hovering(rendered):
    # The basis for each line is the useful part and there is no room for it
    # on the canvas, so it lives in a <title>.
    assert rendered["titles"] >= rendered["proven_edges"] + rendered["pattern_edges"]


MULTI_LEG = """\
version: 1
site: two-leg-site
subnet: 192.168.88.0/24
status: provisional
uplink: rut
nodes:
  rut:
    kind: router
    vendor: Teltonika Networks
    model: RUTM08
    addr: 192.168.88.1
    evidence: {"*": device-api}
    interfaces:
      - name: wan
        addr: 192.168.1.164
        scope: external
        prefix: 24
      - name: br-lan
        addr: 192.168.88.1
        scope: internal
        prefix: 24
      - name: tailscale0
        addr: 100.64.242.104
        scope: external
        prefix: 32
  server:
    kind: server
    vendor: Dell
    addr: 192.168.88.10
    evidence: {"*": arp}
"""


def test_a_three_legged_router_shows_three_addresses_not_one(tmp_path):
    """Every leg gets a line, with its own E/I and its own address.

    Showing one address and a "+2" put the other two in tooltips, where an
    installer standing at a rack cannot read them.
    """
    out = render(stage(tmp_path, site_payload_for(MULTI_LEG, tmp_path)))
    assert out["bad_coords"] is None
    # Three legs on the router, one on the server.
    assert out["scope_glyphs"] == 4
    assert out["iface_names"] == 3, "only the router's legs are named"


def test_the_box_grows_for_a_device_with_more_legs(tmp_path):
    # A fixed height overlapped the router onto the row below it. Asserted
    # as a relationship rather than two magic numbers, so tuning the line
    # height does not break the test that guards the behaviour.
    out = render(stage(tmp_path, site_payload_for(MULTI_LEG, tmp_path)))
    heights = sorted(set(out["box_heights"]))
    assert len(heights) == 2, heights
    one_leg, three_legs = heights
    assert three_legs == one_leg + 2 * (three_legs - one_leg) / 2
    assert three_legs > one_leg, "a three-legged device needs a taller box"


def test_an_address_and_its_interface_name_do_not_overlap(tmp_path):
    """`100.64.242.104` ran straight into a right-aligned `tailscale0`.

    The interface name is at a fixed column now. Right-aligning it put its
    left edge wherever its own length happened to land, so a long address
    and a long interface name collided in the middle of the box.
    """
    out = render(stage(tmp_path, site_payload_for(MULTI_LEG, tmp_path)))
    assert out["min_label_slack"] is not None, "no labelled interfaces drawn"
    assert out["min_label_slack"] > 0, (
        f"an address overlaps its interface name by "
        f"{-out['min_label_slack']:.1f}px"
    )


def test_the_site_subnet_leg_leads(tmp_path):
    # The router reports `wan` first, so taking interfaces[0] as the primary
    # led with 192.168.1.164 - an address on somebody else's network.
    site = site_payload_for(MULTI_LEG, tmp_path)
    ifaces = site["nodes"]["rut"]["interfaces"]
    assert ifaces[0]["addr"] == "192.168.88.1"
    assert ifaces[0]["scope"] == "internal"


def _many_devices(count):
    rows = "".join(f"""  radar_{i}:
    kind: radar
    vendor: Magosys Systems
    addr: 192.168.88.{100 + i}
    evidence: {{"*": arp}}
""" for i in range(count))
    return MULTI_LEG + rows


def test_leaves_never_wrap_so_no_cable_crosses_a_device(tmp_path):
    """A wrapped second row made the diagram read switch -> server -> radar.

    The edge from the switch to a device on the second row had to pass the
    first row, which looks exactly like the server feeding the radar. They
    are only neighbours on the same switch. One row means nothing crosses,
    and the panel scrolls sideways instead.
    """
    site = site_payload_for(_many_devices(9), tmp_path)
    out = render(stage(tmp_path, site))
    assert out["bad_coords"] is None
    # Four layers: outside, router, switch, and ONE row of leaves.
    assert out["leaf_rows"] == 4, (
        f"{out['leaf_rows']} distinct box rows — the leaves wrapped, so an "
        f"edge to the lower row crosses the upper one"
    )


# -- what a forwarding-database read looks like on the page --------------
#
# Five of the seven sites surveyed on 16 Sep 2026 now carry a router->switch
# link proven by the router's own bridge, and one of the diagram's jobs is to
# draw that as a fact rather than as the pattern's guess. None of those sites
# has an unmanaged switch, so the proven-but-anonymous box has no live
# example - which is exactly why it needs one here.

FANOUT_SITE = """\
version: 1
site: fanout-site
subnet: 10.11.10.0/24
status: provisional
nodes:
  router:
    kind: router
    vendor: Teltonika Networks UAB
    model: Teltonika RUTM08
    addr: 10.11.10.2
    roles: [default-gateway]
    evidence: {"*": device-api}
  switchy:
    kind: switch
    vendor: Teltonika Networks UAB
    addr: 10.11.10.128
    evidence: {"*": arp}
  camera_1:
    kind: camera
    vendor: HangZhou JuRu Technology
    addr: 10.11.10.30
    evidence: {"*": arp}
net:
  - from: router
    to: switchy
    port: lan1
    evidence: switch-table
    notes: the router learned this switch's MAC on lan1
"""


def multi(tmp_path, sites):
    ui = tmp_path / "ui-multi"
    ui.mkdir(exist_ok=True)
    shutil.copy(UI / "index.html", ui / "index.html")
    (ui / "site-data.js").write_text(
        "window.SITE_DATA = "
        + json.dumps({"version": 1, "sites": sites, "devices": {}}) + ";\n")
    (ui / "archive-data.js").write_text("window.ARCHIVE_CONTEXT = null;\n")
    return ui


def test_a_read_link_draws_solid_and_labels_its_port(tmp_path):
    site = site_payload_for(FANOUT_SITE, tmp_path)
    out = render(stage(tmp_path, site))
    assert out["proven_edges"] >= 1, "a read link drew as an expectation"
    assert out["read_edge_ports"] >= 1, "the port it was learned on is not shown"
    assert out["bad_coords"] is None


def test_a_switch_proven_by_fan_out_is_not_drawn_as_a_guess(tmp_path):
    """An unmanaged switch has no address, so it can never be a node - and
    the one thing we do know about it is a reading, not an expectation."""
    import sys
    sys.path.insert(0, str(ROOT))
    import export
    import router as router_mod
    from model import load_site

    path = tmp_path / "hidden.yaml"
    path.write_text(FANOUT_SITE.replace("""  switchy:
    kind: switch
    vendor: Teltonika Networks UAB
    addr: 10.11.10.128
    evidence: {"*": arp}
""", "").replace("""net:
  - from: router
    to: switchy
    port: lan1
    evidence: switch-table
    notes: the router learned this switch's MAC on lan1
""", ""))
    site = load_site(path)
    fan = router_mod.FanOut(
        port="lan1", macs=["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"],
        known=["camera_1"], switches=[], unknown=["aa:bb:cc:dd:ee:02"])
    out = render(stage(tmp_path, export.site_payload(site, [fan])))
    assert out["anon_boxes"] == 1, "the proven switch drew as a dashed guess"
    assert out["proven_edges"] >= 1
    assert out["bad_coords"] is None


def test_the_switcher_appears_only_when_there_is_something_to_switch(tmp_path):
    one = site_payload_for(FANOUT_SITE, tmp_path)
    assert render(stage(tmp_path, one))["site_buttons"] == 0

    two = site_payload_for(ROUTERLESS, tmp_path)
    out = render(multi(tmp_path, [one, two]))
    assert out["site_buttons"] == 2
    # ... and it still renders the first site, not a merge of both.
    assert out["site_name"] == "fanout-site"
    assert out["bad_coords"] is None


def test_an_ambiguous_switch_is_a_region_around_the_real_ones(tmp_path):
    """Two switches behind one cable must not draw a third switch.

    The placeholder for "one of these, unread" was rendered as a sibling box
    with a kind label and an address line, so a site with two switches showed
    three, and every device hung off the invented one. It is a region around
    the candidates now: same claim, and it cannot be mistaken for a device.
    """
    import sys
    sys.path.insert(0, str(ROOT))
    import export
    import router as router_mod
    from model import load_site

    path = tmp_path / "ambiguous.yaml"
    path.write_text(FANOUT_SITE.replace("""net:
  - from: router
    to: switchy
    port: lan1
    evidence: switch-table
    notes: the router learned this switch's MAC on lan1
""", "") + """  switchy_2:
    kind: switch
    vendor: Routerboard.com
    addr: 10.11.10.1
    evidence: {"*": arp}
""")
    site = load_site(path)
    fan = router_mod.FanOut(
        port="lan1", macs=["a", "b", "c"],
        known=["switchy", "switchy_2", "camera_1"],
        switches=["switchy", "switchy_2"], unknown=[])
    payload = export.site_payload(site, [fan])

    region = [b for b in payload["topology"]["boxes"] if b["group"]]
    assert len(region) == 1, "expected exactly one region"
    assert sorted(region[0]["members"]) == ["switchy", "switchy_2"]

    out = render(stage(tmp_path, payload))
    assert out["regions"] == 1
    # the region contains both switch boxes, and is not one itself
    assert out["region_encloses"] == [2], out["region_encloses"]
    assert out["bad_coords"] is None
    # every device still has a cable
    reached = {e["child"] for e in payload["topology"]["edges"]}
    assert "camera_1" in reached
