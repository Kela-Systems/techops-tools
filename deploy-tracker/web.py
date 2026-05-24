#!/usr/bin/env python3
"""Deploy Tracker – Web UI backed by FastAPI."""
from __future__ import annotations

import asyncio
import json
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from deploy_tracker import (
    DeploymentSession,
    BreakRecord,
    complete_step,
    export_to_gsheet,
    find_step_by_num,
    generate_report,
    load_config,
    merge_manifest,
    save_report,
)

BASE_DIR = Path(__file__).parent

app = FastAPI(title="Deploy Tracker")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# Single in-memory session (local single-user tool)
session: DeploymentSession | None = None
session_report: dict | None = None


# ── Pydantic models ──────────────────────────────────────────────────────────

class StartRequest(BaseModel):
    manifest: str
    deployer: str = "Unknown"
    dry_run: bool = False


class NoteBody(BaseModel):
    note: str = ""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _step_to_dict(s) -> dict:
    now = datetime.now(timezone.utc)
    elapsed = 0.0
    if s.started_at and not s.finished_at:
        elapsed = (now - s.started_at).total_seconds()
    elif s.started_at and s.finished_at:
        elapsed = (s.finished_at - s.started_at).total_seconds()
    return {
        "menu_num": s.menu_num,
        "device_key": s.device_key,
        "step_id": s.step_id,
        "name": s.name,
        "expected_minutes": s.expected_minutes,
        "actual_minutes": round(s.actual_minutes, 2),
        "status": s.status,
        "notes": s.notes,
        "elapsed_seconds": round(elapsed, 1),
    }


def _session_state() -> dict:
    if session is None:
        return {"active": False}

    now = datetime.now(timezone.utc)
    wall_clock_sec = 0.0
    if session.started_at:
        end = session.finished_at or now
        wall_clock_sec = (end - session.started_at).total_seconds()

    on_break = session.active_break is not None
    break_elapsed = 0.0
    if session.active_break:
        break_elapsed = (now - session.active_break.started_at).total_seconds()

    devices = {}
    for dev_key, dev in session.plan["devices"].items():
        dev_steps = [s for s in session.steps if s.device_key == dev_key]
        devices[dev_key] = {
            "name": dev["name"],
            "type": dev.get("type"),
            "steps": [_step_to_dict(s) for s in dev_steps],
        }

    return {
        "active": True,
        "finished": session.finished_at is not None,
        "deployment_name": session.deployment_info.get("name", ""),
        "version": session.deployment_info.get("version", ""),
        "deployer": session.deployer,
        "dry_run": session.dry_run,
        "wall_clock_seconds": round(wall_clock_sec, 1),
        "active_minutes": round(session.active_wall_clock_minutes, 2),
        "steps_total_minutes": round(session.sum_of_steps_minutes, 2),
        "break_minutes": round(session.total_break_minutes, 2),
        "overlap_minutes": round(session.parallel_overlap_minutes, 2),
        "expected_total_minutes": round(session.total_expected, 2),
        "completed_count": session.completed_count,
        "total_steps": len(session.steps),
        "on_break": on_break,
        "break_elapsed_seconds": round(break_elapsed, 1),
        "active_step_nums": [s.menu_num for s in session.active_steps],
        "devices": devices,
        "breaks": [
            {
                "duration_minutes": round(b.duration_minutes, 2),
                "note": b.note,
            }
            for b in session.breaks
        ],
    }


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(str(BASE_DIR / "static" / "index.html"))


@app.get("/api/manifests")
async def list_manifests():
    deploy_dir = BASE_DIR / "deployments"
    if not deploy_dir.exists():
        return {"manifests": []}
    files = sorted(p.name for p in deploy_dir.glob("*.yaml") if p.name != "example.yaml")
    return {"manifests": files}


@app.post("/api/session/start")
async def start_session(req: StartRequest):
    global session, session_report
    manifest_path = str(BASE_DIR / "deployments" / req.manifest)
    plan = merge_manifest(manifest_path)
    session = DeploymentSession(plan, req.deployer, req.dry_run)
    session.started_at = datetime.now(timezone.utc)
    session_report = None
    return _session_state()


@app.get("/api/session/state")
async def get_state():
    return _session_state()


@app.post("/api/session/step/{num}/start")
async def start_step(num: int):
    if session is None:
        return {"error": "No active session"}
    target = find_step_by_num(session, num)
    if not target:
        return {"error": "Invalid step number"}
    if target.status != "pending":
        return {"error": f"Step already {target.status}"}
    target.status = "in_progress"
    target.started_at = datetime.now(timezone.utc)
    session.active_steps.append(target)
    return {"ok": True}


@app.post("/api/session/step/{num}/complete")
async def complete_step_endpoint(num: int, body: NoteBody):
    if session is None:
        return {"error": "No active session"}
    target = find_step_by_num(session, num)
    if not target or target.status != "in_progress":
        return {"error": "Step not in progress"}
    complete_step(session, target, body.note)
    return {"ok": True}


@app.post("/api/session/step/{num}/skip")
async def skip_step(num: int, body: NoteBody):
    if session is None:
        return {"error": "No active session"}
    target = find_step_by_num(session, num)
    if not target or target.status != "pending":
        return {"error": "Step not pending"}
    target.status = "skipped"
    target.notes = body.note or "skipped"
    return {"ok": True}


@app.post("/api/session/break/toggle")
async def toggle_break(body: NoteBody):
    if session is None:
        return {"error": "No active session"}
    if session.active_break:
        brk = session.active_break
        brk.ended_at = datetime.now(timezone.utc)
        brk.note = body.note
        session.breaks.append(brk)
        session.active_break = None
        return {"on_break": False, "duration_minutes": round(brk.duration_minutes, 2)}
    else:
        session.active_break = BreakRecord()
        return {"on_break": True}


@app.post("/api/session/finish")
async def finish_session():
    global session_report
    if session is None:
        return {"error": "No active session"}

    if session.active_break:
        brk = session.active_break
        brk.ended_at = datetime.now(timezone.utc)
        brk.note = brk.note or "ended with deployment"
        session.breaks.append(brk)
        session.active_break = None

    for ss in list(session.active_steps):
        complete_step(session, ss)

    session.finished_at = datetime.now(timezone.utc)
    report = generate_report(session)
    filepath = save_report(report, session.dry_run)

    gsheet_url = None
    if not session.dry_run:
        config = load_config()
        if config.get("google_sheets"):
            try:
                export_to_gsheet(report, config)
                gsheet_url = (
                    "https://docs.google.com/spreadsheets/d/"
                    + config["google_sheets"]["sheet_id"]
                )
            except Exception as e:
                print(f"Google Sheets export failed: {e}")

    session_report = report
    return {
        "report": report,
        "filepath": filepath,
        "gsheet_url": gsheet_url,
    }


@app.get("/api/session/report")
async def get_report():
    if session_report is None:
        return {"error": "No report available"}
    return session_report


# ── WebSocket ────────────────────────────────────────────────────────────────

@app.websocket("/ws/session")
async def ws_session(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            state = _session_state()
            await websocket.send_json(state)
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


# ── Entrypoint ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Starting Deploy Tracker Web UI at http://localhost:8000")
    webbrowser.open("http://localhost:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
