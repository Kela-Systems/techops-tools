#!/usr/bin/env python3
"""Resolve MAC addresses to vendors, and guess the model from context.

Offline. The vendor database is nmap's `nmap-mac-prefixes`, already on any
machine with nmap installed; nothing is sent anywhere, which matters because
an ARP table from a live site is an inventory of that site.

**Longest-prefix matching is the whole trick.** Half the hardware here sits
behind an IEEE MA-S block, where the vendor is identified by the first 36
bits, not the usual 24. A 24-bit-only lookup reports the four Magos radars as
"Ieee Registration Authority" and tells you nothing:

    8C:1F:64          -> Ieee Registration Authority   (the block's registrar)
    8C:1F:64:E7:4     -> Magosys Systems               (the actual assignment)

So every lookup tries 36 bits, then 28, then 24, and takes the first hit.

A MAC identifies a *vendor*, never a model. The model guess comes from the
vendor plus the hostname and the address, and is reported with its reasoning
so a wrong guess is visible rather than load-bearing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import devices

# Where nmap keeps its vendor list, in the order worth trying.
# Local additions, merged over the system database. nmap's list is a snapshot
# that ships with the package, so anything newer than it resolves to no vendor
# at all - which on a site map reads as "unknown device".
OVERRIDES = Path(__file__).resolve().parent / "oui-overrides.txt"

DB_CANDIDATES = (
    "/opt/homebrew/share/nmap/nmap-mac-prefixes",
    "/usr/local/share/nmap/nmap-mac-prefixes",
    "/usr/share/nmap/nmap-mac-prefixes",
    "/opt/local/share/nmap/nmap-mac-prefixes",
)

# IEEE registrar names. A hit on one of these means the lookup fell back to
# the 24-bit block of an MA-S/MA-M assignment and has NOT found the vendor.
REGISTRARS = ("ieee registration authority", "ieee registration")

_HEX = re.compile(r"[^0-9A-Fa-f]")

# `arp -an` on Linux and macOS, which is what gets pasted in practice:
#   name.lan (192.168.88.1) at 20:97:27:36:55:ec [ether] on enp128s31f6
#   ? (192.168.88.132) at 8c:1f:64:e7:48:c7 [ether] on enp128s31f6
ARP_LINE = re.compile(
    r"^\s*(?P<host>\S+)\s+\((?P<ip>[0-9.]+)\)\s+at\s+"
    r"(?P<mac>[0-9A-Fa-f:.-]{11,17})",
)


class OuiError(Exception):
    pass


@dataclass
class Vendor:
    name: str
    prefix_bits: int
    matched: str

    @property
    def is_registrar(self) -> bool:
        return any(r in self.name.casefold() for r in REGISTRARS)


@dataclass
class Entry:
    """One resolved ARP row."""

    mac: str
    ip: Optional[str] = None
    hostname: Optional[str] = None
    vendor: Optional[Vendor] = None
    model: Optional[str] = None
    # Why the model was guessed. Empty when the vendor alone settled it.
    because: list[str] = None

    def __post_init__(self) -> None:
        if self.because is None:
            self.because = []


def find_db(explicit: Optional[str] = None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise OuiError(f"no vendor database at {path}")
        return path
    for candidate in DB_CANDIDATES:
        path = Path(candidate)
        if path.is_file():
            return path
    raise OuiError(
        "no nmap-mac-prefixes found. Install nmap (brew install nmap) or pass "
        "--db with a path to one."
    )


def load_db(path: Path, overrides: Path | None = OVERRIDES) -> dict[str, str]:
    """prefix (upper hex, no separators) -> vendor name.

    Local overrides are merged last so they win, and a longer prefix beats a
    shorter one in `lookup` regardless of which file it came from.
    """
    table = _read_prefixes(path)
    if not table:
        raise OuiError(f"{path} held no usable entries")
    if overrides and Path(overrides).is_file():
        table.update(_read_prefixes(Path(overrides)))
    return table


def _read_prefixes(path: Path) -> dict[str, str]:
    table: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        prefix, _, name = line.partition(" ")
        if name:
            table[prefix.strip().upper()] = name.strip()
    return table


def normalise(mac: str) -> str:
    digits = _HEX.sub("", mac).upper()
    if len(digits) < 6:
        raise OuiError(f"'{mac}' is not a MAC address")
    return digits


def lookup(mac: str, table: dict[str, str]) -> Optional[Vendor]:
    """Longest prefix first: 36 bits, then 28, then 24."""
    digits = normalise(mac)
    for length in (9, 7, 6):
        if len(digits) < length:
            continue
        prefix = digits[:length]
        if prefix in table:
            return Vendor(
                name=table[prefix], prefix_bits=length * 4, matched=prefix
            )
    return None


# Hostname shapes seen at real sites. Each is a fact recorded somewhere in
# this repo, not a guess about what a name might mean.
HOSTNAME_HINTS = (
    # raythink_camera.py reads the serial out of the HTTP realm, which looks
    # like "Login to KK0552PAZ00681" - two letters, four digits, PAZ, five.
    (re.compile(r"^[A-Z]{2}\d{4}PAZ\d{5}$", re.I), "Raythink PC464A1",
     "hostname is a Raythink serial (XXnnnnPAZnnnnn)"),
    (re.compile(r"^rut[-_]", re.I), "Teltonika RUTM08",
     "hostname starts with 'rut' (Teltonika RUT series)"),
    (re.compile(r"operator", re.I), None, "hostname says operator station"),
    (re.compile(r"otd", re.I), "Teltonika OTD500", "hostname says OTD"),
    (re.compile(r"tsw", re.I), "Teltonika TSW202", "hostname says TSW"),
)


def guess_model(entry: Entry) -> None:
    """Fill in `model` and `because` from vendor, hostname and address."""
    reasons: list[str] = []
    model: Optional[str] = None

    host = (entry.hostname or "").split(".")[0]
    if host and host != "?":
        for pattern, candidate, why in HOSTNAME_HINTS:
            if pattern.search(host):
                reasons.append(why)
                if candidate and not model:
                    model = candidate
                break

    vendor_name = entry.vendor.name if entry.vendor else None
    if vendor_name and not entry.vendor.is_registrar:
        # One catalogue entry for this vendor means the vendor settles it.
        matches = [
            d for d in devices.CATALOGUE
            if d.vendor.casefold().split()[0] in vendor_name.casefold()
        ]
        if len(matches) == 1 and not model:
            model = matches[0].model
            reasons.append(f"only one {matches[0].vendor} device in the catalogue")
        elif len(matches) > 1 and not model:
            reasons.append(
                f"{vendor_name} makes several devices here: "
                + ", ".join(sorted(m.model for m in matches))
            )

    entry.model = model
    entry.because = reasons


def parse_arp(text: str) -> list[Entry]:
    """Read `arp -an` output. Lines that are not ARP rows are ignored."""
    entries: list[Entry] = []
    for line in text.splitlines():
        match = ARP_LINE.match(line)
        if not match:
            continue
        host = match.group("host")
        try:
            mac = normalise(match.group("mac"))
        except OuiError:
            continue
        if len(mac) != 12:
            continue
        entries.append(
            Entry(
                mac=":".join(mac[i:i + 2] for i in range(0, 12, 2)).lower(),
                ip=match.group("ip"),
                hostname=None if host in ("?", "") else host,
            )
        )
    return entries


def resolve(entries: Iterable[Entry], table: dict[str, str]) -> list[Entry]:
    out = []
    for entry in entries:
        entry.vendor = lookup(entry.mac, table)
        guess_model(entry)
        out.append(entry)
    return out
