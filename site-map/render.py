#!/usr/bin/env python3
"""Turn a site model into something a human reads.

Two renderers, because the two graphs answer different questions and a
combined picture answers neither well:

  network  flowchart LR, rooted at the uplink — connection flow, and each
           node labelled with its address and where that address came from
  power    flowchart TD, rooted at the mains — what feeds what, in watts

Mermaid rather than Graphviz: GitHub renders it inline in a README or a PR,
which is where an installer or a reviewer actually looks, and it needs no
binary installed on a bench laptop.

Styling stays to strokes and dashes with no fills — these diagrams get read
in both light and dark, and a hard-coded fill is illegible in one of them.
"""
from __future__ import annotations

import re

from model import Site

_ID_SAFE = re.compile(r"[^A-Za-z0-9_]")

# Dashes carry the addressing story: a solid box has an address someone
# provisioned deliberately, a dashed one holds a lease that can move, and a
# thick one has not been provisioned at all yet.
_CLASS_DEFS = (
    "classDef static stroke-width:1.5px;",
    "classDef leased stroke-width:1.5px,stroke-dasharray:5 3;",
    "classDef factory stroke-width:3px;",
    "classDef external stroke-width:1px,stroke-dasharray:2 2;",
    "classDef unaddressed stroke-width:1px;",
)

_ADDR_CLASS = {
    "static-bench": "static",
    "static-manual": "static",
    "dhcp-lease": "leased",
    "factory": "factory",
}

# Comes in from outside the site: nothing here addresses or powers it.
_EXTERNAL_KINDS = frozenset(
    {"mains", "grid", "internet", "cellular-network", "wan"}
)


def _node_class(node) -> str:
    """Style by what the node IS first, then by how it got its address.

    Keyed off addr_source alone, an unaddressed-but-real device (the OTD500
    holds no address on this subnet) would be drawn as though it were off-site
    plumbing.
    """
    if node.kind in _EXTERNAL_KINDS:
        return "external"
    if node.addr_source in _ADDR_CLASS:
        return _ADDR_CLASS[node.addr_source]
    return "unaddressed"


def _node_id(name: str) -> str:
    return "n_" + _ID_SAFE.sub("_", name)


def _escape(text: str) -> str:
    """Mermaid label text: quotes delimit it, <br/> is the only break."""
    return text.replace('"', "&quot;").replace("\n", "<br/>")


def _node_label(site: Site, name: str) -> str:
    node = site.nodes.get(name)
    if node is None:
        return _escape(f"{name}<br/>(undeclared)")

    lines = [name]
    if node.model:
        lines.append(node.model)
    if node.addr:
        suffix = {
            "dhcp-lease": " (lease)",
            "factory": " (factory)",
            "static-manual": " (manual)",
        }.get(node.addr_source, "")
        lines.append(f"{node.addr}{suffix}")
    roles = [r for r in node.roles if r != "power-source"]
    if roles:
        lines.append(", ".join(roles))
    return _escape("<br/>".join(lines))


def _declare(site: Site, name: str, seen: set[str], out: list[str]) -> None:
    if name in seen:
        return
    seen.add(name)
    node = site.nodes.get(name)
    shape_open, shape_close = "[", "]"
    if node is not None:
        if node.kind in _EXTERNAL_KINDS:
            shape_open, shape_close = "([", "])"
        elif node.kind in ("switch", "poe-switch", "router"):
            shape_open, shape_close = "[[", "]]"
    out.append(
        f'    {_node_id(name)}{shape_open}"{_node_label(site, name)}"{shape_close}'
    )
    if node is not None:
        out.append(f"    class {_node_id(name)} {_node_class(node)};")


def render_network_mermaid(site: Site) -> str:
    """The data graph, rooted at the uplink where there is one."""
    out = [f"%% {site.name} — network / connection flow", "flowchart LR"]
    seen: set[str] = set()

    # Declare in walk order from the uplink so the renderer lays the chain out
    # left to right; anything unreachable is declared afterwards rather than
    # dropped, because an orphan is exactly what you want to see.
    ordered: list[str] = []
    if site.uplink and site.uplink in site.nodes:
        ordered.append(site.uplink)
        for _, edge in site.walk_net(site.uplink):
            ordered.append(edge.child)
    ordered.extend(sorted(site.nodes))

    for name in ordered:
        node = site.nodes.get(name)
        if node is not None and "power-source" in node.roles and not site.net_parents(name):
            continue  # a PSU with no data link belongs only on the power map
        _declare(site, name, seen, out)

    for edge in site.net:
        _declare(site, edge.parent, seen, out)
        _declare(site, edge.child, seen, out)
        label = edge.label()
        arrow = f'-->|"{_escape(label)}"|' if label else "-->"
        out.append(f"    {_node_id(edge.parent)} {arrow} {_node_id(edge.child)}")

    out.extend(f"    {line}" for line in _CLASS_DEFS)
    return "\n".join(out)


