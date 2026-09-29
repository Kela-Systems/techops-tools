#!/usr/bin/env python3
"""The site model: one YAML file per site, loaded into two graphs.

A Magos site is cookie-cutter — four AR-300 radars, two APUs, a camera, a
speaker, a PoE switch, a managed switch and a cellular router — so the map is
*declared* first and proven against hardware second. That order matters:
discovery can only ever report what is, never what was meant to be, and the
whole point of a site map is catching the difference.

Two edge sets, deliberately separate:

  net    parent.port -> child      who is plugged into what, and who hands
                                   out the address (`addr_source`)
  power  source.outlet -> sink     what feeds what, over PoE or DC or mains

They are not the same graph and must not be collapsed into one. The camera
hangs off the PoE switch's port 9 for *data* but takes its power from a
separate PSU — gi9 and gi10 are the IGS-4215's two non-PoE copper sockets.
A single-graph model cannot express that, and it is exactly the case an
installer needs to see.

What this module does NOT do is talk to anything. Loading and validating a
site file is offline, hermetic and safe to run in CI; reaching a real switch
belongs to the verifier.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

import yaml

import evidence

SCHEMA_VERSION = 1

# A site's lifecycle stage. The difference that matters is between a model
# that is INCOMPLETE and one that is WRONG.
#
#   provisional  still being surveyed. Gaps are expected, so a missing power
#                graph is a warning; two devices on one address is still an
#                error, because that is a contradiction, not a gap.
#   active       claimed to be a full model. Gaps are errors.
#
# Without this, the only way to get a half-surveyed site past CI is to invent
# the missing half, which is the worst possible outcome.
STATUSES = ("provisional", "active")
DEFAULT_STATUS = "active"

# How a node comes by its address. The distinction is the "who gives IP to
# who" question, and it is not cosmetic: a static-bench address is a fact
# about the bench run that provisioned it, a dhcp-lease address is a fact
# about the router and can move, and a factory address means nobody has
# provisioned the unit yet.
ADDR_SOURCES = ("static-bench", "static-manual", "dhcp-lease", "factory", "none")

# How power gets from a source to a sink. `poe` is the only one a switch can
# report on its own; everything else has to be declared because no protocol
# carries it.
POWER_VIA = ("poe", "dc", "mains", "usb")


class SiteModelError(Exception):
    """A site file that cannot be loaded at all (bad YAML, bad shape).

    Distinct from a lint finding: a finding is a problem *with* a site that
    loaded fine, and the linter reports many of them at once. This is
    "there is no model here to lint".
    """


# An interface's side of the world. `internal` means an address inside the
# site's own subnet; `external` is everything else - a tailnet address, a WAN
# address, an uplink to somebody else's network. The distinction is the one an
# operator actually needs: which of these can I reach from my desk, and which
# only exists on site.
SCOPE_INTERNAL = "internal"
SCOPE_EXTERNAL = "external"
SCOPES = (SCOPE_INTERNAL, SCOPE_EXTERNAL)


def _sort_key(addr: Optional[str], subnet=None):
    """On-subnet addresses first, then numerically."""
    if not addr:
        return (2, 0)
    try:
        ip = ipaddress.IPv4Address(addr)
    except ValueError:
        return (2, 0)
    on_subnet = subnet is not None and ip in subnet
    return (0 if on_subnet else 1, int(ip))


@dataclass
class Interface:
    """One address on one NIC.

    A device with two legs is the normal case for the two that matter most:
    the router carries br-lan, wan and tailscale0, and a site server carries
    its LAN NIC and tailscale0. A model with one `addr` per node cannot say
    that, and the one address it does hold is whichever the sweep happened to
    see - which for the router is whichever side you came in on.
    """

    name: Optional[str] = None
    addr: Optional[str] = None
    scope: str = SCOPE_INTERNAL
    prefix: Optional[int] = None
    note: Optional[str] = None

    @property
    def ip(self) -> Optional[ipaddress.IPv4Address]:
        return ipaddress.IPv4Address(self.addr) if self.addr else None


@dataclass
class Node:
    """One box at the site."""

    name: str
    kind: str
    # Vendor is separate from model because the evidence for each is
    # different: an OUI lookup settles the vendor, and only the device (or a
    # bench record of it) settles the model.
    vendor: Optional[str] = None
    model: Optional[str] = None
    firmware: Optional[str] = None
    # Models this device COULD be, and why. Deliberately NOT `model`: a
    # narrowing is not a determination, and the whole failure mode being
    # designed against is an inference presented as a reading. The linter
    # never counts these toward model coverage.
    candidates: list[str] = field(default_factory=list)
    candidate_basis: Optional[str] = None
    mac: Optional[str] = None
    addr: Optional[str] = None
    addr_source: str = "none"
    # Every address this device holds, where that was read. Empty means
    # nobody looked, NOT that the device has one NIC - an ARP sweep only ever
    # sees the leg facing the host that swept, so absence here is a limit of
    # the reading and `interfaces_or_addr` says so.
    interfaces: list[Interface] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    critical: bool = False
    # Switch-only: the per-port power budget, and whether the switch is
    # actually being told to honour it. See the `poe_managed` note in lint.py.
    poe_budget_w: Optional[float] = None
    poe_managed: bool = False
    # Nodes that allocate addresses out of a pool rather than taking one
    # fixed address (the camera tool's 30->50 cycle, for instance).
    addr_pool: Optional[str] = None
    notes: Optional[str] = None
    # Which source backs which claim. Defaults to `assumed` throughout, so an
    # unmarked node reads as unverified rather than silently trusted.
    evidence: evidence.EvidenceSet = field(
        default_factory=evidence.EvidenceSet.of)
    verified_at: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    def proves(self, claim: str) -> bool:
        return self.evidence.proves(claim)

    def source_for(self, claim: str) -> str:
        return self.evidence.name_for(claim)

    @property
    def ip(self) -> Optional[ipaddress.IPv4Address]:
        return ipaddress.IPv4Address(self.addr) if self.addr else None

    def interfaces_or_addr(self, subnet=None) -> list[Interface]:
        """The declared interfaces, or the one address we happen to know.

        Never invents a second leg. A device read over SSH has real
        interfaces; a device seen only in an ARP table has exactly one
        address, and this returns exactly that.
        """
        if self.interfaces:
            return sorted(
                self.interfaces,
                key=lambda i: (i.scope != SCOPE_INTERNAL,
                               _sort_key(i.addr, subnet)),
            )
        if not self.addr:
            return []
        scope = SCOPE_INTERNAL
        if subnet is not None:
            try:
                scope = (SCOPE_INTERNAL
                         if ipaddress.IPv4Address(self.addr) in subnet
                         else SCOPE_EXTERNAL)
            except ValueError:
                pass
        return [Interface(name=None, addr=self.addr, scope=scope)]


@dataclass
class NetEdge:
    """A data link: `parent` port `port` carries `child`."""

    parent: str
    child: str
    port: Optional[str] = None
    # The socket at the *child* end. Both ends matter: the PLANET's port map
    # label calls gi10 the management drop, which is the port on the switch
    # being fed, not on the switch feeding it — a one-ended model has to pick
    # one of those and is then wrong on half the links.
    peer_port: Optional[str] = None
    media: Optional[str] = None
    notes: Optional[str] = None
    evidence: evidence.EvidenceSet = field(
        default_factory=evidence.EvidenceSet.of)
    verified_at: Optional[str] = None

    def proves(self, claim: str) -> bool:
        return self.evidence.proves(claim)

    def source_for(self, claim: str) -> str:
        return self.evidence.name_for(claim)

    def label(self) -> str:
        if self.port and self.peer_port:
            return f"{self.port} -> {self.peer_port}"
        return self.port or self.peer_port or self.media or ""


@dataclass
class PowerEdge:
    """A power feed: `source` outlet `outlet` powers `sink`."""

    source: str
    sink: str
    outlet: Optional[str] = None
    # The socket at the *sink* end, the mirror of NetEdge.peer_port. The
    # IGS-4215's two DC inputs are named PWR1/PWR2 on the switch being fed,
    # not on the PSU feeding it, and a faceplate has to know which of its own
    # inputs is live.
    inlet: Optional[str] = None
    via: str = "poe"
    # Watts. On a PoE port this is what the device actually pulls, not the
    # port's configured ceiling — the two are different numbers and conflating
    # them is how a budget silently overcommits.
    draw_w: Optional[float] = None
    notes: Optional[str] = None
    evidence: evidence.EvidenceSet = field(
        default_factory=evidence.EvidenceSet.of)
    verified_at: Optional[str] = None

    def proves(self, claim: str) -> bool:
        return self.evidence.proves(claim)

    def source_for(self, claim: str) -> str:
        return self.evidence.name_for(claim)

    @property
    def survey_only(self) -> bool:
        """True when no protocol could ever prove this feed."""
        return self.via in evidence.SURVEY_ONLY_VIA

    def label(self) -> str:
        socket = (
            f"{self.outlet} -> {self.inlet}"
            if self.outlet and self.inlet
            else self.outlet or self.inlet
        )
        bits = [b for b in (socket, self.via) if b]
        if self.draw_w:
            bits.append(f"{self.draw_w:g} W")
        return " ".join(bits)


@dataclass
class Reservation:
    """A named slice of the subnet, and who owns it.

    Reservations exist so the linter can catch the class of bug where two
    tools hand out addresses from ranges that overlap at one end.

    `dynamic` is the load-bearing distinction. A static reservation is a
    documented plan — radars sit on .50-.53 because the bench pins them there
    by RF channel, and nothing will ever hand .51 to something else. A dynamic
    one is a pool a tool *cycles through*, so any address in it can land on any
    device in turn. Only a dynamic pool can steal an address from a fixed one,
    which is why the linter treats the two differently.
    """

    name: str
    start: ipaddress.IPv4Address
    end: ipaddress.IPv4Address
    owner: Optional[str] = None
    dynamic: bool = False

    def __contains__(self, ip: ipaddress.IPv4Address) -> bool:
        return self.start <= ip <= self.end

    def overlaps(self, other: "Reservation") -> bool:
        return self.start <= other.end and other.start <= self.end

    def __str__(self) -> str:
        if self.start == self.end:
            return str(self.start)
        return f"{self.start}-{self.end}"


@dataclass
class Site:
    """A loaded, structurally-valid site model."""

    name: str
    subnet: ipaddress.IPv4Network
    nodes: dict[str, Node]
    net: list[NetEdge]
    power: list[PowerEdge]
    reservations: list[Reservation] = field(default_factory=list)
    uplink: Optional[str] = None
    status: str = DEFAULT_STATUS
    source_path: Optional[Path] = None

    # -- graph helpers -------------------------------------------------

    def net_children(self, name: str) -> list[NetEdge]:
        return [e for e in self.net if e.parent == name]

    def net_parents(self, name: str) -> list[NetEdge]:
        return [e for e in self.net if e.child == name]

    def power_sinks(self, name: str) -> list[PowerEdge]:
        return [e for e in self.power if e.source == name]

    def power_sources(self, name: str) -> list[PowerEdge]:
        return [e for e in self.power if e.sink == name]

    def nodes_with_role(self, role: str) -> list[Node]:
        return [n for n in self.nodes.values() if role in n.roles]

    def walk_net(self, root: str) -> Iterator[tuple[int, NetEdge]]:
        """Depth-first over the data graph, yielding (depth, edge).

        Cycle-safe: a node already visited is not descended into again, so a
        malformed site still renders instead of hanging. The linter is what
        reports the cycle.
        """
        seen: set[str] = {root}
        stack: list[tuple[int, NetEdge]] = [
            (0, e) for e in reversed(self.net_children(root))
        ]
        while stack:
            depth, edge = stack.pop()
            yield depth, edge
            if edge.child in seen:
                continue
            seen.add(edge.child)
            stack.extend(
                (depth + 1, e) for e in reversed(self.net_children(edge.child))
            )


def _parse_evidence(
    spec: dict[str, Any], what: str
) -> tuple[evidence.EvidenceSet, Optional[str]]:
    try:
        ev = evidence.parse(spec.get("evidence"), what)
    except evidence.EvidenceError as exc:
        raise SiteModelError(str(exc)) from exc
    at = spec.get("verified_at")
    return ev, (str(at) if at is not None else None)


def _require_mapping(value: Any, what: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SiteModelError(f"{what} must be a mapping, got {type(value).__name__}")
    return value


def _require_list(value: Any, what: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SiteModelError(f"{what} must be a list, got {type(value).__name__}")
    return value


def _parse_reservation(name: str, spec: Any, subnet: ipaddress.IPv4Network) -> Reservation:
    """Parse `30-50`, `70`, `192.168.88.30-192.168.88.50` or a mapping.

    Bare numbers are last octets, which is how the bench tools and the
    operator guide talk about these addresses ("the octet range is 30-50"),
    so the site files read the same way.
    """
    owner = None
    dynamic = False
    if isinstance(spec, dict):
        owner = spec.get("owner")
        dynamic = bool(spec.get("dynamic", False))
        spec = spec.get("range")
    if spec is None:
        raise SiteModelError(f"reservation '{name}' has no range")

    text = str(spec).strip()
    lo_text, _, hi_text = text.partition("-")
    hi_text = hi_text or lo_text

    def one(part: str) -> ipaddress.IPv4Address:
        part = part.strip()
        try:
            if "." in part:
                return ipaddress.IPv4Address(part)
            return subnet.network_address + int(part)
        except (ipaddress.AddressValueError, ValueError) as exc:
            raise SiteModelError(
                f"reservation '{name}': cannot read '{part}' as an address "
                f"or a last octet"
            ) from exc

    lo, hi = one(lo_text), one(hi_text)
    if hi < lo:
        raise SiteModelError(f"reservation '{name}': range {lo}-{hi} runs backwards")
    return Reservation(name=name, start=lo, end=hi, owner=owner, dynamic=dynamic)


def _parse_node(name: str, spec: Any) -> Node:
    spec = _require_mapping(spec, f"node '{name}'")
    kind = spec.get("kind")
    if not kind:
        raise SiteModelError(f"node '{name}' has no kind")

    addr = spec.get("addr")
    if addr is not None:
        addr = str(addr)
        try:
            ipaddress.IPv4Address(addr)
        except ipaddress.AddressValueError as exc:
            raise SiteModelError(f"node '{name}': '{addr}' is not an IPv4 address") from exc

    interfaces = []
    raw_ifaces = spec.get("interfaces") or []
    if not isinstance(raw_ifaces, list):
        raise SiteModelError(f"node '{name}': 'interfaces' must be a list")
    for index, item in enumerate(raw_ifaces, 1):
        if not isinstance(item, dict):
            raise SiteModelError(
                f"node '{name}': interface #{index} must be a mapping")
        iface_addr = item.get("addr")
        if iface_addr is not None:
            iface_addr = str(iface_addr)
            try:
                ipaddress.IPv4Address(iface_addr)
            except ipaddress.AddressValueError as exc:
                raise SiteModelError(
                    f"node '{name}': interface #{index} address "
                    f"'{iface_addr}' is not an IPv4 address") from exc
        scope = str(item.get("scope", SCOPE_INTERNAL))
        if scope not in SCOPES:
            raise SiteModelError(
                f"node '{name}': interface #{index} scope '{scope}' is not "
                f"one of {', '.join(SCOPES)}")
        prefix = item.get("prefix")
        interfaces.append(Interface(
            name=(str(item["name"]) if item.get("name") else None),
            addr=iface_addr, scope=scope,
            prefix=(int(prefix) if prefix is not None else None),
            note=(str(item["note"]) if item.get("note") else None),
        ))

    addr_source = spec.get("addr_source", "static-bench" if addr else "none")
    if addr_source not in ADDR_SOURCES:
        raise SiteModelError(
            f"node '{name}': addr_source '{addr_source}' is not one of "
            f"{', '.join(ADDR_SOURCES)}"
        )

    roles = spec.get("roles") or []
    if isinstance(roles, str):
        roles = [roles]

    ev, at = _parse_evidence(spec, f"node '{name}'")

    mac = spec.get("mac")
    if mac is not None:
        digits = re.sub(r"[^0-9A-Fa-f]", "", str(mac))
        if len(digits) != 12:
            raise SiteModelError(
                f"node '{name}': '{mac}' is not a 48-bit MAC address"
            )
        mac = ":".join(digits[i:i + 2] for i in range(0, 12, 2)).lower()

    return Node(
        name=name,
        kind=str(kind),
        vendor=spec.get("vendor"),
        firmware=spec.get("firmware"),
        candidates=[str(c) for c in (spec.get("candidates") or [])],
        candidate_basis=spec.get("candidate_basis"),
        mac=mac,
        model=spec.get("model"),
        addr=addr,
        addr_source=addr_source,
        interfaces=interfaces,
        roles=[str(r) for r in roles],
        critical=bool(spec.get("critical", False)),
        poe_budget_w=spec.get("poe_budget_w"),
        poe_managed=bool(spec.get("poe_managed", False)),
        addr_pool=spec.get("addr_pool"),
        notes=spec.get("notes"),
        evidence=ev,
        verified_at=at,
        raw=spec,
    )


def _parse_net_edge(spec: Any, index: int) -> NetEdge:
    spec = _require_mapping(spec, f"net edge #{index + 1}")
    for key in ("from", "to"):
        if not spec.get(key):
            raise SiteModelError(f"net edge #{index + 1} has no '{key}'")
    port = spec.get("port")
    peer_port = spec.get("peer_port")
    ev, at = _parse_evidence(spec, f"net edge #{index + 1}")
    return NetEdge(
        parent=str(spec["from"]),
        child=str(spec["to"]),
        port=str(port) if port is not None else None,
        peer_port=str(peer_port) if peer_port is not None else None,
        media=spec.get("media"),
        notes=spec.get("notes"),
        evidence=ev,
        verified_at=at,
    )


def _parse_power_edge(spec: Any, index: int) -> PowerEdge:
    spec = _require_mapping(spec, f"power edge #{index + 1}")
    for key in ("from", "to"):
        if not spec.get(key):
            raise SiteModelError(f"power edge #{index + 1} has no '{key}'")
    via = str(spec.get("via", "poe"))
    if via not in POWER_VIA:
        raise SiteModelError(
            f"power edge #{index + 1}: via '{via}' is not one of {', '.join(POWER_VIA)}"
        )
    outlet = spec.get("outlet")
    inlet = spec.get("inlet")
    draw = spec.get("draw_w")
    if draw is not None and not isinstance(draw, (int, float)):
        raise SiteModelError(f"power edge #{index + 1}: draw_w must be a number")
    ev, at = _parse_evidence(spec, f"power edge #{index + 1}")
    return PowerEdge(
        source=str(spec["from"]),
        sink=str(spec["to"]),
        outlet=str(outlet) if outlet is not None else None,
        inlet=str(inlet) if inlet is not None else None,
        via=via,
        draw_w=float(draw) if draw is not None else None,
        notes=spec.get("notes"),
        evidence=ev,
        verified_at=at,
    )


def load_site(path: str | Path) -> Site:
    """Read and structurally validate one site YAML file.

    Raises SiteModelError for anything that stops a model existing. Problems
    *within* a valid model — collisions, orphans, overcommitted budgets — are
    the linter's job, not this function's, so that one bad address does not
    hide the other nine findings.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SiteModelError(f"cannot read {path}: {exc}") from exc

    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SiteModelError(f"{path}: invalid YAML: {exc}") from exc

    doc = _require_mapping(doc, f"{path}")
    if not doc:
        raise SiteModelError(f"{path} is empty")

    version = doc.get("version")
    if version != SCHEMA_VERSION:
        raise SiteModelError(
            f"{path}: version {version!r} is not the supported schema "
            f"version {SCHEMA_VERSION}"
        )

    name = doc.get("site")
    if not name:
        raise SiteModelError(f"{path}: no 'site' name")

    status = str(doc.get("status", DEFAULT_STATUS))
    if status not in STATUSES:
        raise SiteModelError(
            f"{path}: status '{status}' is not one of {', '.join(STATUSES)}"
        )

    subnet_text = doc.get("subnet")
    if not subnet_text:
        raise SiteModelError(f"{path}: no 'subnet'")
    try:
        subnet = ipaddress.IPv4Network(str(subnet_text), strict=False)
    except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, ValueError) as exc:
        raise SiteModelError(f"{path}: '{subnet_text}' is not an IPv4 subnet") from exc

    nodes: dict[str, Node] = {}
    for node_name, spec in _require_mapping(doc.get("nodes"), "nodes").items():
        nodes[str(node_name)] = _parse_node(str(node_name), spec)
    if not nodes:
        raise SiteModelError(f"{path}: no nodes")

    net = [_parse_net_edge(s, i) for i, s in enumerate(_require_list(doc.get("net"), "net"))]
    power = [
        _parse_power_edge(s, i)
        for i, s in enumerate(_require_list(doc.get("power"), "power"))
    ]

    reservations = [
        _parse_reservation(str(res_name), spec, subnet)
        for res_name, spec in _require_mapping(
            doc.get("reservations"), "reservations"
        ).items()
    ]

    return Site(
        name=str(name),
        subnet=subnet,
        nodes=nodes,
        net=net,
        power=power,
        reservations=reservations,
        uplink=doc.get("uplink"),
        status=status,
        source_path=path,
    )
