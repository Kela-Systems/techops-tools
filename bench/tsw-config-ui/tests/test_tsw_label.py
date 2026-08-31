"""The BenchConfigurator base prints a QA label on a verified-OK run (TEC-352).

Driven through the TSW202 because it is the plainest of the five tools on this
base (no site name, no hostname), but what is under test is
`BenchConfigurator.execute_run` — so this covers OTD500, RUTM08, Raythink and
the speaker too. The Magos base has its own orchestration and its own test:
`magos-config-ui/tests/test_apu_label.py`.
"""
import asyncio

import pytest

from bench_core.bench_ui import VerifyBody
from bench_core.label_printer import LabelPrinter, PrinterSettings

import tsw_app as mod

cfg = mod.configurator

FINAL_IP = "192.168.88.2"


def fake_result(ok=True, verified=True, serial="6010212527"):
    checks = [{"item": "management IP", "expected": FINAL_IP,
               "actual": FINAL_IP, "ok": bool(verified)}]
    return {
        "ok": ok,
        "hostname": "tsw-00f7",
        "identity": {"serial": serial, "mac": "20:97:2b:2b:00:f7",
                     "model": "TSW202", "firmware": "TSW2_R_00.07.14.1"},
        "warnings": [], "error": None if ok else "boom", "steps": [],
        "verification": checks if verified is not None else [],
        "log": "", "ip": FINAL_IP, "firmware_note": "at the floor",
    }


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    cfg.state.update(cfg.initial_state())
    cfg.state["config_loaded"] = True
    monkeypatch.setattr(cfg, "_save_log", lambda entry: None)
    monkeypatch.setattr(cfg, "label_printer",
                        LabelPrinter(PrinterSettings(
                            sink=str(tmp_path / "labels.zpl"))))
    return tmp_path


def labels(tmp_path) -> list[str]:
    sink = tmp_path / "labels.zpl"
    if not sink.exists():
        return []
    return [b for b in sink.read_text(encoding="utf-8").split("^XA") if b.strip()]


def run(monkeypatch, *, verify_mode=False, **kwargs):
    result = fake_result(**kwargs)
    monkeypatch.setattr(cfg, "_do_verify" if verify_mode else "_do_configure",
                        lambda inputs: result)
    inputs = {"host": "192.168.1.2", "mac": "20:97:2b:2b:00:f7",
              "password_source": "shared-fallback"}
    if verify_mode:
        asyncio.run(cfg.execute_verify(inputs, "test"))
    else:
        asyncio.run(cfg.execute_run(inputs, "test"))
    return cfg.state["history"][0]


def test_a_verified_configure_run_prints_one_label(monkeypatch, clean_state):
    entry = run(monkeypatch)
    assert entry["label"]["printed"] is True
    assert entry["label"]["face"] == "shared-ip"
    assert len(labels(clean_state)) == 1


def test_a_verify_pass_prints_too_so_a_lost_label_can_be_replaced(monkeypatch,
                                                                 clean_state):
    entry = run(monkeypatch, verify_mode=True)
    assert entry["kind"] == "verify"
    assert entry["label"]["printed"] is True
    assert len(labels(clean_state)) == 1


def test_a_failed_verification_prints_nothing(monkeypatch, clean_state):
    entry = run(monkeypatch, verified=False)
    assert entry["verified"] is False
    assert "label" not in entry
    assert labels(clean_state) == []


def test_a_run_with_no_checks_in_scope_prints_nothing(monkeypatch, clean_state):
    """"Configured but NOT verified" — the third outcome. A label is a claim
    that someone checked, so an unchecked unit must not carry one."""
    entry = run(monkeypatch, verified=None)
    assert entry["verified"] is None
    assert "label" not in entry
    assert labels(clean_state) == []


def test_a_verify_pass_that_lost_its_configure_record_prints_nothing(
        monkeypatch, clean_state):
    """When a verify pass can't find the unit's configure record it emits a red
    `prior run` row rather than dropping the checks it needed (TEC-348). That
    makes the run unverified, so no label — a QA sweep must not certify a unit
    it could not actually compare against anything."""
    result = fake_result()
    result["verification"] = [
        {"item": "prior run", "expected": "a configure record for this serial",
         "actual": "none found", "ok": False}]
    monkeypatch.setattr(cfg, "_do_verify", lambda inputs: result)
    asyncio.run(cfg.execute_verify({"host": "192.168.88.2"}, "test"))
    entry = cfg.state["history"][0]
    assert entry["verified"] is False
    assert "label" not in entry
    assert labels(clean_state) == []


def test_a_failed_run_prints_nothing(monkeypatch, clean_state):
    entry = run(monkeypatch, ok=False, verified=False)
    assert entry["status"] == "error"
    assert "label" not in entry
    assert labels(clean_state) == []


def test_the_label_block_is_attached_before_the_record_is_written(monkeypatch,
                                                                 clean_state):
    """_save_log writes the per-run JSON and spools the record to central, so
    a block attached afterwards would be missing from both."""
    seen = {}
    monkeypatch.setattr(cfg, "_save_log",
                        lambda entry: seen.update(entry) or None)
    run(monkeypatch)
    assert seen["label"]["printed"] is True


def test_a_printer_failure_leaves_the_run_configured(monkeypatch, clean_state):
    monkeypatch.setattr(cfg, "label_printer",
                        LabelPrinter(PrinterSettings(queue="nothing-here")))
    entry = run(monkeypatch)
    assert entry["status"] == "ok"
    assert cfg.state["phase"] == "configured"
    assert entry["label"]["printed"] is False
    assert entry["label"]["error"]


def test_an_unprinted_pass_warns_the_operator_by_serial(monkeypatch,
                                                        clean_state):
    monkeypatch.setattr(cfg, "label_printer",
                        LabelPrinter(PrinterSettings(queue="nothing-here")))
    run(monkeypatch)
    warning = cfg.public_state()["printer"]["warning"]
    assert "6010212527" in warning
    assert "by hand" in warning


def test_the_page_is_told_about_the_printer(clean_state):
    state = cfg.public_state()
    assert set(state["printer"]) == {"available", "target", "warning"}


def test_a_second_pass_clears_the_warning(monkeypatch, clean_state):
    """The banner must not outlive the problem, or an operator learns to ignore
    it."""
    dead = LabelPrinter(PrinterSettings(queue="nothing-here"))
    monkeypatch.setattr(cfg, "label_printer", dead)
    run(monkeypatch)
    assert cfg.public_state()["printer"]["warning"]

    monkeypatch.setattr(cfg, "label_printer",
                        LabelPrinter(PrinterSettings(
                            sink=str(clean_state / "labels.zpl"))))
    run(monkeypatch)
    assert cfg.public_state()["printer"]["warning"] is None


def test_the_printed_label_carries_the_serial_and_the_address(monkeypatch,
                                                             clean_state):
    run(monkeypatch)
    zpl = (clean_state / "labels.zpl").read_text(encoding="utf-8")
    assert "6010212527" in zpl
    assert FINAL_IP in zpl
    # Never, on any face: the firmware the unit happened to ship with.
    assert "TSW2_R_00.07.14.1" not in zpl


def test_verify_body_route_still_works_with_the_printer_wired(monkeypatch,
                                                             clean_state):
    """A smoke check that the shared /api/verify path is untouched."""
    monkeypatch.setattr(cfg, "_do_verify", lambda inputs: fake_result())
    inputs = cfg.verify_inputs(VerifyBody())
    assert "host" in inputs
