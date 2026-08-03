"""State-machine tests for the OTD500 configurator (otd_app.py).

No hardware/network: detection is faked via `_reachable` + `read_device_mac`,
and the device pipeline via `_do_configure`. Run with `pytest` from the bench
root (the shared venv) or from this folder.
"""
import asyncio

import pytest

from bench_core.bench_ui import OperatorStore
from bench_core.run_record import RUN_RECORD_SCHEMA

import otd_app as mod

cfg = mod.configurator


def fake_result(ok=True, serial="SN-OTD-1", hostname="otd-haifa",
                mac="aa:bb:cc:dd:ee:01"):
    """A bench_ui `_do_configure` result (post-pipeline shape)."""
    return {
        "ok": ok,
        "hostname": hostname,
        "identity": {"serial": serial, "mac": mac, "model": "OTD500",
                     "firmware": "OTD5_R_00.07.20", "imei": "350000000000001"},
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


def set_detection(monkeypatch, reachable, mac="aa:bb:cc:dd:ee:01"):
    monkeypatch.setattr(cfg, "_reachable", lambda *a, **k: reachable)
    monkeypatch.setattr(mod, "read_device_mac", lambda *a, **k: mac)


def poll():
    async def run():
        await cfg.poll_once(asyncio.get_running_loop())
    asyncio.run(run())


def configure(inputs):
    asyncio.run(cfg.execute_run(inputs, "test"))


def test_waiting_to_detected_and_back(monkeypatch):
    set_detection(monkeypatch, True)
    poll()
    assert cfg.state["phase"] == "detected"
    assert cfg.state["detected"] is True
    assert cfg.state["active_mac"] == "aa:bb:cc:dd:ee:01"

    set_detection(monkeypatch, False)
    poll()
    assert cfg.state["phase"] == "waiting"
    assert cfg.state["active_mac"] is None


def test_busy_blocks_detection(monkeypatch):
    cfg.state["busy"] = True
    cfg.state["phase"] = "configuring"
    set_detection(monkeypatch, True)
    poll()
    assert cfg.state["phase"] == "configuring"   # detection skipped mid-run
    assert cfg.state["detected"] is False


def test_configure_records_history(monkeypatch):
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result())
    configure({"site_name": "haifa", "label_password": "x", "mac": "aa:bb:cc:dd:ee:01"})
    assert cfg.state["phase"] == "configured"
    entry = cfg.state["history"][0]
    assert entry["schema"] == RUN_RECORD_SCHEMA
    assert entry["tool"] == "otd"
    assert entry["status"] == "ok"
    assert entry["serial"] == "SN-OTD-1"
    assert entry["device"]["site_name"] == "haifa"
    assert entry["device"]["hostname"] == "otd-haifa"
    assert cfg.state["busy"] is False


def test_failed_run_sets_error(monkeypatch):
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result(ok=False))
    configure({"site_name": "haifa", "label_password": "", "mac": None})
    assert cfg.state["phase"] == "error"
    assert cfg.state["history"][0]["status"] == "error"


def test_detected_stays_until_unplug(monkeypatch):
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result())
    configure({"site_name": "haifa", "label_password": "", "mac": "aa:bb:cc:dd:ee:01"})
    assert cfg.state["phase"] == "configured"
    # Still plugged in after the run: poll must not bounce back to "detected".
    set_detection(monkeypatch, True)
    poll()
    assert cfg.state["phase"] == "configured"


def test_hostname_uses_prefix():
    assert cfg.hostname_for({"site_name": "Haifa Port"}) == "otd-haifa-port"


# ── run provenance (operator / station / version / config, TEC-345+356) ──────

def test_run_records_carry_provenance(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "operator_store", OperatorStore(tmp_path / "op.json"))
    cfg.operator_store.set("Dana K")
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result())
    configure({"site_name": "haifa", "label_password": "", "mac": None})
    entry = cfg.state["history"][0]
    assert entry["operator"] == "Dana K"
    assert entry["station_id"] == cfg.station_id
    assert entry["bench_version"] == cfg.bench_version
    assert entry["config_hash"] == cfg.config_hash


def test_unset_operator_stamps_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "operator_store", OperatorStore(tmp_path / "op.json"))
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result())
    configure({"site_name": "haifa", "label_password": "", "mac": None})
    assert cfg.state["history"][0]["operator"] == "unknown"


def test_operator_store_roundtrip(tmp_path):
    store = OperatorStore(tmp_path / "op.json")
    assert store.get() == ""
    store.set("  Dana K  ")
    assert store.get() == "Dana K"
    # Another tool's store on the same station file sees the change.
    assert OperatorStore(tmp_path / "op.json").get() == "Dana K"
    store.set("")
    assert store.get() == ""


def test_public_state_exposes_station_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "operator_store", OperatorStore(tmp_path / "op.json"))
    cfg.operator_store.set("Dana K")
    s = cfg.public_state()
    assert s["operator"] == "Dana K"
    assert s["station_id"] == cfg.station_id
    assert s["bench_version"] == cfg.bench_version
    # Config self-check (TEC-356): the fingerprint and warnings are part of
    # /api/state, so the page banner and central tooling can read them.
    assert s["config_hash"] == cfg.config_hash
    assert s["config_warnings"] == cfg.config_warnings
    assert isinstance(s["config_warnings"], list)
