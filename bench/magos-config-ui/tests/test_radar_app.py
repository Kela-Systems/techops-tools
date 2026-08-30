"""State-machine tests for the radar configurator (app.py).

These drive the engine's `poll_step` with fake detection sequences and a fake
device, so they run with no hardware and no network. Run with:
    .venv/bin/python -m pytest
"""
import asyncio

import pytest

from bench_core.run_record import RUN_RECORD_SCHEMA

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
        "on_factory_ip": False, "settled_host": None,
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
FINISHED = "192.168.88.51"       # where a provisioned radar lives


def poll(active, on_factory_ip=True):
    asyncio.run(radar.poll_step(active, on_factory_ip))


def poll_finished(active=FINISHED):
    """A unit answering on a permanent address: finished, so Verify-only."""
    poll(active, on_factory_ip=False)


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
    entry = radar.state["history"][0]
    assert entry["schema"] == RUN_RECORD_SCHEMA
    assert entry["tool"] == "magos-radar"
    assert entry["status"] == "ok"
    assert entry["device"]["ip"] == "192.168.88.50/24"
    assert entry["device"]["channel"] == "0"
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


def test_configure_route_rejects_when_not_detected():
    result = asyncio.run(radar.configure_request("0", None))
    assert "error" in result


# ── run provenance (operator / station / version / config, TEC-345+356) ──────

def test_run_records_carry_provenance(device, monkeypatch, tmp_path):
    from bench_core.bench_ui import OperatorStore
    monkeypatch.setattr(radar, "operator_store", OperatorStore(tmp_path / "op.json"))
    radar.operator_store.set("Dana K")
    asyncio.run(radar.run_configuration({"channel": "0", "ip": "192.168.88.50"}, HOST))
    entry = radar.state["history"][0]
    assert entry["operator"] == "Dana K"
    assert entry["station_id"] == radar.station_id
    assert entry["bench_version"] == radar.bench_version
    assert entry["config_hash"] == radar.config_hash


def test_config_hash_tracks_settings_changes():
    before = radar.config_hash
    assert before == radar._config_fingerprint()
    original_ntp = radar.cfg["ntp"]
    radar.apply_settings({"ntp": "10.99.99.99"})
    try:
        assert radar.config_hash != before          # settings drift → new hash
        assert radar.public_state()["config_hash"] == radar.config_hash
    finally:
        radar.apply_settings({"ntp": original_ntp})
    assert radar.config_hash == before              # restored settings → same hash


def test_apply_settings_persists_to_config_file(tmp_path, monkeypatch):
    """A UI settings edit is written back into the tool's section of the
    config file — preserving comments, non-UI keys (host, channel_ips) and
    the other tool's section."""
    import json
    path = tmp_path / "persisted.config.json"
    path.write_text(json.dumps({
        "_comment": "top-level comment",
        "ar300": {"_comment": "radar comment", "host": "192.168.40.50",
                  "channel_ips": {"0": "192.168.88.50"}},
        "apu": {"host": "192.168.40.60"},
    }), encoding="utf-8")
    monkeypatch.setattr(radar, "config_path", path)

    original_ntp = radar.cfg["ntp"]
    radar.apply_settings({"ntp": "10.11.12.13"})
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["ar300"]["ntp"] == "10.11.12.13"
        assert saved["ar300"]["_comment"] == "radar comment"
        assert saved["ar300"]["host"] == "192.168.40.50"
        assert saved["ar300"]["channel_ips"] == {"0": "192.168.88.50"}
        assert saved["apu"] == {"host": "192.168.40.60"}   # other tool untouched
        assert saved["_comment"] == "top-level comment"
        # The full session settings land in the section, not just the edit —
        # so a restart reconstructs exactly what the UI showed.
        assert saved["ar300"]["gateway"] == radar.cfg["gateway"]
    finally:
        monkeypatch.setattr(radar, "config_path", tmp_path / "scratch.json")
        radar.apply_settings({"ntp": original_ntp})


def test_apply_settings_survives_missing_config_file(tmp_path, monkeypatch):
    """Persistence must create the file when absent (fresh checkout) and a
    write failure must never block the in-memory settings change."""
    import json
    path = tmp_path / "created" / "magos.config.json"
    monkeypatch.setattr(radar, "config_path", path)
    original_ntp = radar.cfg["ntp"]
    radar.apply_settings({"ntp": "10.99.88.77"})
    try:
        assert radar.cfg["ntp"] == "10.99.88.77"
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["ar300"]["ntp"] == "10.99.88.77"
    finally:
        radar.apply_settings({"ntp": original_ntp})


