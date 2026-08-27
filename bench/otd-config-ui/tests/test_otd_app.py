"""State-machine tests for the OTD500 configurator (otd_app.py).

No hardware/network: detection is faked via `_reachable` + `read_device_mac`,
and the device pipeline via `_do_configure`. Run with `pytest` from the bench
root (the shared venv) or from this folder.
"""
import asyncio
import json

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


# ── device-label scanning (TEC-349) ──────────────────────────────────────────

REAL_OTD_LABEL = ("SN:6008219573;I:864088065513384;M:2097272B00F7;"
                  "U:admin;PW:zZ?40*kA;B:015;")
LABEL_MAC = "20:97:27:2b:00:f7"     # the same MAC as the label, ARP-formatted
LABEL_PW = "zZ?40*kA"
OTHER_MAC = "20:97:27:32:f6:38"


@pytest.fixture
def detected(monkeypatch):
    """A device on the bench whose MAC matches REAL_OTD_LABEL."""
    set_detection(monkeypatch, True, mac=LABEL_MAC)
    poll()
    cfg.clear_armed_label()
    yield
    cfg.clear_armed_label()


def scan_state():
    return cfg.public_state()["label_scan"]


def test_scan_arms_the_password(detected):
    assert cfg.arm_label(REAL_OTD_LABEL) == {}
    assert cfg.resolve_label_password("") == (LABEL_PW, "scan")


def test_scan_survives_the_wedge_prefix_and_suffix(detected):
    assert cfg.arm_label("~" + REAL_OTD_LABEL + "\r") == {}
    assert cfg.resolve_label_password("")[0] == LABEL_PW


def test_scan_of_a_different_device_is_refused(detected):
    # The case this whole cross-check exists for: the operator scanned the box
    # next to the one that is plugged in.
    monkeypatch_mac(OTHER_MAC)
    err = cfg.arm_label(REAL_OTD_LABEL)["error"]
    assert "2097272B00F7" in err and OTHER_MAC in err
    assert cfg.armed_label() is None
    assert cfg.resolve_label_password("") == ("", "shared-fallback")


def monkeypatch_mac(mac):
    cfg.state["active_mac"] = mac


def test_refusal_drops_a_previously_armed_scan(detected):
    assert cfg.arm_label(REAL_OTD_LABEL) == {}
    monkeypatch_mac(OTHER_MAC)
    cfg.arm_label(REAL_OTD_LABEL)
    # The latest scan is the operator's intent — an older one must not linger
    # and get applied to a device they never scanned.
    assert cfg.armed_label() is None


def test_unreadable_device_mac_still_arms(monkeypatch):
    # ARP can come up empty. That is "could not check", not "does not match",
    # so the scan is usable — the UI flags it as unverified.
    set_detection(monkeypatch, True, mac=None)
    poll()
    assert cfg.arm_label(REAL_OTD_LABEL) == {}
    assert scan_state()["armed"]["matches_active"] is None
    cfg.clear_armed_label()


def test_label_without_a_password_is_refused(detected):
    err = cfg.arm_label("SN:6008219573;M:2097272B00F7;U:admin;PW:;B:015;")["error"]
    assert "no password" in err
    assert cfg.armed_label() is None


@pytest.mark.parametrize("raw", ["", "hello world", "EMP-00417", "1234567890"])
def test_non_label_scans_are_refused(detected, raw):
    err = cfg.arm_label(raw)["error"]
    assert "not a device label" in err
    assert cfg.armed_label() is None


def test_refusal_of_an_unparsed_scan_does_not_echo_it(detected):
    # An unparsed scan can still be a password, and the refusal text goes
    # straight to the browser — so it may report the length and nothing else.
    raw = "PW:hunter2-not-a-label"
    err = cfg.arm_label(raw)["error"]
    assert "hunter2" not in err
    assert f"{len(raw)} characters" in err


def test_armed_scan_expires(detected, monkeypatch):
    import bench_core.bench_ui as bench_ui
    assert cfg.arm_label(REAL_OTD_LABEL) == {}
    monkeypatch.setattr(cfg, "_armed_at",
                        cfg._armed_at - bench_ui.LABEL_SCAN_TTL_SEC - 1)
    assert cfg.armed_label() is None
    assert cfg.resolve_label_password("") == ("", "shared-fallback")


