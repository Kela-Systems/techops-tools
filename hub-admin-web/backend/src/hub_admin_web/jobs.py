"""In-memory background job store — used for slow operations (profile apply)."""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

_JOB_TTL_S = 3600.0


@dataclass
class Job:
    id: str
    kind: str
    status: str = "running"  # running | done | failed
    result: Any = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)


class JobStore:
    def __init__(self, max_workers: int = 2):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="job"
        )

    def submit(self, kind: str, fn: Callable[[], Any]) -> Job:
        job = Job(id=uuid.uuid4().hex, kind=kind)
        with self._lock:
            self._prune()
            self._jobs[job.id] = job

        def run():
            try:
                job.result = fn()
                job.status = "done"
            except Exception as e:  # surfaced to the client via polling
                job.error = str(e)
                job.status = "failed"

        self._executor.submit(run)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _prune(self) -> None:
        cutoff = time.time() - _JOB_TTL_S
        for jid, job in list(self._jobs.items()):
            if job.status != "running" and job.created_at < cutoff:
                del self._jobs[jid]

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


jobs = JobStore()
