"""State-machine tests for the APU configurator (apu_app.py).

Same approach as test_radar_app.py: fake detection sequences + a fake device,
no hardware or network needed. Run with:  .venv/bin/python -m pytest
"""
import asyncio

import pytest

import apu_app as apu_mod
from magos_bench import MISS_THRESHOLD

apu = apu_mod.configurator


def fake_result(ip, serial, ok=True, skipped=False):
    return {
        "ok": ok and not skipped, "skipped": skipped, "ip": ip,
        "identity": {"serial": serial, "mac": "aa:bb", "model": "MSA1588APU"},
        "raw": {}, "steps": [], "log": "", "error": None if ok else "boom",
        "verified": ok, "verify_detail": "ok" if ok else None,
    }


class FakeDevice:
    def __init__(self, serial="APU-001", ok=True):
        self.serial = serial
        self.ok = ok
        self.runs = []  # (ip, radar_ip, serial) per actual configuration

    def __call__(self, target, host, avoid_serial=None):
        if avoid_serial and self.serial == avoid_serial:
            return fake_result(target["ip"], self.serial, skipped=True)
        self.runs.append((target["ip"], target.get("radar_ip"), self.serial))
        return fake_result(target["ip"], self.serial, ok=self.ok)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    apu._misses = 0
    apu.state.update({
        "phase": "waiting", "detected": False, "active_host": None, "busy": False,
        "message": "", "last_result": None, "history": [],
        "auto": {"enabled": False, "channel": None, "ip": None, "radar_ip": None},
        "cycle": {"enabled": False, "index": 0, "count": 0},
        "last_ok_serial": None, "net_warning": None,
    })
    monkeypatch.setattr(apu, "_save_log", lambda entry, raw: None)


@pytest.fixture
def device(monkeypatch):
    dev = FakeDevice()
    monkeypatch.setattr(apu, "do_configure", dev)
    return dev


HOST = "192.168.40.60"


def poll(active):
    asyncio.run(apu.poll_step(active))


def unplug():
    for _ in range(MISS_THRESHOLD):
        poll(None)


def test_waiting_to_detected_and_back():
    poll(HOST)
    assert apu.state["phase"] == "detected"
    unplug()
    assert apu.state["phase"] == "waiting"


def test_single_miss_does_not_reset_detected():
    poll(HOST)
    poll(None)
    assert apu.state["phase"] == "detected"


def test_cycle_maps_apu_and_controlled_radar_ips(device):
    apu.state["cycle"] = {"enabled": True, "index": 0, "count": 0}
    for i in range(4):
        device.serial = f"APU-{i:03d}"
        poll(HOST)
        unplug()
    assert device.runs == [
        ("192.168.88.60", "192.168.88.50", "APU-000"),
        ("192.168.88.61", "192.168.88.51", "APU-001"),
        ("192.168.88.62", "192.168.88.52", "APU-002"),
        ("192.168.88.63", "192.168.88.53", "APU-003"),
    ]
    assert apu.state["cycle"]["count"] == 4
    assert apu.state["cycle"]["index"] == 0  # wrapped, ready for the next group


def test_cycle_skip_same_unit_flap(device):
    apu.state["cycle"] = {"enabled": True, "index": 0, "count": 0}
    poll(HOST)                      # APU-001 -> channel 0
    poll(None)                      # blip while it applies its new IP
    poll(HOST)                      # same unit answers again: skipped
    assert len(device.runs) == 1
    assert apu.state["cycle"] == {"enabled": True, "index": 1, "count": 1}
    assert apu.state["phase"] == "configured"


def test_cycle_failure_does_not_advance_channel(device):
    apu.state["cycle"] = {"enabled": True, "index": 0, "count": 0}
    device.ok = False
    poll(HOST)
    assert apu.state["phase"] == "error"
    assert apu.state["cycle"] == {"enabled": True, "index": 0, "count": 0}


def test_auto_mode_uses_fixed_target(device):
    apu.state["auto"] = {"enabled": True, "channel": "3", "ip": None, "radar_ip": None}
    poll(HOST)
    assert device.runs == [("192.168.88.63", "192.168.88.53", "APU-001")]
    assert apu.state["phase"] == "configured"


def test_auto_mode_skips_same_serial_on_flap(device):
    apu.state["auto"] = {"enabled": True, "channel": "0", "ip": None, "radar_ip": None}
    poll(HOST)
    poll(None)
    poll(HOST)
    assert len(device.runs) == 1
    unplug()
    device.serial = "APU-002"
    poll(HOST)
    assert len(device.runs) == 2


def test_busy_blocks_poll_decisions(device):
    apu.state["busy"] = True
    apu.state["cycle"] = {"enabled": True, "index": 0, "count": 0}
    poll(HOST)
    assert device.runs == []


def test_auto_and_cycle_are_mutually_exclusive():
    apu.set_cycle(True)
    assert apu.state["cycle"]["enabled"] is True
    apu.set_auto(True, "1", None)
    assert apu.state["auto"]["enabled"] is True
    assert apu.state["cycle"]["enabled"] is False
    apu.set_cycle(True)
    assert apu.state["auto"]["enabled"] is False


def test_configure_route_rejects_when_not_detected():
    result = asyncio.run(apu.configure_request("0", None))
    assert "error" in result
