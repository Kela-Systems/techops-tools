"""FastAPI app — REST API + static serving of the built frontend."""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path

import grpc
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from hub_admin_web.api import router
from hub_admin_web.connections import ConnectionError_, manager
from hub_admin_web.jobs import jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

# Built frontend (vite build output). Overridable for docker layouts.
# main.py -> hub_admin_web -> src -> backend -> hub-admin-web/frontend/dist
_DEFAULT_STATIC = Path(__file__).resolve().parents[3] / "frontend" / "dist"
STATIC_DIR = Path(os.environ.get("HUB_ADMIN_WEB_STATIC", _DEFAULT_STATIC))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    manager.close_all()
    jobs.shutdown()


app = FastAPI(title="hub-admin-web", lifespan=lifespan)
app.include_router(router)


@app.exception_handler(grpc.RpcError)
async def grpc_error_handler(request: Request, exc: grpc.RpcError):
    code = exc.code().name if hasattr(exc, "code") else "UNKNOWN"
    details = exc.details() if hasattr(exc, "details") else str(exc)
    return JSONResponse(
        status_code=502,
        content={"detail": f"hub RPC failed ({code}): {details}"},
    )


@app.exception_handler(ConnectionError_)
async def connection_error_handler(request: Request, exc: ConnectionError_):
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.exception_handler(KeyError)
async def key_error_handler(request: Request, exc: KeyError):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


if STATIC_DIR.is_dir():
    app.mount(
        "/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets"
    )

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        # SPA fallback: serve real files if they exist, index.html otherwise.
        candidate = STATIC_DIR / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")
