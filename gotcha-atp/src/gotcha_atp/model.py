"""Rows, stages and the stamp rule — the vocabulary every other module speaks.

A stage declares its rows up front (`RowSpec`) so the UI, the record and the
PDF show the whole catalogue even for stages that are not implemented yet. A
stage's `run(ctx)` yields one `Row` per spec.

Row states:
    pass   ok is True
    fail   ok is False
    amber  ok is None — not evaluated: a prerequisite failed, the evidence was
           unreadable, or the stage is not implemented. Amber is never green.

Stamp rule (canvas v1.4): a critical/high fail or a critical amber blocks the
stamp; a medium/low fail is a warning. Only a full run can be stamp-eligible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

SEVERITIES = ("critical", "high", "medium", "low")
CLASSES = ("effect", "read-back", "manual")
BLOCKING_SEVERITIES = ("critical", "high")
WARNING_SEVERITIES = ("medium", "low")

PASS, FAIL, AMBER = "pass", "fail", "amber"


@dataclass(frozen=True)
class RowSpec:
    id: str
    item: str
    expected: str
    severity: str
    cls: str = "effect"
    depends_on: tuple[str, ...] = ()
    # Manual rows only: the question put to the engineer, whether Skip is
    # allowed, and a fact key that must be truthy for the question to be
    # offered at all (S10.4 is only offered when a radar was quiet).
    prompt: str = ""
    skippable: bool = False
    offer_if: str = ""

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"{self.id}: unknown severity {self.severity!r}")
        if self.cls not in CLASSES:
            raise ValueError(f"{self.id}: unknown class {self.cls!r}")
        if self.cls == "manual" and not self.prompt:
            raise ValueError(f"{self.id}: a manual row needs a prompt")


@dataclass
class Row:
    id: str
    item: str
    expected: str
    severity: str
    cls: str
    actual: str
    ok: Optional[bool]
    reason: str = ""
    hint: str = ""
    detail: dict = field(default_factory=dict)

    @property
    def state(self) -> str:
        return PASS if self.ok is True else FAIL if self.ok is False else AMBER

    @property
    def blocking(self) -> bool:
        if self.state == FAIL:
            return self.severity in BLOCKING_SEVERITIES
        return self.state == AMBER and self.severity == "critical"

    @property
    def warning(self) -> bool:
        return self.state == FAIL and self.severity in WARNING_SEVERITIES

    def to_dict(self) -> dict:
        return {"id": self.id, "item": self.item, "expected": self.expected,
                "actual": self.actual, "ok": self.ok, "state": self.state,
                "severity": self.severity, "cls": self.cls,
                "reason": self.reason, "hint": self.hint, "detail": self.detail}

    @classmethod
    def from_dict(cls, d: dict) -> "Row":
        return cls(id=d["id"], item=d["item"], expected=d["expected"],
                   severity=d["severity"], cls=d["cls"], actual=d["actual"],
                   ok=d["ok"], reason=d.get("reason", ""), hint=d.get("hint", ""),
                   detail=d.get("detail") or {})


def result(spec: RowSpec, actual: str, ok: Optional[bool], *,
           detail: Optional[dict] = None, reason: str = "") -> Row:
    return Row(id=spec.id, item=spec.item, expected=spec.expected,
               severity=spec.severity, cls=spec.cls, actual=actual, ok=ok,
               reason=reason, detail=detail or {})


def amber(spec: RowSpec, reason: str, *, actual: str = "",
          detail: Optional[dict] = None) -> Row:
    return result(spec, actual or reason, None, detail=detail, reason=reason)


class Checks:
    """Sub-checks that roll up into one row: any False fails the row, otherwise
    any None makes it amber, otherwise it passes. `actual` lists every
    sub-check so the row says what was seen, not only that something was off."""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, label: str, ok: Optional[bool], actual: Any) -> "Checks":
        self.items.append({"label": label, "ok": ok, "actual": str(actual)})
        return self

    @property
    def ok(self) -> Optional[bool]:
        oks = [c["ok"] for c in self.items]
        if any(o is False for o in oks):
            return False
        if any(o is None for o in oks) or not oks:
            return None
        return True

    @property
    def actual(self) -> str:
        def mark(c: dict) -> str:
            return "" if c["ok"] is True else " ✗" if c["ok"] is False else " ?"
        return " · ".join(f"{c['label']}: {c['actual']}{mark(c)}" for c in self.items)

    @property
    def reason(self) -> str:
        unknown = [c["label"] for c in self.items if c["ok"] is None]
        return ("not evaluated: " + ", ".join(unknown)) if unknown and self.ok is None else ""

    def row(self, spec: RowSpec, **detail: Any) -> Row:
        return result(spec, self.actual, self.ok, reason=self.reason,
                      detail={"checks": self.items, **detail})


@dataclass(frozen=True)
class StageSpec:
    id: str
    name: str
    rows: tuple[RowSpec, ...]
    depends_on: tuple[str, ...] = ()
    vantage: str = ""
    implemented: bool = True
    # S9: only part of a run when the engineer chose the extended (soak) run.
    optional: bool = False

    def spec(self, row_id: str) -> RowSpec:
        for s in self.rows:
            if s.id == row_id:
                return s
        raise KeyError(f"{self.id} declares no row {row_id}")


def stage_state(rows: list[Row]) -> str:
    """The dot on the Run screen: fail beats warn beats amber beats pass."""
    if any(r.state == FAIL and r.severity in BLOCKING_SEVERITIES for r in rows):
        return "fail"
    if any(r.warning for r in rows):
        return "warn"
    if any(r.state == AMBER for r in rows):
        return "amber"
    return "pass"


def summarize(rows: list[Row], *, full_run: bool) -> dict:
    """Verdict and stamp eligibility for a set of rows (canvas stamp rule)."""
    failed = [r.id for r in rows if r.state == FAIL and r.severity in BLOCKING_SEVERITIES]
    critical_amber = [r.id for r in rows if r.state == AMBER and r.severity == "critical"]
    amber_rows = [r.id for r in rows if r.state == AMBER]
    warnings = [r.id for r in rows if r.warning]
    verdict = "FAIL" if failed else "INCOMPLETE" if critical_amber else "PASS"
    stamp_eligible = full_run and not failed and not critical_amber
    if stamp_eligible:
        why = "full run, no critical/high fail, no critical amber"
    elif not full_run:
        why = "partial run — only a full run can be stamp-eligible"
    elif failed:
        why = "critical/high fail: " + ", ".join(failed)
    else:
        why = "critical amber: " + ", ".join(critical_amber)
    return {
        "verdict": verdict,
        "stamp_eligible": stamp_eligible,
        "stamp_reason": why,
        "full_run": full_run,
        "failed": failed,
        "critical_amber": critical_amber,
        "amber": amber_rows,
        "warnings": warnings,
        "counts": {
            "rows": len(rows),
            "pass": sum(r.state == PASS for r in rows),
            "fail": sum(r.state == FAIL for r in rows),
            "amber": len(amber_rows),
        },
    }
