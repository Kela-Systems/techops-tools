"""State-machine tests for the RUTM08 configurator (rutm_app.py).

No hardware/network: detection is faked via `_detect_host` + `read_device_mac`,
and the device pipeline via `_do_configure`.
"""
import asyncio
import json

import pytest

from bench_core.run_record import RUN_RECORD_SCHEMA

import rutm_app as mod

cfg = mod.configurator

FACTORY = "192.168.1.1"
FINAL_LAN = "192.168.88.1"


def fake_result(ok=True, serial="SN-RUT-1", hostname="rut-haifa",
                mac="aa:bb:cc:dd:ee:02"):
    return {
        "ok": ok,
        "hostname": hostname,
        "identity": {"serial": serial, "mac": mac, "model": "RUTM08",
                     "firmware": "RUTM_R_00.07.20"},
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


def set_detection(monkeypatch, host, mac="aa:bb:cc:dd:ee:02"):
    monkeypatch.setattr(cfg, "_detect_host", lambda *a, **k: host)
    monkeypatch.setattr(mod, "read_device_mac", lambda *a, **k: mac)


def poll():
    async def run():
        await cfg.poll_once(asyncio.get_running_loop())
    asyncio.run(run())


def configure(inputs):
    asyncio.run(cfg.execute_run(inputs, "test"))


def test_waiting_to_detected_and_back(monkeypatch):
    set_detection(monkeypatch, FACTORY)
    poll()
    assert cfg.state["phase"] == "detected"
    assert cfg.state["active_host"] == FACTORY
    assert cfg.state["at_final_lan"] is False

    set_detection(monkeypatch, None)
    poll()
    assert cfg.state["phase"] == "waiting"
    assert cfg.state["active_host"] is None


def test_detected_on_final_lan_flags_already_provisioned(monkeypatch):
    set_detection(monkeypatch, FINAL_LAN)
    poll()
    assert cfg.state["phase"] == "detected"
    assert cfg.state["at_final_lan"] is True


def test_busy_blocks_detection(monkeypatch):
    cfg.state["busy"] = True
    cfg.state["phase"] = "configuring"
    set_detection(monkeypatch, FACTORY)
    poll()
    assert cfg.state["phase"] == "configuring"
    assert cfg.state["detected"] is False


def test_configure_records_history(monkeypatch):
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result())
    configure({"site_name": "haifa", "initial_password": "x",
               "host": FACTORY, "mac": "aa:bb:cc:dd:ee:02"})
    assert cfg.state["phase"] == "configured"
    entry = cfg.state["history"][0]
    assert entry["schema"] == RUN_RECORD_SCHEMA
    assert entry["tool"] == "rutm"
    assert entry["status"] == "ok"
    assert entry["serial"] == "SN-RUT-1"
    assert entry["device"]["site_name"] == "haifa"


def test_failed_run_sets_error(monkeypatch):
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result(ok=False))
    configure({"site_name": "haifa", "initial_password": "",
               "host": FACTORY, "mac": None})
    assert cfg.state["phase"] == "error"
    assert cfg.state["history"][0]["status"] == "error"


def test_hostname_uses_prefix():
    assert cfg.hostname_for({"site_name": "Haifa Port"}) == "rut-haifa-port"


# ── device-label scanning (TEC-349) ──────────────────────────────────────────
#
# The parser and the arming machinery are covered in bench-core and the OTD
# suite. What is specific here is the field naming: the UI field and the
# pipeline argument are `initial_password` on this tool and `label_password` on
# the OTD one, so the wiring is easy to get subtly wrong in exactly one place.

# A real RUTM08 sticker — note there is no IMEI, this family has no modem.
REAL_RUTM_LABEL = "SN:6010212527;M:20972732F638;U:admin;PW:mL$7=b6N;B:039;"
LABEL_MAC = "20:97:27:32:f6:38"
LABEL_PW = "mL$7=b6N"


@pytest.fixture
def detected(monkeypatch):
    set_detection(monkeypatch, FACTORY, mac=LABEL_MAC)
    poll()
    cfg.clear_armed_label()
    yield
    cfg.clear_armed_label()


def test_scan_arms_the_password(detected):
    assert cfg.arm_label(REAL_RUTM_LABEL) == {}
    assert cfg.resolve_label_password("") == (LABEL_PW, "scan")
    assert cfg.public_state()["label_scan"]["armed"]["family"] == "non-cellular"


def test_scan_of_a_different_router_is_refused(detected):
    cfg.state["active_mac"] = "20:97:27:2b:00:f7"
    assert "error" in cfg.arm_label(REAL_RUTM_LABEL)
    assert cfg.armed_label() is None


def test_scanned_password_reaches_the_pipeline_as_initial_password(detected, monkeypatch):
    seen = {}
    monkeypatch.setattr(cfg, "_do_configure",
                        lambda inputs: seen.update(inputs) or fake_result())
    cfg.arm_label(REAL_RUTM_LABEL)
    password, source = cfg.resolve_label_password("")
    configure({"site_name": "haifa", "initial_password": password,
               "password_source": source, "host": FACTORY, "mac": LABEL_MAC})
    assert seen["initial_password"] == LABEL_PW
    assert cfg.state["history"][0]["device"]["password_source"] == "scan"


def test_typed_password_beats_a_scan(detected):
    cfg.arm_label(REAL_RUTM_LABEL)
    assert cfg.resolve_label_password("by-hand") == ("by-hand", "typed")


def test_scan_state_never_carries_the_password(detected):
    cfg.arm_label(REAL_RUTM_LABEL)
    assert LABEL_PW not in json.dumps(cfg.public_state())


def test_re_running_a_provisioned_router_still_needs_no_scan(monkeypatch):
    # The documented way to re-run an already-provisioned router is to leave
    # the password empty; scanning must not have made that harder.
    set_detection(monkeypatch, FINAL_LAN, mac=LABEL_MAC)
    poll()
    assert cfg.resolve_label_password("") == ("", "shared-fallback")