def test_unset_operator_stamps_unknown(device, monkeypatch, tmp_path):
    from bench_core.bench_ui import OperatorStore
    monkeypatch.setattr(radar, "operator_store", OperatorStore(tmp_path / "op.json"))
    asyncio.run(radar.run_configuration({"channel": "0", "ip": "192.168.88.50"}, HOST))
    assert radar.state["history"][0]["operator"] == "unknown"


# ── detection of already-provisioned units (TEC-851) ─────────────────────────
#
# Verify is unreachable unless the tool can FIND a finished radar, and detection
# used to probe only the factory hosts — a radar the bench moved to
# 192.168.88.51 was invisible to it. Extending the sweep to the permanent
# addresses is what makes the button reachable, and also what creates the two
# hazards below.

@pytest.fixture
def answering(monkeypatch):
    """Control which addresses answer a dashboard probe."""
    hosts = set()
    monkeypatch.setattr(magos_bench, "probe_http",
                        lambda host, *a, **k: host in hosts)
    return hosts


def test_the_sweep_covers_the_permanent_addresses(answering):
    answering.add(FINISHED)
    assert radar.detect() == (FINISHED, False)


def test_a_fresh_radar_on_a_factory_address_wins(answering):
    # Both plugged in at once is unusual but a fresh unit has work to do, so it
    # is the one offered.
    answering.update({HOST, FINISHED})
    assert radar.detect() == (HOST, True)


def test_nothing_answering_is_no_radar(answering):
    assert radar.detect() == (None, False)


def test_a_finished_radar_is_offered_verify_not_configure():
    poll_finished()
    assert radar.state["phase"] == "detected"
    assert radar.state["on_factory_ip"] is False
    assert "Press Verify" in radar.state["message"]


def test_swapping_a_fresh_radar_for_a_finished_one_updates_the_banner():
    # Inside the miss window the phase stays "detected", so a banner set once on
    # the way in would still be telling the operator to pick a channel for a unit
    # that is already done.
    poll(HOST)
    assert "Pick a channel" in radar.state["message"]
    poll_finished()
    assert "Press Verify" in radar.state["message"]
    poll(HOST)
    assert "Pick a channel" in radar.state["message"]


def test_auto_mode_never_configures_a_finished_radar(device):
    # The hazard the permanent-address sweep creates: with auto armed, a radar
    # brought back for a QA pass would be re-provisioned.
    radar.state["auto"] = {"enabled": True, "channel": "2", "ip": None}
    poll_finished()
    assert device.runs == []
    assert radar.state["phase"] == "detected"


def test_cycle_mode_never_configures_a_finished_radar(device):
    radar.state["cycle"] = {"enabled": True, "index": 0, "count": 0}
    poll_finished()
    assert device.runs == []
    assert radar.state["cycle"] == {"enabled": True, "index": 0, "count": 0}


def test_the_configure_route_refuses_a_finished_radar():
    poll_finished()
    result = asyncio.run(radar.configure_request("0", None))
    assert "already provisioned" in result["error"]


def test_the_just_configured_radar_is_not_a_fresh_detection(device, answering):
    # It moves to its permanent address, which the sweep now covers — so without
    # this it would answer there immediately, the "unplug and plug in the next
    # one" prompt would never come back, and cycle mode would stall.
    poll(HOST)
    asyncio.run(radar.run_configuration({"channel": "1", "ip": FINISHED}, HOST))
    assert radar.state["settled_host"] == FINISHED

    answering.add(FINISHED)
    assert radar.detect() == (None, False)
    poll(*radar.detect())
    assert radar.state["phase"] == "configured"


def test_unplugging_the_configured_radar_releases_its_address(device, answering):
    # And the next unit that answers there IS a fresh detection — otherwise a
    # finished radar could never be verified on an address the bench had used.
    asyncio.run(radar.run_configuration({"channel": "1", "ip": FINISHED}, HOST))
    answering.clear()
    assert radar.detect() == (None, False)
    assert radar.state["settled_host"] is None
    answering.add(FINISHED)
    assert radar.detect() == (FINISHED, False)


def test_a_verified_radar_resets_to_waiting_when_unplugged():
    poll_finished()
    radar.state["phase"] = "verified"
    unplug()
    assert radar.state["phase"] == "waiting"


def test_the_network_warning_covers_both_subnets(monkeypatch):
    # During a QA sweep the operator is on 192.168.88.x only, and warning that
    # they are "not on the factory subnet" would be noise about a working setup.
    monkeypatch.setattr(magos_bench, "is_on_link",
                        lambda host: host == FINISHED)
    assert radar.net_warning() is None
    monkeypatch.setattr(magos_bench, "is_on_link", lambda host: False)
    warning = radar.net_warning()
    assert "factory subnet" in warning and "finished units" in warning


