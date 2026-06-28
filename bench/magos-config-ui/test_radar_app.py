"""State-machine tests for the radar configurator (app.py).

These drive the engine's `poll_step` with fake detection sequences and a fake
device, so they run with no hardware and no network. Run with:
    .venv/bin/python -m pytest
"""
import asyncio
import contextlib
from types import SimpleNamespace

import pytest

import app as radar_mod
import magos_bench
from magos_bench import AUTO_IDLE_TIMEOUT_SEC, MISS_THRESHOLD

radar = radar_mod.configurator


def fake_result(ip, serial, ok=True, skipped=False):
    return {
        "ok": ok and not skipped, "skipped": skipped,
        "ip": ip if "/" in ip else f"{ip}/24",
        "identity": {"serial": serial, "mac": "aa:bb", "model": "AR-300"},
        "raw": {}, "steps": [], "log": "", "error": None if ok else "boom",
        "verified": ok, "verify_detail": "ok" if ok else None,
    }


class FakeDevice:
    """Stands in for do_configure: configures whatever 'unit' is plugged in."""

    def __init__(self, serial="SN-001", ok=True):
        self.serial = serial
        self.ok = ok
        self.runs = []      # (ip, serial) per actual configuration
        self.channels = []  # channel passed per actual configuration

    def __call__(self, target, host, avoid_serial=None):
        if avoid_serial and self.serial == avoid_serial:
            return fake_result(target["ip"], self.serial, skipped=True)
        self.runs.append((target["ip"], self.serial))
        self.channels.append(target["channel"])
        return fake_result(target["ip"], self.serial, ok=self.ok)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    radar._misses = 0
    radar.state.update({
        "phase": "waiting", "detected": False, "active_host": None, "busy": False,
        "message": "", "last_result": None, "history": [],
        "auto": {"enabled": False, "channel": None, "ip": None},
        "cycle": {"enabled": False, "index": 0, "count": 0},
        "last_ok_serial": None, "net_warning": None,
    })
    monkeypatch.setattr(radar, "_save_log", lambda entry, raw: None)


@pytest.fixture
def device(monkeypatch):
    dev = FakeDevice()
    monkeypatch.setattr(radar, "do_configure", dev)
    return dev


HOST = "192.168.40.50"


def poll(active):
    asyncio.run(radar.poll_step(active))


def unplug():
    for _ in range(MISS_THRESHOLD):
        poll(None)


def test_waiting_to_detected_and_back():
    poll(HOST)
    assert radar.state["phase"] == "detected"
    unplug()
    assert radar.state["phase"] == "waiting"


def test_single_miss_does_not_reset_detected():
    poll(HOST)
    poll(None)  # one blip < MISS_THRESHOLD
    assert radar.state["phase"] == "detected"


def test_manual_configure_records_history(device):
    poll(HOST)
    asyncio.run(radar.run_configuration({"channel": "0", "ip": "192.168.88.50"}, HOST))
    assert radar.state["phase"] == "configured"
    assert radar.state["history"][0]["status"] == "ok"
    assert radar.state["history"][0]["ip"] == "192.168.88.50/24"
    assert radar.state["last_ok_serial"] == "SN-001"


def test_auto_mode_configures_on_detect(device):
    radar.state["auto"] = {"enabled": True, "channel": "2", "ip": None}
    poll(HOST)
    assert device.runs == [("192.168.88.52", "SN-001")]
    assert device.channels == ["2"]  # RF channel passed through to do_configure
    assert radar.state["phase"] == "configured"


def test_auto_mode_skips_same_serial_on_flap(device):
    radar.state["auto"] = {"enabled": True, "channel": "0", "ip": None}
    poll(HOST)                      # SN-001 configured
    poll(None)                      # blip while it applies its new IP
    poll(HOST)                      # same unit answers again
    assert len(device.runs) == 1    # not configured twice
    assert radar.state["phase"] == "configured"
    unplug()
    device.serial = "SN-002"        # a genuinely new unit
    poll(HOST)
    assert len(device.runs) == 2


def test_cycle_assigns_sequential_channels_and_wraps(device):
    radar.state["cycle"] = {"enabled": True, "index": 0, "count": 0}
    for i in range(5):
        device.serial = f"SN-{i:03d}"
        poll(HOST)
        unplug()
    ips = [ip for ip, _ in device.runs]
    assert ips == ["192.168.88.50", "192.168.88.51", "192.168.88.52",
                   "192.168.88.53", "192.168.88.50"]  # wraps after 4
    assert radar.state["cycle"]["count"] == 5
    assert radar.state["cycle"]["index"] == 1


def test_cycle_failure_does_not_advance_channel(device):
    radar.state["cycle"] = {"enabled": True, "index": 0, "count": 0}
    device.ok = False
    poll(HOST)
    assert radar.state["phase"] == "error"
    assert radar.state["cycle"] == {"enabled": True, "index": 0, "count": 0}