def test_swapping_the_device_drops_the_armed_scan(detected):
    assert cfg.arm_label(REAL_OTD_LABEL) == {}
    monkeypatch_mac(OTHER_MAC)          # a different unit plugged in
    assert cfg.armed_label() is None


def test_a_run_consumes_the_armed_scan(detected, monkeypatch):
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result())
    assert cfg.arm_label(REAL_OTD_LABEL) == {}
    configure({"site_name": "haifa", "label_password": LABEL_PW, "mac": LABEL_MAC})
    # One scan, one device: a retry must not silently reuse it.
    assert cfg.armed_label() is None


def test_typed_password_beats_a_scan(detected):
    assert cfg.arm_label(REAL_OTD_LABEL) == {}
    assert cfg.resolve_label_password("  typed-by-hand  ") == ("typed-by-hand", "typed")


def test_scan_state_never_carries_the_password(detected):
    cfg.arm_label(REAL_OTD_LABEL)
    state = cfg.public_state()
    assert LABEL_PW not in json.dumps(state)
    armed = state["label_scan"]["armed"]
    assert armed["serial"] == "6008219573"
    assert armed["batch"] == "015"
    assert armed["family"] == "cellular"
    assert armed["has_password"] is True
    assert armed["password_length"] == 8
    assert armed["matches_active"] is True
    assert "password" not in armed


def test_refusal_shows_as_a_problem_then_retires_on_device_change(detected):
    monkeypatch_mac(OTHER_MAC)
    cfg.arm_label(REAL_OTD_LABEL)
    assert "Scan the label" in scan_state()["problem"]
    monkeypatch_mac("aa:bb:cc:dd:ee:99")   # something else on the bench
    assert scan_state()["problem"] is None


def test_scan_route_is_registered_and_wired():
    route = next(r for r in mod.app.routes
                 if getattr(r, "path", None) == "/api/label-scan")
    assert "POST" in route.methods


def test_scan_route_arms_through_the_endpoint(detected):
    from bench_core.bench_ui import LabelScanBody
    route = next(r for r in mod.app.routes
                 if getattr(r, "path", None) == "/api/label-scan")
    returned = asyncio.run(route.endpoint(LabelScanBody(raw=REAL_OTD_LABEL)))
    assert "error" not in returned
    assert returned["label_scan"]["armed"]["serial"] == "6008219573"
    assert cfg.resolve_label_password("")[0] == LABEL_PW


def test_scan_route_returns_the_refusal(detected):
    from bench_core.bench_ui import LabelScanBody
    route = next(r for r in mod.app.routes
                 if getattr(r, "path", None) == "/api/label-scan")
    monkeypatch_mac(OTHER_MAC)
    returned = asyncio.run(route.endpoint(LabelScanBody(raw=REAL_OTD_LABEL)))
    assert "error" in returned


def test_disabled_tool_exposes_nothing_and_refuses(monkeypatch):
    # The other four tools don't opt in; they must not grow a scan UI or a
    # usable arming path just because the base carries the code.
    monkeypatch.setattr(type(cfg), "label_scan_enabled", False)
    assert cfg.public_state()["label_scan"] == {"enabled": False}
    assert "error" in cfg.arm_label(REAL_OTD_LABEL)


def test_run_record_stamps_where_the_password_came_from(detected, monkeypatch):
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result())
    cfg.arm_label(REAL_OTD_LABEL)
    password, source = cfg.resolve_label_password("")
    configure({"site_name": "haifa", "label_password": password,
               "password_source": source, "mac": LABEL_MAC})
    assert cfg.state["history"][0]["device"]["password_source"] == "scan"


@pytest.mark.parametrize("typed,expected", [
    ("abc", "typed"),
    ("", "shared-fallback"),
])
def test_password_source_is_inferred_for_direct_runs(monkeypatch, typed, expected):
    # execute_run can be driven without going through /api/configure; the
    # record must still say something true.
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result())
    configure({"site_name": "haifa", "label_password": typed, "mac": None})
    assert cfg.state["history"][0]["device"]["password_source"] == expected


# ── the scanned password must not be written down anywhere (TEC-349) ─────────
#
# This is the load-bearing test of the feature. Records are shipped off the
# station to bench-central, so a password that reaches one has left the bench
# for good. The scan exists so that nobody — and nothing — has to handle it.

