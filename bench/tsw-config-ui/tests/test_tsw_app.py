"""State-machine tests for the TSW202 configurator (tsw_app.py).

No hardware/network: detection is faked via `_detect_host` + `read_device_mac`,
and the device pipeline via `_do_configure`. Run with `pytest` from the bench
root (the shared venv) or from this folder.

What is specific to this tool, versus the router tools next door:
* two detection addresses, and the factory one is 192.168.1.2 (not .1);
* no site name — /api/configure takes the label password and nothing else;
* per-run JSON named after the serial, since nothing sets a hostname.
"""
import asyncio
import json

import pytest

from bench_core.run_record import RUN_RECORD_SCHEMA

import tsw_app as mod

cfg = mod.configurator

FACTORY_IP = "192.168.1.2"
FINAL_IP = "192.168.88.2"


def fake_result(ok=True, serial="6010212527", mac="20:97:27:2b:00:f7",
                firmware="TSW2_R_00.01.07.1", firmware_note="at the floor"):
    """A bench_ui `_do_configure` result (post-pipeline shape). `ip` and
    `firmware_note` are pipeline findings the base passes through."""
    return {
        "ok": ok,
        "hostname": "tsw-00f7",
        "identity": {"serial": serial, "mac": mac, "model": "TSW202",
                     "firmware": firmware},
        "warnings": [],
        "error": None if ok else "boom",
        "steps": [],
        "verification": [],
        "log": "",
        "ip": FINAL_IP,
        "firmware_note": firmware_note,
    }


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    cfg.state.update(cfg.initial_state())
    cfg.state["config_loaded"] = True
    monkeypatch.setattr(cfg, "_save_log", lambda entry: None)


def set_detection(monkeypatch, host, mac="20:97:27:2b:00:f7"):
    monkeypatch.setattr(cfg, "_detect_host", lambda *a, **k: host)
    monkeypatch.setattr(mod, "read_device_mac", lambda *a, **k: mac)


def poll():
    async def run():
        await cfg.poll_once(asyncio.get_running_loop())
    asyncio.run(run())


def configure(inputs):
    asyncio.run(cfg.execute_run(inputs, "test"))


# ── detection ────────────────────────────────────────────────────────────────

def test_waiting_to_detected_and_back(monkeypatch):
    set_detection(monkeypatch, FACTORY_IP)
    poll()
    assert cfg.state["phase"] == "detected"
    assert cfg.state["active_host"] == FACTORY_IP
    assert cfg.state["at_final_lan"] is False
    assert cfg.state["active_mac"] == "20:97:27:2b:00:f7"

    set_detection(monkeypatch, None)
    poll()
    assert cfg.state["phase"] == "waiting"
    assert cfg.state["active_mac"] is None


def test_detection_on_the_final_address_says_so(monkeypatch):
    # An already-provisioned switch plugged back in. The operator has to be
    # told, because the factory password no longer applies to it.
    set_detection(monkeypatch, FINAL_IP)
    poll()
    assert cfg.state["at_final_lan"] is True
    assert "already on the final management address" in cfg.state["message"]
    assert "leave the label password empty" in cfg.state["message"]


def test_both_addresses_are_probed_and_the_factory_one_is_dot_two():
    # The .2 is the whole reason this tool can share the bench subnet with the
    # OTD500/RUTM08 tools, which sit on .1 — so it is worth pinning down.
    probed = []
    original = mod.socket.create_connection

    def record(addr, *a, **k):
        probed.append(addr)
        raise OSError("nothing there")

    mod.socket.create_connection = record
    try:
        assert cfg._detect_host() is None
    finally:
        mod.socket.create_connection = original
    assert probed == [(FACTORY_IP, 443), (FINAL_IP, 443)]


def test_busy_blocks_detection(monkeypatch):
    cfg.state["busy"] = True
    cfg.state["phase"] = "configuring"
    set_detection(monkeypatch, FACTORY_IP)
    poll()
    assert cfg.state["phase"] == "configuring"   # detection skipped mid-run
    assert cfg.state["detected"] is False


def test_detected_stays_until_unplug(monkeypatch):
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result())
    configure({"initial_password": "", "mac": "20:97:27:2b:00:f7"})
    assert cfg.state["phase"] == "configured"
    set_detection(monkeypatch, FINAL_IP)
    poll()
    assert cfg.state["phase"] == "configured"