def test_cycle_skip_does_not_advance_channel(device):
    radar.state["cycle"] = {"enabled": True, "index": 0, "count": 0}
    poll(HOST)                      # SN-001 -> channel 0
    poll(None)
    poll(HOST)                      # same unit flaps back: skipped
    assert len(device.runs) == 1
    assert radar.state["cycle"]["index"] == 1   # still waiting for unit #2


def test_busy_blocks_poll_decisions(device):
    radar.state["busy"] = True
    radar.state["auto"] = {"enabled": True, "channel": "0", "ip": None}
    poll(HOST)
    assert device.runs == []


def test_auto_and_cycle_are_mutually_exclusive():
    radar.set_cycle(True)
    assert radar.state["cycle"]["enabled"] is True
    radar.set_auto(True, "1", None)
    assert radar.state["auto"]["enabled"] is True
    assert radar.state["cycle"]["enabled"] is False
    radar.set_cycle(True)
    assert radar.state["auto"]["enabled"] is False


def test_cycle_defaults_to_channel_zero():
    radar.set_cycle(True)
    assert radar.state["cycle"]["index"] == 0
    radar.set_cycle(False)


def test_cycle_starts_from_requested_channel():
    radar.set_cycle(True, start_channel="2")
    assert radar.state["cycle"]["enabled"] is True
    assert radar.state["cycle"]["index"] == radar.cycle_channels.index("2")
    radar.set_cycle(False)


def test_cycle_rejects_unknown_start_channel():
    res = radar.set_cycle(True, start_channel="9")
    assert "error" in res
    assert radar.state["cycle"]["enabled"] is False


@pytest.fixture
def clock(monkeypatch):
    """A controllable monotonic clock so idle-timeout tests don't really wait."""
    now = {"t": 1000.0}
    monkeypatch.setattr(radar, "_now", lambda: now["t"])
    radar._last_activity = now["t"]
    return now


def test_auto_mode_disarms_after_idle(clock):
    radar.state["auto"] = {"enabled": True, "channel": "0", "ip": None}
    poll(None)                                  # idle, but not long enough yet
    assert radar.state["auto"]["enabled"] is True
    clock["t"] += AUTO_IDLE_TIMEOUT_SEC
    poll(None)
    assert radar.state["auto"]["enabled"] is False
    assert radar.state["phase"] == "waiting"


def test_cycle_mode_disarms_after_idle(clock):
    radar.state["cycle"] = {"enabled": True, "index": 2, "count": 2}
    clock["t"] += AUTO_IDLE_TIMEOUT_SEC
    poll(None)
    assert radar.state["cycle"]["enabled"] is False
    assert radar.state["phase"] == "waiting"


def test_device_presence_resets_idle_timer(clock, device):
    radar.state["auto"] = {"enabled": True, "channel": "0", "ip": None}
    clock["t"] += AUTO_IDLE_TIMEOUT_SEC - 1     # almost timed out...
    poll(HOST)                                  # ...but a unit shows up: activity
    unplug()
    clock["t"] += AUTO_IDLE_TIMEOUT_SEC - 1     # not enough since that activity
    poll(None)
    assert radar.state["auto"]["enabled"] is True


def test_closing_last_tab_stops_server(monkeypatch):
    monkeypatch.setattr(magos_bench, "SHUTDOWN_GRACE_SEC", 0.01)

    async def scenario():
        radar._clients = 0
        radar._shutdown_task = None
        radar._server = SimpleNamespace(should_exit=False)
        radar._note_client_connect()        # tab opened
        radar._note_client_disconnect()     # tab closed — arms the timer
        await radar._shutdown_task           # let the grace window elapse
        return radar._server.should_exit

    assert asyncio.run(scenario()) is True


def test_refresh_does_not_stop_server(monkeypatch):
    monkeypatch.setattr(magos_bench, "SHUTDOWN_GRACE_SEC", 0.05)

    async def scenario():
        radar._clients = 0
        radar._shutdown_task = None
        radar._server = SimpleNamespace(should_exit=False)
        radar._note_client_connect()        # tab opened
        radar._note_client_disconnect()     # refresh drops the socket...
        pending = radar._shutdown_task
        radar._note_client_connect()        # ...and reconnects right away
        with contextlib.suppress(asyncio.CancelledError):
            await pending                    # the armed stop was cancelled
        await asyncio.sleep(0.1)             # well past the old grace window
        return radar._server.should_exit, radar._clients

    should_exit, clients = asyncio.run(scenario())
    assert should_exit is False
    assert clients == 1


def test_configure_route_rejects_when_not_detected():
    result = asyncio.run(radar.configure_request("0", None))
    assert "error" in result