def run_with_a_scan(monkeypatch, tmp_path, *, ok=True, result=None):
    """Arm a real scan and complete a run, with logging pointed at tmp_path and
    central shipping switched on so the outbox is actually written."""
    monkeypatch.setenv("BENCH_CENTRAL_URL", "http://central.invalid:8100")
    monkeypatch.setattr(cfg, "log_dir", tmp_path)
    # Undo the autouse stub so the real writers run: per-run JSON + outbox.
    monkeypatch.setattr(cfg, "_save_log", type(cfg)._save_log.__get__(cfg))
    monkeypatch.setattr(cfg, "_do_configure",
                        lambda inputs: result or fake_result(ok=ok))
    cfg.state["active_mac"] = LABEL_MAC
    cfg.state["detected"] = True
    assert cfg.arm_label(REAL_OTD_LABEL) == {}
    password, source = cfg.resolve_label_password("")
    assert (password, source) == (LABEL_PW, "scan")
    configure({"site_name": "haifa", "label_password": password,
               "password_source": source, "mac": LABEL_MAC})
    return cfg.state["history"][0]


def written_files(tmp_path):
    return [p for p in tmp_path.rglob("*") if p.is_file()]


def test_scanned_password_is_absent_from_the_record_and_every_file(monkeypatch, tmp_path):
    entry = run_with_a_scan(monkeypatch, tmp_path)

    # The in-memory record, which is also what /api/state serves.
    assert LABEL_PW not in json.dumps(entry)
    assert LABEL_PW not in json.dumps(cfg.public_state())
    # ...and the provenance is there instead, which is the point.
    assert entry["device"]["password_source"] == "scan"

    # Everything that reached the disk: the per-run JSON, the rolling log, and
    # the outbox copy queued for bench-central.
    files = written_files(tmp_path)
    assert any(p.parent.name == "outbox" for p in files), \
        "no outbox payload was written, so this test proved nothing"
    for path in files:
        assert LABEL_PW not in path.read_text(encoding="utf-8"), path


def test_the_outbox_payload_is_a_record_with_no_password(monkeypatch, tmp_path):
    # The outbox is what actually leaves the station, so check its content
    # rather than just its bytes.
    run_with_a_scan(monkeypatch, tmp_path)
    queued = list((tmp_path / "outbox").glob("*.json"))
    assert len(queued) == 1
    shipped = json.loads(queued[0].read_text(encoding="utf-8"))
    assert LABEL_PW not in json.dumps(shipped)
    assert shipped["device"]["password_source"] == "scan"
    assert shipped["serial"] == "SN-OTD-1"


def test_a_failed_run_does_not_record_the_password_either(monkeypatch, tmp_path):
    # The failure path is the one that gets read back later, and the one where
    # the device is still on its label password.
    failed = fake_result(ok=False)
    failed["verification"] = [
        {"item": "admin/root password", "expected": "the shared password",
         "actual": "NOT set — the device is still on another password", "ok": False},
    ]
    failed["steps"] = [{"time": "10:00:00", "level": "error", "sn": "SN-OTD-1",
                        "msg": "Login failed"}]
    failed["log"] = "[error] [SN-OTD-1] Login failed"
    entry = run_with_a_scan(monkeypatch, tmp_path, result=failed)

    assert entry["status"] == "error"
    assert LABEL_PW not in json.dumps(entry)
    for path in written_files(tmp_path):
        assert LABEL_PW not in path.read_text(encoding="utf-8"), path


def test_steps_and_log_are_covered_by_the_check(monkeypatch, tmp_path):
    # Guard the guard: if the password DID reach the step log, the assertions
    # above have to fail. Otherwise they only prove the fixture is quiet.
    leaky = fake_result()
    leaky["steps"] = [{"time": "10:00:00", "level": "info", "sn": "SN-OTD-1",
                       "msg": f"Logged in with {LABEL_PW}"}]
    leaky["log"] = f"[info] [SN-OTD-1] Logged in with {LABEL_PW}"
    entry = run_with_a_scan(monkeypatch, tmp_path, result=leaky)
    assert LABEL_PW in json.dumps(entry)
    assert any(LABEL_PW in p.read_text(encoding="utf-8")
               for p in written_files(tmp_path))


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