def render_power_mermaid(site: Site) -> str:
    """The power graph, rooted at whatever comes in from outside."""
    out = [f"%% {site.name} — power", "flowchart TD"]
    seen: set[str] = set()

    powered = {e.sink for e in site.power} | {e.source for e in site.power}
    for name in sorted(powered):
        _declare(site, name, seen, out)

    for edge in site.power:
        _declare(site, edge.source, seen, out)
        _declare(site, edge.sink, seen, out)
        label = edge.label()
        # PoE is dotted, hard-wired power is solid: on a site map the useful
        # question is "does this survive the switch losing power", and that
        # reads off the line style.
        arrow = "-.->" if edge.via == "poe" else "-->"
        if label:
            arrow = f'-.->|"{_escape(label)}"|' if edge.via == "poe" else f'-->|"{_escape(label)}"|'
        out.append(f"    {_node_id(edge.source)} {arrow} {_node_id(edge.sink)}")

    out.extend(f"    {line}" for line in _CLASS_DEFS)
    return "\n".join(out)


def render_markdown(site: Site) -> str:
    """Both diagrams plus the address table, as one document."""
    parts = [
        f"# {site.name}",
        "",
        f"Subnet `{site.subnet}`"
        + (f", uplink via `{site.uplink}`" if site.uplink else ""),
        "",
        "## Network",
        "",
        "```mermaid",
        render_network_mermaid(site),
        "```",
        "",
        "## Power",
        "",
        "```mermaid",
        render_power_mermaid(site),
        "```",
        "",
        "## Addresses",
        "",
        "| Address | Node | Kind | Source |",
        "| --- | --- | --- | --- |",
    ]

    addressed = [n for n in site.nodes.values() if n.addr]
    for node in sorted(addressed, key=lambda n: n.ip):
        parts.append(
            f"| `{node.addr}` | {node.name} | {node.kind} | {node.addr_source} |"
        )

    unaddressed = sorted(n.name for n in site.nodes.values() if not n.addr)
    if unaddressed:
        parts.extend(["", f"No address: {', '.join(unaddressed)}."])

    if site.reservations:
        parts.extend(["", "## Reserved ranges", "",
                      "| Range | Name | Allocated by |", "| --- | --- | --- |"])
        for res in sorted(site.reservations, key=lambda r: r.start):
            parts.append(f"| `{res}` | {res.name} | {res.owner or '—'} |")

    return "\n".join(parts) + "\n"


def render_tree(site: Site) -> str:
    """Terminal view: the data chain, indented, with the power feed alongside."""
    lines = [f"{site.name}  ({site.subnet})"]

    def power_note(name: str) -> str:
        feeds = site.power_sources(name)
        if not feeds:
            node = site.nodes.get(name)
            if node and ("power-source" in node.roles or node.kind in _EXTERNAL_KINDS):
                return ""
            return "   [no power declared]"
        bits = []
        for feed in feeds:
            label = f"{feed.source}:{feed.outlet}" if feed.outlet else feed.source
            if feed.draw_w:
                label += f" {feed.draw_w:g}W"
            bits.append(label)
        return f"   [power: {', '.join(bits)}]"

    def describe(name: str) -> str:
        node = site.nodes.get(name)
        if node is None:
            return f"{name} (undeclared)"
        bits = [name]
        if node.addr:
            bits.append(node.addr)
        if node.addr_source not in ("static-bench", "none"):
            bits.append(f"({node.addr_source})")
        return " ".join(bits)

    root = site.uplink if site.uplink in site.nodes else None
    if root:
        lines.append(f"  {describe(root)}{power_note(root)}")
        for depth, edge in site.walk_net(root):
            pad = "  " * (depth + 2)
            port = f"{edge.port} -> " if edge.port else ""
            lines.append(f"{pad}{port}{describe(edge.child)}{power_note(edge.child)}")

    shown = {root} if root else set()
    if root:
        shown |= {e.child for _, e in site.walk_net(root)}
    rest = sorted(set(site.nodes) - shown)
    if rest:
        lines.append("  not on the data path:")
        for name in rest:
            lines.append(f"    {describe(name)}{power_note(name)}")

    return "\n".join(lines)
