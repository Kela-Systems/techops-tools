from fastapi import APIRouter, HTTPException

from hub_admin.resources.server import restart_hub_server
from hub_admin_web.connections import NAMESPACE, manager

router = APIRouter(tags=["server"])


@router.post("/hubs/{context}/restart")
def restart(context: str) -> dict:
    success, message = restart_hub_server(context, NAMESPACE)
    # The pod behind our port-forward is gone; force a fresh connection next time.
    manager.invalidate(context)
    if not success:
        raise HTTPException(502, message)
    return {"message": message}
