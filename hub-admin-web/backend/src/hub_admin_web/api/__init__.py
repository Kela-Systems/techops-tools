"""REST API routers — one module per command group, mirroring the CLI layout."""

from fastapi import APIRouter

from hub_admin_web.api import (
    contexts,
    device_configs,
    devices,
    integrations,
    links,
    manifests,
    profiles,
    server,
)

router = APIRouter(prefix="/api")
router.include_router(contexts.router)
router.include_router(manifests.router)
router.include_router(integrations.router)
router.include_router(devices.router)
router.include_router(device_configs.router)
router.include_router(links.router)
router.include_router(profiles.router)
router.include_router(server.router)
