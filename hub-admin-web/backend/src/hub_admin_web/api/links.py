from dataclasses import asdict

from fastapi import APIRouter
from pydantic import BaseModel

from hub_admin.resources.links import LinkResource
from hub_admin_web.connections import manager

router = APIRouter(tags=["links"])


class CreateLinkRequest(BaseModel):
    source_id: str
    target_id: str


@router.get("/hubs/{context}/assets")
def list_assets(context: str) -> list[dict]:
    conn = manager.get(context)
    return [asdict(a) for a in LinkResource(conn.channel).list_assets()]


@router.get("/hubs/{context}/links")
def list_links(context: str) -> list[dict]:
    conn = manager.get(context)
    links = LinkResource(conn.channel)
    assets = links.list_assets()
    return [asdict(link) for link in links.list_links(assets)]


@router.post("/hubs/{context}/links")
def create_link(context: str, body: CreateLinkRequest) -> dict:
    """Add to SiteConfig.available_links. Needs a hub restart to take effect."""
    conn = manager.get(context)
    created = LinkResource(conn.channel).add_available_link(
        body.source_id, body.target_id
    )
    return {"created": created}
