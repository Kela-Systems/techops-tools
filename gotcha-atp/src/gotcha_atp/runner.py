"""The engine: runs every stage in order and never gates.

* Every stage runs, whatever failed before it. A stage whose prerequisite row
  did not pass emits all its automated rows amber with "prerequisite Sx.y
  failed"; a row whose own `depends_on` did not pass is overridden the same way.
  Amber is never counted as pass.
* Stubbed stages emit their declared rows amber "not implemented", so the UI and
  the PDF show the whole catalogue from day one.
* Manual rows are collected from every stage that ran and asked at the end
  (`needs_input`), independent of the automated results — the engineer's
  attestation stands on its own.
* Events: run_started, stage_started, row, stage_finished, needs_input, log,
  run_finished. The UI renders the stream; the CLI prints it. Everything emitted
  passes through the run's Redactor.
"""
from __future__ import annotations

import socket
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import yaml

from . import PACKAGE_DIR, __version__, git_revision, record, stages
from .benchcentral import BenchCentral
from .context import Context
from .model import AMBER, FAIL, PASS, Row, RowSpec, StageSpec, amber, result, stage_state, summarize
from .report import html as report_html
from .report import pdf as report_pdf

NOT_IMPLEMENTED = "not implemented yet (Phase 1 scaffold)"
ANSWERS = ("yes", "no", "skip")

Emit = Callable[[dict], None]

_HINTS: Optional[dict] = None


def hints() -> dict[str, str]:
    global _HINTS
    if _HINTS is None:
        with (PACKAGE_DIR / "hints.yaml").open() as fh:
            _HINTS = {str(k): " ".join(str(v).split()) for k, v in (yaml.safe_load(fh) or {}).items()}
    return _HINTS


class Answers:
    """Manual-step answers, handed from the UI/CLI thread to the runner."""

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._pending: dict[str, RowSpec] = {}
        self._answers: dict[str, dict] = {}

    def pending(self) -> list[dict]:
        with self._cv:
            return [question(s) for s in self._pending.values() if s.id not in self._answers]

    def answer(self, row_id: str, choice: str, note: str = "") -> None:
        choice = choice.lower().strip()
        with self._cv:
            spec = self._pending.get(row_id)
            if spec is None:
                raise KeyError(f"{row_id} is not waiting for an answer")
            if choice not in ANSWERS or (choice == "skip" and not spec.skippable):
                raise ValueError(f"{row_id}: answer must be yes/no" + ("/skip" if spec.skippable else ""))
            self._answers[row_id] = {"answer": choice, "note": note.strip()}
            self._cv.notify_all()

    def ask(self, specs: list[RowSpec], cancel: threading.Event) -> dict[str, dict]:
        with self._cv:
            self._pending = {s.id: s for s in specs}
            self._answers = {}
            while not cancel.is_set() and any(s.id not in self._answers for s in specs):
                self._cv.wait(timeout=0.5)
            got = dict(self._answers)
            self._pending = {}
            return got


def question(spec: RowSpec) -> dict:
    return {"id": spec.id, "item": spec.item, "prompt": spec.prompt,
            "expected": spec.expected, "skippable": spec.skippable}


def selection_error(stages: Optional[list[str]], soak: bool) -> Optional[str]:
    """Why a requested stage selection cannot run, or None. The soak judges
    recovery against the full S1–S8 baseline of the same run."""
    if stages and "S9" in stages:
        return "S9 (power-cycle soak) only runs after a full ATP — use 'Run ATP + power-cycle soak'"
    if soak and stages:
        return "the power-cycle soak needs a full run, not a partial one"
    return None


@dataclass
class RunOptions:
    stages: Optional[list[str]] = None     # None = full run
    soak: bool = False
    manual: bool = True                    # False: manual rows are amber "not asked"
    engineer: str = ""


