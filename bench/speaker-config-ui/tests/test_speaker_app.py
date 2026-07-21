"""State-machine tests for the Provision-ISR speaker configurator
(speaker_app.py).

No hardware/network: detection is faked via `_find_speaker` + `read_device_mac`
and the device pipeline via `_do_configure`.
"""
import asyncio

import pytest

from bench_core.run_record import RUN_RECORD_SCHEMA

import speaker_app as mod

cfg = mod.configurator

HOST = "192.168.1.57"


def fake_result(ok=True, serial="TM-CS20-000001-XX", hostname="speaker-70",
                mac="74:f8:db:5f:25:6a"):
    return {
        "ok": ok,
        "hostname": hostname,
        "identity": {"serial": serial, "mac": mac,
                     "model": "Provision-ISR speaker", "firmware": "V3.3.39-PR1"},
        "warnings": [],
        "error": None if ok else "boom",
        "steps": [],
        "verification": [],
        "log": "",
    }


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    cfg.state.update(cfg.initial_state())
    cfg.state["config_loaded"] = True
    monkeypatch.setattr(cfg, "_save_log", lambda entry: None)


def set_detection(monkeypatch, host, mac="74:f8:db:5f:25:6a"):
    monkeypatch.setattr(cfg, "_find_speaker", lambda: host)
    monkeypatch.setattr(mod, "read_device_mac", lambda *a, **k: mac)


def poll():
    async def run():
        await cfg.poll_once(asyncio.get_running_loop())
    asyncio.run(run())


def run_configure(ok=True, monkeypatch=None):
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result(ok=ok))
    inputs = {"host": HOST, "mac": "74:f8:db:5f:25:6a", "media_path": ""}
    asyncio.run(cfg.execute_run(inputs, "test"))


def test_waiting_to_detected_and_back(monkeypatch):
    set_detection(monkeypatch, HOST)
    poll()
    assert cfg.state["phase"] == "detected"
    assert cfg.state["active_host"] == HOST
    assert cfg.state["active_mac"] == "74:f8:db:5f:25:6a"

    set_detection(monkeypatch, None)
    poll()
    assert cfg.state["phase"] == "waiting"
    assert cfg.state["active_host"] is None


def test_busy_blocks_detection(monkeypatch):
    cfg.state["busy"] = True
    cfg.state["phase"] = "configuring"
    set_detection(monkeypatch, HOST)
    poll()
    assert cfg.state["phase"] == "configuring"


def test_detection_keeps_finished_phase_while_visible(monkeypatch):
    # After a run the speaker may still answer (now on its static IP); the
    # result must stay on screen instead of flipping back to "detected".
    cfg.state["phase"] = "configured"
    set_detection(monkeypatch, cfg._target_ip())
    poll()
    assert cfg.state["phase"] == "configured"


def test_configure_records_history(monkeypatch):
    run_configure(ok=True, monkeypatch=monkeypatch)
    assert cfg.state["phase"] == "configured"
    entry = cfg.state["history"][0]
    assert entry["schema"] == RUN_RECORD_SCHEMA
    assert entry["tool"] == "speaker"
    assert entry["status"] == "ok"
    assert entry["serial"] == "TM-CS20-000001-XX"
    assert entry["device"]["ip"] == cfg._target_ip()
    assert entry["device"]["from_host"] == HOST


def test_failed_configure_sets_error(monkeypatch):
    run_configure(ok=False, monkeypatch=monkeypatch)
    assert cfg.state["phase"] == "error"
    assert cfg.state["history"][0]["status"] == "error"


def test_hostname_uses_target_ip_octet():
    assert cfg.hostname_for({}) == f"speaker-{cfg._target_ip().rsplit('.', 1)[-1]}"
