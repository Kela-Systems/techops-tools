#!/usr/bin/env python3
"""Parsing of the QR label printed on a device (TEC-349).

Teltonika stickers carry a semicolon-delimited key/value string. Two real ones:

    OTD500  SN:6008219573;I:864088065513384;M:2097272B00F7;U:admin;PW:zZ?40*kA;B:015;
    RUTM08  SN:6010212527;M:20972732F638;U:admin;PW:mL$7=b6N;B:039;

Scanning that instead of retyping the password off the sticker removes the one
step of bench provisioning that is pure manual transcription of a
case-sensitive, punctuation-heavy string — and, because the label also carries
the LAN MAC, it lets a tool confirm the operator scanned the device that is
actually plugged in (`DeviceLabel.matches_mac`).

This module is the ONE parser. The browser sends the raw scanned string to the
server and never picks it apart itself, so there is no second implementation to
drift out of step, the whole thing is testable with no hardware, and the
password never has to be put into the page.

Handling the password safely is a design constraint here, not an afterthought:
`DeviceLabel.password` is the only way to reach it, `redacted()` is what goes
anywhere near state or a log, and `__repr__` is overridden so the value cannot
escape through a traceback, an f-string, or a debugger dump.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from bench_core import canonical_mac

# Label key -> our field name. Longest keys first so the alternation below can
# never match a short key where a longer one starts (harmless today — none of
# these is a prefix of another — but it stops a future key like `SNX` from
# quietly breaking every scan).
KEY_MAP = {
    "SN": "serial",
    "PW": "password",
    "I": "imei",
    "M": "mac",
    "U": "username",
    "B": "batch",
}
_KEYS_ALT = "|".join(sorted(KEY_MAP, key=len, reverse=True))

# A scan is only treated as a device label when it carries at least this many
# recognised keys. One is too easy to hit by accident (any text with "M:" in
# it); two effectively requires the delimited structure.
MIN_KEYS = 2

# What a key looks like, whether or not we know it.
_KEYISH = r"[A-Za-z][A-Za-z0-9_]{0,7}"

# Every field, anchored on the START OF THE NEXT KEY rather than on the next
# `;`. That distinction is the whole point: a factory password may legitimately
# contain `;` or `:`, and splitting naively on `;` would truncate it — producing
# a password that is *almost* right, which is the worst failure available here
# because it presents as a device fault rather than a parsing bug.
#
# The lookahead deliberately accepts ANY key-shaped token, not just the ones in
# KEY_MAP, so a firmware that adds a field cannot make the *preceding* value
# swallow it. The cost is one pathological input: a password containing a
# literal `;<word>:`. A format with no escaping cannot resolve that either way,
# and an unknown key appearing in a real label is far likelier than a random
# 8-character password happening to contain that exact sequence.
_FIELD_RE = re.compile(
    rf"(?:^|;)\s*({_KEYS_ALT})\s*:(.*?)(?=;\s*{_KEYISH}\s*:|;?\s*$)",
    re.IGNORECASE | re.DOTALL,
)

# Where the label actually starts, so a scanner prefix (`~`, configured to mark
# a scan) or any other leading noise is skipped rather than breaking the anchor
# of the first field. The key must be preceded by start-of-string or a
# non-alphanumeric, so the `I:` inside a hypothetical `MI:` is not mistaken for
# the IMEI key.
_FIRST_KEY_RE = re.compile(rf"(?:^|[^A-Za-z0-9])((?:{_KEYS_ALT})\s*:)",
                           re.IGNORECASE)

# Control characters a keyboard-wedge scanner can append (CR/LF suffix, the
# occasional NUL) — never part of a value.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(repr=False)
class DeviceLabel:
    """One parsed device label.

    `password` is deliberately reachable only by naming it. Use `redacted()`
    for anything that is logged, put in the UI state, or written to a record.
    """

    serial: str = ""
    imei: str = ""
    mac: str = ""
    username: str = ""
    password: str = ""
    batch: str = ""
    # Keys seen in the scan that this module does not know about. Kept so a new
    # model that adds a field is still parsed instead of rejected, and so an
    # unexpected label shape is visible when debugging.
    unknown_keys: list[str] = field(default_factory=list)

    def family(self) -> str:
        """`"cellular"` or `"non-cellular"`, inferred from whether the label
        carries an IMEI.

        Advisory only — it is a property of the label, not an assertion about
        the device, and nothing in phase 1 acts on it. It happens to separate
        the two tools' devices cleanly (an OTD500 has a modem and prints an
        IMEI; a RUTM08 does not), which is what a future "scan the label and
        open the right tool" step would key off. The authoritative model check
        stays where it is: `assert_device_model()`, reading the model off the
        device itself after login.
        """
        return "cellular" if self.imei else "non-cellular"

    def matches_mac(self, other: Optional[str]) -> Optional[bool]:
        """Whether this label's MAC is the same device as `other`.

        None when either side is unknown — "we could not tell", which callers
        must treat differently from "they disagree". Comparison goes through
        `canonical_mac`, so the label's separator-free `2097272B00F7` and ARP's
        `20:97:27:2b:00:f7` compare equal.
        """
        if not self.mac or not other:
            return None
        return canonical_mac(self.mac) == canonical_mac(other)

    def redacted(self) -> dict:
        """Everything about this label except the password, plus enough about
        the password to reason about it (whether there is one, and how long).
        Safe for `/api/state`, logs and run records."""
        return {
            "serial": self.serial,
            "imei": self.imei,
            "mac": self.mac,
            "username": self.username,
            "batch": self.batch,
            "family": self.family(),
            "has_password": bool(self.password),
            "password_length": len(self.password),
        }

    # Both of these exist so the password cannot escape by accident — through a
    # traceback frame, a bare f-string, a `print`, or a debugger's object dump.
    def __repr__(self) -> str:
        return (f"DeviceLabel(serial={self.serial!r}, mac={self.mac!r}, "
                f"imei={self.imei!r}, batch={self.batch!r}, "
                f"password=<redacted {len(self.password)} chars>)")

    __str__ = __repr__


def parse_device_label(raw: str) -> Optional[DeviceLabel]:
    """Parse one scanned label, or return None if it is not a device label.

    Tolerant of everything a scanner realistically adds or drops: a configured
    prefix character, a CR/LF suffix, surrounding whitespace, lower-case keys,
    spaces around the delimiters, a missing trailing `;`, and keys this module
    has never heard of.
    """
    if not raw:
        return None
    text = _CONTROL_RE.sub("", raw).strip()
    start = _FIRST_KEY_RE.search(text)
    if not start:
        return None
    text = text[start.start(1):]

    label, seen = DeviceLabel(), 0
    for match in _FIELD_RE.finditer(text):
        name = KEY_MAP[match.group(1).upper()]
        # First occurrence wins: a duplicated key is a malformed label, and
        # silently taking the later value would be an odd way to resolve it.
        if not getattr(label, name):
            setattr(label, name, match.group(2).strip())
        seen += 1
    if seen < MIN_KEYS:
        return None

    # Anything shaped like a key that we did not consume — recorded, not fatal.
    for candidate in re.findall(rf"(?:^|;)\s*({_KEYISH})\s*:", text):
        if candidate.upper() not in KEY_MAP:
            label.unknown_keys.append(candidate)

    return label
