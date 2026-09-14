#!/usr/bin/env python3
"""What proves a fact, and what that proof actually covers.

This module exists because of a specific mistake worth not repeating. An ARP
table was read as evidence of device *models*. It is not: an OUI lookup
identifies a vendor, and a vendor is not a model. Teltonika makes the RUTM08,
the RUTM51 and the RUTX50; Magos makes both the AR-300 and the APU. Reading
ARP as model evidence produces a site map that is confidently wrong, which is
worse than one that is visibly incomplete.

So evidence here is not a confidence score. Each kind declares **what it
proves**, as a set of claims, and the linter checks that every fact on the map
is backed by evidence that actually covers that fact:

    CLAIM_MODEL   this box is that model
    CLAIM_PRESENT this box is powered up and on the network right now
    CLAIM_MAC     this is its MAC
    CLAIM_ADDR    this is its address
    CLAIM_LINK    this cable runs between these two sockets
    CLAIM_POWER   this feed really carries power to that device
    CLAIM_DRAW    it really draws that many watts

`assumed` is the default on purpose. An unmarked fact is an unverified fact,
and the map should say so rather than let silence read as confirmation.

One hard limit, stated here so no caller has to rediscover it: **no protocol
proves a DC or mains power path.** A switch can report that PWR1 has voltage;
nothing can report which PSU, breaker or UPS is at the other end of that
wire. Those edges are verifiable only by a human survey, and CLAIM_POWER is
never granted by a network source for them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

CLAIM_PRESENT = "present"
CLAIM_MAC = "mac"
# Vendor and model are deliberately separate claims. An OUI lookup settles the
# vendor and nothing more: Teltonika makes the RUTM08, the RUTM51 and the
# RUTX50, and Magos makes both the AR-300 and the APU. Collapsing the two into
# one claim is exactly how an ARP table ends up presented as a model list.
CLAIM_VENDOR = "vendor"
CLAIM_MODEL = "model"
# Firmware is its own claim and the most perishable one here: it is true only
# until someone flashes the unit. Nothing infers it - only the device itself,
# or a bench record of a run against it, ever knew.
CLAIM_FIRMWARE = "firmware"
CLAIM_ADDR = "addr"
CLAIM_LINK = "link"
CLAIM_POWER = "power"
CLAIM_DRAW = "draw"

ALL_CLAIMS = (
    CLAIM_PRESENT, CLAIM_MAC, CLAIM_VENDOR, CLAIM_MODEL, CLAIM_FIRMWARE,
    CLAIM_ADDR, CLAIM_LINK, CLAIM_POWER, CLAIM_DRAW,
)

# The wildcard key in a per-claim evidence mapping: the source to fall back on
# for any claim not named explicitly.
ANY = "*"


@dataclass(frozen=True)
class Source:
    """One kind of evidence."""

    name: str
    proves: frozenset
    # Read off the hardware (or its controller) rather than out of a document
    # or a config we wrote. Only these count toward verified coverage.
    live: bool
    summary: str

    def proves_claim(self, claim: str) -> bool:
        return claim in self.proves


def _s(name, proves, live, summary):
    return Source(name, frozenset(proves), live, summary)


SOURCES: dict[str, Source] = {s.name: s for s in (
    # --- read off the hardware -------------------------------------------
    _s("device-api",
       {CLAIM_PRESENT, CLAIM_MAC, CLAIM_VENDOR, CLAIM_MODEL, CLAIM_FIRMWARE,
        CLAIM_ADDR}, True,
       "the device's own API reported it (every bench tool's get_identity)"),
    _s("switch-table", {CLAIM_PRESENT, CLAIM_MAC, CLAIM_VENDOR, CLAIM_LINK}, True,
       "a switch's MAC address table places this MAC on that port"),
    _s("lldp", {CLAIM_PRESENT, CLAIM_LINK}, True,
       "LLDP neighbour on that port"),
    _s("dhcp-lease", {CLAIM_PRESENT, CLAIM_MAC, CLAIM_VENDOR, CLAIM_ADDR}, True,
       "the DHCP server's own lease table"),
    _s("poe-status", {CLAIM_PRESENT, CLAIM_LINK, CLAIM_POWER, CLAIM_DRAW}, True,
       "the switch reports this port delivering power, with a measured draw"),
    _s("arp", {CLAIM_PRESENT, CLAIM_MAC, CLAIM_VENDOR, CLAIM_ADDR}, True,
       "an ARP entry plus an OUI lookup - proves something answered at that "
       "address and who made it, NOT which of that vendor's models it is"),
    _s("survey", set(ALL_CLAIMS), True,
       "a person traced it on site and recorded it (the only thing that can "
       "prove a DC or mains path)"),

    # --- collected earlier, still authoritative for identity -------------
    _s("bench-record",
       {CLAIM_MAC, CLAIM_VENDOR, CLAIM_MODEL, CLAIM_FIRMWARE}, True,
       "a bench-central run record - the model the device reported when it "
       "was provisioned; says nothing about it being present now"),

    # --- not evidence about reality --------------------------------------
    _s("config", set(), False,
       "what a bench tool is configured to apply - intent, not observation"),
    _s("doc", set(), False,
       "written down in a runbook or a port-map label"),
    _s("assumed", set(), False,
       "nobody has checked"),
)}

DEFAULT = "assumed"


def get(name: Optional[str]) -> Source:
    return SOURCES.get(name or DEFAULT, SOURCES[DEFAULT])


def is_known(name: str) -> bool:
    return name in SOURCES


def names() -> list[str]:
    return list(SOURCES)


def proves(name: Optional[str], claim: str) -> bool:
    return get(name).proves_claim(claim)


# Claims that no network protocol can establish, so a map must not imply it
# has. Kept here rather than in lint so the rule is stated once.
SURVEY_ONLY_VIA = frozenset({"dc", "mains", "usb"})


@dataclass
class EvidenceSet:
    """Which source backs which claim about one node or edge.

    Per-claim rather than per-item, because a real device's facts arrive from
    different places: an ARP sweep proves the vendor, and bench-central proves
    the model, on the same box. Recording one source for the whole node would
    force a choice between overstating the model and understating the vendor.

    `ANY` ("*") is the fallback for claims not named explicitly; anything with
    no entry at all is `assumed`.
    """

    by_claim: dict

    @classmethod
    def of(cls, source: str = DEFAULT) -> "EvidenceSet":
        return cls({ANY: source})

    def source_for(self, claim: str) -> Source:
        name = self.by_claim.get(claim, self.by_claim.get(ANY, DEFAULT))
        return get(name)

    def name_for(self, claim: str) -> str:
        return self.source_for(claim).name

    def proves(self, claim: str) -> bool:
        return self.source_for(claim).proves_claim(claim)

    def sources_used(self) -> set:
        return set(self.by_claim.values())

    def is_uniform(self) -> bool:
        return set(self.by_claim) <= {ANY}


class EvidenceError(Exception):
    """A malformed `evidence:` block."""


def parse(spec, what: str) -> EvidenceSet:
    """Read an `evidence:` value: a bare source name, or a claim mapping."""
    if spec is None:
        return EvidenceSet.of()

    if isinstance(spec, str):
        if not is_known(spec):
            raise EvidenceError(
                f"{what}: evidence '{spec}' is not one of {', '.join(names())}"
            )
        return EvidenceSet.of(spec)

    if isinstance(spec, dict):
        by_claim = {}
        for claim, source in spec.items():
            claim, source = str(claim), str(source)
            if claim != ANY and claim not in ALL_CLAIMS:
                raise EvidenceError(
                    f"{what}: '{claim}' is not a claim; use one of "
                    f"{', '.join(ALL_CLAIMS)} or '{ANY}'"
                )
            if not is_known(source):
                raise EvidenceError(
                    f"{what}: evidence '{source}' for '{claim}' is not one of "
                    f"{', '.join(names())}"
                )
            by_claim[claim] = source
        return EvidenceSet(by_claim)

    raise EvidenceError(
        f"{what}: evidence must be a source name or a claim mapping, got "
        f"{type(spec).__name__}"
    )
