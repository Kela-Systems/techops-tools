#!/usr/bin/env python3
"""Turn an ARP table into a site model, with each fact's evidence recorded.

The whole point is that the three questions have three different answers and
three different sources, and the output says which is which:

    is it there, at that address?   the ARP entry          -> `arp`
    who made it?                    OUI longest-prefix     -> `arp`
    which model, which firmware?    bench-central by MAC   -> `bench-record`
    what is it plugged into?        NOBODY ASKED YET       -> `assumed`
    what powers it?                 NOBODY ASKED YET       -> `assumed`

So a discovered site comes out with vendor and address proven, model and
firmware proven only for devices the bench has actually configured, and
**no network or power edges at all** — because an ARP sweep establishes
neither. The emitted file is `status: provisional` for that reason.

Nothing here writes to a device or to bench-central. It reads an ARP table
someone else collected and issues GETs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import bench_central
import devices
import oui

# A device's role guessed from its address, using the bench's own plan. This
# only ever sets the node's NAME and `kind`, never its model — a name is a
# label for humans, and getting it wrong is visible, whereas a wrong model is
# not.
ADDR_ROLES = (
    (1, 1, "router", "router"),
    (2, 2, "switch_mgmt", "switch"),
    (3, 3, "switch_poe", "poe-switch"),
    (10, 20, "server", "server"),
    (29, 29, "operator_station", "operator-station"),
    (30, 50, "camera", "camera"),
    (60, 61, "apu", "apu"),
    (70, 70, "speaker", "speaker"),
)

# Vendor substring -> the kind to use when the address plan says nothing.
VENDOR_KINDS = (
    ("magos", "magos-device"),
    ("teltonika", "network-device"),
    ("planet", "poe-switch"),
    ("provision", "speaker"),
    ("dell", "operator-station"),
)


@dataclass
class Discovered:
    """One device, and where each of its facts came from."""

    entry: oui.Entry
    facts: Optional[bench_central.DeviceFacts] = None
    name: str = ""
    kind: str = "unknown"
    evidence: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def vendor(self) -> Optional[str]:
        v = self.entry.vendor
        if not v or v.is_registrar:
            return None
        return v.name

    @property
    def model(self) -> Optional[str]:
        return self.facts.model if self.facts and self.facts.model else None

    @property
    def firmware(self) -> Optional[str]:
        return self.facts.firmware if self.facts and self.facts.firmware else None


def _last_octet(ip: Optional[str]) -> Optional[int]:
    if not ip:
        return None
    try:
        return int(ip.split(".")[-1])
    except (ValueError, IndexError):
        return None


def _role_for(entry: oui.Entry) -> tuple[Optional[str], Optional[str]]:
    octet = _last_octet(entry.ip)
    if octet is None:
        return None, None
    for lo, hi, name, kind in ADDR_ROLES:
        if lo <= octet <= hi:
            return name, kind
    return None, None


def _kind_for_vendor(vendor: Optional[str]) -> str:
    if not vendor:
        return "unknown"
    low = vendor.casefold()
    for needle, kind in VENDOR_KINDS:
        if needle in low:
            return kind
    return "unknown"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_") or "device"


def name_devices(found: list[Discovered]) -> None:
    """Give each device a stable, readable node name.

    Named from the address plan where it says something, from the vendor
    otherwise, and suffixed when several share a role. Names are for humans;
    the MAC is the identity.
    """
    for item in found:
        role, kind = _role_for(item.entry)
        if kind:
            item.kind = kind
        elif item.kind == "unknown":
            item.kind = _kind_for_vendor(item.vendor)
        item.name = role or _slug(item.vendor or item.kind)

    counts: dict[str, int] = {}
    for item in found:
        counts[item.name] = counts.get(item.name, 0) + 1

    seen: dict[str, int] = {}
    for item in found:
        if counts[item.name] == 1:
            continue
        seen[item.name] = seen.get(item.name, 0) + 1
        item.name = f"{item.name}_{seen[item.name]}"


def discover(
    arp_text: str,
    oui_table: dict,
    client: Optional[bench_central.Client] = None,
) -> tuple[list[Discovered], list[str]]:
    """Resolve an ARP table. Returns (devices, warnings)."""
    warnings: list[str] = []
    entries = oui.resolve(oui.parse_arp(arp_text), oui_table)
    entries.sort(key=lambda e: (
        tuple(int(p) for p in e.ip.split(".")) if e.ip else ()
    ))

    found = [Discovered(entry=e) for e in entries]
    name_devices(found)

    for item in found:
        # ARP proves presence, the MAC, the address, and - through the OUI -
        # the vendor. It proves nothing else, so everything else is left to
        # default to `assumed`.
        item.evidence["*"] = "arp"

        if item.entry.vendor and item.entry.vendor.is_registrar:
            item.notes.append(
                f"OUI resolved only to the registrar "
                f"({item.entry.vendor.name}); the vendor is an MA-S/MA-M "
                f"assignment this database does not carry."
            )
        elif not item.entry.vendor:
            item.notes.append("MAC prefix not in the vendor database.")

        if item.vendor:
            catalogue = [
                d for d in devices.CATALOGUE
                if d.vendor.casefold().split()[0] in item.vendor.casefold()
            ]
            if len(catalogue) > 1:
                item.notes.append(
                    f"{item.vendor} makes several devices here ("
                    + ", ".join(sorted(d.model for d in catalogue))
                    + "); a MAC cannot tell them apart."
                )

    if client is None:
        warnings.append(
            "No bench-central URL given, so no model or firmware was looked "
            "up. Every device is vendor-only. Pass --central to close that."
        )
        return found, warnings

    misses: list[str] = []
    for item in found:
        try:
            item.facts = client.facts_for_mac(item.entry.mac)
        except bench_central.CentralError as exc:
            warnings.append(f"{item.entry.mac}: {exc}")
            continue

        if item.facts.model:
            # The model came from a run record, not from the sweep. Recording
            # that per claim is the only honest way to hold both.
            item.evidence["model"] = "bench-record"
            if item.facts.firmware:
                item.evidence["firmware"] = "bench-record"
            note = f"bench-central: {item.facts.runs_seen} run(s)"
            if item.facts.tool:
                note += f", last by {item.facts.tool}"
            if item.facts.last_run:
                note += f" at {item.facts.last_run}"
            if item.facts.serial:
                note += f"; serial {item.facts.serial}"
            item.notes.append(note + ".")
            if not item.facts.firmware:
                item.notes.append(
                    "No firmware recorded in any run for this MAC."
                )
        else:
            misses.append(f"{item.name} ({item.entry.ip})")
            item.notes.append(
                "Not found in bench-central, so the model is still unknown. "
                "That is not evidence of anything: central shipping is opt-in "
                "per station, and only devices a bench tool configured are "
                "ever recorded."
            )

    if misses:
        warnings.append(
            f"{len(misses)} device(s) had no bench-central record: "
            + ", ".join(misses)
        )
    return found, warnings


def _yaml_scalar(value: str) -> str:
    """Quote anything YAML could misread. Cheap, and avoids a dependency."""
    text = str(value)
    if text and not re.search(r"[:#\-{}\[\],&*?|>!%@`\"']|^\s|\s$", text):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def to_yaml(
    site_name: str,
    subnet: str,
    found: list[Discovered],
    warnings: list[str],
    source_note: str = "",
) -> str:
    """Render a provisional site file.

    No `net:` or `power:` block is emitted. An ARP sweep establishes neither,
    and writing a plausible-looking one would be the single most damaging
    thing this function could do.
    """
    out: list[str] = []
    add = out.append

    add(f"# {site_name}, discovered from an ARP table"
        + (f" ({source_note})" if source_note else "") + ".")
    add("#")
    add("# Generated by `sitemap.py discover`. Every fact carries the source")
    add("# that established it:")
    add("#")
    add("#   arp           it answered at that address; its MAC; its vendor")
    add("#   bench-record  the model/firmware the device reported to a bench")
    add("#                 tool, looked up by MAC in bench-central")
    add("#   assumed       nobody has checked")
    add("#")
    add("# There is deliberately NO net: or power: block. An ARP sweep proves")
    add("# no cable and no power feed, so inventing either here would make")
    add("# this file confidently wrong. Add them from a switch MAC-address")
    add("# table, LLDP, and PoE port status - then mark them accordingly.")
    if warnings:
        add("#")
        add("# At discovery time:")
        for warning in warnings:
            for line in _wrap(warning, 68):
                add(f"#   {line}")
    add("version: 1")
    add(f"site: {_yaml_scalar(site_name)}")
    add("status: provisional")
    add(f"subnet: {_yaml_scalar(subnet)}")
    add("")
    add("nodes:")

    for item in found:
        add(f"  {item.name}:")
        add(f"    kind: {_yaml_scalar(item.kind)}")
        if item.vendor:
            add(f"    vendor: {_yaml_scalar(item.vendor)}")
        if item.model:
            add(f"    model: {_yaml_scalar(item.model)}")
        if item.firmware:
            add(f"    firmware: {_yaml_scalar(item.firmware)}")
        add(f"    mac: {_yaml_scalar(item.entry.mac)}")
        if item.entry.ip:
            add(f"    addr: {_yaml_scalar(item.entry.ip)}")
            add("    addr_source: static-bench")
        if len(item.evidence) == 1 and "*" in item.evidence:
            add(f"    evidence: {item.evidence['*']}")
        else:
            add("    evidence:")
            for claim in sorted(item.evidence, key=lambda c: (c != "*", c)):
                key = '"*"' if claim == "*" else claim
                add(f"      {key}: {item.evidence[claim]}")
        if item.entry.hostname:
            add(f"    # hostname: {item.entry.hostname}")
        if item.notes:
            add("    notes: >-")
            for note in item.notes:
                for line in _wrap(note, 64):
                    add(f"      {line}")
        add("")

    return "\n".join(out).rstrip() + "\n"


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines or [""]
