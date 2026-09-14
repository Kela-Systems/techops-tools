#!/usr/bin/env python3
"""Offline checks over a site model. No device is contacted.

Everything here runs from the YAML alone, which is the point: these are the
faults worth catching *before* someone drives to the site, and they can run
in CI on every change to a site file.

The checks are grouped the way the questions are asked:

  addressing   who has which address, and who hands them out
  data         what is plugged into what
  power        what feeds what, and what dies when one thing fails

Severities. `error` is a site that is wrong — two boxes on one address, a
device with no power. `warn` is a site that is fragile or unprovable — a
budget that cannot be checked, a single feed under everything critical. Only
errors fail the exit code, so a warning can sit in a site file indefinitely
without training anyone to ignore red.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Iterable

import evidence
from model import Node, PowerEdge, Site

ERROR = "error"
WARN = "warn"

# Kinds that are sources of power or connectivity from outside the site, so
# nothing at the site powers them and nothing needs to.
EXTERNAL_KINDS = frozenset({"mains", "grid", "internet", "cellular-network", "wan"})


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    message: str
    where: str | None = None

    def __str__(self) -> str:
        prefix = f"{self.where}: " if self.where else ""
        return f"[{self.severity}] {self.code}: {prefix}{self.message}"


def _needs_power(node: Node) -> bool:
    return node.kind not in EXTERNAL_KINDS and "power-source" not in node.roles


# -- addressing --------------------------------------------------------


def check_addresses(site: Site) -> list[Finding]:
    findings: list[Finding] = []

    by_addr: dict[str, list[str]] = {}
    for node in site.nodes.values():
        if node.addr:
            by_addr.setdefault(node.addr, []).append(node.name)

    for addr, names in sorted(by_addr.items()):
        if len(names) > 1:
            findings.append(
                Finding(
                    ERROR,
                    "addr-collision",
                    f"{len(names)} nodes share {addr}: {', '.join(sorted(names))}",
                )
            )

    for node in site.nodes.values():
        ip = node.ip
        if ip is None:
            continue
        if ip not in site.subnet:
            # Not automatically wrong — a factory unit legitimately sits on
            # another /24 until its last provisioning step moves it — so the
            # severity follows addr_source rather than the address alone.
            severity = WARN if node.addr_source == "factory" else ERROR
            findings.append(
                Finding(
                    severity,
                    "addr-off-subnet",
                    f"{node.addr} is outside the site subnet {site.subnet}"
                    + (" (factory address, not yet provisioned)"
                       if node.addr_source == "factory" else ""),
                    node.name,
                )
            )

    return findings


def check_pools(site: Site) -> list[Finding]:
    """Reserved ranges that overlap, and fixed addresses that fall inside one.

    This is the check that earns the tool. The camera tool cycles its octet
    30 -> 50 *inclusive* while radar_0 sits on .50, so a site with enough
    cameras eventually hands a camera the radar's address — and the failure
    turns up weeks later as a radar that "stopped reporting", nowhere near
    whoever configured the camera.
    """
    findings: list[Finding] = []

    for i, a in enumerate(site.reservations):
        for b in site.reservations[i + 1:]:
            if not a.overlaps(b):
                continue
            lo = max(a.start, b.start)
            hi = min(a.end, b.end)
            overlap = str(lo) if lo == hi else f"{lo}-{hi}"
            dyn = [r.name for r in (a, b) if r.dynamic]
            detail = (
                f" — '{dyn[0]}' is allocated dynamically, so it can hand out "
                f"{overlap} to anything"
                if len(dyn) == 1
                else ""
            )
            findings.append(
                Finding(
                    ERROR,
                    "pool-overlap",
                    f"reserved range '{a.name}' ({a}) overlaps '{b.name}' ({b}) "
                    f"at {overlap}{detail}",
                )
            )

    # Only a dynamic pool can take an address that something else holds: a
    # static range overlapping a node's address is just the range documenting
    # that node, which is what a static range is for.
    for node in site.nodes.values():
        ip = node.ip
        if ip is None:
            continue
        for res in site.reservations:
            if not res.dynamic or ip not in res:
                continue
            if node.addr_pool == res.name:
                continue  # this node is the one allocating from that pool
            findings.append(
                Finding(
                    ERROR,
                    "addr-in-foreign-pool",
                    f"fixed address {node.addr} sits inside dynamic pool "
                    f"'{res.name}' ({res})"
                    + (f", which {res.owner} allocates from" if res.owner else "")
                    + " — it can be handed to another device",
                    node.name,
                )
            )

    for node in site.nodes.values():
        if node.addr_pool and not any(r.name == node.addr_pool for r in site.reservations):
            findings.append(
                Finding(
                    ERROR,
                    "unknown-pool",
                    f"allocates from reserved range '{node.addr_pool}', "
                    f"which is not declared",
                    node.name,
                )
            )

    return findings


def check_dhcp(site: Site) -> list[Finding]:
    """Who gives IP to who — stated, not assumed."""
    findings: list[Finding] = []

    servers = site.nodes_with_role("dhcp-server")
    leased = [n for n in site.nodes.values() if n.addr_source == "dhcp-lease"]

    if leased and not servers:
        findings.append(
            Finding(
                ERROR,
                "dhcp-no-server",
                f"{len(leased)} node(s) take a DHCP lease "
                f"({', '.join(sorted(n.name for n in leased))}) but no node "
                f"has the 'dhcp-server' role",
            )
        )

    if len(servers) > 1:
        findings.append(
            Finding(
                ERROR,
                "dhcp-multi-server",
                f"{len(servers)} nodes claim the 'dhcp-server' role on one "
                f"subnet: {', '.join(sorted(n.name for n in servers))}",
            )
        )

    for node in leased:
        if node.addr:
            # A lease is the router's to give and can move; recording it as
            # the device's address makes the map quietly wrong the next time
            # the lease changes.
            findings.append(
                Finding(
                    WARN,
                    "dhcp-pinned-addr",
                    f"has addr {node.addr} but addr_source is 'dhcp-lease' — "
                    f"record it as a static reservation on the router, or drop "
                    f"the addr",
                    node.name,
                )
            )

    return findings


# -- data graph --------------------------------------------------------


def _unknown_refs(site: Site) -> list[Finding]:
    findings: list[Finding] = []
    for edge in site.net:
        for role, name in (("from", edge.parent), ("to", edge.child)):
            if name not in site.nodes:
                findings.append(
                    Finding(
                        ERROR,
                        "unknown-node",
                        f"net edge {role} '{name}' is not a declared node",
                        f"{edge.parent} -> {edge.child}",
                    )
                )
    for pedge in site.power:
        for role, name in (("from", pedge.source), ("to", pedge.sink)):
            if name not in site.nodes:
                findings.append(
                    Finding(
                        ERROR,
                        "unknown-node",
                        f"power edge {role} '{name}' is not a declared node",
                        f"{pedge.source} -> {pedge.sink}",
                    )
                )
    return findings


def _find_cycle(edges: Iterable[tuple[str, str]]) -> list[str] | None:
    """Return one cycle as a node path, or None. Iterative, so a deep site
    cannot blow the stack."""
    graph: dict[str, list[str]] = {}
    for parent, child in edges:
        graph.setdefault(parent, []).append(child)

    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[str, int] = {}

    for root in list(graph):
        if colour.get(root, WHITE) != WHITE:
            continue
        path: list[str] = []
        stack: list[tuple[str, bool]] = [(root, False)]
        while stack:
            node, leaving = stack.pop()
            if leaving:
                colour[node] = BLACK
                path.pop()
                continue
            if colour.get(node, WHITE) == GREY:
                return path[path.index(node):] + [node]
            if colour.get(node, WHITE) == BLACK:
                continue
            colour[node] = GREY
            path.append(node)
            stack.append((node, True))
            for child in graph.get(node, ()):
                stack.append((child, False))
    return None


def check_net_graph(site: Site) -> list[Finding]:
    findings: list[Finding] = []

    seen_ports: dict[tuple[str, str], str] = {}
    for edge in site.net:
        if edge.port is None:
            continue
        key = (edge.parent, edge.port)
        if key in seen_ports:
            findings.append(
                Finding(
                    ERROR,
                    "port-reuse",
                    f"{edge.parent} port {edge.port} carries both "
                    f"{seen_ports[key]} and {edge.child}",
                )
            )
        else:
            seen_ports[key] = edge.child

    seen_peer_ports: dict[tuple[str, str], str] = {}
    for edge in site.net:
        if edge.peer_port is None:
            continue
        key = (edge.child, edge.peer_port)
        if key in seen_peer_ports:
            findings.append(
                Finding(
                    ERROR,
                    "port-reuse",
                    f"{edge.child} port {edge.peer_port} is fed by both "
                    f"{seen_peer_ports[key]} and {edge.parent}",
                )
            )
        else:
            seen_peer_ports[key] = edge.parent

    for name in sorted(site.nodes):
        parents = site.net_parents(name)
        if len(parents) > 1:
            findings.append(
                Finding(
                    ERROR,
                    "net-multi-parent",
                    f"is plugged into {len(parents)} parents: "
                    + ", ".join(sorted(f"{p.parent}:{p.port or '?'}" for p in parents)),
                    name,
                )
            )

    cycle = _find_cycle((e.parent, e.child) for e in site.net)
    if cycle:
        findings.append(
            Finding(ERROR, "net-cycle", "data path loops: " + " -> ".join(cycle))
        )

    root = site.uplink
    if root and root in site.nodes and not cycle:
        reachable = {root} | {edge.child for _, edge in site.walk_net(root)}
        for name, node in sorted(site.nodes.items()):
            if name in reachable:
                continue
            if node.kind in EXTERNAL_KINDS or "power-source" in node.roles:
                continue
            findings.append(
                Finding(
                    WARN,
                    "net-orphan",
                    f"no data path back to the uplink '{root}'",
                    name,
                )
            )
    elif root and root not in site.nodes:
        findings.append(
            Finding(ERROR, "unknown-node", f"uplink '{root}' is not a declared node")
        )

    return findings


# -- power graph -------------------------------------------------------


def check_power_graph(site: Site) -> list[Finding]:
    findings: list[Finding] = []

    cycle = _find_cycle((e.source, e.sink) for e in site.power)
    if cycle:
        findings.append(
            Finding(ERROR, "power-cycle", "power feed loops: " + " -> ".join(cycle))
        )

    for name, node in sorted(site.nodes.items()):
        if not _needs_power(node):
            continue
        feeds = site.power_sources(name)
        if not feeds:
            findings.append(
                Finding(ERROR, "unpowered", "nothing declared as powering it", name)
            )
        elif len(feeds) > 1:
            # Two feeds is redundancy when the device takes redundant input
            # (the IGS-4215's PWR1/PWR2) and a modelling mistake otherwise.
            # Distinguishing them needs per-model knowledge the model does
            # not carry, so this stays a warning.
            findings.append(
                Finding(
                    WARN,
                    "power-multi-source",
                    f"has {len(feeds)} power feeds "
                    + ", ".join(sorted(f"{f.source}:{f.outlet or '?'}" for f in feeds))
                    + " — redundant input, or a duplicate edge?",
                    name,
                )
            )

    seen_outlets: dict[tuple[str, str], str] = {}
    for edge in site.power:
        if edge.outlet is None:
            continue
        key = (edge.source, edge.outlet)
        if key in seen_outlets:
            findings.append(
                Finding(
                    ERROR,
                    "outlet-reuse",
                    f"{edge.source} outlet {edge.outlet} feeds both "
                    f"{seen_outlets[key]} and {edge.sink}",
                )
            )
        else:
            seen_outlets[key] = edge.sink

    seen_inlets: dict[tuple[str, str], str] = {}
    for edge in site.power:
        if edge.inlet is None:
            continue
        key = (edge.sink, edge.inlet)
        if key in seen_inlets:
            findings.append(
                Finding(
                    ERROR,
                    "inlet-reuse",
                    f"{edge.sink} input {edge.inlet} is fed by both "
                    f"{seen_inlets[key]} and {edge.source}",
                )
            )
        else:
            seen_inlets[key] = edge.source

    return findings


def check_poe_budget(site: Site) -> list[Finding]:
    """PoE draw against the switch's budget.

    `poe_managed: false` is the live state of the PLANET IGS-4215 as of
    2026-09-14 — the bench stopped setting limits, budget and priorities and
    lets the switch negotiate per port. So a declared budget is documentation
    of the hardware's capability, not something anything enforces, and an
    overcommit is reported as a warning to be proven against the switch's own
    PoE status rather than an error the model can settle on its own.
    """
    findings: list[Finding] = []

    for name, node in sorted(site.nodes.items()):
        poe_edges = [e for e in site.power_sinks(name) if e.via == "poe"]
        if not poe_edges:
            continue

        if node.poe_budget_w is None:
            findings.append(
                Finding(
                    WARN,
                    "poe-no-budget",
                    f"feeds {len(poe_edges)} PoE device(s) but declares no "
                    f"poe_budget_w, so the draw cannot be checked",
                    name,
                )
            )
            continue

        unknown = [e for e in poe_edges if e.draw_w is None]
        total = sum(e.draw_w or 0.0 for e in poe_edges)

        if total > node.poe_budget_w:
            findings.append(
                Finding(
                    ERROR if node.poe_managed else WARN,
                    "poe-overcommit",
                    f"PoE draw {total:g} W over {len(poe_edges)} port(s) exceeds "
                    f"the {node.poe_budget_w:g} W budget"
                    + ("" if node.poe_managed
                       else " (poe_managed is false — the switch negotiates its "
                            "own limits, so confirm against its PoE status)"),
                    name,
                )
            )

        if unknown:
            findings.append(
                Finding(
                    WARN,
                    "poe-unknown-draw",
                    f"{len(unknown)} PoE port(s) declare no draw_w "
                    + ", ".join(sorted(e.sink for e in unknown))
                    + f" — the {total:g} W total is a floor, not the real load",
                    name,
                )
            )

    return findings


def check_power_resilience(site: Site) -> list[Finding]:
    """What stops working when one feed fails."""
    findings: list[Finding] = []

    def critical_below(start: str) -> set[str]:
        found: set[str] = set()
        seen = {start}
        stack = [start]
        while stack:
            current = stack.pop()
            for edge in site.power_sinks(current):
                if edge.sink in seen:
                    continue
                seen.add(edge.sink)
                node = site.nodes.get(edge.sink)
                if node and node.critical:
                    found.add(edge.sink)
                stack.append(edge.sink)
        return found

    for name, node in sorted(site.nodes.items()):
        # The incoming mains is a single feed under everything critical at
        # every site, and no site file can fix it. Reporting it would add one
        # guaranteed warning to every map, which is how people learn to skim
        # past the warnings that do matter.
        if node.kind in EXTERNAL_KINDS:
            continue
        dependents = critical_below(name)
        if len(dependents) < 2:
            continue
        own_feeds = site.power_sources(name)
        if len(own_feeds) >= 2:
            continue
        findings.append(
            Finding(
                WARN,
                "power-spof",
                f"single feed under {len(dependents)} critical device(s) "
                + ", ".join(sorted(dependents))
                + " — one failure here takes all of them down",
                name,
            )
        )

    return findings


# -- evidence ----------------------------------------------------------


def check_evidence(site: Site) -> list[Finding]:
    """Every fact on the map must be backed by evidence that covers it.

    This is the check that would have caught reading an ARP table as proof of
    device models. `arp` proves an address answered; it does not prove what
    answered. The finding names the claim, the evidence, and what that
    evidence actually covers, so the fix is obvious.

    All of these are warnings by default and errors under
    `--require-evidence`, which is how a site gets gated once it is meant to
    be fully verified.
    """
    findings: list[Finding] = []

    for name, node in sorted(site.nodes.items()):

        def unbacked(claim, value, label, fix=""):
            if not value or node.proves(claim):
                return
            src = node.evidence.source_for(claim)
            findings.append(
                Finding(
                    WARN,
                    f"{claim}-unverified",
                    f"{label} rests on '{src.name}' ({src.summary}), which "
                    f"does not establish a {claim}."
                    + (" A vendor is not a model." if src.name == "arp"
                       and claim == evidence.CLAIM_MODEL else "")
                    + (f" {fix}" if fix else ""),
                    name,
                )
            )

        unbacked(evidence.CLAIM_VENDOR, node.vendor,
                 f"the vendor {node.vendor}")
        unbacked(evidence.CLAIM_MODEL, node.model,
                 f"the model {node.model}",
                 "Read it off the device, or look the MAC up in bench-central "
                 "(sitemap.py discover --central ...).")
        unbacked(evidence.CLAIM_FIRMWARE, node.firmware,
                 f"firmware {node.firmware}",
                 "Only the device or a bench record of it ever knew.")
        unbacked(evidence.CLAIM_ADDR, node.addr, f"address {node.addr}")

        # A narrowing is progress, not an answer. Say so, so a candidate list
        # is never mistaken on the page for a settled model.
        if node.candidates and not node.model:
            findings.append(
                Finding(
                    WARN,
                    "model-narrowed-not-known",
                    f"could be {' or '.join(node.candidates)}"
                    + (f" ({node.candidate_basis})" if node.candidate_basis
                       else "")
                    + ". That is a shortlist, not the model — only the device "
                      "or a bench record settles it.",
                    name,
                )
            )

    for edge in site.net:
        if edge.proves(evidence.CLAIM_LINK):
            continue
        src = edge.evidence.source_for(evidence.CLAIM_LINK)
        findings.append(
            Finding(
                WARN,
                "link-unverified",
                f"the cable is not proven: '{src.name}' ({src.summary}). "
                f"A switch MAC-address table or LLDP neighbour proves it.",
                f"{edge.parent} -> {edge.child}",
            )
        )

    for edge in site.power:
        where = f"{edge.source} -> {edge.sink}"
        src = edge.evidence.source_for(evidence.CLAIM_POWER)

        if edge.survey_only and not edge.proves(evidence.CLAIM_POWER):
            # Say plainly that this one will never be closed by a script, so
            # nobody waits for a verifier that cannot exist.
            findings.append(
                Finding(
                    WARN,
                    "power-needs-survey",
                    f"a {edge.via} feed cannot be proven by any protocol - a "
                    f"switch can report that an input has voltage, never what "
                    f"is at the other end of the wire. Only 'survey' evidence "
                    f"closes this.",
                    where,
                )
            )
        elif not edge.proves(evidence.CLAIM_POWER):
            findings.append(
                Finding(
                    WARN,
                    "power-unverified",
                    f"the feed rests on '{src.name}' ({src.summary}). The "
                    f"switch's PoE port status proves it.",
                    where,
                )
            )

        if edge.draw_w is not None and not edge.proves(evidence.CLAIM_DRAW):
            draw_src = edge.evidence.source_for(evidence.CLAIM_DRAW)
            findings.append(
                Finding(
                    WARN,
                    "draw-unverified",
                    f"{edge.draw_w:g} W rests on '{draw_src.name}' "
                    f"({draw_src.summary}), "
                    f"not a measurement. Read the switch's PoE status.",
                    where,
                )
            )

    return findings


CHECKS = (
    check_addresses,
    check_pools,
    check_dhcp,
    _unknown_refs,
    check_net_graph,
    check_power_graph,
    check_poe_budget,
    check_power_resilience,
    check_evidence,
)


# Findings that say "nobody has proven this", as opposed to "this is wrong".
# `--require-evidence` promotes exactly these.
EVIDENCE_CODES = frozenset({
    "vendor-unverified", "model-unverified", "firmware-unverified",
    "model-narrowed-not-known",
    "addr-unverified", "link-unverified",
    "power-unverified", "draw-unverified", "power-needs-survey",
})


# Errors that mean "this part has not been surveyed yet", as opposed to "this
# is self-contradictory". A provisional site downgrades these; a collision or
# a cycle stays an error at every stage.
INCOMPLETENESS_CODES = frozenset({"unpowered"})


def lint(site: Site, require_evidence: bool = False) -> list[Finding]:
    """Run every check. Errors first, then warnings, each group by code."""
    findings: list[Finding] = []
    for check in CHECKS:
        findings.extend(check(site))

    if site.status == "provisional":
        findings = [
            Finding(WARN, f.code, f.message + " (site is provisional)", f.where)
            if f.severity == ERROR and f.code in INCOMPLETENESS_CODES else f
            for f in findings
        ]

    if require_evidence:
        findings = [
            Finding(ERROR, f.code, f.message, f.where)
            if f.code in EVIDENCE_CODES else f
            for f in findings
        ]

    order = {ERROR: 0, WARN: 1}
    return sorted(
        findings, key=lambda f: (order.get(f.severity, 9), f.code, f.where or "")
    )


def coverage(site: Site) -> dict:
    """How much of the map is actually proven, by claim.

    The answer to "is this 100% tested". Counted per claim rather than as one
    number, because the claims are not equally reachable: models and links can
    get to 100%, DC power paths never can without a survey.
    """
    def tally(items, claim, applicable=None):
        """Score against everything that COULD carry the claim, not just the
        items that happen to claim it.

        Two false-assurance traps, both hit while building this:

        1. `pct` is None when nothing is applicable, never 100 — a site with
           no power graph must not report its power as fully verified.
        2. The denominator is `applicable`, not the number of items making
           the claim. Three proven models out of three claimed, on a site of
           eight devices, is 37.5% knowledge of the models — not 100%. The
           five devices with no model recorded are the gap, and scoring them
           out of the question hides exactly what was asked about.
        """
        claimed = len(items)
        total = claimed if applicable is None else applicable
        proven = sum(1 for i in items if i.proves(claim))
        return {
            "proven": proven,
            "claimed": claimed,
            "total": total,
            "pct": round(100.0 * proven / total, 1) if total else None,
        }

    # Real boxes only: the mains feed and the carrier network have no model,
    # firmware or address, so counting them as gaps would be noise.
    real = [n for n in site.nodes.values() if n.kind not in EXTERNAL_KINDS]
    n_real = len(real)

    vended = [n for n in real if n.vendor]
    modelled = [n for n in real if n.model]
    flashed = [n for n in real if n.firmware]
    addressed = [n for n in site.nodes.values() if n.addr]
    drawn = [e for e in site.power if e.draw_w is not None]
    survey_only = [e for e in site.power if e.survey_only]

    by_source: dict[str, int] = {}
    for item in list(site.nodes.values()) + list(site.net) + list(site.power):
        for source in item.evidence.sources_used():
            by_source[source] = by_source.get(source, 0) + 1

    return {
        "vendor": tally(vended, evidence.CLAIM_VENDOR, n_real),
        "model": tally(modelled, evidence.CLAIM_MODEL, n_real),
        "firmware": tally(flashed, evidence.CLAIM_FIRMWARE, n_real),
        "addr": tally(addressed, evidence.CLAIM_ADDR, n_real),
        "link": tally(site.net, evidence.CLAIM_LINK),
        "power": tally(site.power, evidence.CLAIM_POWER),
        "draw": tally(drawn, evidence.CLAIM_DRAW),
        "by_source": dict(sorted(by_source.items())),
        "survey_only_feeds": len(survey_only),
    }
