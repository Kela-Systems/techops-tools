"""Typed domain models — one place to update when protos change."""

from dataclasses import dataclass, field

ASSET_TYPE_LABELS = {
    0: "unknown",
    1: "camera",
    2: "drone",
    3: "radar",
    4: "trail_cam",
    5: "lidar",
    6: "dock",
    7: "weapon_system",
    8: "ugv",
    9: "ecm",
    10: "launcher",
    11: "loitering_munition",
    12: "vtol",
    13: "speaker",
    14: "smart_switch",
    15: "fence",
}


@dataclass
class Manifest:
    manifest_id: str
    name: str
    version: str
    description: str


@dataclass
class Integration:
    id: str
    name: str
    manifest_id: str | None
    device_count: int
    device_setup_info_schema: dict | None


@dataclass
class ExportedIntegration:
    """One integration as captured by `integrations export`.

    `section` is the device_config.json section key the integration's devices
    were written under (None when the integration has no devices).
    """

    id: str
    name: str
    manifest_id: str | None
    section: str | None
    device_count: int


@dataclass
class ProfileIntegration:
    """One integration as captured in a site profile bundle."""

    name: str
    manifest_id: str | None
    manifest_name: str | None
    has_config: bool
    device_count: int


@dataclass
class ProfileSummary:
    """Human-facing summary of a `profile export` bundle."""

    source_context: str
    integrations: list[ProfileIntegration]
    site_config_keys: list[str] = field(default_factory=list)


@dataclass
class AppliedIntegration:
    """Result of deploying one integration from a profile onto a hub.

    `integration_id` is None when the integration could not be created (e.g. no
    manifest matched on the destination); `error` then holds the reason.
    `matched_by_name` is True when the destination manifest was resolved from
    the stored manifest name rather than its (possibly hub-specific) ID.
    """

    name: str
    manifest_name: str | None
    integration_id: str | None
    manifest_id: str | None
    matched_by_name: bool
    devices: list[tuple[str, str]] = field(default_factory=list)
    dropped: dict[str, list[str]] = field(default_factory=dict)
    error: str | None = None


@dataclass
class ProfileApplyReport:
    """Outcome of `profile apply` — per-integration results + site config flag."""

    target_context: str
    applied: list[AppliedIntegration] = field(default_factory=list)
    site_config_applied: bool = False


@dataclass
class Asset:
    id: str
    name: str
    asset_type: str
    sensors: list[str] = field(default_factory=list)

    @classmethod
    def from_proto(cls, proto) -> "Asset":
        return cls(
            id=proto.id,
            name=proto.name,
            asset_type=ASSET_TYPE_LABELS.get(proto.asset_type, str(proto.asset_type)),
            sensors=[s.sensor_id for s in proto.sensors] if proto.sensors else [],
        )


@dataclass
class EntityLink:
    source_id: str
    target_id: str
    source_name: str = ""
    target_name: str = ""
    link_type: str = ""
