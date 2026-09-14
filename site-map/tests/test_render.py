"""Rendering. These assert on structure, not on exact diagram text."""
import textwrap

from model import load_site
from render import (
    render_markdown,
    render_network_mermaid,
    render_power_mermaid,
    render_tree,
)

SITE = """
version: 1
site: demo
subnet: 192.168.88.0/24
uplink: router
reservations:
  cameras: {range: 30-50, owner: cam, dynamic: true}
nodes:
  mains: {kind: mains, roles: [power-source]}
  router:
    kind: router
    model: Teltonika RUTM08
    addr: 192.168.88.1
    roles: [gateway, dhcp-server]
  sw: {kind: poe-switch, addr: 192.168.88.3}
  radar: {kind: radar, addr: 192.168.88.50}
  cam: {kind: camera, addr: 192.168.88.30, addr_pool: cameras, addr_source: dhcp-lease}
  otd: {kind: modem, model: Teltonika OTD500}
net:
  - {from: router, to: sw, port: lan1, peer_port: gi10}
  - {from: sw, to: radar, port: gi1}
  - {from: sw, to: cam, port: gi9}
  - {from: router, to: otd, peer_port: wan}
power:
  - {from: mains, to: router, via: mains}
  - {from: mains, to: sw, via: mains}
  - {from: mains, to: cam, via: dc}
  - {from: mains, to: otd, via: dc}
  - {from: sw, to: radar, outlet: gi1, via: poe, draw_w: 35}
"""


def site(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(textwrap.dedent(SITE), encoding="utf-8")
    return load_site(path)


def test_network_diagram_has_every_node_and_edge(tmp_path):
    out = render_network_mermaid(site(tmp_path))
    assert out.startswith("%% demo")
    assert "flowchart LR" in out
    for name in ("router", "sw", "radar", "cam", "otd"):
        assert f"n_{name}[" in out or f"n_{name}[[" in out
    assert out.count("-->") == 4


def test_network_edge_label_shows_both_ports(tmp_path):
    out = render_network_mermaid(site(tmp_path))
    assert '-->|"lan1 -> gi10"|' in out


def test_a_psu_with_no_data_link_stays_off_the_network_diagram(tmp_path):
    out = render_network_mermaid(site(tmp_path))
    assert "n_mains" not in out


def test_power_diagram_distinguishes_poe_from_hard_wired(tmp_path):
    out = render_power_mermaid(site(tmp_path))
    assert "flowchart TD" in out
    # PoE dotted, everything else solid — the line style answers "does this
    # survive the switch losing power".
    assert '-.->|"gi1 poe 35 W"|' in out
    assert "n_mains --> n_router" in out or '-->|"' in out


def test_addresses_carry_their_source_into_the_label(tmp_path):
    out = render_network_mermaid(site(tmp_path))
    assert "192.168.88.30 (lease)" in out
    assert "class n_cam leased;" in out
    assert "class n_router static;" in out
    # A real device with no address on this subnet is not off-site plumbing.
    assert "class n_otd unaddressed;" in out
    assert "class n_mains external;" not in out  # mains is not on this diagram


def test_labels_escape_quotes(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(
        'version: 1\nsite: q\nsubnet: 192.168.88.0/24\n'
        'nodes:\n  a: {kind: switch, model: \'the "big" one\'}\n',
        encoding="utf-8",
    )
    out = render_network_mermaid(load_site(path))
    assert "&quot;big&quot;" in out
    assert '"big"' not in out


def test_markdown_bundles_both_diagrams_and_the_tables(tmp_path):
    out = render_markdown(site(tmp_path))
    assert out.count("```mermaid") == 2
    assert "## Network" in out and "## Power" in out
    assert "| `192.168.88.1` | router | router | static-bench |" in out
    # Addresses ascend numerically, not lexically: .3 before .30.
    assert out.index("`192.168.88.3`") < out.index("`192.168.88.30`")
    assert "## Reserved ranges" in out
    assert "No address: mains, otd." in out


def test_tree_shows_the_chain_with_power_alongside(tmp_path):
    out = render_tree(site(tmp_path))
    assert out.splitlines()[0].startswith("demo  (192.168.88.0/24)")
    assert "gi1 -> radar 192.168.88.50   [power: sw:gi1 35W]" in out
    assert "(dhcp-lease)" in out
    assert "not on the data path:" in out
    assert "mains" in out


def test_tree_flags_a_device_with_no_declared_power(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(
        textwrap.dedent(SITE).replace("  - {from: mains, to: sw, via: mains}\n", ""),
        encoding="utf-8",
    )
    assert "[no power declared]" in render_tree(load_site(path))


def test_a_cyclic_site_still_renders(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(
        textwrap.dedent(SITE).replace(
            "  - {from: sw, to: radar, port: gi1}",
            "  - {from: sw, to: radar, port: gi1}\n  - {from: radar, to: router}"),
        encoding="utf-8",
    )
    loaded = load_site(path)
    assert "flowchart" in render_network_mermaid(loaded)
    assert render_tree(loaded)
