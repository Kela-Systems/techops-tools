"""State-machine tests for the APU configurator (apu_app.py).

Same approach as test_radar_app.py: fake detection sequences + a fake device,
no hardware or network needed. Run with:  .venv/bin/python -m pytest
"""
import asyncio

import pytest

from bench_core.run_record import RUN_RECORD_SCHEMA

import apu_app as apu_mod
from apu_configure import (
    APU_RADAR_ASSIGNMENTS,
    REQUIRED_APU_FIRMWARE,
    firmware_ok,
    firmware_version,
    radar_base_url,
)
from magos_bench import AUTO_IDLE_TIMEOUT_SEC, MISS_THRESHOLD

apu = apu_mod.configurator

# (radar_id, ip) pairs each APU slot must assign — derived from the same map
# the app uses, asserted literally in the cycle test below.
APU0_RADARS = [("radar_0", "192.168.88.50"), ("radar_1", "192.168.88.51")]
APU1_RADARS = [("radar_2", "192.168.88.52"), ("radar_3", "192.168.88.53")]


def radar_pairs(target):
    return [(r["radar_id"], r["ip"]) for r in target.get("radars") or []]


def fake_result(ip, serial, ok=True, skipped=False):
    return {
        "ok": ok and not skipped, "skipped": skipped, "ip": ip,
        "identity": {"serial": serial, "mac": "aa:bb", "model": "MSA1588APU"},
        "raw": {}, "firmware": REQUIRED_APU_FIRMWARE, "steps": [], "log": "",
        "error": None if ok else "boom",
        "verified": ok, "verify_detail": "ok" if ok else None,
    }


