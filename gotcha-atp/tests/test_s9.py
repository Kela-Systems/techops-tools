"""S9 power-cycle soak, driven with fakes for the unit (no real power cut)."""
from pathlib import Path

import pytest

from gotcha_atp import release
from gotcha_atp.access.creds import Credentials, Redactor
from gotcha_atp.access.exec import ExecResult
from gotcha_atp.access.session import Unit
from gotcha_atp.context import Context
from gotcha_atp.model import result
from gotcha_atp.stages import s1, s3, s9

ROOT = Path(__file__).resolve().parents[1]
REL = release.load(ROOT / "release.yaml")


class FakeSession:
    def __init__(self):
        self.calls = []

    def disconnect(self):
        self.calls.append("disconnect")

    def reconnect(self):
        self.calls.append("reconnect")
        return {}


def _ctx(answers):
    ctx = Context(unit=Unit("gotcha-x"), creds=Credentials(), redact=Redactor(), release=REL)
    ctx.session = FakeSession()
    ctx.ask = (lambda specs: {s.id: {"answer": answers[s.id], "note": ""} for s in specs if s.id in answers}) \
        if answers is not None else None
    ctx.results = {
        "S1.1": result(s1.STAGE.spec("S1.1"), "ok", True),
        "S1.2": result(s1.STAGE.spec("S1.2"), "dup", False),          # failing before the cut
        "S3.1": result(s3.STAGE.spec("S3.1"), "ok", True),
    }
    ctx.facts["inventory"] = [{"ip": REL.plan.camera, "error": ""}, {"ip": REL.plan.speaker, "error": "no answer"}]
    return ctx


@pytest.fixture(autouse=True)
def _restore_s9():
    saved = {k: getattr(s9, k) for k in ("time", "_btime", "_uptimes", "_reachable", "_unit_back",
                                         "_wait", "_pods", "_recheck")}
    yield
    for k, v in saved.items():
        setattr(s9, k, v)


def _states(rows):
    return {r.id: r.state for r in rows}


def test_helpers():
    ctx = _ctx({})
    assert s9.baseline_passed(ctx.results) == ["S1.1", "S3.1"]
    assert s9.not_regained(["S1.1", "S3.1"], {"S1.1": ctx.results["S1.1"]}) == ["S3.1"]
    pods = {"items": [{"metadata": {"namespace": "kela", "name": "hub"},
                       "status": {"containerStatuses": [{"name": "c", "restartCount": 2}]}}]}
    assert s9.restarts(pods) == {"kela/hub/c": 2}


def test_no_engineer_or_no_cut_is_amber():
    assert set(_states(s9.run(_ctx(None))).values()) == {"amber"}
    ctx = _ctx({"S9.cut": "no"})
    _patch_reads(ctx)
    rows = list(s9.run(ctx))
    assert set(_states(rows).values()) == {"amber"} and "answered No" in rows[0].reason
    assert ctx.session.calls == ["disconnect", "reconnect"]


class FakeClock:
    """Each read moves 20 s on, so a soak that really runs for minutes fits in a test."""
    def __init__(self):
        self.now = 1_791_000_000.0

    def time(self):
        self.now += 20
        return self.now


def _patch_reads(ctx, *, rebooted=True, regained=True):
    clock = FakeClock()
    s9.time = clock
    t_start = clock.now
    boots = iter([1000, int(t_start) + 60 if rebooted else 1000])
    s9._btime = lambda c: next(boots)
    ups = iter([{"radar_0": 90000.0, "router": 90000.0}, {"radar_0": 30.0 if rebooted else 90100.0, "router": 25.0}])
    s9._uptimes = lambda c: next(ups)
    s9._reachable = lambda c, ips: {ip: True for ip in ips}
    s9._unit_back = lambda c, t0, deadline: 42.0
    s9._wait = lambda c, s: None
    s9._pods = lambda c: {"items": []}
    s9._recheck = lambda c: {"S1.1": result(s1.STAGE.spec("S1.1"), "ok", True),
                             "S3.1": result(s3.STAGE.spec("S3.1"), "ok" if regained else "NotReady", regained)}
    ctx.server = lambda script, timeout=30: ExecResult(0, "2026-10-06T10:52:04+0300 system converged\n")


def test_full_soak_passes():
    ctx = _ctx({"S9.cut": "yes", "S9.hands": "no"})
    _patch_reads(ctx)
    rows = {r.id: r for r in s9.run(ctx)}
    assert _states(rows.values()) == {"S9.1": "pass", "S9.2": "pass", "S9.3": "pass", "S9.4": "pass"}
    s93 = {c["label"]: c for c in rows["S9.3"].detail["checks"]}
    assert "S1.2" in s93["failing before the cut (not judged)"]["actual"]
    assert rows["S9.1"].detail["timeline"]["ssh_back_s"] == 42


def test_soak_catches_no_reboot_hands_and_regressions():
    ctx = _ctx({"S9.cut": "yes", "S9.hands": "yes"})
    _patch_reads(ctx, rebooted=False, regained=False)
    rows = {r.id: r for r in s9.run(ctx)}           # re-checks until the 15 min deadline
    s91 = {c["label"]: c["ok"] for c in rows["S9.1"].detail["checks"]}
    assert s91["cold boot"] is False and s91["no hands"] is False
    assert {c["label"]: c["ok"] for c in rows["S9.2"].detail["checks"]}["radar_0"] is False
    assert rows["S9.3"].state == "fail"