# ── the run record ───────────────────────────────────────────────────────────

def test_configure_records_history(monkeypatch):
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result())
    configure({"initial_password": "x", "mac": "20:97:27:2b:00:f7"})
    assert cfg.state["phase"] == "configured"
    entry = cfg.state["history"][0]
    assert entry["schema"] == RUN_RECORD_SCHEMA
    assert entry["tool"] == "tsw"
    assert entry["status"] == "ok"
    assert entry["serial"] == "6010212527"
    assert entry["model"] == "TSW202"
    assert entry["device"]["ip"] == FINAL_IP
    # No hostname: the switch baseline does not name the device, so claiming
    # one in the record would be inventing it.
    assert "hostname" not in entry["device"]
    assert cfg.state["busy"] is False


def test_the_firmware_note_reaches_the_record(monkeypatch):
    # The floor semantics are only legible after the fact if the record says
    # what the firmware step decided — this is the field that carries it.
    note = "TSW2_R_00.01.10 is newer than the TSW2_R_00.01.07.1 floor — left as-is"
    monkeypatch.setattr(cfg, "_do_configure",
                        lambda inputs: fake_result(firmware="TSW2_R_00.01.10",
                                                   firmware_note=note))
    configure({"initial_password": "", "mac": None})
    assert cfg.state["history"][0]["device"]["firmware_note"] == note


def test_failed_run_sets_error(monkeypatch):
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result(ok=False))
    configure({"initial_password": "", "mac": None})
    assert cfg.state["phase"] == "error"
    assert cfg.state["history"][0]["status"] == "error"


def test_per_run_json_is_named_after_the_serial(monkeypatch, tmp_path):
    # The shared writer defaults to device.hostname, which this tool has none
    # of; without the override every file would be named "_ok.json".
    monkeypatch.setattr(cfg, "log_dir", tmp_path)
    monkeypatch.setattr(cfg, "_save_log", type(cfg)._save_log.__get__(cfg))
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result())
    configure({"initial_password": "", "mac": None})
    written = [p.name for p in tmp_path.glob("*.json")]
    assert len(written) == 1
    assert "6010212527" in written[0] and written[0].endswith("_ok.json")


def test_success_message_names_the_final_address(monkeypatch):
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result())
    configure({"initial_password": "", "mac": None})
    assert FINAL_IP in cfg.state["message"]
    assert "6010212527" in cfg.state["message"]


# ── the run label (there is no hostname to derive it from) ────────────────────

@pytest.mark.parametrize("mac,expected", [
    ("20:97:27:2b:00:f7", "tsw-00f7"),
    ("20-97-27-2B-00-F7", "tsw-00f7"),
    (None, "tsw-unknown"),
    ("", "tsw-unknown"),
    ("ab", "tsw-unknown"),          # too short to identify anything
])
def test_run_label_comes_from_the_mac(mac, expected):
    # It is a label, not a hostname: the base needs something to show before
    # login, and the ARP MAC is the only identifier available that early.
    assert cfg.hostname_for({"mac": mac}) == expected


# ── /api/configure ───────────────────────────────────────────────────────────

def route(path):
    return next(r for r in mod.app.routes if getattr(r, "path", None) == path)


def post_configure(**body):
    return asyncio.run(route("/api/configure").endpoint(mod.ConfigureBody(**body)))


def test_configure_refuses_when_nothing_is_detected():
    assert post_configure(initial_password="x") == {"error": "No switch is currently detected."}


def test_configure_needs_no_site_name(monkeypatch):
    # The body has exactly one field. A tool that asked for a site name would
    # be asking the operator to fill in something nothing consumes.
    assert set(mod.ConfigureBody.model_fields) == {"initial_password"}
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result())
    set_detection(monkeypatch, FACTORY_IP)
    poll()
    state = post_configure()
    assert "error" not in state
    assert cfg.state["history"][0]["status"] == "ok"


def test_configure_passes_the_detected_host_to_the_pipeline(monkeypatch):
    seen = {}
    monkeypatch.setattr(cfg, "_do_configure",
                        lambda inputs: seen.update(inputs) or fake_result())
    set_detection(monkeypatch, FINAL_IP)
    poll()
    post_configure()
    assert seen["host"] == FINAL_IP


