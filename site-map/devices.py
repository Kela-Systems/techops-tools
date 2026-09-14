#!/usr/bin/env python3
"""A catalogue of the hardware a site is built from.

Two things live here that the site model deliberately does not carry:

  faceplate   the physical socket layout of a device, so the UI can draw it
              and colour each socket by what is plugged in
  reference   vendor, form factor, datasheet link, and a slot for a photo

Why draw a faceplate rather than show a product photo? Because the question
an installer actually has is "which socket is gi8", and a photo does not
answer it. A drawn faceplate whose ports carry the same ids the site model
uses does, and it stays legible at phone width.

**Only layouts that are documented somewhere go in here.** The IGS-4215 is
known port-for-port because the bench tool configures every socket by name
(planet-config-ui/config/planet.config.json). The rest are listed for their
reference data with `ports: None`, which the UI renders as a plain box — an
invented port layout on a site map is worse than no port layout, because
someone will wire to it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

# Port roles. These drive the faceplate colours and, more importantly, the
# "can this socket power anything" question — the single most misread fact
# about the PoE switch.
PORT_POE = "poe"          # copper, supplies power
PORT_COPPER = "copper"    # copper, data only
PORT_SFP = "sfp"          # fibre cage
PORT_POWER_IN = "power-in"  # a DC input on the device itself
PORT_WAN = "wan"


@dataclass
class Port:
    """One socket. `id` matches the `port`/`outlet`/`peer_port` used in edges."""

    id: str
    role: str
    label: Optional[str] = None
    row: int = 0

    @property
    def powers(self) -> bool:
        return self.role == PORT_POE


@dataclass
class Device:
    """Reference data for one hardware model."""

    model: str
    vendor: str
    kind: str
    form_factor: str = "box"
    # None means "layout not documented" — the UI draws a plain box. Do not
    # fill this in from a photograph or a guess.
    ports: Optional[list[Port]] = None
    datasheet_url: Optional[str] = None
    # Relative path to a photo published alongside the page, e.g.
    # "images/igs-4215.jpg". Left None until someone drops a real file in;
    # an artifact page cannot load an image from an external host.
    image: Optional[str] = None
    notes: Optional[str] = None
    aliases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["ports"] = (
            [{**asdict(p), "powers": p.powers} for p in self.ports]
            if self.ports is not None
            else None
        )
        return data


def _igs4215_ports() -> list[Port]:
    """The IGS-4215-8UP2T2S, exactly as the bench names its sockets.

    gi1-gi8 are the PoE++ ports; gi9 and gi10 are copper and supply NO power,
    which is why the camera on gi9 needs an external feed; 11/12 are SFP.
    Source: planet-config-ui/config/planet.config.json (poe.ports) and the
    port-map note in bench/OPERATOR-GUIDE.md.
    """
    ports = [Port(id=f"gi{n}", role=PORT_POE, row=0) for n in range(1, 9)]
    ports += [
        Port(id="gi9", role=PORT_COPPER, label="no PoE", row=1),
        Port(id="gi10", role=PORT_COPPER, label="no PoE", row=1),
        Port(id="sfp11", role=PORT_SFP, row=1),
        Port(id="sfp12", role=PORT_SFP, row=1),
    ]
    # The switch's own dual DC inputs. The 240 W budget may only be treated
    # as higher when both are wired, so they belong on the faceplate.
    ports += [
        Port(id="PWR1", role=PORT_POWER_IN, row=2),
        Port(id="PWR2", role=PORT_POWER_IN, row=2),
    ]
    return ports


CATALOGUE: list[Device] = [
    Device(
        model="PLANET IGS-4215-8UP2T2S",
        vendor="PLANET",
        kind="poe-switch",
        form_factor="din-rail",
        ports=_igs4215_ports(),
        datasheet_url="https://www.planet.com.tw/en/product/igs-4215-8up2t2s",
        notes=(
            "8x PoE++ (gi1-gi8), 2x copper with no PoE (gi9-gi10), 2x SFP. "
            "240 W budget, and only above that if BOTH DC inputs are wired. "
            "PoE has been left unmanaged since 2026-09-14 - the switch "
            "negotiates per port."
        ),
        aliases=["IGS-4215-8UP2T2S", "IGS-4215"],
    ),
    Device(
        model="Teltonika RUTM08",
        vendor="Teltonika Networks",
        kind="router",
        form_factor="din-rail",
        datasheet_url="https://teltonika-networks.com/products/routers/rutm08",
        notes="The site gateway and its only DHCP server.",
        aliases=["RUTM08"],
    ),
    Device(
        model="Teltonika TSW202",
        vendor="Teltonika Networks",
        kind="switch",
        form_factor="din-rail",
        datasheet_url="https://teltonika-networks.com/products/switches/tsw202",
        notes=(
            "Managed switch. Some units are the PROFINET variant, which ships "
            "with no address and cannot be found by the bench tool."
        ),
        aliases=["TSW202"],
    ),
    Device(
        model="Teltonika OTD500",
        vendor="Teltonika Networks",
        kind="modem",
        form_factor="outdoor",
        datasheet_url="https://teltonika-networks.com/products/modems/otd500",
        notes="Outdoor unit; provides the WAN feed to the router.",
        aliases=["OTD500"],
    ),
    # The three radar models the bench has actually configured, per 44 runs
    # in bench-central (tool=magos-radar). Vendor string is "Magosys Systems"
    # as the OUI registry spells it.
    Device(
        model="AR-300",
        vendor="Magosys Systems",
        kind="radar",
        form_factor="outdoor",
        notes="Draws roughly 35 W over PoE. Pinned by RF channel 0-3.",
        aliases=["Magos AR-300", "AR300"],
    ),
    Device(
        model="SR1000-I",
        vendor="Magosys Systems",
        kind="radar",
        form_factor="outdoor",
        notes=(
            "The first Magos model here confirmed by reading a live unit's "
            "own API rather than inferred: two at kela-fob-03 on 2026-09-14, "
            "both on System Software 1.1.4 with Dashboard 2.4.3, DSP 1.2.4 "
            "and FPGA 1.2.6. Reports four independently-versioned components, "
            "so 'firmware' on a Magos radar is a set, not a string. Worth "
            "noting how it was found: the shortlist inferred from "
            "bench-central run records was AR-300 / SR1000-F / SR500-F, and "
            "the right answer was in none of them — the archive simply had no "
            "'-I' variant in it."
        ),
        aliases=["Magos SR1000-I", "SR1000I"],
    ),
    Device(
        model="SR1000-F",
        vendor="Magosys Systems",
        kind="radar",
        form_factor="outdoor",
        notes="Seen in bench-central run records alongside the AR-300.",
        aliases=["Magos SR1000-F"],
    ),
    Device(
        model="SR500-F",
        vendor="Magosys Systems",
        kind="radar",
        form_factor="outdoor",
        notes="Seen in bench-central run records alongside the AR-300.",
        aliases=["Magos SR500-F"],
    ),
    Device(
        model="AR Processing Unit (APU)",
        vendor="Nvidia",
        kind="apu",
        form_factor="box",
        notes=(
            "Jetson-based, so its MAC resolves to NVIDIA, not to Magos. That "
            "is the cleanest way to tell an APU from a radar by MAC alone: "
            "across 65 bench-central runs the split is absolute - 21/21 APUs "
            "are NVIDIA prefixes, 44/44 radars are Magosys. Firmware floor "
            "3.1.2."
        ),
        aliases=["Magos APU", "APU"],
    ),
    # Two distinct camera generations, under two different OUIs. Which model
    # string a unit reports follows its vendor prefix, per 31 raythink runs.
    Device(
        model="PC464A1",
        vendor="HangZhou JuRu Technology",
        kind="camera",
        form_factor="outdoor",
        notes=(
            "Thermal camera on the BC:74:D7 (HangZhou JuRu) prefix. In "
            "bench-central every camera on that prefix reports PC464A1. "
            "Typically on the PoE switch's gi9, which supplies NO power - it "
            "needs an external feed."
        ),
        aliases=["Raythink PC464A1"],
    ),
    Device(
        model="XX-MVP-PC4-V100",
        vendor="RayThink Technology",
        kind="camera",
        form_factor="outdoor",
        notes=(
            "The newer camera, on the AC:86:D1 (RayThink Technology) prefix. "
            "A different unit from the PC464A1 despite both being 'the "
            "Raythink camera' in conversation."
        ),
        aliases=["MVP-PC4"],
    ),
    Device(
        model="Provision-ISR PR-HS15W-IP",
        vendor="Provision-ISR",
        kind="speaker",
        form_factor="outdoor",
        notes="IP speaker, roughly 20 W over PoE. Arrives on DHCP at the bench.",
        aliases=["PR-HS15W-IP"],
    ),
]


def _index() -> dict[str, Device]:
    index: dict[str, Device] = {}
    for device in CATALOGUE:
        index[device.model.casefold()] = device
        for alias in device.aliases:
            index[alias.casefold()] = device
    return index


_INDEX = _index()


def lookup(model: Optional[str]) -> Optional[Device]:
    """Find a device by model string or alias. Case- and spacing-tolerant."""
    if not model:
        return None
    key = " ".join(model.split()).casefold()
    if key in _INDEX:
        return _INDEX[key]
    # A site file may carry a longer name than the catalogue entry; match on
    # any alias appearing in it rather than forcing the two to be identical.
    for alias, device in _INDEX.items():
        if alias in key:
            return device
    return None


def catalogue_dict() -> dict[str, dict]:
    return {d.model: d.to_dict() for d in CATALOGUE}
