#!/usr/bin/env python3
"""Station-side shipping to the central collector (TEC-573).

Two halves, both hanging off the per-tool `logs/` directory:

* the spools — `spool_run_record()`, called by the one shared JSON writer
  (`bench_ui.save_run_record`) right after a run completes, and
  `spool_label_record()` (TEC-845) beside it. Each drops a copy into its own
  queue directory, atomically, and neither ever raises: a bench must keep
  configuring devices whether or not the collector (or the disk) is having a
  bad day. Raw device payloads never reach a queue — they are a file-only
  `extra` of the local per-run JSON, not part of the record.

* `CentralUploader` — a daemon thread (one per tool process) that drains those
  queues oldest-first: POST each entry to its endpoint over the tailnet,
  delete on acceptance, back off exponentially while the collector is
  unreachable. Entries the collector permanently rejects (4xx) are set aside
  as `*.json.bad` instead of poison-pilling the queue forever.

Two queues rather than one because the two records have nothing to do with
each other once they leave: a run record is the story of one visit, a device
label is a durable fact about a device (see `label_record.py` for why they are
kept apart). One thread drains both — the shipping machinery is identical and
a second thread per tool would buy nothing.

Enabled per station by setting BENCH_CENTRAL_URL (the collector's base URL,
e.g. "http://techops-automations-host:8100") before launching — same pattern
as BENCH_STATION_ID. When unset, nothing is spooled and no thread starts.

The queues are deliberately NOT pruned: entries are a few KB each (no raw
payloads) and only accumulate while the bench is offline, then drain. Losing
them would defeat the point (TEC-347: stop evaporating the audit trail).

Upload contract (consumed by the collector, TEC-574):
    POST {BENCH_CENTRAL_URL}/api/v1/runs           body = one run record
    POST {BENCH_CENTRAL_URL}/api/v1/device-labels  body = one device label
    2xx  accepted            -> queue entry deleted
    409  duplicate run_id    -> already ingested, treated as accepted
    4xx  permanently invalid -> entry quarantined as *.json.bad
    5xx / unreachable        -> kept, retried with backoff
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

OUTBOX_DIRNAME = "outbox"
LABEL_OUTBOX_DIRNAME = "outbox-labels"
RUNS_ENDPOINT = "/api/v1/runs"
LABELS_ENDPOINT = "/api/v1/device-labels"
REQUEST_TIMEOUT_SEC = 10
POLL_INTERVAL_SEC = 20.0
BACKOFF_MAX_SEC = 900.0

# Anything in a serial that has no business in a filename.
_QUEUE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def central_url() -> str:
    """The collector's base URL from BENCH_CENTRAL_URL, normalized (no
    trailing slash), or '' when central shipping is off on this station."""
    return os.environ.get("BENCH_CENTRAL_URL", "").strip().rstrip("/")


def outbox_queues(log_dir: Path, url: str) -> tuple[tuple[Path, str], ...]:
    """The (queue directory, endpoint) pairs one tool's uploader drains.

    Run records first: a device label cross-references the run it was captured
    during, so shipping the run first means that reference resolves as soon as
    the label lands.
    """
    return ((log_dir / OUTBOX_DIRNAME, url + RUNS_ENDPOINT),
            (log_dir / LABEL_OUTBOX_DIRNAME, url + LABELS_ENDPOINT))


def _spool(queue_dir: Path, name: str, payload: dict) -> None:
    """Write one queue entry, atomically (tmp + replace) so the uploader thread
    never reads a torn file.

    Best-effort by design — any failure (disk full, permissions, a
    non-serializable payload) is swallowed so the run itself never blocks or
    fails on account of shipping.
    """
    with contextlib.suppress(OSError, TypeError, ValueError):
        queue_dir.mkdir(exist_ok=True)
        tmp = queue_dir / (name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, queue_dir / name)


def _stamp(when: Optional[str]) -> str:
    """`when` (an ISO-8601 string) as a sortable filename prefix, so a plain
    sort of a queue directory is oldest-first. Falls back to now."""
    ts = None
    with contextlib.suppress(TypeError, ValueError):
        ts = datetime.fromisoformat(when or "")
    return (ts or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")


def spool_run_record(log_dir: Path, entry: dict) -> None:
    """Queue one run record under `log_dir/outbox/` for central upload."""
    if not central_url():
        return
    _spool(log_dir / OUTBOX_DIRNAME,
           f"{_stamp(entry.get('timestamp'))}_{entry.get('run_id') or 'no-id'}.json",
           entry)


def spool_label_record(log_dir: Path, record: dict) -> None:
    """Queue one device-label record under `log_dir/outbox-labels/` (TEC-845).

    Named after the serial alone — no timestamp — because the serial is the key
    the collector stores it under, so a queue entry IS the current reading for
    that device. A unit read twice before the queue drains (a re-scan after a
    mistake, a retried run) replaces its own entry instead of queueing a second
    one behind it, and the value that reaches central is the last one the
    operator actually saw.
    """
    if not central_url():
        return
    _spool(log_dir / LABEL_OUTBOX_DIRNAME,
           _QUEUE_NAME_RE.sub("-", record.get("serial") or "unknown") + ".json",
           record)


class CentralUploader:
    """Drains one tool's upload queues to the collector from a background
    thread.

    `drain_once()` holds all the logic and runs synchronously (that's what the
    tests drive); `start()` wraps it in a daemon thread with the retry/backoff
    cadence. Unreachability is logged once on the way down and once on
    recovery, not every cycle — an offline bench would otherwise fill its
    rolling log with the same warning for hours."""

    def __init__(self, log_dir: Path, url: str,
                 logger: Optional[logging.Logger] = None, *,
                 poll_sec: float = POLL_INTERVAL_SEC,
                 backoff_max_sec: float = BACKOFF_MAX_SEC) -> None:
        self.log_dir = log_dir
        self.queues = outbox_queues(log_dir, url)
        self.log = logger or logging.getLogger("bench-central")
        self.poll_sec = poll_sec
        self.backoff_max_sec = backoff_max_sec
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._unreachable = False

    # ── the drain (synchronous, fully testable) ──────────────────────────────

    def drain_once(self) -> bool:
        """Upload every queued entry, oldest first, queue by queue. Returns
        True when they all ended up empty (drained, or nothing to send), False
        when the collector is unreachable/unhealthy and a backoff should apply.

        One unreachable collector stops the whole pass rather than only the
        queue that hit it: the next queue is the same host and would fail the
        same way, one timeout at a time.
        """
        for queue_dir, endpoint in self.queues:
            if not self._drain_queue(queue_dir, endpoint):
                return False
        return True

    def _drain_queue(self, queue_dir: Path, endpoint: str) -> bool:
        if not queue_dir.is_dir():
            return True
        for path in sorted(queue_dir.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._quarantine(path, "unreadable JSON")
                continue
            try:
                resp = requests.post(endpoint, json=record,
                                     timeout=REQUEST_TIMEOUT_SEC)
            except requests.RequestException as e:
                self._note_unreachable(str(e))
                return False
            if resp.status_code < 300 or resp.status_code == 409:
                # accepted (409 = the collector already has this run_id)
                self._note_reachable()
                with contextlib.suppress(OSError):
                    path.unlink()
            elif 400 <= resp.status_code < 500:
                self._note_reachable()
                self._quarantine(path, f"rejected by the collector "
                                       f"(HTTP {resp.status_code})")
            else:  # 5xx — collector unhealthy; keep the record, back off
                self._note_unreachable(f"HTTP {resp.status_code}")
                return False
        return True

    def _quarantine(self, path: Path, reason: str) -> None:
        """Set a record aside as *.json.bad so it stops blocking the queue but
        stays on disk for a human to look at."""
        self.log.warning("Central upload: setting %s aside (%s).",
                         path.name, reason)
        with contextlib.suppress(OSError):
            os.replace(path, path.with_name(path.name + ".bad"))

    def _note_unreachable(self, detail: str) -> None:
        if not self._unreachable:
            self._unreachable = True
            self.log.warning("Central collector unreachable (%s) — records "
                             "will queue on disk and retry.", detail)

    def _note_reachable(self) -> None:
        if self._unreachable:
            self._unreachable = False
            self.log.info("Central collector reachable again — draining the queues.")

    # ── the thread ────────────────────────────────────────────────────────────

    def start(self) -> "CentralUploader":
        self._thread = threading.Thread(target=self._loop,
                                        name="central-uploader", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=REQUEST_TIMEOUT_SEC + 5)

    def _loop(self) -> None:
        delay = self.poll_sec
        while not self._stop.wait(delay):
            try:
                drained = self.drain_once()
            except Exception:  # noqa: BLE001 — the uploader must never die
                self.log.exception("Central uploader error — retrying later.")
                drained = False
            delay = (self.poll_sec if drained
                     else min(max(delay, self.poll_sec) * 2, self.backoff_max_sec))


def start_central_uploader(log_dir: Path,
                           logger: Optional[logging.Logger] = None,
                           ) -> Optional[CentralUploader]:
    """Start this tool's queue uploader if central shipping is configured
    (BENCH_CENTRAL_URL). Returns the running uploader, or None when off."""
    url = central_url()
    if not url:
        return None
    uploader = CentralUploader(log_dir, url, logger)
    uploader.log.info("Central shipping enabled — records upload to %s.", url)
    return uploader.start()
