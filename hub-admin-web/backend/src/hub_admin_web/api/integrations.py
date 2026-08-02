from dataclasses import asdict

from fastapi import APIRouter
from pydantic import BaseModel

from hub_admin.resources.integrations import IntegrationResource
from hub_admin_web.connections import manager

router = APIRouter(tags=["integrations"])


class CreateIntegrationRequest(BaseModel):
    manifest_id: str
    config: dict | None = None


@router.get("/hubs/{context}/integrations")
def list_integrations(context: str) -> list[dict]:
    conn = manager.get(context)
    return [asdict(i) for i in IntegrationResource(conn.channel).list()]


@router.post("/hubs/{context}/integrations")
def create_integration(context: str, body: CreateIntegrationRequest) -> dict:
    conn = manager.get(context)
    integration_id = IntegrationResource(conn.channel).create(
        body.manifest_id, body.config
    )
    return {"integration_id": integration_id}


@router.get("/hubs/{context}/integrations/export")
def export_integrations(context: str) -> dict:
    """Dump all integrations + devices into device_config.json shape."""
    conn = manager.get(context)
    config, summary = IntegrationResource(conn.channel).export()
    return {"config": config, "summary": [asdict(s) for s in summary]}
