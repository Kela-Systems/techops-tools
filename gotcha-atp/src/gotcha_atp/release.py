"""release.yaml loading and validation (schema gotcha-release/1, row S0.1).

release.yaml is the only config file: fleet-wide pins, thresholds and the LAN
plan. Nothing per unit lives here — per-unit facts are discovered (S0.6).

A pin that is empty or starts with "TODO" is *not pinned*. Rows comparing
against one record the value and go amber; they never pass on a placeholder.
"""
from __future__ import annotations

import hashlib
import ipaddress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from . import PROJECT_DIR, git_revision

SCHEMA = "gotcha-release/1"
DEFAULT_PATH = PROJECT_DIR / "release.yaml"

_REQUIRED = {
    "hub": ("image_tag", "product_services", "magos_agent", "integration_config", "detector"),
    "server": ("init_version", "k3s_floor", "node_controller_floor", "l4t", "timezone"),
    "magos": ("apu_firmware", "radar_firmware"),
    "camera": ("generation", "firmware", "streams"),
    "speaker": ("firmware", "outvolume_min"),
    "teltonika": ("rutm08", "otd500", "tsw202_floor"),
    "planet": ("firmware_floor",),
    "operator": ("setup_version",),
    "plan": ("router", "switch", "poe_switch", "server", "camera", "radars",
             "apus", "speaker", "modem", "pan_nominal_deg"),
    "thresholds": ("poe_budget_w", "ping_avg_ms", "ping_loss_pct", "ping_jitter_ms",
                   "ping_count", "ntp_offset_s", "hub_api_p95_ms", "hub_ttfb_ms",
                   "chrony_offset_max_ms",
                   "disk_used_max_pct", "mem_available_min_mb",
                   "modem_signal_min_dbm", "pod_stable_min_minutes", "magos_time_sync_offset_s",
                   "magos_temp_max_c", "magos_ethernet_speed", "poe_radar_w",
                   "stream_health_min", "detection_sample_s",
                   "calib_yaw_vs_declared_max_deg", "calib_tilt_spread_max_deg"),
}

RADAR_IDS = ("radar_0", "radar_1", "radar_2", "radar_3")


class ReleaseError(ValueError):
    """release.yaml is missing, unparsable or does not match the schema."""


def pinned(value: Any) -> bool:
    """True when a release.yaml value is a real pin rather than a placeholder."""
    if value is None:
        return False
    text = str(value).strip()
    return bool(text) and not text.upper().startswith("TODO")


@dataclass(frozen=True)
class Plan:
    router: str
    switch: str
    poe_switch: str
    server: str
    camera: str
    speaker: str
    modem: str
    radars: dict[str, str]           # radar_0 -> 192.168.88.50
    apus: dict[str, list[str]]       # 192.168.88.60 -> [radar_0, radar_1]
    pan_nominal_deg: dict[str, float]

    def addresses(self) -> dict[str, str]:
        """Every planned LAN address (the server excluded) -> a display name, in
        plan order. The S1.1 ping matrix and S1.2 duplicate-IP rows walk this."""
        out = {self.router: "router RUTM08", self.switch: "switch TSW202",
               self.poe_switch: "PoE switch IGS-4215", self.camera: "camera"}
        for rid, ip in self.radars.items():
            out[ip] = rid
        for ip, radars in self.apus.items():
            out[ip] = "APU " + "+".join(radars)
        out[self.speaker] = "speaker"
        return out

    def apu_for(self, radar_id: str) -> Optional[str]:
        for ip, radars in self.apus.items():
            if radar_id in radars:
                return ip
        return None


@dataclass(frozen=True)
class Release:
    name: str
    data: dict
    plan: Plan
    path: Path
    sha256: str
    commit: str
    thresholds: dict = field(default_factory=dict)

    def get(self, dotted: str, default: Any = None) -> Any:
        """release.get('server.init_version')."""
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def pin(self, dotted: str) -> Optional[str]:
        """The value at `dotted` as a string when it is pinned, else None."""
        value = self.get(dotted)
        return str(value).strip() if pinned(value) else None

    def info(self) -> dict:
        return {"name": self.name, "commit": self.commit,
                "sha256": self.sha256, "path": str(self.path)}


def _ip(value: Any, where: str) -> str:
    try:
        return str(ipaddress.IPv4Address(str(value)))
    except ValueError:
        raise ReleaseError(f"{where}: {value!r} is not an IPv4 address")


def load(path: Optional[Path] = None) -> Release:
    path = Path(path or DEFAULT_PATH)
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise ReleaseError(f"cannot read {path}: {e}")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ReleaseError(f"{path.name} is not valid YAML: {e}")
    if not isinstance(data, dict):
        raise ReleaseError(f"{path.name} must be a mapping")
    if data.get("schema") != SCHEMA:
        raise ReleaseError(f"schema is {data.get('schema')!r}, expected {SCHEMA!r}")
    if not str(data.get("name") or "").strip():
        raise ReleaseError("name is missing")

    missing = [f"{section}.{key}" for section, keys in _REQUIRED.items()
               for key in keys
               if not isinstance(data.get(section), dict) or key not in data[section]]
    if missing:
        raise ReleaseError("missing keys: " + ", ".join(missing))

    p = data["plan"]
    radars = {str(k): _ip(v, f"plan.radars.{k}") for k, v in (p["radars"] or {}).items()}
    if tuple(sorted(radars)) != RADAR_IDS:
        raise ReleaseError(f"plan.radars must name exactly {', '.join(RADAR_IDS)}")
    apus: dict[str, list[str]] = {}
    for ip, ids in (p["apus"] or {}).items():
        ids = [str(i) for i in ids or []]
        unknown = [i for i in ids if i not in radars]
        if unknown:
            raise ReleaseError(f"plan.apus.{ip} names unknown radars: {unknown}")
        apus[_ip(ip, "plan.apus")] = ids
    assigned = sorted(i for ids in apus.values() for i in ids)
    if assigned != sorted(RADAR_IDS):
        raise ReleaseError("plan.apus must assign every radar to exactly one APU")
    plan = Plan(
        router=_ip(p["router"], "plan.router"), switch=_ip(p["switch"], "plan.switch"),
        poe_switch=_ip(p["poe_switch"], "plan.poe_switch"),
        server=_ip(p["server"], "plan.server"), camera=_ip(p["camera"], "plan.camera"),
        speaker=_ip(p["speaker"], "plan.speaker"), modem=_ip(p["modem"], "plan.modem"),
        radars=radars, apus=apus,
        pan_nominal_deg={str(k): float(v) for k, v in (p["pan_nominal_deg"] or {}).items()},
    )

    t = data["thresholds"]
    for key in _REQUIRED["thresholds"]:
        value = t[key]
        values = value if key == "poe_radar_w" else [value]
        if key == "poe_radar_w" and (not isinstance(value, list) or len(value) != 2):
            raise ReleaseError("thresholds.poe_radar_w must be [min, max]")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
            raise ReleaseError(f"thresholds.{key} must be a number")

    return Release(name=str(data["name"]), data=data, plan=plan, path=path,
                   sha256=hashlib.sha256(raw).hexdigest(),
                   commit=git_revision(path.parent), thresholds=dict(t))
