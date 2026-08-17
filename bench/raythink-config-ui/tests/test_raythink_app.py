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


def run_inputs(octet=30, advance_cycle=False, ok=True, monkeypatch=None,
               ip_mode="manual", result=None):
    monkeypatch.setattr(cfg, "_do_configure",
                        lambda inputs: result or fake_result(ok=ok))
    inputs = {"profile": "lan", "profile_path": "x", "octet": octet,
              "ip_mode": ip_mode,
              "target_ip": "" if octet is None else f"192.168.88.{octet}",
              "advance_cycle": advance_cycle,
              "host": HOST, "mac": "aa:bb:cc:dd:ee:03"}
    asyncio.run(cfg.execute_run(inputs, "test"))


def configure(monkeypatch, **body):
    """POST /api/configure through the real route, with the pipeline stubbed."""
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result())
    captured = {}
    real_execute = cfg.execute_run

    async def execute_run(inputs, label):
        captured["inputs"] = inputs
        captured["label"] = label
        return await real_execute(inputs, label)

    monkeypatch.setattr(cfg, "execute_run", execute_run)
    handler = next(r.endpoint for r in mod.app.routes
                   if getattr(r, "path", "") == "/api/configure")
    return asyncio.run(handler(mod.ConfigureBody(**body))), captured


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


# ── DHCP mode ────────────────────────────────────────────────────────────────

def test_hostname_for_a_dhcp_run():
    assert cfg.hostname_for({"octet": None}) == "raythink-dhcp"


def test_dhcp_run_records_no_ip(monkeypatch):
    """The bench assigns no address on a DHCP run, so the record carries none —
    only how the camera was addressed."""
    result = fake_result(hostname="raythink-dhcp")
    result["ip_mode"] = "dhcp"
    run_inputs(octet=None, ip_mode="dhcp", monkeypatch=monkeypatch, result=result)

    entry = cfg.state["history"][0]
    assert entry["status"] == "ok"
    assert entry["device"]["hostname"] == "raythink-dhcp"
    assert entry["device"]["ip"] == ""
    assert entry["device"]["ip_mode"] == "dhcp"


def test_static_run_records_the_mode_too(monkeypatch):
    run_inputs(octet=35, monkeypatch=monkeypatch)
    assert cfg.state["history"][0]["device"]["ip_mode"] == "static"


def test_dhcp_run_never_burns_a_cycle_number(monkeypatch):
    cfg.state["cycle_next"] = 30
    run_inputs(octet=None, ip_mode="dhcp", monkeypatch=monkeypatch)
    assert cfg.state["cycle_next"] == 30


# ── the configure route's mode handling ──────────────────────────────────────

def test_configure_dhcp_needs_no_octet(monkeypatch):
    set_detection(monkeypatch, True)
    poll()
    _out, captured = configure(monkeypatch, profile="lan", ip_mode="dhcp")

    assert captured["inputs"]["octet"] is None
    assert captured["inputs"]["target_ip"] == ""
    assert "DHCP" in captured["label"]


def test_configure_dhcp_refused_without_a_mac(monkeypatch):
    """DHCP is verified by finding the camera again by MAC — without one there
    is nothing to find it by, so the run is refused up front."""
    set_detection(monkeypatch, True, mac=None)
    poll()
    out, captured = configure(monkeypatch, profile="lan", ip_mode="dhcp")

    assert "MAC" in out["error"]
    assert "inputs" not in captured


def test_configure_manual_still_requires_an_octet_in_range(monkeypatch):
    set_detection(monkeypatch, True)
    poll()
    out, _ = configure(monkeypatch, profile="lan", ip_mode="manual")
    assert "octet" in out["error"]

    out, _ = configure(monkeypatch, profile="lan", ip_mode="manual", octet=99)
    assert "out of range" in out["error"]


def test_configure_persists_the_chosen_mode(monkeypatch):
    set_detection(monkeypatch, True)
    poll()
    configure(monkeypatch, profile="lan", ip_mode="dhcp")
    assert cfg.state["ip_mode"] == "dhcp"


def test_unknown_mode_falls_back_to_manual():
    assert mod.norm_ip_mode("dhcp") == "dhcp"
    assert mod.norm_ip_mode("cycle") == "cycle"
    for junk in ("", "static", "nonsense", None):
        assert mod.norm_ip_mode(junk) == "manual"
