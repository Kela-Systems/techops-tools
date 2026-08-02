import json
import os
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hub_admin.resources.profile import ProfileResource
from hub_admin_web.connections import manager
from hub_admin_web.jobs import jobs

router = APIRouter(tags=["profiles"])

# Saved profile bundles kept server-side, applied straight from the UI.
# profiles.py -> api -> hub_admin_web -> src -> backend -> hub-admin-web/profiles
_DEFAULT_PROFILES_DIR = Path(__file__).resolve().parents[4] / "profiles"
PROFILES_DIR = Path(os.environ.get("HUB_ADMIN_PROFILES_DIR", _DEFAULT_PROFILES_DIR))


def _profile_path(name: str) -> Path:
    """Resolve a saved-profile filename, rejecting path traversal."""
    if "/" in name or "\\" in name or name.startswith(".") or not name.endswith(".json"):
        raise HTTPException(400, f"invalid profile name: {name}")
    path = PROFILES_DIR / name
    if not path.is_file():
        raise HTTPException(404, f"profile '{name}' not found")
    return path


@router.get("/profiles")
def list_saved_profiles() -> list[dict]:
    """List the profile bundles in the server-side profiles folder."""
    if not PROFILES_DIR.is_dir():
        return []
    out: list[dict] = []
    for path in sorted(PROFILES_DIR.glob("*.json")):
        entry: dict = {"file_name": path.name}
        try:
            bundle = json.loads(path.read_text())
            summary = ProfileResource.summarize(bundle)
            entry.update(
                source_context=summary.source_context,
                exported_at=bundle.get("exported_at"),
                integration_count=len(summary.integrations),
                device_count=sum(i.device_count for i in summary.integrations),
                has_site_config=bool(summary.site_config_keys),
            )
        except (json.JSONDecodeError, AttributeError, TypeError) as e:
            entry["error"] = f"unreadable bundle: {e}"
        out.append(entry)
    return out


@router.get("/profiles/{name}")
def get_saved_profile(name: str) -> dict:
    """Return a saved profile's bundle + offline summary."""
    path = _profile_path(name)
    try:
        bundle = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise HTTPException(422, f"profile '{name}' is not valid JSON: {e}")
    return {
        "file_name": name,
        "bundle": bundle,
        "summary": asdict(ProfileResource.summarize(bundle)),
    }


class ExportProfileRequest(BaseModel):
    include_site_config: bool = True


class InspectProfileRequest(BaseModel):
    bundle: dict


class ApplyProfileRequest(BaseModel):
    bundle: dict
    include_site_config: bool = True


@router.post("/hubs/{context}/profile/export")
def export_profile(context: str, body: ExportProfileRequest) -> dict:
    conn = manager.get(context)
    bundle, summary = ProfileResource(conn.channel).export(
        source_context=context, include_site_config=body.include_site_config
    )
    return {"bundle": bundle, "summary": asdict(summary)}


@router.post("/profile/inspect")
def inspect_profile(body: InspectProfileRequest) -> dict:
    """Offline summary of an uploaded profile bundle — no hub connection."""
    return asdict(ProfileResource.summarize(body.bundle))


@router.post("/hubs/{context}/profile/apply")
def apply_profile(context: str, body: ApplyProfileRequest) -> dict:
    """Deploy a profile onto a hub as a background job; poll /api/jobs/{id}."""
    conn = manager.get(context)
    bundle = body.bundle
    include_site_config = body.include_site_config

    def run():
        report = ProfileResource(conn.channel).apply(
            bundle, target_context=context, include_site_config=include_site_config
        )
        return asdict(report)

    job = jobs.submit("profile-apply", run)
    return {"job_id": job.id}


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "result": job.result,
        "error": job.error,
    }
