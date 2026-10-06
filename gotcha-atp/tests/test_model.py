from gotcha_atp.model import Checks, Row, RowSpec, amber, result, stage_state, summarize


def spec(i, sev, cls="effect"):
    return RowSpec(i, f"check {i}", "x", sev, cls)


def test_stamp_rule_blocks_on_critical_or_high_fail():
    rows = [result(spec("A", "critical"), "ok", True), result(spec("B", "high"), "bad", False)]
    s = summarize(rows, full_run=True)
    assert s["verdict"] == "FAIL" and not s["stamp_eligible"] and s["failed"] == ["B"]


def test_medium_low_fail_is_a_warning():
    rows = [result(spec("A", "critical"), "ok", True), result(spec("B", "medium"), "old fw", False),
            result(spec("C", "low"), "x", False)]
    s = summarize(rows, full_run=True)
    assert s["verdict"] == "PASS" and s["stamp_eligible"] and s["warnings"] == ["B", "C"]


def test_critical_amber_blocks_but_high_amber_does_not():
    crit = summarize([amber(spec("A", "critical"), "not implemented")], full_run=True)
    assert crit["verdict"] == "INCOMPLETE" and not crit["stamp_eligible"]
    high = summarize([amber(spec("A", "high"), "unreadable")], full_run=True)
    assert high["verdict"] == "PASS" and high["stamp_eligible"]


def test_partial_run_is_never_stamp_eligible():
    s = summarize([result(spec("A", "critical"), "ok", True)], full_run=False)
    assert s["verdict"] == "PASS" and not s["stamp_eligible"]


def test_checks_roll_up():
    assert Checks().add("a", True, 1).add("b", True, 2).ok is True
    assert Checks().add("a", True, 1).add("b", None, "?").ok is None
    assert Checks().add("a", None, 1).add("b", False, "x").ok is False
    c = Checks().add("a", True, 1).add("b", None, "unknown")
    assert c.reason == "not evaluated: b"
    assert "b: unknown ?" in c.actual


def test_stage_state_order():
    ok = result(spec("A", "critical"), "", True)
    warn = result(spec("B", "low"), "", False)
    amb = amber(spec("C", "high"), "x")
    fail = result(spec("D", "high"), "", False)
    assert stage_state([ok]) == "pass"
    assert stage_state([ok, amb]) == "amber"
    assert stage_state([ok, amb, warn]) == "warn"
    assert stage_state([ok, amb, warn, fail]) == "fail"


def test_row_round_trip():
    r = result(spec("A", "high"), "seen", False, detail={"k": 1})
    assert Row.from_dict(r.to_dict()) == r
