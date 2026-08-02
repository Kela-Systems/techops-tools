from dataclasses import asdict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hub_admin.resources.devices import DeviceResource
from hub_admin.resources.integrations import IntegrationResource
from hub_admin_web.connections import manager

router = APIRouter(tags=["devices"])


class CreateDeviceRequest(BaseModel):
    name: str
    setup_info: dict = {}


class UpdateDeviceRequest(BaseModel):
    """RFC 7396 merge-patch for setup_info and/or a rename — both optional."""

    setup_info_patch: dict | None = None
    name: str | None = None


class ImportDevicesRequest(BaseModel):
    """A device_config.json section (uploaded and parsed client-side)."""

    integration_id: str
    devices: dict


@router.get("/hubs/{context}/integrations/{integration_id}/devices")
def list_devices(context: str, integration_id: str) -> list[dict]:
    conn = manager.get(context)
    return [asdict(d) for d in DeviceResource(conn.channel).list(integration_id)]


@router.post("/hubs/{context}/integrations/{integration_id}/devices")
def create_device(
    context: str, integration_id: str, body: CreateDeviceRequest
) -> dict:
    conn = manager.get(context)
    device_id = DeviceResource(conn.channel).create(
        integration_id, body.name, body.setup_info
    )
    return {"device_id": device_id}


@router.patch("/hubs/{context}/integrations/{integration_id}/devices/{device_id}")
def update_device(
    context: str, integration_id: str, device_id: str, body: UpdateDeviceRequest
) -> dict:
    if body.setup_info_patch is None and body.name is None:
        raise HTTPException(400, "provide setup_info_patch and/or name")
    conn = manager.get(context)
    devices = DeviceResource(conn.channel)
    if body.setup_info_patch is not None:
        devices.update_setup_info(integration_id, device_id, body.setup_info_patch)
    if body.name is not None:
        devices.rename(integration_id, device_id, body.name)
    return {"ok": True}


@router.post("/hubs/{context}/devices/import")
def import_devices(context: str, body: ImportDevicesRequest) -> dict:
    """Add a batch of devices from a device_config section.

    setup_info fields not in the target integration's schema are dropped
    (same behaviour as `hub-admin devices add`) and reported per device.
    """
    conn = manager.get(context)
    integration = next(
        (
            i
            for i in IntegrationResource(conn.channel).list()
            if i.id == body.integration_id
        ),
        None,
    )
    if integration is None:
        raise HTTPException(404, f"integration {body.integration_id} not found")

    devices = DeviceResource(conn.channel)
    allowed = devices.allowed_keys_from_schema(integration.device_setup_info_schema)
    dropped: dict[str, list[str]] = {}
    created = devices.add_devices(
        body.integration_id,
        body.devices,
        allowed_keys=allowed,
        on_drop=lambda name, keys: dropped.__setitem__(name, keys),
    )
    return {
        "created": [{"name": name, "device_id": dev_id} for name, dev_id in created],
        "dropped": dropped,
    }
