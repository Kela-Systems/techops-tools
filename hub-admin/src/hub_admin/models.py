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
