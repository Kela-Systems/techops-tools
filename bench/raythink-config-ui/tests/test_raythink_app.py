"""State-machine tests for the Raythink camera configurator (raythink_app.py).

No hardware/network: detection is faked via `_reachable` + `read_device_mac`,
the device pipeline via `_do_configure`, and the IP-state file write is stubbed
so the real ip_state.json is never touched.
"""
import asyncio

import pytest

from bench_core.run_record import RUN_RECORD_SCHEMA

import raythink_app as mod

cfg = mod.configurator

HOST = "192.168.1.123"


def fake_result(ok=True, serial="SN-CAM-1", hostname="raythink-30",
                mac="aa:bb:cc:dd:ee:03"):
    return {
        "ok": ok,
        "hostname": hostname,
        "identity": {"serial": serial, "mac": mac, "model": "Raythink",
                     "firmware": "1.0.0"},
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
    cfg.state["cycle_next"] = 30
    cfg.state["ip_mode"] = "manual"
    monkeypatch.setattr(cfg, "_save_log", lambda entry: None)
    monkeypatch.setattr(cfg, "_save_ip_state", lambda: None)


def set_detection(monkeypatch, reachable, mac="aa:bb:cc:dd:ee:03"):
    monkeypatch.setattr(cfg, "_reachable", lambda *a, **k: reachable)
    monkeypatch.setattr(mod, "read_device_mac", lambda *a, **k: mac)


def poll():
    async def run():
        await cfg.poll_once(asyncio.get_running_loop())
    asyncio.run(run())


def run_inputs(octet=30, advance_cycle=False, ok=True, monkeypatch=None):
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result(ok=ok))
    inputs = {"profile": "lan", "profile_path": "x", "octet": octet,
              "target_ip": f"192.168.88.{octet}", "advance_cycle": advance_cycle,
              "host": HOST, "mac": "aa:bb:cc:dd:ee:03"}
    asyncio.run(cfg.execute_run(inputs, "test"))


def test_waiting_to_detected_and_back(monkeypatch):
    set_detection(monkeypatch, True)
    poll()
    assert cfg.state["phase"] == "detected"
    assert cfg.state["active_mac"] == "aa:bb:cc:dd:ee:03"

    set_detection(monkeypatch, False)
    poll()
    assert cfg.state["phase"] == "waiting"
    assert cfg.state["active_host"] is None


def test_busy_blocks_detection(monkeypatch):
    cfg.state["busy"] = True
    cfg.state["phase"] = "configuring"
    set_detection(monkeypatch, True)
    poll()
    assert cfg.state["phase"] == "configuring"


def test_configure_records_history(monkeypatch):
    run_inputs(octet=35, monkeypatch=monkeypatch)
    assert cfg.state["phase"] == "configured"
    entry = cfg.state["history"][0]
    assert entry["schema"] == RUN_RECORD_SCHEMA
    assert entry["tool"] == "raythink"
    assert entry["status"] == "ok"
    assert entry["device"]["ip"] == "192.168.88.35"
    assert entry["device"]["profile"] == "lan"


def test_cycle_advances_only_on_success(monkeypatch):
    cfg.state["cycle_next"] = 30
    run_inputs(octet=30, advance_cycle=True, ok=True, monkeypatch=monkeypatch)
    assert cfg.state["cycle_next"] == 31

    cfg.state["cycle_next"] = 31
    run_inputs(octet=31, advance_cycle=True, ok=False, monkeypatch=monkeypatch)
    assert cfg.state["cycle_next"] == 31   # failed run keeps its slot


def test_advance_cycle_wraps_at_max():
    lo, hi = cfg._octet_range()
    cfg.state["cycle_next"] = hi
    cfg._advance_cycle()
    assert cfg.state["cycle_next"] == lo


def test_hostname_uses_octet():
    assert cfg.hostname_for({"octet": 30}) == "raythink-30"
