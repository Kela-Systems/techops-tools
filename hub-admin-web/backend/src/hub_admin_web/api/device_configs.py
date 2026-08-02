"""Server-side device_config.json files — edit in the UI, apply to a hub.

A device config groups devices by section (`onvifcams`, `magos`, ...). On
apply, each section is matched to an integration on the destination hub by
case-insensitive substring (same heuristic as the CLI's
``DeviceResource.find_matching_section``, reversed) and its devices are added
with the usual schema filtering.
"""

import json
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hub_admin.resources.devices import DeviceResource
from hub_admin.resources.integrations import IntegrationResource
from hub_admin_web.connections import manager
from hub_admin_web.jobs import jobs

router = APIRouter(tags=["device-configs"])

# device_configs.py -> api -> hub_admin_web -> src -> backend -> hub-admin-web/device-configs
_DEFAULT_DIR = Path(__file__).resolve().parents[4] / "device-configs"
DEVICE_CONFIGS_DIR = Path(
    os.environ.get("HUB_ADMIN_DEVICE_CONFIGS_DIR", _DEFAULT_DIR)
)


def _validate_name(name: str) -> None:
    if "/" in name or "\\" in name or name.startswith(".") or not name.endswith(".json"):
        raise HTTPException(400, f"invalid config name: {name}")


class SaveDeviceConfigRequest(BaseModel):
    config: dict


class ApplyDeviceConfigRequest(BaseModel):
    config: dict
    sections: list[str] | None = None  # None = all sections


@router.get("/device-configs")
def list_device_configs() -> list[dict]:
    if not DEVICE_CONFIGS_DIR.is_dir():
        return []
    out: list[dict] = []
    for path in sorted(DEVICE_CONFIGS_DIR.glob("*.json")):
        entry: dict = {"file_name": path.name}
        try:
            config = json.loads(path.read_text())
            entry["sections"] = {
                section: len(devices) if isinstance(devices, dict) else 0
                for section, devices in config.items()
            }
        except (json.JSONDecodeError, AttributeError) as e:
            entry["error"] = f"unreadable config: {e}"
        out.append(entry)
    return out


@router.get("/device-configs/{name}")
def get_device_config(name: str) -> dict:
    _validate_name(name)
    path = DEVICE_CONFIGS_DIR / name
    if not path.is_file():
        raise HTTPException(404, f"config '{name}' not found")
    try:
        config = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise HTTPException(422, f"config '{name}' is not valid JSON: {e}")
    return {"file_name": name, "config": config}


@router.put("/device-configs/{name}")
def save_device_config(name: str, body: SaveDeviceConfigRequest) -> dict:
    """Write a config file (creates it if new)."""
    _validate_name(name)
    DEVICE_CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    path = DEVICE_CONFIGS_DIR / name
    path.write_text(json.dumps(body.config, indent=2, ensure_ascii=False) + "\n")
    return {"file_name": name, "saved": True}


@router.post("/hubs/{context}/device-config/apply")
def apply_device_config(context: str, body: ApplyDeviceConfigRequest) -> dict:
    """Add each section's devices to the matching integration, as a job.

    Section -> integration matching is by case-insensitive substring against
    the integration name. Unmatched sections are reported, not fatal.
    """
    conn = manager.get(context)
    sections = body.sections or list(body.config.keys())
    config = body.config

    def match_integration(section: str, integrations):
        section_lower = section.lower()
        for integration in integrations:
            name_lower = integration.name.lower()
            if section_lower in name_lower or name_lower in section_lower:
                return integration
        return None

    def run():
        integrations = IntegrationResource(conn.channel).list()
        devices_res = DeviceResource(conn.channel)
        applied = []
        for section in sections:
            entry: dict = {"section": section}
            devices = config.get(section)
            if not isinstance(devices, dict):
                entry["error"] = "section missing or not an object"
                applied.append(entry)
                continue
            integration = match_integration(section, integrations)
            if integration is None:
                entry["error"] = "no matching integration on the hub"
                applied.append(entry)
                continue
            entry["integration_name"] = integration.name
            entry["integration_id"] = integration.id
            allowed = devices_res.allowed_keys_from_schema(
                integration.device_setup_info_schema
            )
            dropped: dict[str, list[str]] = {}
            created = devices_res.add_devices(
                integration.id,
                devices,
                allowed_keys=allowed,
                on_drop=lambda n, k, _d=dropped: _d.__setitem__(n, k),
            )
            entry["created"] = [
                {"name": n, "device_id": d} for n, d in created
            ]
            entry["dropped"] = dropped
            applied.append(entry)
        return {"target_context": context, "applied": applied}

    job = jobs.submit("device-config-apply", run)
    return {"job_id": job.id}
