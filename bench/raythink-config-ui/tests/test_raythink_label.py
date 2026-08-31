"""The Raythink IP-cycle counter is independent of the label printer (TEC-352).

Raythink's "cycle" is not a hands-free provisioning loop like the Magos one —
it is a persisted counter deciding which octet the next camera gets. But it
advances from `on_run_recorded`, which runs just after the print call, and it
burns a number only on a clean run. So the question this file settles is
whether a printer that failed can cost a camera its address, or skip one.

The Magos auto/cycle loop is covered in
`magos-config-ui/tests/test_apu_label.py`.
"""
import asyncio

import pytest

from bench_core.label_printer import LabelPrinter, PrinterSettings

import raythink_app as mod

cfg = mod.configurator

HOST = "192.168.1.168"


def fake_result(ok=True, verified=True, ip="192.168.88.30"):
    return {
        "ok": ok,
        "identity": {"serial": "RC5A012035", "mac": "aa:bb:cc:dd:ee:03",
                     "model": "Raythink", "firmware": "2.8.1"},
        "warnings": [], "error": None if ok else "boom", "steps": [],
        "verification": [{"item": "static IP", "expected": ip, "actual": ip,
                          "ok": bool(verified)}],
        "log": "", "ip": ip, "ip_mode": "static", "hostname": "raythink-30",
        "profile": "lan",
    }


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    cfg.state.update(cfg.initial_state())
    cfg.state["config_loaded"] = True
    cfg.state["cycle_next"] = 30
    cfg.state["ip_mode"] = "cycle"
    monkeypatch.setattr(cfg, "_save_log", lambda entry: None)
    monkeypatch.setattr(cfg, "_save_ip_state", lambda: None)
    monkeypatch.setattr(cfg, "label_printer",
                        LabelPrinter(PrinterSettings(
                            sink=str(tmp_path / "labels.zpl"))))
    return tmp_path


def labels(tmp_path) -> list[str]:
    sink = tmp_path / "labels.zpl"
    if not sink.exists():
        return []
    return [b for b in sink.read_text(encoding="utf-8").split("^XA") if b.strip()]


def run(monkeypatch, octet=30, **kwargs):
    monkeypatch.setattr(cfg, "_do_configure",
                        lambda inputs: fake_result(ip=f"192.168.88.{octet}",
                                                   **kwargs))
    inputs = {"profile": "lan", "profile_path": "x", "octet": octet,
              "ip_mode": "cycle", "target_ip": f"192.168.88.{octet}",
              "advance_cycle": True, "host": HOST,
              "mac": "aa:bb:cc:dd:ee:03"}
    asyncio.run(cfg.execute_run(inputs, "test"))
    return cfg.state["history"][0]


def test_the_cycle_counter_advances_with_no_printer(monkeypatch, clean_state):
    monkeypatch.setattr(cfg, "label_printer",
                        LabelPrinter(PrinterSettings(queue="nothing-here")))
    entry = run(monkeypatch, octet=30)
    assert entry["status"] == "ok"
    assert entry["label"]["printed"] is False
    assert cfg.state["cycle_next"] == 31        # the camera still got its number


def test_three_cameras_in_a_row_with_a_dead_printer(monkeypatch, clean_state):
    monkeypatch.setattr(cfg, "label_printer",
                        LabelPrinter(PrinterSettings(queue="nothing-here")))
    for octet in (30, 31, 32):
        run(monkeypatch, octet=octet)
    assert cfg.state["cycle_next"] == 33
    assert [h["status"] for h in cfg.state["history"]] == ["ok"] * 3


def test_a_working_printer_prints_once_per_camera(monkeypatch, clean_state):
    for octet in (30, 31, 32):
        run(monkeypatch, octet=octet)
    assert cfg.state["cycle_next"] == 33
    assert len(labels(clean_state)) == 3


def test_a_failed_camera_keeps_its_number_and_gets_no_label(monkeypatch,
                                                            clean_state):
    """The counter burns a number only on a clean run — and a failed run gets
    no label either, so the two stay consistent."""
    entry = run(monkeypatch, octet=30, ok=False, verified=False)
    assert entry["status"] == "error"
    assert "label" not in entry
    assert cfg.state["cycle_next"] == 30
    assert labels(clean_state) == []


def test_the_run_is_never_left_busy_by_the_printer(monkeypatch, clean_state):
    """`busy` gates the detection loop and blocks a second run. Left set by a
    printer problem, the tool is frozen."""
    monkeypatch.setattr(cfg, "label_printer",
                        LabelPrinter(PrinterSettings(queue="nothing-here")))
    run(monkeypatch)
    assert cfg.state["busy"] is False
    assert cfg.state["phase"] == "configured"
