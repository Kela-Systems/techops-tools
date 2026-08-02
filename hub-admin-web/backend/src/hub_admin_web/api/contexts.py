import os
import re
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hub_admin_web.connections import manager
from hub_admin_web.jobs import jobs

router = APIRouter(tags=["contexts"])

# contexts.py -> api -> hub_admin_web -> src -> backend -> hub-admin-web/scripts
_DEFAULT_SCRIPT = (
    Path(__file__).resolve().parents[4] / "scripts" / "k3s_kubeconfig.sh"
)
K3S_SCRIPT = Path(os.environ.get("HUB_ADMIN_K3S_SCRIPT", _DEFAULT_SCRIPT))

# Hostnames / usernames / context names — safe charset, no shell metacharacters
_ARG_RE = re.compile(r"^[A-Za-z0-9._@-]+$")


class AddContextRequest(BaseModel):
    host: str
    user: str = "kela"
    context_name: str | None = None


@router.get("/contexts")
def list_contexts() -> dict:
    return {"contexts": manager.list_contexts()}


@router.post("/contexts")
def add_context(body: AddContextRequest) -> dict:
    """Register a kubectl context for a k3s site via scripts/k3s_kubeconfig.sh.

    SSHes into the site, fetches its k3s.yaml, and writes the context into
    this server's kubeconfig. Runs as a background job; poll /api/jobs/{id}.
    """
    for field, value in (("host", body.host), ("user", body.user)):
        if not _ARG_RE.match(value):
            raise HTTPException(400, f"invalid {field}: {value!r}")
    if body.context_name and not _ARG_RE.match(body.context_name):
        raise HTTPException(400, f"invalid context_name: {body.context_name!r}")
    if not K3S_SCRIPT.is_file():
        raise HTTPException(500, f"k3s script not found at {K3S_SCRIPT}")

    context_name = body.context_name or body.host
    if context_name in manager.list_contexts():
        raise HTTPException(409, f"context '{context_name}' already exists")

    cmd = ["bash", str(K3S_SCRIPT), "--user", body.user, "--host", body.host]
    if body.context_name:
        cmd += ["--context-name", body.context_name]

    def run():
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180
        )
        output = (proc.stdout + proc.stderr).strip()
        if proc.returncode != 0:
            raise RuntimeError(output or f"script exited {proc.returncode}")
        return {"context": context_name, "output": output}

    job = jobs.submit("add-context", run)
    return {"job_id": job.id}