# ── label scanning (TEC-349) — the TSW202 sticker is the same format ──────────
#
# A real TSW202 label per the Teltonika wiki: same semicolon-delimited keys as
# the OTD500/RUTM08, minus the IMEI (no modem). The shared parser handles it
# unchanged, which is what these confirm.

TSW_LABEL = "SN:6010212527;M:20972B2B00F7;U:admin;PW:qN4$8xTr;B:001;"
LABEL_MAC = "20:97:2b:2b:00:f7"
LABEL_PW = "qN4$8xTr"
OTHER_MAC = "20:97:27:32:f6:38"


@pytest.fixture
def detected(monkeypatch):
    set_detection(monkeypatch, FACTORY_IP, mac=LABEL_MAC)
    poll()
    cfg.clear_armed_label()
    yield
    cfg.clear_armed_label()


def test_scan_arms_the_password(detected):
    assert cfg.arm_label(TSW_LABEL) == {}
    assert cfg.resolve_label_password("") == (LABEL_PW, "scan")


def test_the_label_reads_as_non_cellular(detected):
    # No IMEI on a switch sticker. Advisory only, but it is what a future
    # "scan the label, open the right tool" step would key off.
    cfg.arm_label(TSW_LABEL)
    assert cfg.public_state()["label_scan"]["armed"]["family"] == "non-cellular"


def test_scan_of_a_different_device_is_refused(detected):
    cfg.state["active_mac"] = OTHER_MAC
    err = cfg.arm_label(TSW_LABEL)["error"]
    assert "20972B2B00F7" in err and OTHER_MAC in err
    assert cfg.armed_label() is None
    assert cfg.resolve_label_password("") == ("", "shared-fallback")


def test_scan_state_never_carries_the_password(detected):
    cfg.arm_label(TSW_LABEL)
    state = cfg.public_state()
    assert LABEL_PW not in json.dumps(state)
    armed = state["label_scan"]["armed"]
    assert armed["serial"] == "6010212527"
    assert armed["has_password"] is True
    assert "password" not in armed


def test_scan_route_is_registered():
    assert "POST" in route("/api/label-scan").methods


def test_run_record_stamps_where_the_password_came_from(detected, monkeypatch):
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result())
    cfg.arm_label(TSW_LABEL)
    post_configure()
    assert cfg.state["history"][0]["device"]["password_source"] == "scan"


def test_the_scanned_password_never_reaches_a_written_record(monkeypatch, tmp_path):
    # The load-bearing one: records are shipped off the station to
    # bench-central, so a password in one has left the bench for good.
    monkeypatch.setenv("BENCH_CENTRAL_URL", "http://central.invalid:8100")
    monkeypatch.setattr(cfg, "log_dir", tmp_path)
    monkeypatch.setattr(cfg, "_save_log", type(cfg)._save_log.__get__(cfg))
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result())
    cfg.state["active_mac"] = LABEL_MAC
    cfg.state["detected"] = True
    assert cfg.arm_label(TSW_LABEL) == {}
    post_configure()

    entry = cfg.state["history"][0]
    assert entry["device"]["password_source"] == "scan"
    assert LABEL_PW not in json.dumps(entry)
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert any(p.parent.name == "outbox" for p in files), \
        "no outbox payload was written, so this test proved nothing"
    for path in files:
        assert LABEL_PW not in path.read_text(encoding="utf-8"), path


@pytest.mark.parametrize("typed,expected", [("abc", "typed"), ("", "shared-fallback")])
def test_password_source_is_inferred_for_direct_runs(monkeypatch, typed, expected):
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result())
    configure({"initial_password": typed, "mac": None})
    assert cfg.state["history"][0]["device"]["password_source"] == expected


# ── config surface the page reads ────────────────────────────────────────────

def test_public_state_carries_what_the_page_shows():
    state = cfg.public_state()
    assert state["ntp_server"] == "192.168.88.10"
    assert state["min_firmware"] == "TSW2_R_00.01.07.1"
    # Only a switch that arrives BELOW the floor needs the image, so its
    # absence is reported rather than blocking anything.
    assert isinstance(state["firmware_found"], bool)
