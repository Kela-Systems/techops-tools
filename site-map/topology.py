#!/usr/bin/env python3
"""Lay a site out as a diagram: what is proven, and what the pattern expects.

A FOB is built to a pattern, and the pattern is worth drawing even before
anyone has read a switch port:

    internet --- router (Teltonika RUT, SIM, mounted high for signal)
                   |
                 switch(es) (Mikrotik / Teltonika TSW-PSW / Planet), in the box
                   |
                 everything else - cameras, radars, lidars, server,
                 operator station

So this module produces two kinds of edge and never blurs them:

    declared   an edge in the site model, carrying whatever evidence the
               model recorded for it - a switch MAC-address table, LLDP, a
               person who traced the cable
    pattern    what the FOB architecture expects, given what was seen

A pattern edge is a *question*, drawn dashed and labelled as an expectation.
It is never written into a site file's `net:` block: `discover.to_yaml` goes
out of its way not to invent cabling, and the reason holds here too - someone
will wire to a diagram. The proposal is offered so it can be confirmed or
contradicted cheaply, which is the point of drawing it at all.

Nothing here is decided by address. `.1` being the router is a bench
convention, not a fact about a site, and a site where it is untrue is exactly
the site where a diagram built on it misleads. Devices are identified by what
their MAC says they are, by the model a bench record or a probe established,
and by evidence-backed roles.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

# Vendor and model tells, strongest first. All of these come from a MAC's OUI
# or from a model that something actually read off the device, never from an
# address.
ROUTER_VENDORS = ("teltonika",)
ROUTER_MODEL_HINTS = ("rut",)

SWITCH_VENDORS = ("routerboard", "mikrotik", "planet")
SWITCH_MODEL_HINTS = ("tsw", "psw", "igs-", "crs", "css")

# Roles the model records with evidence. A device that something proved is
# the gateway is the internet leg whatever its vendor. Matched as substrings
# because the models in the wild say "default-gateway", and an exact `in`
# test silently missed the one site that declares it.
ROUTER_ROLES = ("gateway", "uplink")
SWITCH_KINDS = ("switch", "poe-switch")

# The layer a device is drawn on. Layer 0 is what comes in from outside.
LAYER_INTERNET, LAYER_ROUTER, LAYER_SWITCH, LAYER_LEAF = 0, 1, 2, 3

INTERNET = "__internet__"
UNSEEN_SWITCH = "__switch_not_seen__"
# Several switches behind one cable. Which of them a given device hangs off
# is unread, but "behind this port" is not - so the devices get a parent that
# says exactly that much, instead of no cable at all. A box with nothing
# reaching it reads as a device with no network, which is a stronger and
# wronger claim than "we did not read which switch".
FABRIC = "__switch_fabric__"


@dataclass
class Edge:
    parent: str
    child: str
    source: str           # "declared", "read", or "pattern"
    basis: str            # why this edge is here, in one clause
    port: str | None = None
    peer_port: str | None = None
    evidence: str | None = None   # for declared edges, the model's own source


@dataclass
class Box:
    """One thing on the diagram. Not always a device: layer 0 is the SIM."""

    name: str
    label: str
    layer: int
    kind: str = "unknown"
    real: bool = True     # False for the internet cloud and an unseen switch
    # True for a box that is not a device in the model but was still PROVEN
    # to exist - an unmanaged switch, which holds no address to be modelled
    # by and yet is as certain as anything on the diagram. Without this the
    # page draws it in the same dashed "expected" style as a guess.
    proven: bool = False
    # A box that is not a thing but a REGION: "one of these, unread". Drawn
    # as an outline around its members rather than as another sibling box,
    # because a placeholder with a kind label and an address line next to two
    # real switches reads as a third switch that everything hangs off - which
    # is a claim nobody made.
    group: bool = False
    members: list = field(default_factory=list)
    note: str | None = None


@dataclass
class Topology:
    boxes: list[Box] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    router: str | None = None
    switches: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_payload(self) -> dict:
        return {
            "boxes": [asdict(b) for b in self.boxes],
            "edges": [asdict(e) for e in self.edges],
            "router": self.router,
            "switches": list(self.switches),
            "notes": list(self.notes),
            "counts": {
                "declared": sum(1 for e in self.edges if e.source == "declared"),
                "read": sum(1 for e in self.edges if e.source == "read"),
                "pattern": sum(1 for e in self.edges if e.source == "pattern"),
            },
        }


def _has(text: str | None, needles) -> bool:
    low = (text or "").casefold()
    return any(n in low for n in needles)


def find_router(site) -> tuple[str | None, str]:
    """The internet leg. Returns (name, why)."""
    # An evidence-backed role beats every guess: something proved it.
    for name, node in site.nodes.items():
        hit = next((r for r in node.roles if _has(r, ROUTER_ROLES)), None)
        if hit:
            return name, f"{name} is recorded as {hit}, with evidence"

    if site.uplink and site.uplink in site.nodes:
        return site.uplink, f"{site.uplink} is declared as the site's uplink"

    # A model somebody read off the device.
    for name, node in site.nodes.items():
        if _has(node.model, ROUTER_MODEL_HINTS):
            return name, f"{node.model} is a Teltonika RUT, the FOB's SIM router"

    # Vendor only. A Teltonika could be the router or one of its switches, so
    # this is the weakest tell and says so.
    teltonika = [n for n, node in site.nodes.items()
                 if _has(node.vendor, ROUTER_VENDORS)
                 and not _is_switch(site.nodes[n])[0]]
    if len(teltonika) == 1:
        return teltonika[0], (
            f"{teltonika[0]} is the only Teltonika here that is not a switch; "
            f"its model is unread, so this is the vendor talking"
        )
    if teltonika:
        return None, (
            f"{len(teltonika)} Teltonika devices and no model read on any of "
            f"them, so which one is the RUT is undetermined"
        )
    return None, "nothing here identifies as a router"


def _is_switch(node) -> tuple[bool, str]:
    if node.kind in SWITCH_KINDS:
        return True, f"the model records it as a {node.kind}"
    if _has(node.model, SWITCH_MODEL_HINTS):
        return True, f"{node.model} is a switch, read off the device"
    for candidate in node.candidates or []:
        if _has(candidate, SWITCH_MODEL_HINTS):
            return True, (
                f"shortlisted as {candidate}, which is a switch - a shortlist "
                f"is a lead and not a reading"
            )
    if _has(node.vendor, SWITCH_VENDORS):
        return True, f"{node.vendor} makes the switches a FOB is built with"
    if _has(node.vendor, ROUTER_VENDORS):
        # A FOB has one Teltonika router and often a Teltonika switch, and
        # the OUI cannot tell them apart - 20:97:27 covers the RUT, the
        # TSW202 and the OTD500 alike. The router is identified separately
        # and excluded before this runs, so a second Teltonika on the LAN is
        # most likely the switch. Weakest tell here, and it says so: without
        # it a site whose switch holds no named lease drew as "switch not
        # seen" while the switch sat in the device list.
        return True, (
            f"a second {node.vendor} device on the LAN, and the router is "
            f"accounted for separately - at a FOB that is usually the "
            f"switch. The OUI cannot confirm it: 20:97:27 covers the RUT, "
            f"the TSW202 and the OTD500 alike, so this is a lead. Reading "
            f"the model settles it."
        )
    return False, ""


def find_switches(site, router: str | None) -> list[tuple[str, str]]:
    out = []
    for name, node in site.nodes.items():
        if name == router:
            continue
        is_switch, why = _is_switch(node)
        if is_switch:
            out.append((name, why))
    return sorted(out)


def propose(site, fanout: list | None = None) -> Topology:
    """Draw what is known, and what the FOB pattern expects on top of it.

    `fanout` is the router's forwarding database read as ports (see
    `router.fan_out`), and it is optional because a site loaded from a YAML
    file has no such reading - only a live survey does. It adds exactly one
    thing the model cannot hold: a switch that is PROVEN to exist and has no
    address, so there is no node to declare it as. Several MACs learned on
    one port with no switch among them is an unmanaged switch on that port,
    and drawing it as an expectation would understate what was read.
    """
    top = Topology()
    router, router_why = find_router(site)
    switches = find_switches(site, router)
    top.router = router
    top.switches = [name for name, _ in switches]

    # -- declared edges first. These are the facts, and they win. ---------
    declared_parent: dict[str, str] = {}
    for edge in site.net:
        source = edge.evidence.by_claim.get("*") if edge.evidence else None
        top.edges.append(Edge(
            parent=edge.parent, child=edge.child, source="declared",
            basis=edge.notes or "declared in the site model",
            port=edge.port, peer_port=edge.peer_port, evidence=source,
        ))
        declared_parent[edge.child] = edge.parent

    # -- the boxes --------------------------------------------------------
    def box(name, layer, note=None):
        node = site.nodes[name]
        label = name.replace("_", " ")
        top.boxes.append(Box(name=name, label=label, layer=layer,
                             kind=node.kind, note=note))

    if router:
        top.boxes.append(Box(
            name=INTERNET, label="internet", layer=LAYER_INTERNET,
            kind="internet", real=False,
            note="over the router's SIM. Nothing here has read the WAN side.",
        ))
        box(router, LAYER_ROUTER, router_why)
        top.edges.append(Edge(
            parent=INTERNET, child=router, source="pattern",
            basis="the FOB's internet leg is the router's SIM, mounted high "
                  "for signal. Not read from the device.",
        ))
    for name, why in switches:
        box(name, LAYER_SWITCH, why)

    placed = {router, *top.switches} - {None}
    leaves = [n for n in sorted(site.nodes) if n not in placed]
    for name in leaves:
        box(name, LAYER_LEAF)

    # -- what the forwarding database proved ------------------------------
    #
    # Only a live survey has this. It settles two things the model cannot:
    # a switch with no address (proven present, permanently anonymous), and
    # which router port each device was learned on - which is what lets a
    # two-switch site attach its leaves to the right one instead of to
    # neither.
    placed_by_fdb: set = set()
    if fanout and router:
        for fan in fanout:
            behind = [n for n in fan.known
                      if n != router and n not in top.switches]
            if fan.hidden_switch:
                name = f"{UNSEEN_SWITCH}_{fan.port}"
                top.boxes.append(Box(
                    name=name, label=f"switch on {fan.port} (no address)",
                    layer=LAYER_SWITCH, kind="switch", real=False,
                    proven=True,
                    note=(
                        f"PROVEN, not expected: the router learned "
                        f"{len(fan.macs)} MACs on {fan.port} and none of them "
                        f"reads as a switch. Several devices behind one cable "
                        f"is a switch whether or not it holds an address. It "
                        f"never sources a frame, so it is in no table "
                        f"anywhere - its position and its clients are known, "
                        f"its model never will be."
                    ),
                ))
                top.edges.append(Edge(
                    parent=router, child=name, source="read",
                    evidence="switch-table",
                    basis=f"{len(fan.macs)} MACs learned on {fan.port}",
                ))
                for leaf in behind:
                    if leaf not in declared_parent:
                        top.edges.append(Edge(
                            parent=name, child=leaf, source="pattern",
                            basis=f"learned on the router's {fan.port}, so it "
                                  f"is behind whatever fans out there",
                        ))
                        placed_by_fdb.add(leaf)
            elif len(fan.switches) > 1:
                name = f"{FABRIC}_{fan.port}"
                top.boxes.append(Box(
                    name=name,
                    label=f"{fan.port}: one of these {len(fan.switches)}",
                    layer=LAYER_SWITCH, kind="switch", real=False,
                    group=True, members=list(fan.switches),
                    note=(
                        f"{len(fan.macs)} MACs were learned on {fan.port}, "
                        f"{len(fan.switches)} of them switches "
                        f"({', '.join(fan.switches)}). Everything here is "
                        f"behind that one cable - that much is read. Which "
                        f"switch each device hangs off, and which switch is "
                        f"upstream of which, needs their own MAC-address "
                        f"tables: one read each."
                    ),
                ))
                for switch in fan.switches:
                    if switch not in declared_parent:
                        top.edges.append(Edge(
                            parent=router, child=switch, source="pattern",
                            basis=f"its MAC was learned on {fan.port}, so it "
                                  f"is behind that port. Whether the router's "
                                  f"cable lands on it, or on the other switch "
                                  f"with this one behind that, is unread",
                        ))
                for leaf in behind:
                    if leaf not in declared_parent:
                        top.edges.append(Edge(
                            parent=name, child=leaf, source="pattern",
                            basis=f"learned on the router's {fan.port}, so it "
                                  f"is behind the switching there; which "
                                  f"switch is unread",
                        ))
                        placed_by_fdb.add(leaf)
            elif len(fan.switches) == 1:
                parent = fan.switches[0]
                for leaf in behind:
                    if leaf not in declared_parent:
                        top.edges.append(Edge(
                            parent=parent, child=leaf, source="pattern",
                            basis=f"learned on the router's {fan.port}, whose "
                                  f"only switch is {parent}. Behind that port "
                                  f"is read; which of {parent}'s ports is not",
                        ))
                        placed_by_fdb.add(leaf)
            elif not fan.fans_out and len(behind) == 1:
                placed_by_fdb.add(behind[0])   # a read edge already says it

    # -- pattern edges, only where nothing is declared --------------------
    if router and switches:
        for name, _ in switches:
            if name not in declared_parent:
                top.edges.append(Edge(
                    parent=router, child=name, source="pattern",
                    basis="a FOB runs one cable from the router to the switch "
                          "in the box",
                ))

    if len(switches) == 1:
        parent = switches[0][0]
        for name in leaves:
            if name not in declared_parent and name not in placed_by_fdb:
                top.edges.append(Edge(
                    parent=parent, child=name, source="pattern",
                    basis=f"everything on the LAN hangs off {parent}; which "
                          f"port needs its MAC-address table",
                ))
    elif len(switches) > 1:
        # Two switches is a real FOB variant - a site outgrows one switch's
        # ports. Which device is on which is then genuinely unknown, and
        # picking one would be inventing the very fact worth having.
        unplaced = [n for n in leaves
                    if n not in declared_parent and n not in placed_by_fdb]
        # Whatever is left still has to hang off something. Without this the
        # two- and three-switch sites drew every camera, radar and server
        # with no cable reaching it at all, which reads as "this device has
        # no network" - a stronger claim than the truth, which is only that
        # nobody has read which of the switches it is on.
        if unplaced and router:
            if not any(b.name == FABRIC for b in top.boxes):
                top.boxes.append(Box(
                    name=FABRIC,
                    label=f"one of these {len(switches)}",
                    layer=LAYER_SWITCH, kind="switch", real=False,
                    group=True, members=list(top.switches),
                    note=(
                        f"{len(switches)} switches here "
                        f"({', '.join(top.switches)}) and nothing read that "
                        f"says which device is on which. Everything below "
                        f"hangs off one of them; a MAC-address table from "
                        f"each settles all of it."
                    ),
                ))
            for name in unplaced:
                top.edges.append(Edge(
                    parent=FABRIC, child=name, source="pattern",
                    basis=f"everything on the LAN hangs off one of this "
                          f"site's {len(switches)} switches; which one is "
                          f"unread",
                ))
        if placed_by_fdb:
            top.notes.append(
                f"{len(switches)} switches here ({', '.join(top.switches)}). "
                f"The router's forwarding database placed "
                f"{len(placed_by_fdb)} device(s) behind the port their MAC "
                f"was learned on, which is what assigns them to a switch at "
                f"all" + (
                    f"; {len(unplaced)} still unassigned."
                    if unplaced else "."
                )
            )
        else:
            top.notes.append(
                f"{len(switches)} switches here ({', '.join(top.switches)}), "
                f"which a FOB runs when one has too few ports. Which device "
                f"is on which is NOT proposed: only a switch's MAC-address "
                f"table settles it, and guessing would invent the one fact "
                f"worth reading."
            )
    elif router and leaves and not placed_by_fdb:
        # The switch a FOB certainly has did not answer. ARP only lists what
        # the host has recently exchanged traffic with, and a switch that has
        # talked to nothing never appears - so this is an expected absence.
        top.boxes.append(Box(
            name=UNSEEN_SWITCH, label="switch (not seen)", layer=LAYER_SWITCH,
            kind="switch", real=False,
            note="A FOB has one, and nothing in this model is one. A switch "
                 "that has exchanged no traffic never appears in an ARP "
                 "table, so this is an expected absence rather than a "
                 "missing device.",
        ))
        top.edges.append(Edge(
            parent=router, child=UNSEEN_SWITCH, source="pattern",
            basis="a FOB runs one cable from the router to the switch in the box",
        ))
        for name in leaves:
            if name not in declared_parent:
                top.edges.append(Edge(
                    parent=UNSEEN_SWITCH, child=name, source="pattern",
                    basis="everything on the LAN hangs off the site's switch",
                ))

    if not router:
        # Only suggest reading a Teltonika where there is one to read. On a
        # site whose gateway is some other vendor the advice is just wrong,
        # and wrong advice in a findings list is worse than none.
        unread = [n for n, node in site.nodes.items()
                  if _has(node.vendor, ROUTER_VENDORS) and not node.model]
        fix = (
            f" Reading the model off {', '.join(sorted(unread))} would settle "
            f"it - `sitemap.py probe` does that over the device's own API."
            if unread else
            " Nothing here is a Teltonika, so the internet leg is either a "
            "vendor this does not recognise or it is not on this subnet. "
            "The router's own identity is what settles it."
        )
        top.notes.append(
            f"No router identified, so there is no root to draw from: "
            f"{router_why}.{fix}"
        )

    servers = [n for n, node in site.nodes.items() if node.kind == "server"]
    if not servers:
        top.notes.append(
            "No server identified, and every site has at least one. A MAC "
            "cannot find it - a server and an operator station are both Dell "
            "on the same OUI - so this comes from the DHCP lease hostname, "
            "which means either the lease table was not read or the server "
            "holds a static address and never appears in it."
        )

    if not site.net:
        top.notes.append(
            "No cabling is declared for this site at all, so every solid line "
            "below is absent and every dashed one is the pattern's "
            "expectation. A switch MAC-address table turns the dashed lines "
            "into facts in one read."
        )
    return top
