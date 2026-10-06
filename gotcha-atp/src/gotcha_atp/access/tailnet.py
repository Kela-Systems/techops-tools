"""The laptop's view of the tailnet: `tailscale status --json` (Home, S0.2).

Units are found, not typed: every peer named `<site>-operator` is a unit, and
`<site>` is its server's own tailnet name.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .exec import run_local

OPERATOR_SUFFIX = "-operator"
_MAC_APP_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


def tailscale_cli() -> Optional[str]:
    found = shutil.which("tailscale")
    if found:
        return found
    return _MAC_APP_CLI if Path(_MAC_APP_CLI).exists() else None


def status() -> dict:
    """`tailscale status --json`, or {'error': ...} when it cannot be read."""
    cli = tailscale_cli()
    if not cli:
        return {"error": "tailscale CLI not found on this laptop"}
    r = run_local([cli, "status", "--json"], timeout=10)
    if not r.ok:
        return {"error": (r.err or r.out).strip() or f"tailscale status exited {r.rc}"}
    try:
        return json.loads(r.out)
    except ValueError:
        return {"error": "tailscale status --json did not return JSON"}


@dataclass
class Peer:
    name: str
    online: bool
    ip: str

    def to_dict(self) -> dict:
        return {"name": self.name, "online": self.online, "ip": self.ip}


def _peer_name(p: dict) -> str:
    dns = str(p.get("DNSName") or "").split(".")[0]
    return (dns or str(p.get("HostName") or "")).lower()


def peers(st: dict) -> dict[str, Peer]:
    out = {}
    for p in (st.get("Peer") or {}).values():
        name = _peer_name(p)
        if name:
            ips = p.get("TailscaleIPs") or []
            v4 = next((ip for ip in ips if ":" not in ip), ips[0] if ips else "")
            out[name] = Peer(name, bool(p.get("Online")), v4)
    return out


def self_online(st: dict) -> bool:
    me = st.get("Self") or {}
    return st.get("BackendState") == "Running" and bool(me.get("Online", True))


@dataclass
class UnitPeers:
    site: str
    operator: Optional[Peer]
    server: Optional[Peer]

    @property
    def nonstandard_operator(self) -> bool:
        return bool(self.operator and self.operator.name != self.site + OPERATOR_SUFFIX)

    @property
    def state(self) -> str:
        op = bool(self.operator and self.operator.online)
        sv = bool(self.server and self.server.online)
        if not op and not sv:
            s = "offline"
        else:
            s = ("server online" if sv else "server offline") + " · " + \
                ("operator online" if op else "operator offline" if self.operator
                 else "no operator peer")
        if self.nonstandard_operator:
            s += f" · operator is {self.operator.name}"
        return s

    def to_dict(self) -> dict:
        return {"site": self.site, "state": self.state,
                "nonstandard_operator": self.nonstandard_operator,
                "operator": self.operator.to_dict() if self.operator else None,
                "server": self.server.to_dict() if self.server else None}


def units(st: dict) -> list[UnitPeers]:
    """Every `*-operator` peer as a unit, online ones first, then by name.
    A unit whose operator is named otherwise is not listed — its site is
    typed on Home, with the operator named explicitly."""
    ps = peers(st)
    found = []
    for name, peer in ps.items():
        if name.endswith(OPERATOR_SUFFIX):
            site = name[: -len(OPERATOR_SUFFIX)]
            found.append(UnitPeers(site, peer, ps.get(site)))
    return sorted(found, key=lambda u: (not (u.operator and u.operator.online), u.site))


def unit(st: dict, site: str, operator: Optional[str] = None) -> UnitPeers:
    """The unit `site`. Its operator is the peer named `operator` when one is
    given, otherwise `<site>-operator`. Never guessed from other peer names."""
    ps = peers(st)
    site = site.lower().removesuffix(OPERATOR_SUFFIX)
    name = operator.lower().split(".")[0] if operator else site + OPERATOR_SUFFIX
    return UnitPeers(site, ps.get(name), ps.get(site))
