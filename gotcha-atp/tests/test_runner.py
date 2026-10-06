"""The engine against fake stages: prerequisites, stubs, manual answers,
engine errors, redaction and the written record."""
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from gotcha_atp import release, stages
from gotcha_atp.access.creds import Credentials, Redactor
from gotcha_atp.access.session import Unit
from gotcha_atp.context import Context
from gotcha_atp.model import RowSpec, StageSpec, result
from gotcha_atp.runner import Answers, Runner, RunOptions

ROOT = Path(__file__).resolve().parents[1]
SECRET = "Hunter2!x"


def _stage(id_, rows, run=None, **kw):
    st = StageSpec(id=id_, name=f"stage {id_}", rows=tuple(rows), **kw)
    return SimpleNamespace(STAGE=st, run=run) if run else SimpleNamespace(STAGE=st)


def _s0(ctx):
    yield result(stages_fake["S0"].STAGE.spec("S0.1"), f"logged in with {SECRET}", True)
    yield result(stages_fake["S0"].STAGE.spec("S0.2"), "operator offline", False)


def _s2(ctx):
    st = stages_fake["S2"].STAGE
    yield result(st.spec("S2.1"), "fine", True)
    yield result(st.spec("S2.2"), "would pass", True)


def _s4(ctx):
    yield result(stages_fake["S4"].STAGE.spec("S4.1"), "first", True)
    raise RuntimeError("boom")


stages_fake = {
    "S0": _stage("S0", [RowSpec("S0.1", "release", "valid", "critical"),
                        RowSpec("S0.2", "tailnet", "online", "critical")], _s0),
    "S1": _stage("S1", [RowSpec("S1.1", "ping", "0% loss", "critical")],
                 run=lambda ctx: iter(()), depends_on=("S0.2",)),
    "S2": _stage("S2", [RowSpec("S2.1", "host", "ok", "high"),
                        RowSpec("S2.2", "disk", "ok", "critical", depends_on=("S0.2",)),
                        RowSpec("S2.9", "look at it", "yes", "high", "manual", prompt="Is it on?")], _s2),
    "S3": _stage("S3", [RowSpec("S3.1", "cluster", "ready", "critical")], implemented=False),
    "S4": _stage("S4", [RowSpec("S4.1", "a", "x", "high"), RowSpec("S4.2", "b", "x", "high")], _s4),
    "S9": _stage("S9", [RowSpec("S9.1", "soak", "x", "critical")], implemented=False, optional=True),
}


@pytest.fixture
def fake_stages(monkeypatch):
    monkeypatch.setattr(stages, "STAGES", tuple(m.STAGE for m in stages_fake.values()))
    monkeypatch.setattr(stages, "BY_ID", dict(stages_fake))


def _ctx():
    return Context(unit=Unit(site="kela-gotcha-99"), creds=Credentials(ssh_password=SECRET),
                   redact=Redactor([SECRET]), release=release.load(ROOT / "release.yaml"))


def _answer_when_asked(answers, choice="yes"):
    def work():
        for _ in range(200):
            pending = answers.pending()
            if pending:
                for q in pending:
                    answers.answer(q["id"], choice, f"note with {SECRET}")
                return
            time.sleep(0.02)
    t = threading.Thread(target=work, daemon=True)
    t.start()
    return t


def test_full_run(fake_stages, tmp_path):
    events = []
    answers = Answers()
    t = _answer_when_asked(answers)
    runner = Runner(_ctx(), emit=events.append, answers=answers, options=RunOptions(),
                    out_dir=tmp_path)
    out = runner.run()
    t.join(2)
    rows = runner.rows

    assert rows["S1.1"].state == "amber" and rows["S1.1"].reason == "prerequisite S0.2 failed"
    assert rows["S2.1"].state == "pass"
    assert rows["S2.2"].state == "amber" and rows["S2.2"].detail["would_be"]["ok"] is True
    assert rows["S3.1"].reason.startswith("not implemented")
    assert rows["S4.1"].state == "pass"
    assert rows["S4.2"].state == "amber" and "engine error in S4" in rows["S4.2"].reason
    assert rows["S2.9"].ok is True and rows["S2.9"].actual.startswith("Yes")
    assert "S9.1" not in rows  # soak not selected

    s = out["summary"]
    assert s["verdict"] == "FAIL" and s["failed"] == ["S0.2"] and not s["stamp_eligible"]
    types = [e["type"] for e in events]
    assert types[0] == "run_started" and types[-1] == "run_finished" and "needs_input" in types

    text = Path(out["record"]).read_text()
    assert SECRET not in text and SECRET not in json.dumps(events)
    assert SECRET not in Path(out["html"]).read_text()
    rec = json.loads(text)
    assert rec["schema"] == "bench-run-record/1" and rec["kind"] == "verify"
    assert rec["serial"] == "kela-gotcha-99" and rec["verified"] is False
    assert rec["device"]["site_name"] == "kela-gotcha-99"
    assert [st["id"] for st in rec["stages"]] == ["S0", "S1", "S2", "S3", "S4"]
    assert out["upload"]["status"] == "disabled"


def test_partial_run_keeps_s0_and_uses_prior_rows(fake_stages, tmp_path):
    ctx = _ctx()
    ctx.prior = {}
    runner = Runner(ctx, emit=lambda e: None, answers=Answers(),
                    options=RunOptions(stages=["S3"], manual=False), out_dir=tmp_path)
    out = runner.run()
    assert set(runner.rows) == {"S0.1", "S0.2", "S3.1"}
    assert out["summary"]["full_run"] is False and not out["summary"]["stamp_eligible"]


def test_soak_adds_s9_and_no_manual_marks_amber(fake_stages, tmp_path):
    runner = Runner(_ctx(), emit=lambda e: None, answers=Answers(),
                    options=RunOptions(soak=True, manual=False), out_dir=tmp_path)
    runner.run()
    assert runner.rows["S9.1"].state == "amber"
    assert runner.rows["S2.9"].reason == "not asked (run without manual steps)"


def test_release_failure_ambers_everything_after_s0(fake_stages, tmp_path):
    ctx = _ctx()
    ctx.release = None
    ctx.release_error = "schema is wrong"
    runner = Runner(ctx, emit=lambda e: None, answers=Answers(),
                    options=RunOptions(manual=False), out_dir=tmp_path)
    runner.run()
    assert runner.rows["S2.1"].reason == "prerequisite S0.1 failed"


def test_answers_validate():
    a = Answers()
    spec = RowSpec("S10.4", "walk", "x", "low", "manual", prompt="walk?", skippable=False)
    cancel = threading.Event()
    th = threading.Thread(target=lambda: a.ask([spec], cancel), daemon=True)
    th.start()
    time.sleep(0.05)
    with pytest.raises(ValueError):
        a.answer("S10.4", "skip")
    with pytest.raises(KeyError):
        a.answer("S1.1", "yes")
    a.answer("S10.4", "no")
    th.join(1)
    assert not th.is_alive()


def test_soak_needs_a_full_run():
    from gotcha_atp.runner import selection_error
    assert selection_error(None, True) is None and selection_error(["S1"], False) is None
    assert "full ATP" in selection_error(["S9"], False)
    assert "full run" in selection_error(["S1", "S2"], True)