# ── the verify pass (TEC-851) ────────────────────────────────────────────────

def fake_verify_result(host, ok=True, rows=None):
    return {
        "ok": ok, "skipped": False, "ip": host,
        "identity": {"serial": "SN-001", "mac": "aa:bb", "model": "AR-300"},
        "raw": {}, "steps": [], "log": "", "error": None if ok else "verify:timezone",
        "verified": ok, "verify_detail": None if ok else "failed: timezone",
        "verification": rows if rows is not None else [
            {"item": "reached at", "expected": host, "actual": host, "ok": ok}],
    }


@pytest.fixture
def verifier(monkeypatch):
    """Stands in for do_verify, recording what it was asked to check."""
    calls = []

    def fake(host, resolve):
        calls.append(host)
        return fake_verify_result(host, ok=fake.ok)

    fake.ok = True
    fake.calls = calls
    monkeypatch.setattr(radar, "do_verify", fake)
    return fake


def test_verify_refuses_when_nothing_is_detected():
    assert "error" in asyncio.run(radar.verify_request())


def test_verify_refuses_mid_run():
    poll_finished()
    radar.state["busy"] = True
    result = asyncio.run(radar.verify_request())
    assert result["error"] == "A run is already in progress."


def test_a_verify_run_is_recorded_as_its_own_kind(verifier):
    poll_finished()
    asyncio.run(radar.verify_request())
    entry = radar.state["history"][0]
    assert entry["kind"] == "verify"
    assert entry["tool"] == "magos-radar"
    assert entry["status"] == "ok"
    assert entry["verified"] is True
    assert entry["verification"][0]["item"] == "reached at"
    assert entry["device"]["ip"] == FINISHED
    # A verify pass assigns no channel, so the record must not claim one.
    assert "channel" not in entry["device"]
    assert radar.state["phase"] == "verified"


def test_a_failed_verify_run_ends_in_error(verifier):
    verifier.ok = False
    poll_finished()
    asyncio.run(radar.verify_request())
    entry = radar.state["history"][0]
    assert entry["status"] == "error"
    assert entry["verified"] is False
    assert radar.state["phase"] == "error"


def test_a_verify_run_carries_the_run_provenance(verifier, monkeypatch, tmp_path):
    from bench_core.bench_ui import OperatorStore
    monkeypatch.setattr(radar, "operator_store", OperatorStore(tmp_path / "op.json"))
    radar.operator_store.set("Dana K")
    poll_finished()
    asyncio.run(radar.verify_request())
    entry = radar.state["history"][0]
    assert entry["operator"] == "Dana K"
    assert entry["station_id"] == radar.station_id
    assert entry["config_hash"] == radar.config_hash


def test_a_verify_records_log_file_is_tellable_apart(verifier, monkeypatch):
    # An operator asked to send "the log for that unit" has to be able to pick
    # the right file without opening either.
    saved = {}
    monkeypatch.setattr(radar, "_save_log", radar.__class__._save_log.__get__(radar))
    monkeypatch.setattr(magos_bench, "save_run_record",
                        lambda log_dir, entry, **kw: saved.update(kw) or "x.json")
    poll_finished()
    asyncio.run(radar.verify_request())
    assert saved["prefix"].startswith("verify_")


def test_verify_runs_are_counted_separately_from_configures(device, verifier):
    # A sweep re-checks radars already in the "done" pile, so folding the two
    # together would double-count the batch.
    poll(HOST)
    asyncio.run(radar.run_configuration({"channel": "0", "ip": "192.168.88.50"}, HOST))
    poll_finished()
    asyncio.run(radar.verify_request())
    assert radar.counts() == {"done": 1, "error": 0, "verified": 1,
                              "verify_failed": 0}


def test_a_failed_verify_does_not_count_as_a_failed_configure(verifier):
    verifier.ok = False
    poll_finished()
    asyncio.run(radar.verify_request())
    assert radar.counts() == {"done": 0, "error": 0, "verified": 0,
                              "verify_failed": 1}


def test_the_verify_route_is_registered(verifier):
    assert radar.verify_supported is True
    paths = {r.path for r in radar.build_app().routes}
    assert "/api/verify" in paths


def test_the_expected_values_come_from_the_units_own_configure_run(tmp_path,
                                                                  monkeypatch):
    # The resolver is what stops a radar nobody provisioned from verifying green,
    # and it has to look under THIS tool's records rather than every tool's.
    monkeypatch.setattr(radar, "log_dir", tmp_path)
    expected, prior = radar.verify_resolver()({"serial": "SN-NOBODY", "mac": ""})
    assert prior is not None
    assert prior["ok"] is False
    assert expected == {}