class Runner:
    def __init__(self, ctx: Context, *, emit: Emit, answers: Answers, options: RunOptions,
                 out_dir: Path, central: Optional[BenchCentral] = None) -> None:
        self.ctx = ctx
        self._emit_raw = emit
        self.answers = answers
        self.options = options
        self.out_dir = out_dir
        self.central = central
        self.run_id = str(uuid.uuid4())
        self.rows: dict[str, Row] = {}
        self.steps: list[dict] = []
        ctx.log = self.log
        ctx.ask = self._ask_now if options.manual else None

    def _ask_now(self, specs: list[RowSpec]) -> dict[str, dict]:
        """Questions a stage needs answered before it can go on (not rows)."""
        self.emit({"type": "needs_input", "during": True, "questions": [question(s) for s in specs]})
        return self.answers.ask(specs, self.ctx.cancel)

    # ── events ───────────────────────────────────────────────────────────────

    def emit(self, event: dict) -> None:
        self._emit_raw(self.ctx.redact.scrub({"run_id": self.run_id, "ts": time.time(), **event}))

    def log(self, msg: str, level: str = "INFO") -> None:
        msg = self.ctx.redact(msg)
        self.steps.append({"time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                           "level": level, "sn": self.ctx.unit.site, "msg": msg})
        self.emit({"type": "log", "level": level, "msg": msg})

    # ── selection ────────────────────────────────────────────────────────────

    def selected(self) -> list[StageSpec]:
        want = self.options.stages
        out = []
        for st in stages.STAGES:
            if want is None:
                if st.optional and not self.options.soak:
                    continue
            elif st.id != "S0" and st.id not in want and not (st.optional and self.options.soak):
                continue
            out.append(st)
        return out

    # ── prerequisites ────────────────────────────────────────────────────────

    def _prereq(self, deps: tuple[str, ...]) -> Optional[str]:
        for dep in deps:
            if "." in dep:
                r = self.ctx.row(dep)
                if r is None:
                    return f"prerequisite {dep} not run"
                if r.state == FAIL:
                    return f"prerequisite {dep} failed"
                if r.state == AMBER:
                    return f"prerequisite {dep} not evaluated"
            else:
                rows = [r for r in {**self.ctx.prior, **self.ctx.results}.values()
                        if r.id.split(".")[0] == dep]
                if not rows:
                    return f"prerequisite {dep} not run"
                if any(r.state != PASS for r in rows):
                    return f"prerequisite {dep} not passed"
        return None

    # ── rows ─────────────────────────────────────────────────────────────────

    def _accept(self, stage: StageSpec, row: Row) -> Row:
        spec = stage.spec(row.id)
        why = self._prereq(spec.depends_on) if row.state != AMBER else None
        if why:
            row = amber(spec, why, detail={"would_be": row.to_dict()})
        if row.state == FAIL:
            row.hint = hints().get(row.id, "")
        self.rows[row.id] = row
        self.ctx.results[row.id] = row
        self.emit({"type": "row", "stage": stage.id, "row": row.to_dict()})
        return row

    def _run_stage(self, stage: StageSpec, manual: list[tuple[StageSpec, RowSpec]]) -> None:
        self.emit({"type": "stage_started", "stage": stage.id, "name": stage.name})
        automated = [s for s in stage.rows if s.cls != "manual"]
        manual.extend((stage, s) for s in stage.rows if s.cls == "manual")
        module = stages.BY_ID[stage.id]
        if self.ctx.cancel.is_set():
            blanket = "run cancelled"
        elif self.ctx.release is None and stage.id != "S0":
            blanket = "prerequisite S0.1 failed"
        elif not stage.implemented:
            blanket = NOT_IMPLEMENTED
        else:
            blanket = self._prereq(stage.depends_on)
        if blanket or not automated:
            for spec in automated:
                self._accept(stage, amber(spec, blanket))
        else:
            self._iterate(stage, module, automated)
        self._stage_finished(stage)

    def _iterate(self, stage: StageSpec, module, automated: list[RowSpec]) -> None:
        produced: set[str] = set()
        error = ""
        try:
            for row in module.run(self.ctx):
                if row.id in produced:
                    continue
                produced.add(row.id)
                self._accept(stage, row)
        except Exception as e:  # noqa: BLE001 — the engine must finish the run
            error = self.ctx.redact(f"engine error in {stage.id}: {type(e).__name__}: {e}")
            self.log(error + "\n" + self.ctx.redact(traceback.format_exc()), "ERROR")
        for spec in automated:
            if spec.id not in produced:
                self._accept(stage, amber(spec, error or f"{stage.id} produced no result for this row"))

    def _stage_finished(self, stage: StageSpec) -> None:
        rows = [self.rows[s.id] for s in stage.rows if s.id in self.rows]
        self.emit({"type": "stage_finished", "stage": stage.id,
                   "state": stage_state(rows) if rows else "pending"})

    def _questions(self, manual: list[tuple[StageSpec, RowSpec]]) -> None:
        ask: list[tuple[StageSpec, RowSpec]] = []
        for stage, spec in manual:
            if spec.offer_if and not self.ctx.facts.get(spec.offer_if):
                self._accept(stage, amber(spec, "not offered (no radar was quiet)"))
            elif not self.options.manual:
                self._accept(stage, amber(spec, "not asked (run without manual steps)"))
            else:
                ask.append((stage, spec))
        if not ask:
            return
        self.emit({"type": "needs_input", "questions": [question(s) for _, s in ask]})
        got = self.answers.ask([s for _, s in ask], self.ctx.cancel)
        for stage, spec in ask:
            a = got.get(spec.id)
            if a is None:
                row = amber(spec, "not answered (run cancelled)")
            else:
                text = a["answer"].capitalize() + (f" — {a['note']}" if a["note"] else "")
                if a["answer"] == "skip":
                    row = amber(spec, "skipped by the engineer", actual=text)
                else:
                    row = result(spec, text, a["answer"] == "yes")
            self._accept(stage, row)
        for stage in {st.id: st for st, _ in ask}.values():
            self._stage_finished(stage)

    # ── the run ──────────────────────────────────────────────────────────────

    def run(self) -> dict:
        started = datetime.now(timezone.utc)
        selected = self.selected()
        full_run = self.options.stages is None
        self.emit({"type": "run_started", "site": self.ctx.unit.site, "full_run": full_run,
                   "stages": [{"id": s.id, "name": s.name, "implemented": s.implemented,
                               "rows": [{"id": r.id, "item": r.item, "severity": r.severity,
                                         "cls": r.cls} for r in s.rows]} for s in selected]})
        manual: list[tuple[StageSpec, RowSpec]] = []
        error = ""
        try:
            for stage in selected:
                self._run_stage(stage, manual)
            self._questions(manual)
        except Exception as e:  # noqa: BLE001
            error = self.ctx.redact(f"{type(e).__name__}: {e}")
            self.log("run aborted: " + error + "\n" + self.ctx.redact(traceback.format_exc()), "ERROR")
        finally:
            self.ctx.close_devices()

        order = [spec.id for st in selected for spec in st.rows]
        rows = [self.rows[i] for i in order if i in self.rows]
        summary = summarize(rows, full_run=full_run and not error)
        finished = datetime.now(timezone.utc)

        rec = record.build(
            run_id=self.run_id, ctx=self.ctx, rows=rows, stage_specs=selected,
            summary=summary, started=started, finished=finished, steps=self.steps,
            engineer=self.options.engineer or self.ctx.creds.engineer,
            station=socket.gethostname(), version=f"{__version__}+{git_revision()}",
            requested=self.options.stages, soak=self.options.soak, error=error)
        paths = record.write(rec, self.out_dir)
        html_text = report_html.render(rec)
        paths["html"] = paths["json"].with_suffix(".html")
        paths["html"].write_text(html_text, encoding="utf-8")
        pdf_path, pdf_note = report_pdf.write(html_text, paths["json"].with_suffix(".pdf"))
        upload = self.central.post_run(rec) if self.central else {"status": "disabled",
                                                                   "detail": "bench-central URL not set"}
        payload = {"type": "run_finished", "summary": summary, "error": error,
                   "record": str(paths["json"]), "html": str(paths["html"]),
                   "pdf": str(pdf_path) if pdf_path else None, "pdf_note": pdf_note,
                   "upload": upload, "duration_s": int((finished - started).total_seconds())}
        self.emit(payload)
        return payload
