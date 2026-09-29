#!/usr/bin/env python3
"""Flatten a site model into the payload the UI reads.

The UI must never hold a second copy of the facts. It gets exactly this
payload, generated from the same YAML the linter reads, so a site file and
the page that draws it cannot drift apart.

Emitted as JavaScript (`window.SITE_DATA = ...`) rather than JSON because an
artifact page loads a same-origin script tag reliably, while a fetch() of a
data file has to get past the page's content-security policy. One less thing
to debug in a browser someone else is holding.
"""
from __future__ import annotations

import json
from dataclasses import asdict

import devices
import topology
from lint import coverage, lint
from model import Site

PAYLOAD_VERSION = 1

# A site file whose name starts with "_" is a template: a worked example of the
# schema with invented devices. `sites/*.yaml` globs it in with the real ones,
# and the UI reads the FIRST site in the payload — so the template sorted ahead
# of kela-fob-03 and the dashboard rendered a fictional site for several
# published versions, under prose written about the real one. Nobody caught it
# because no test and no lint pass ever opened the page.
#
# Templates are dropped here rather than in the UI, so every consumer of the
# payload gets the same answer. `--include-templates` exists for anyone who
# genuinely wants to preview one.
TEMPLATE_PREFIX = "_"


def is_template(site: Site) -> bool:
    return site.name.startswith(TEMPLATE_PREFIX)


def drop_templates(sites: list[Site]) -> tuple[list[Site], list[str]]:
    """Real sites, plus the names of the templates that were left out."""
    kept = [s for s in sites if not is_template(s)]
    dropped = [s.name for s in sites if is_template(s)]
    return kept, dropped


def _node_payload(site: Site, name: str) -> dict:
    node = site.nodes[name]
    device = devices.lookup(node.model)
    return {
        "name": node.name,
        "kind": node.kind,
        "vendor": node.vendor,
        "model": node.model,
        "firmware": node.firmware,
        "candidates": node.candidates,
        "candidate_basis": node.candidate_basis,
        "mac": node.mac,
        "evidence": dict(node.evidence.by_claim),
        "proven": {
            claim: node.proves(claim)
            for claim in ("vendor", "model", "firmware", "addr", "present")
        },
        "verified_at": node.verified_at,
        "addr": node.addr,
        "addr_source": node.addr_source,
        # Every leg, with which side of the world it faces. Falls back to the
        # one address we know: an ARP entry can only ever see the interface
        # facing the host that swept, and an empty list would read as "this
        # device has no addresses" rather than "nobody read its interfaces".
        "interfaces": [
            {"name": i.name, "addr": i.addr, "scope": i.scope,
             "prefix": i.prefix, "note": i.note}
            for i in node.interfaces_or_addr(site.subnet)
        ],
        "interfaces_read": bool(node.interfaces),
        "roles": node.roles,
        "critical": node.critical,
        "notes": node.notes,
        "addr_pool": node.addr_pool,
        "poe_budget_w": node.poe_budget_w,
        "poe_managed": node.poe_managed,
        "device_model": device.model if device else None,
        # Pre-computed so the UI does not re-derive the graph. The tree view,
        # the inspector and the faceplate all need these.
        "net_parents": [
            {"node": e.parent, "port": e.port, "peer_port": e.peer_port,
             "media": e.media, "notes": e.notes}
            for e in site.net_parents(name)
        ],
        "net_children": [
            {"node": e.child, "port": e.port, "peer_port": e.peer_port,
             "media": e.media, "notes": e.notes}
            for e in site.net_children(name)
        ],
        "power_in": [
            {"node": e.source, "outlet": e.outlet, "inlet": e.inlet,
             "via": e.via, "draw_w": e.draw_w, "notes": e.notes}
            for e in site.power_sources(name)
        ],
        "power_out": [
            {"node": e.sink, "outlet": e.outlet, "inlet": e.inlet,
             "via": e.via, "draw_w": e.draw_w, "notes": e.notes}
            for e in site.power_sinks(name)
        ],
    }


def _tree_payload(site: Site) -> list[dict]:
    """The data path in walk order, so the UI renders it without a graph walk."""
    if not site.uplink or site.uplink not in site.nodes:
        return []
    rows = [{"depth": 0, "node": site.uplink, "port": None, "peer_port": None}]
    for depth, edge in site.walk_net(site.uplink):
        rows.append({
            "depth": depth + 1,
            "node": edge.child,
            "port": edge.port,
            "peer_port": edge.peer_port,
        })
    return rows


def site_payload(site: Site, fanout: list | None = None) -> dict:
    """The payload the UI reads.

    `fanout` is the router's forwarding database, and only a live survey has
    one - a site read from a YAML file passes None and gets exactly what it
    always got.
    """
    findings = lint(site)
    on_tree = {row["node"] for row in _tree_payload(site)}

    total_poe = {}
    for name, node in site.nodes.items():
        poe = [e for e in site.power_sinks(name) if e.via == "poe"]
        if poe:
            total_poe[name] = {
                "draw_w": sum(e.draw_w or 0.0 for e in poe),
                "budget_w": node.poe_budget_w,
                "managed": node.poe_managed,
                "ports": len(poe),
                "unknown_draw": sum(1 for e in poe if e.draw_w is None),
            }

    return {
        "name": site.name,
        "status": site.status,
        "subnet": str(site.subnet),
        "uplink": site.uplink,
        "source_path": str(site.source_path) if site.source_path else None,
        "nodes": {name: _node_payload(site, name) for name in site.nodes},
        "net": [asdict(e) for e in site.net],
        "power": [asdict(e) for e in site.power],
        "reservations": [
            {
                "name": r.name,
                "start": str(r.start),
                "end": str(r.end),
                "owner": r.owner,
                "dynamic": r.dynamic,
                "label": str(r),
            }
            for r in sorted(site.reservations, key=lambda r: r.start)
        ],
        "tree": _tree_payload(site),
        # The connection diagram, with declared edges and the FOB pattern's
        # expectations kept separate. Computed here so the page draws what
        # the CLI would draw.
        "topology": topology.propose(site, fanout).as_payload(),
        "off_tree": sorted(set(site.nodes) - on_tree),
        "poe": total_poe,
        "findings": [
            {"severity": f.severity, "code": f.code, "message": f.message,
             "where": f.where}
            for f in findings
        ],
        "coverage": coverage(site),
        "counts": {
            "nodes": len(site.nodes),
            "net": len(site.net),
            "power": len(site.power),
            "errors": sum(1 for f in findings if f.severity == "error"),
            "warnings": sum(1 for f in findings if f.severity == "warn"),
        },
    }


def payload(sites: list[Site]) -> dict:
    return {
        "version": PAYLOAD_VERSION,
        "sites": [site_payload(s) for s in sites],
        "devices": devices.catalogue_dict(),
    }


def as_json(sites: list[Site], indent: int | None = 2) -> str:
    return json.dumps(payload(sites), indent=indent, ensure_ascii=False) + "\n"


def as_js(sites: list[Site]) -> str:
    body = json.dumps(payload(sites), indent=2, ensure_ascii=False)
    return (
        "// Generated by sitemap.py export - do not edit.\n"
        "// Regenerate with: sitemap.py export sites/*.yaml -o site-data.js\n"
        f"window.SITE_DATA = {body};\n"
    )
