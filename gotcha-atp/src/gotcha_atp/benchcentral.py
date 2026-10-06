"""bench-central over the tailnet: GET per serial (S4.9), POST the run record.

Same contract as the bench stations' uploader (bench-central/collector.py):
    POST /api/v1/runs                 201 stored · 409 duplicate run_id (= stored) · 4xx rejected
    GET  /api/v1/runs?serial=&kind=   summaries, newest first
    GET  /api/v1/device-labels/{device}
    GET  /api/v1/health

A record that cannot be delivered is queued in runs/outbox/ and retried on the
next start (or "Retry upload" on Result); a 4xx-rejected one is set aside as
*.json.bad so it never blocks the queue.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

from .record import RUNS_DIR

TIMEOUT_S = 10
OUTBOX = RUNS_DIR / "outbox"


class BenchCentral:
    def __init__(self, url: str, outbox: Path = OUTBOX) -> None:
        self.url = url.rstrip("/")
        self.outbox = outbox

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def health(self) -> bool:
        if not self.enabled:
            return False
        try:
            return httpx.get(f"{self.url}/api/v1/health", timeout=5).status_code == 200
        except httpx.HTTPError:
            return False

    def runs_for_serial(self, serial: str, kind: str = "configure", limit: int = 20) -> list[dict]:
        r = httpx.get(f"{self.url}/api/v1/runs",
                      params={"serial": serial, "kind": kind, "limit": limit}, timeout=TIMEOUT_S)
        r.raise_for_status()
        return r.json().get("runs") or []

    def device_label(self, device: str) -> Optional[dict]:
        r = httpx.get(f"{self.url}/api/v1/device-labels/{device}", timeout=TIMEOUT_S)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    # ── upload ───────────────────────────────────────────────────────────────

    def _post(self, entry: dict) -> dict:
        try:
            r = httpx.post(f"{self.url}/api/v1/runs", json=entry, timeout=TIMEOUT_S)
        except httpx.HTTPError as e:
            return {"status": "unreachable", "detail": f"{type(e).__name__}: {e}"}
        if r.status_code < 300:
            return {"status": "uploaded", "detail": f"stored as {entry['run_id']}"}
        if r.status_code == 409:
            return {"status": "uploaded", "detail": f"already stored ({entry['run_id']})"}
        if 400 <= r.status_code < 500:
            return {"status": "rejected", "detail": f"HTTP {r.status_code}: {r.text[:200]}"}
        return {"status": "unreachable", "detail": f"HTTP {r.status_code}"}

    def post_run(self, entry: dict) -> dict:
        if not self.enabled:
            path = self._queue(entry)
            return {"status": "queued", "detail": f"bench-central URL not set — queued in {path}"}
        res = self._post(entry)
        if res["status"] == "unreachable":
            path = self._queue(entry)
            res = {"status": "queued", "detail": f"{res['detail']} — queued in {path}"}
        res["run_id"] = entry["run_id"]
        return res

    def _queue(self, entry: dict) -> Path:
        self.outbox.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromisoformat(entry["timestamp"]).strftime("%Y%m%d-%H%M%S")
        path = self.outbox / f"{stamp}_{entry['run_id']}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def pending(self) -> int:
        return len(list(self.outbox.glob("*.json"))) if self.outbox.is_dir() else 0

    def drain(self) -> list[dict]:
        """Upload every queued record, oldest first; stop at the first
        unreachable so a dead link costs one timeout, not one per record."""
        out: list[dict] = []
        if not self.enabled or not self.outbox.is_dir():
            return out
        for path in sorted(self.outbox.glob("*.json")):
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                os.replace(path, path.with_name(path.name + ".bad"))
                continue
            res = self._post(entry)
            res["run_id"] = entry.get("run_id")
            out.append(res)
            if res["status"] == "uploaded":
                path.unlink(missing_ok=True)
            elif res["status"] == "rejected":
                os.replace(path, path.with_name(path.name + ".bad"))
            else:
                break
        return out