class FakeDevice:
    def __init__(self, serial="APU-001", ok=True):
        self.serial = serial
        self.ok = ok
        self.runs = []  # (ip, [(radar_id, ip), ...], serial) per configuration

    def __call__(self, target, host, avoid_serial=None):
        if avoid_serial and self.serial == avoid_serial:
            return fake_result(target["ip"], self.serial, skipped=True)
        self.runs.append((target["ip"], radar_pairs(target), self.serial))
        return fake_result(target["ip"], self.serial, ok=self.ok)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    apu._misses = 0
    apu.state.update({
        "phase": "waiting", "detected": False, "active_host": None, "busy": False,
        "message": "", "last_result": None, "history": [],
        "auto": {"enabled": False, "channel": None, "ip": None, "radar_ips": None},
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


def test_cycle_maps_apu_and_controlled_radars(device):
    apu.state["cycle"] = {"enabled": True, "index": 0, "count": 0}
    for i in range(3):
        device.serial = f"APU-{i:03d}"
        poll(HOST)
        unplug()
    assert device.runs == [
        ("192.168.88.60", APU0_RADARS, "APU-000"),
        ("192.168.88.61", APU1_RADARS, "APU-001"),
        ("192.168.88.60", APU0_RADARS, "APU-002"),  # wrapped after 2
    ]
    assert apu.state["cycle"]["count"] == 3
    assert apu.state["cycle"]["index"] == 1


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


def test_configure_records_canonical_entry(device):
    asyncio.run(apu.run_configuration(apu.resolve_target("0", None), HOST))
    entry = apu.state["history"][0]
    assert entry["schema"] == RUN_RECORD_SCHEMA
    assert entry["tool"] == "magos-apu"
    assert entry["status"] == "ok"
    assert entry["firmware"] == REQUIRED_APU_FIRMWARE
    assert entry["device"]["ip"] == "192.168.88.60"
    assert entry["device"]["radars"] == APU_RADAR_ASSIGNMENTS["0"]
    assert entry["device"]["radar_ip"] == \
        "radar_0=192.168.88.50, radar_1=192.168.88.51"


def test_manual_target_assigns_radar_ids_in_order():
    target = apu.resolve_target("other", "192.168.10.60",
                                "192.168.10.50, 192.168.10.51")
    assert target["ip"] == "192.168.10.60"
    assert radar_pairs(target) == [("radar_0", "192.168.10.50"),
                                   ("radar_1", "192.168.10.51")]


def test_auto_mode_uses_fixed_target(device):
    apu.state["auto"] = {"enabled": True, "channel": "1", "ip": None, "radar_ips": None}
    poll(HOST)
    assert device.runs == [("192.168.88.61", APU1_RADARS, "APU-001")]
    assert apu.state["phase"] == "configured"


def test_auto_mode_skips_same_serial_on_flap(device):
    apu.state["auto"] = {"enabled": True, "channel": "0", "ip": None, "radar_ips": None}
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


def test_cycle_defaults_to_channel_zero():
    apu.set_cycle(True)
    assert apu.state["cycle"]["index"] == 0
    apu.set_cycle(False)


def test_cycle_starts_from_requested_channel():
    apu.set_cycle(True, start_channel="1")
    assert apu.state["cycle"]["enabled"] is True
    assert apu.state["cycle"]["index"] == apu.cycle_channels.index("1")
    apu.set_cycle(False)


def test_cycle_rejects_unknown_start_channel():
    res = apu.set_cycle(True, start_channel="9")
    assert "error" in res
    assert apu.state["cycle"]["enabled"] is False


@pytest.fixture
def clock(monkeypatch):
    """A controllable monotonic clock so idle-timeout tests don't really wait."""
    now = {"t": 1000.0}
    monkeypatch.setattr(apu, "_now", lambda: now["t"])
    apu._last_activity = now["t"]
    return now


def test_auto_mode_disarms_after_idle(clock):
    apu.state["auto"] = {"enabled": True, "channel": "0", "ip": None, "radar_ips": None}
    poll(None)                                  # idle, but not long enough yet
    assert apu.state["auto"]["enabled"] is True
    clock["t"] += AUTO_IDLE_TIMEOUT_SEC
    poll(None)
    assert apu.state["auto"]["enabled"] is False
    assert apu.state["phase"] == "waiting"


def test_cycle_mode_disarms_after_idle(clock):
    apu.state["cycle"] = {"enabled": True, "index": 2, "count": 2}
    clock["t"] += AUTO_IDLE_TIMEOUT_SEC
    poll(None)
    assert apu.state["cycle"]["enabled"] is False
    assert apu.state["phase"] == "waiting"


def test_device_presence_resets_idle_timer(clock, device):
    apu.state["auto"] = {"enabled": True, "channel": "0", "ip": None, "radar_ips": None}
    clock["t"] += AUTO_IDLE_TIMEOUT_SEC - 1     # almost timed out...
    poll(HOST)                                  # ...but a unit shows up: activity
    unplug()
    clock["t"] += AUTO_IDLE_TIMEOUT_SEC - 1     # not enough since that activity
    poll(None)
    assert apu.state["auto"]["enabled"] is True


def test_configure_route_rejects_when_not_detected():
    result = asyncio.run(apu.configure_request("0", None))
    assert "error" in result


# ── firmware gate (the multi-radar API only exists on 3.1.2+) ───────────────


def _system_payload(version):
    """Shaped like a live APU's identity payloads: the firmware version is
    /systemStatus's softwareVersion; /system's swComponents only lists
    sub-component versions (confirmed against a real 3.1.2-rc5 unit)."""
    return {
        "/systemStatus": {"productModel": "AR Processing Unit (APU)",
                          "softwareVersion": version},
        "/system": {"swComponents": [{"name": "chrony", "version": "4.2.0"},
                                     {"name": "dashboard", "version": "2.5.0-rc1"},
                                     {"name": "phoenix", "version": "1.3.1"}]},
    }


def test_firmware_version_extracted_from_system_status():
    assert firmware_version(_system_payload("3.1.2-rc5")) == "3.1.2-rc5"
    assert firmware_version({}) is None


def test_firmware_version_ignores_component_versions():
    # Without softwareVersion, the swComponents entries (phoenix 1.3.1, ...)
    # must NOT be picked up as the unit's firmware.
    raw = {"/system": _system_payload("x")["/system"]}
    assert firmware_version(raw) is None


def test_radar_base_url_requires_scheme():
    # The 3.1.2 firmware rejects a bare IP for remote_base_url ("relative URL
    # without a base"); the client must send a full URL.
    assert radar_base_url("192.168.88.50") == "http://192.168.88.50"
    assert radar_base_url("https://192.168.88.50") == "https://192.168.88.50"


@pytest.mark.parametrize("version,ok", [
    ("3.1.2", True),
    ("3.1.2-rc5", True),      # rc builds accepted for now (current units)
    ("v3.1.2-rc1", True),
    ("3.0.1", False),
    ("3.1.1", False),
    ("3.1.20", False),
    (None, False),
])
def test_firmware_ok(version, ok):
    assert firmware_ok(version) is ok


class FakeAPUClient:
    """Stands in for apu_configure.APUClient in do_configure; records the
    configuration calls so the gate tests can assert what was (not) changed."""
    version = "3.0.1"
    calls: list = []

    def __init__(self, host, scheme="http", verify=True):
        pass

    def login(self, username, password):
        pass

    def get_identity(self):
        return {"serial": "SN-GATE", "mac": "aa:bb", "model": "MSA1588APU",
                "raw": _system_payload(self.version)}

    def set_ntp_tz(self, *a, **k):
        FakeAPUClient.calls.append("ntp")

    def set_radars(self, radars):
        FakeAPUClient.calls.append(("radars", [(r["radar_id"], r["ip"]) for r in radars]))

    def set_network(self, *a, **k):
        FakeAPUClient.calls.append("network")


@pytest.fixture
def fake_client(monkeypatch):
    FakeAPUClient.calls = []
    monkeypatch.setattr(apu_mod, "APUClient", FakeAPUClient)
    monkeypatch.setattr(apu_mod, "verify_device_at",
                        lambda *a, **k: {"verified": True, "detail": None})
    return FakeAPUClient


def test_firmware_gate_blocks_old_apu(fake_client):
    fake_client.version = "3.0.1"
    result = apu.do_configure(apu.resolve_target("0", None), HOST, None)
    assert result["ok"] is False
    assert result["firmware"] == "3.0.1"
    assert "3.1.2" in result["error"] and "Upgrade the APU manually" in result["error"]
    assert fake_client.calls == []            # nothing was changed on the unit


def test_firmware_gate_accepts_rc_build(fake_client):
    fake_client.version = "3.1.2-rc5"
    result = apu.do_configure(apu.resolve_target("1", None), HOST, None)
    assert result["ok"] is True
    assert result["firmware"] == "3.1.2-rc5"
    assert fake_client.calls == ["ntp", ("radars", APU1_RADARS), "network"]
