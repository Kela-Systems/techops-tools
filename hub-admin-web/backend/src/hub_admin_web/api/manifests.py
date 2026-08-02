from dataclasses import asdict

from fastapi import APIRouter

from hub_admin.resources.manifests import ManifestResource
from hub_admin_web.connections import manager

router = APIRouter(tags=["manifests"])


@router.get("/hubs/{context}/manifests")
def list_manifests(context: str) -> list[dict]:
    conn = manager.get(context)
    return [asdict(m) for m in ManifestResource(conn.channel).list()]


@router.get("/hubs/{context}/manifests/{manifest_id}/schemas")
def get_schemas(context: str, manifest_id: str) -> dict:
    conn = manager.get(context)
    int_schema, dev_schema = ManifestResource(conn.channel).get_schemas(manifest_id)
    return {
        "integration_config_schema": int_schema,
        "device_setup_info_schema": dev_schema,
    }
