#!/usr/bin/env python3
"""How a bench tool decides what address a device ends up on (TEC-848).

Every tool used to hardcode one answer — the Raythink cycled an octet per
camera, the speaker always landed on .70, the TSW202 always on .2 — and an
operator who needed a different one had to re-address the device by hand
afterwards, which left no record anywhere. This module is the one place that
answer is made, so the four modes below arrive in a tool by opting in rather
than by being written a fourth time.

The four modes:

  fixed    Every device gets the SAME address, which the operator sets once and
           which then survives units and restarts. Not a mistake to be guarded
           against: sites that take a single device of a kind all want the same
           default configuration, so a batch destined for twenty such sites
           should come off the bench identically addressed. Two of them never
           meet on a live network.
  cycle    Each device gets the next address in a range, wrapping at the top —
           for kinds that DO land several to a site. The counter is burned only
           by a successful run, so a failed unit keeps its slot for the retry.
  manual   The operator states the address for this one unit.
  dhcp     The device is left on DHCP and the bench assigns nothing. There is
           no address to record; the tool finds the device again by MAC to
           check it.

A tool declares which subset it offers (`IpModePolicy.modes`) — a switch has no
use for `cycle`, and offering it would be a mode that means nothing for the
device in front of the operator.

Addresses are handled as a subnet prefix plus a last octet, because that is what
all of this bench's assignments are: a 192.168.88.x host on a /24 whose gateway
and netmask come from the tool's config. Operators may still type a full dotted
address — `parse_octet` accepts either and rejects one on the wrong subnet,
which is the mistake worth catching.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# The four ways a device can get its address. `fixed` is first because it is
# the mode a tool with no range configured should open on.
MODE_FIXED = "fixed"
MODE_CYCLE = "cycle"
MODE_MANUAL = "manual"
MODE_DHCP = "dhcp"
ALL_MODES = (MODE_FIXED, MODE_CYCLE, MODE_MANUAL, MODE_DHCP)

# Modes that end with the bench having chosen an address. `dhcp` is the only one
# that doesn't, which is why it is the exception in almost every branch here.
ASSIGNING_MODES = (MODE_FIXED, MODE_CYCLE, MODE_MANUAL)

_IPV4 = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")

# .0 is the network and .255 the broadcast on the /24s this bench runs on, so
# neither is ever a device address.
OCTET_FLOOR = 1
OCTET_CEILING = 254


class IpModeError(ValueError):
    """A mode or address the operator asked for that cannot be used.

    Raised with a message written FOR the operator: these reach the browser
    verbatim as the error on a rejected Configure.
    """


def split_ip(ip: str) -> tuple[str, Optional[int]]:
    """`"192.168.88.70"` -> `("192.168.88", 70)`. `("", None)` when it is not a
    dotted IPv4 address.

    Lets a tool keep expressing its default as the full address its config has
    always carried (`static.ip`, `lan_ip`) instead of growing a second, more
    easily-contradicted way to say the same thing.
    """
    m = _IPV4.match((ip or "").strip())
    if not m:
        return "", None
    parts = [int(p) for p in m.groups()]
    if any(p > 255 for p in parts):
        return "", None
    return ".".join(str(p) for p in parts[:3]), parts[3]


@dataclass
class IpModePolicy:
    """What a given tool allows: which modes, on which subnet, in which range.

    Built from the tool's config on startup and again on reload, so a station
    that edits its config file doesn't have to restart to widen a range.
    """

    modes: tuple[str, ...] = ALL_MODES
    prefix: str = "192.168.88"
    octet_min: int = OCTET_FLOOR
    octet_max: int = OCTET_CEILING
    # The cycle range, when it differs from the range a manual entry may use —
    # the speaker cycles .70/.71 but may be sent anywhere on the subnet.
    cycle_min: int = 0
    cycle_max: int = 0
    default_mode: str = MODE_FIXED
    default_fixed_octet: int = 0

    def __post_init__(self) -> None:
        self.modes = tuple(m for m in ALL_MODES if m in self.modes) or (MODE_FIXED,)
        self.octet_min = max(OCTET_FLOOR, min(OCTET_CEILING, int(self.octet_min)))
        self.octet_max = max(self.octet_min,
                             min(OCTET_CEILING, int(self.octet_max)))
        # An unset cycle range means "the same range manual entry uses", which
        # is what the Raythink has always done.
        self.cycle_min = int(self.cycle_min) or self.octet_min
        self.cycle_max = int(self.cycle_max) or self.octet_max
        if self.cycle_max < self.cycle_min:
            self.cycle_min, self.cycle_max = self.cycle_max, self.cycle_min
        if self.default_mode not in self.modes:
            self.default_mode = self.modes[0]
        self.default_fixed_octet = (int(self.default_fixed_octet)
                                    or self.octet_min)

    def supports(self, mode: str) -> bool:
        return mode in self.modes

    def normalize_mode(self, mode: str) -> str:
        """`mode` if this tool offers it, else the default — so a stale value in
        a state file (or a mode removed from the tool since) degrades to
        something usable instead of erroring on every run."""
        return mode if self.supports(mode) else self.default_mode

    def ip_for(self, octet: Optional[int]) -> str:
        """The full address for `octet`, or `""` for the DHCP case."""
        return "" if octet is None else f"{self.prefix}.{int(octet)}"

    def clamp(self, octet: int, *, cycle: bool = False) -> int:
        lo, hi = ((self.cycle_min, self.cycle_max) if cycle
                  else (self.octet_min, self.octet_max))
        return lo if octet < lo or octet > hi else octet

    def parse_octet(self, value) -> int:
        """The last octet `value` names, validated against this tool's range.

        Accepts a bare octet (`"70"`) or a full address (`"192.168.88.70"`). A
        full address on another subnet is refused rather than quietly reduced to
        its last octet: the tool writes its own gateway and netmask alongside,
        so honouring only half of what was typed would strand the device.
        """
        text = str(value if value is not None else "").strip()
        if not text:
            raise IpModeError(
                f"Enter an address — the last octet ({self.octet_min}-"
                f"{self.octet_max}) or the full {self.prefix}.x address.")
        if "." in text:
            prefix, octet = split_ip(text)
            if octet is None:
                raise IpModeError(f"'{text}' is not a valid IPv4 address.")
            if prefix != self.prefix:
                raise IpModeError(
                    f"{text} is not on this tool's subnet ({self.prefix}.x). "
                    "The gateway and netmask it applies belong to that subnet, "
                    "so the device would be unreachable.")
        else:
            try:
                octet = int(text)
            except ValueError:
                raise IpModeError(
                    f"'{text}' is not a number — enter the last octet "
                    f"({self.octet_min}-{self.octet_max}) or the full "
                    f"{self.prefix}.x address.") from None
        if not (self.octet_min <= octet <= self.octet_max):
            raise IpModeError(
                f"{self.prefix}.{octet} is outside the allowed range "
                f"{self.prefix}.{self.octet_min}-{self.octet_max}.")
        return octet


@dataclass(frozen=True)
class IpAssignment:
    """What one run should do about addressing, decided before the device is
    touched so the answer can be logged, shown and recorded as one thing."""

    mode: str
    octet: Optional[int]      # None on DHCP — nothing was assigned
    ip: str                   # "" on DHCP
    advance_cycle: bool = False

    @property
    def dhcp(self) -> bool:
        return self.mode == MODE_DHCP


class IpModeStore:
    """The selected mode and its settings, persisted per tool.

    Station state, not per-device state: an operator picks a mode once and then
    works through a batch, so it has to survive both the next unit and a restart
    of the tool. Kept in its own small JSON file beside the tool rather than in
    the config, because the config is the station's checked-in intent and this
    is a switch the operator flips during a shift (and `config_fingerprint`
    would otherwise change every time they did).
    """

    FILENAME = "ip-state.json"

    def __init__(self, path: Path, policy: IpModePolicy,
                 logger: Optional[logging.Logger] = None) -> None:
        self.path = Path(path)
        self.policy = policy
        self.log = logger or logging.getLogger(__name__)
        self.mode = policy.default_mode
        self.cycle_next = policy.cycle_min
        self.fixed_octet = policy.default_fixed_octet
        self.load()

    # ── persistence ──────────────────────────────────────────────────────────

    def load(self) -> None:
        """Read the saved selection, falling back to the policy's defaults for
        anything missing, unreadable or no longer allowed."""
        data: dict = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self.log.warning("Could not read %s — starting from the config "
                             "defaults.", self.path.name)
        if not isinstance(data, dict):
            data = {}
        # `ip_mode`/`cycle_next` are the key names the Raythink's own state file
        # used before this module existed; read them so an in-place station
        # keeps its counter instead of silently restarting the range.
        self.mode = self.policy.normalize_mode(
            str(data.get("mode") or data.get("ip_mode") or ""))
        self.cycle_next = self.policy.clamp(
            _as_int(data.get("cycle_next"), self.policy.cycle_min), cycle=True)
        self.fixed_octet = self.policy.clamp(
            _as_int(data.get("fixed_octet"), self.policy.default_fixed_octet))

    def save(self) -> None:
        payload = {"mode": self.mode, "cycle_next": self.cycle_next,
                   "fixed_octet": self.fixed_octet}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            self.log.warning("Could not persist the address mode to %s — the "
                             "selection will not survive a restart.", self.path)

    def retarget(self, policy: IpModePolicy) -> None:
        """Adopt a policy re-read from the config, re-clamping the selection
        into whatever the ranges now are."""
        self.policy = policy
        self.mode = policy.normalize_mode(self.mode)
        self.cycle_next = policy.clamp(self.cycle_next, cycle=True)
        self.fixed_octet = policy.clamp(self.fixed_octet)

    # ── the operator's choices ───────────────────────────────────────────────

    def set_mode(self, mode: str, octet=None) -> None:
        """Select a mode, optionally setting the address it uses.

        `octet` is only meaningful for `fixed` — it is what makes that mode a
        thing the operator configures once rather than a second config field.
        """
        mode = (mode or "").strip()
        if not self.policy.supports(mode):
            offered = ", ".join(self.policy.modes)
            raise IpModeError(f"'{mode}' is not an address mode this tool "
                              f"offers ({offered}).")
        if mode == MODE_FIXED and str(octet or "").strip():
            self.fixed_octet = self.policy.parse_octet(octet)
        self.mode = mode
        self.save()

    def advance_cycle(self) -> None:
        """Burn the current cycle number. Called only after a run that actually
        used one and succeeded — see `BenchConfigurator.execute_run`."""
        nxt = self.cycle_next + 1
        self.cycle_next = (self.policy.cycle_min
                           if nxt > self.policy.cycle_max else nxt)
        self.save()

    # ── the decision ─────────────────────────────────────────────────────────

    def resolve(self, mode: str = "", octet=None) -> IpAssignment:
        """What this run should do about addressing.

        `mode` blank means "the selected one", which is the normal path: the
        operator set it before the batch and the page just submits. Raises
        `IpModeError` with an operator-readable message when the request cannot
        be honoured.
        """
        mode = self.policy.normalize_mode((mode or "").strip() or self.mode)
        if mode == MODE_DHCP:
            return IpAssignment(mode=mode, octet=None, ip="")
        if mode == MODE_CYCLE:
            chosen = self.policy.clamp(self.cycle_next, cycle=True)
            return IpAssignment(mode=mode, octet=chosen,
                                ip=self.policy.ip_for(chosen),
                                advance_cycle=True)
        if mode == MODE_FIXED:
            chosen = self.policy.clamp(self.fixed_octet)
            return IpAssignment(mode=mode, octet=chosen,
                                ip=self.policy.ip_for(chosen))
        chosen = self.policy.parse_octet(octet)
        return IpAssignment(mode=mode, octet=chosen,
                            ip=self.policy.ip_for(chosen))

    def next_ip(self) -> str:
        """The address the next device would get, or `""` under DHCP. For the
        page's preview line and the tool's startup banner."""
        try:
            return self.resolve().ip
        except IpModeError:
            return ""    # manual mode: nothing is chosen until it is typed

    # ── UI ───────────────────────────────────────────────────────────────────

    def describe(self, mode: Optional[str] = None) -> str:
        """One sentence for the status line, explaining what the selected mode
        will do to the next device."""
        mode = mode or self.mode
        prefix = self.policy.prefix
        if mode == MODE_DHCP:
            return ("Address mode: DHCP — the device keeps the address its own "
                    "DHCP server gives it, and the bench assigns nothing.")
        if mode == MODE_CYCLE:
            return (f"Address mode: cycle — the next device gets "
                    f"{prefix}.{self.cycle_next}, then it advances on each "
                    f"success (wrapping {self.policy.cycle_max} to "
                    f"{self.policy.cycle_min}).")
        if mode == MODE_FIXED:
            return (f"Address mode: fixed — every device gets "
                    f"{prefix}.{self.fixed_octet}.")
        return (f"Address mode: manual — type the address "
                f"({prefix}.{self.policy.octet_min}-{self.policy.octet_max}) "
                "for each device.")

    def public_state(self) -> dict:
        """The block the page renders the mode picker from. Merged into
        `/api/state` by `BenchConfigurator.public_state`."""
        return {
            "ip_mode": self.mode,
            "ip_modes": list(self.policy.modes),
            "cycle_next": self.cycle_next,
            "fixed_octet": self.fixed_octet,
            "fixed_ip": self.policy.ip_for(self.fixed_octet),
            "next_ip": self.next_ip(),
            "subnet_prefix": self.policy.prefix,
            "octet_min": self.policy.octet_min,
            "octet_max": self.policy.octet_max,
            "cycle_min": self.policy.cycle_min,
            "cycle_max": self.policy.cycle_max,
        }


def _as_int(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback
