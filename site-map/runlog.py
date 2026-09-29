#!/usr/bin/env python3
"""A record of what one survey actually did, so a later "it went wrong" can be
answered from evidence rather than from memory.

Every remote command is recorded with its exit status, how long it took and
what it returned. That is the whole point: the failures this tool has had so
far were all *silent* ones - `hostname` returning rc 127 on BusyBox, an ssh
refused for a username rather than a key, a neighbour table full of k3s pods,
a host answering with the wrong MAC. None of those announce themselves, and
all of them are obvious in a transcript.

**Secrets are redacted before anything is written.** The passwords are held
in memory for the length of a survey and never reach a log line; every value
the resolver produced is scrubbed from every command and every output, so a
password that turns up inside a command string (a `--password` flag on an
inner tool, say) does not survive into the file.

One JSON file per run under `runs/`, newest name sorting last. They are
plain data: `sitemap.py runs` lists them and `sitemap.py runs --last` prints
one.
"""
from __future__ import annotations

import json
import os
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

RUNS_DIR = Path(__file__).resolve().parent / "runs"

# A single command's output, capped. The useful reads here are a neighbour
# table and a lease file - kilobytes - so this only ever truncates something
# unexpected, and says when it did.
MAX_OUTPUT = 20000
# How many runs to keep. A survey a minute for a day is 1440 files; there is
# no value in the ones from last month and they make `runs` unreadable.
KEEP = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def redact(text: str, secrets) -> str:
    """Replace every known secret with a marker, whatever it is embedded in."""
    if not text:
        return text
    for secret in secrets:
        if secret and len(secret) >= 3 and secret in text:
            text = text.replace(secret, "[redacted]")
    return text


@dataclass
class RunLog:
    """One survey. Append as it goes, `finish` to write."""

    request: dict = field(default_factory=dict)
    secrets: tuple = ()
    # A factory, not a bare default: a dataclass bakes a plain default into
    # the generated __init__ at class-creation time, so a test that points
    # RUNS_DIR at a tmp_path could not move it and the suite wrote a hundred
    # fixtures into the real runs/ - burying the actual survey logs, which is
    # the one thing this file exists to prevent.
    directory: Path = field(default_factory=lambda: RUNS_DIR)
    steps: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    started: str = field(default_factory=_now)
    _t0: float = field(default_factory=time.monotonic)

    # -- recording ------------------------------------------------------

    def command(self, where: str, command: str, *, rc: int | None = None,
                seconds: float | None = None, output: str | None = None,
                error: str | None = None) -> None:
        clean = redact(output or "", self.secrets)
        truncated = len(clean) > MAX_OUTPUT
        self.steps.append({
            "kind": "command",
            "at": _now(),
            "where": where,
            "command": redact(command, self.secrets),
            "rc": rc,
            "seconds": round(seconds, 3) if seconds is not None else None,
            "output": clean[:MAX_OUTPUT],
            "output_truncated": truncated,
            "output_bytes": len(clean),
            "error": redact(error or "", self.secrets) or None,
        })

    def step(self, what: str, **fields) -> None:
        self.steps.append({"kind": "step", "at": _now(), "what": what,
                           **{k: v for k, v in fields.items()}})

    def note(self, text: str) -> None:
        self.notes.append(redact(text, self.secrets))

    def timed(self, where: str, command: str):
        """Context manager that records a command and how long it took."""
        return _Timed(self, where, command)

    # -- writing --------------------------------------------------------

    def finish(self, *, site: dict | None = None, error: BaseException | None = None,
               warnings=(), answered: int | None = None,
               swept: int | None = None) -> Path | None:
        record = {
            "started": self.started,
            "finished": _now(),
            "seconds": round(time.monotonic() - self._t0, 3),
            "ok": error is None,
            "request": self.request,
            "swept": swept,
            "answered": answered,
            "warnings": [redact(str(w), self.secrets) for w in warnings],
            "notes": self.notes,
            "steps": self.steps,
        }
        if error is not None:
            record["error"] = {
                "type": type(error).__name__,
                "message": redact(str(error), self.secrets),
                "traceback": redact(
                    "".join(traceback.format_exception(
                        type(error), error, error.__traceback__)),
                    self.secrets),
            }
        if site:
            # A summary, not the payload: the payload is large, and what a
            # reader wants is which devices came out and with what evidence.
            record["result"] = {
                "site": site.get("name"),
                "subnet": site.get("subnet"),
                "coverage": {
                    claim: f"{v['proven']}/{v['total']}"
                    for claim, v in (site.get("coverage") or {}).items()
                    if isinstance(v, dict) and "proven" in v
                },
                "devices": [
                    {
                        "name": name,
                        "addr": node.get("addr"),
                        "mac": node.get("mac"),
                        "kind": node.get("kind"),
                        "vendor": node.get("vendor"),
                        "model": node.get("model"),
                        "firmware": node.get("firmware"),
                        "candidates": node.get("candidates"),
                        "evidence": node.get("evidence"),
                        "interfaces": node.get("interfaces"),
                    }
                    for name, node in (site.get("nodes") or {}).items()
                ],
                "topology": {
                    "router": (site.get("topology") or {}).get("router"),
                    "switches": (site.get("topology") or {}).get("switches"),
                    "counts": (site.get("topology") or {}).get("counts"),
                    "notes": (site.get("topology") or {}).get("notes"),
                },
            }

        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            stamp = self.started.replace(":", "").replace("-", "")
            label = (site or {}).get("name") or self.request.get("server") or "run"
            safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in str(label))
            path = self.directory / f"{stamp}-{safe}.json"
            path.write_text(json.dumps(record, indent=2, ensure_ascii=False),
                            encoding="utf-8")
        except OSError:
            # A survey must not fail because its log could not be written.
            return None
        prune(self.directory)
        return path


class _Timed:
    def __init__(self, log: RunLog, where: str, command: str):
        self.log, self.where, self.command = log, where, command

    def __enter__(self):
        self.t0 = time.monotonic()
        return self

    def done(self, *, rc=None, output=None, error=None):
        self.log.command(self.where, self.command, rc=rc,
                         seconds=time.monotonic() - self.t0,
                         output=output, error=error)

    def __exit__(self, kind, value, tb):
        if value is not None:
            self.done(error=f"{type(value).__name__}: {value}")
        return False


def prune(directory: Path = RUNS_DIR, keep: int = KEEP) -> int:
    runs = sorted(directory.glob("*.json"))
    dropped = 0
    for path in runs[:-keep] if len(runs) > keep else []:
        try:
            os.unlink(path)
            dropped += 1
        except OSError:
            pass
    return dropped


def list_runs(directory: Path = RUNS_DIR) -> list:
    """Newest last. Each entry is (path, summary dict)."""
    out = []
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append((path, record))
    return out
