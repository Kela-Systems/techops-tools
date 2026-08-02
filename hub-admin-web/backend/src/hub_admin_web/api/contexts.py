from fastapi import APIRouter

from hub_admin_web.connections import manager

router = APIRouter(tags=["contexts"])


@router.get("/contexts")
def list_contexts() -> dict:
    return {"contexts": manager.list_contexts()}
