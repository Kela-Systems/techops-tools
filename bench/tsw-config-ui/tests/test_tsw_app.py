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

from bench_core.bench_ui import VerifyBody      # /api/verify is shared, TEC-348
from bench_core.central import LABEL_OUTBOX_DIRNAME, OUTBOX_DIRNAME
from bench_core.run_record import RUN_RECORD_SCHEMA

import tsw_app as mod

cfg = mod.configurator

FACTORY_IP = "192.168.1.2"
FINAL_IP = "192.168.88.2"


def fake_result(ok=True, serial="6010212527", mac="20:97:27:2b:00:f7",
                firmware="TSW2_R_00.01.07.1", firmware_note="at the floor",
                ip=FINAL_IP, ip_mode=mod.MODE_FIXED, reached_at=FINAL_IP):
    """A bench_ui `_do_configure` result (post-pipeline shape). `ip`,
    `ip_mode`, `reached_at` and `firmware_note` are pipeline findings the base
    passes through."""
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
        "ip": ip,
        "ip_mode": ip_mode,
        "reached_at": reached_at,
        "firmware_note": firmware_note,
    }


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    cfg.state.update(cfg.initial_state())
    cfg.state["config_loaded"] = True
    monkeypatch.setattr(cfg, "_save_log", lambda entry: None)
    # The address mode is station state that survives restarts, so every test
    # starts from the shipped default rather than from whatever the last one
    # (or the last real run on this machine) left behind.
    monkeypatch.setattr(cfg.ip_modes, "save", lambda: None)
    cfg.ip_modes.mode = mod.MODE_FIXED
    cfg.ip_modes.fixed_octet = 2


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
    # The only thing asked about the DEVICE is its label password — the rest is
    # where it should end up (TEC-848). A tool that asked for a site name would
    # be asking the operator to fill in something nothing consumes.
    assert set(mod.ConfigureBody.model_fields) == {"initial_password", "ip_mode",
                                                   "octet"}
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


def test_the_scanned_password_never_reaches_a_run_record(monkeypatch, tmp_path):
    # The load-bearing one: run records are shipped off the station and feed
    # the read-only dashboard, so a password in one has left the bench for
    # good. Since TEC-845 the factory password IS kept centrally — but through
    # its own queue and its own store, which is what keeps it to one row per
    # device instead of a copy in every record of every run that unit ever had.
    # So the rule is "nowhere but the label queue", not "nowhere".
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
    assert any(p.parent.name == OUTBOX_DIRNAME for p in files), \
        "no outbox payload was written, so this test proved nothing"
    for path in files:
        if path.parent.name == LABEL_OUTBOX_DIRNAME:
            continue
        assert LABEL_PW not in path.read_text(encoding="utf-8"), path


@pytest.mark.parametrize("typed,expected", [("abc", "typed"), ("", "shared-fallback")])
def test_password_source_is_inferred_for_direct_runs(monkeypatch, typed, expected):
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result())
    configure({"initial_password": typed, "mac": None})
    assert cfg.state["history"][0]["device"]["password_source"] == expected


# ── /api/verify (TEC-348) ────────────────────────────────────────────────────

def verify_result(ok=True, serial="6010212527", verification=None):
    result = fake_result(ok=ok, serial=serial)
    result["verification"] = verification or []
    result["firmware_note"] = "no firmware step on a verify run"
    return result


def post_verify(**body):
    return asyncio.run(route("/api/verify").endpoint(VerifyBody(**body)))


def test_the_verify_route_exists_because_the_tool_opted_in():
    assert cfg.verify_supported is True
    assert "POST" in route("/api/verify").methods


def test_verify_refuses_when_nothing_is_detected():
    assert post_verify() == {"error": "No device is currently detected."}


def test_verify_records_a_verify_run_not_a_configure_one(monkeypatch):
    monkeypatch.setattr(cfg, "_do_verify", lambda inputs: verify_result())
    set_detection(monkeypatch, FINAL_IP)
    poll()
    assert "error" not in post_verify()

    entry = cfg.state["history"][0]
    assert entry["kind"] == "verify"
    assert entry["tool"] == "tsw"
    assert entry["status"] == "ok"
    assert cfg.state["phase"] == "verified"


def test_a_verify_pass_does_not_say_configured(monkeypatch):
    # The operator has to be able to tell the two apart at a glance; only one
    # of them changed the switch.
    monkeypatch.setattr(cfg, "_do_verify", lambda inputs: verify_result())
    set_detection(monkeypatch, FINAL_IP)
    poll()
    post_verify()
    assert "PASSED verification" in cfg.state["message"]
    assert "Configured" not in cfg.state["message"]


def test_a_failed_verify_is_recorded_as_an_error(monkeypatch):
    failing = [{"item": "timezone", "expected": "Asia/Jerusalem",
                "actual": "clock at +0000", "ok": False}]
    monkeypatch.setattr(cfg, "_do_verify",
                        lambda inputs: verify_result(ok=False,
                                                     verification=failing))
    set_detection(monkeypatch, FINAL_IP)
    poll()
    post_verify()
    entry = cfg.state["history"][0]
    assert entry["kind"] == "verify"
    assert entry["status"] == "error"
    assert entry["verified"] is False
    assert cfg.state["phase"] == "error"


def test_verify_reaches_the_switch_where_detection_found_it(monkeypatch):
    seen = {}
    monkeypatch.setattr(cfg, "_do_verify",
                        lambda inputs: seen.update(inputs) or verify_result())
    set_detection(monkeypatch, FINAL_IP)
    poll()
    post_verify()
    assert seen["host"] == FINAL_IP


def test_verify_asks_for_no_password(monkeypatch):
    # A finished unit is on the station's shared password. Asking the operator
    # for one would be asking for something they don't have, and a unit that is
    # NOT on it fails its password row — which is the useful answer.
    assert set(VerifyBody.model_fields) == {"expected"}
    seen = {}
    monkeypatch.setattr(cfg, "_do_verify",
                        lambda inputs: seen.update(inputs) or verify_result())
    set_detection(monkeypatch, FINAL_IP)
    poll()
    post_verify()
    assert seen["password_source"] == "shared-fallback"
    assert "initial_password" not in seen


def test_a_verify_run_cannot_start_while_a_configure_run_is_going(monkeypatch):
    set_detection(monkeypatch, FINAL_IP)
    poll()
    cfg.state["busy"] = True
    assert post_verify() == {"error": "A run is already in progress."}


def test_verify_runs_are_counted_separately_from_configures(monkeypatch):
    # An end-of-batch sweep re-checks every unit already in the done pile.
    # Folding those into `done` would report twice as many switches provisioned
    # as the bench actually saw.
    set_detection(monkeypatch, FINAL_IP)
    poll()
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result())
    post_configure()
    monkeypatch.setattr(cfg, "_do_verify", lambda inputs: verify_result())
    post_verify()
    post_verify()

    counts = cfg.counts()
    assert counts == {"done": 1, "error": 0, "verified": 2, "verify_failed": 0}


def test_a_verify_record_is_filed_under_its_own_name(monkeypatch, tmp_path):
    # Two kinds of run in one folder. Someone asked to send "the log for that
    # switch" should not have to open files to find out which is which.
    monkeypatch.setattr(cfg, "log_dir", tmp_path)
    monkeypatch.setattr(cfg, "_save_log", type(cfg)._save_log.__get__(cfg))
    monkeypatch.setattr(cfg, "_do_verify", lambda inputs: verify_result())
    set_detection(monkeypatch, FINAL_IP)
    poll()
    post_verify()
    written = [p.name for p in tmp_path.glob("*.json")]
    assert len(written) == 1
    assert written[0].startswith("verify_")
    assert "6010212527" in written[0]


def test_a_verify_run_still_stamps_who_ran_it(monkeypatch):
    # TEC-345 provenance is not configure-only: a QA pass an operator signed
    # off on is exactly the kind of run you need to trace back to a person.
    monkeypatch.setattr(cfg, "_do_verify", lambda inputs: verify_result())
    set_detection(monkeypatch, FINAL_IP)
    poll()
    post_verify()
    entry = cfg.state["history"][0]
    assert entry["station_id"]
    assert entry["bench_version"]
    assert entry["config_hash"]


def test_detection_leaves_a_finished_verify_result_on_screen(monkeypatch):
    # `verified` is a terminal phase like `configured`; detection must not reset
    # the page to "detected" under the operator while the unit is still plugged
    # in and its PASS/FAIL is being read.
    monkeypatch.setattr(cfg, "_do_verify", lambda inputs: verify_result())
    set_detection(monkeypatch, FINAL_IP)
    poll()
    post_verify()
    assert cfg.state["phase"] == "verified"
    poll()
    assert cfg.state["phase"] == "verified"


def test_the_page_is_told_the_tool_supports_verifying():
    assert cfg.public_state()["verify_supported"] is True


# ── address modes (TEC-848) ──────────────────────────────────────────────────
#
# Until this, every switch landed on 192.168.88.2 and the operator's only
# escape was to re-address it by hand afterwards, which left no record anywhere.

def detect_and_configure(monkeypatch, **body):
    """Detect a factory switch, then POST /api/configure — returning the inputs
    the pipeline was handed, which is where the address decision shows up."""
    seen = {}
    monkeypatch.setattr(cfg, "_do_configure",
                        lambda inputs: seen.update(inputs) or fake_result(
                            ip=inputs.get("target_ip", ""),
                            ip_mode=inputs.get("ip_mode", ""),
                            reached_at=inputs.get("target_ip") or "192.168.88.57"))
    set_detection(monkeypatch, FACTORY_IP)
    poll()
    return seen, post_configure(**body)


def test_three_modes_are_offered_and_cycle_is_not():
    # A site takes one switch, so a counter would have no second address to
    # move to — offering the mode would mean nothing for the device in front of
    # the operator.
    assert cfg.public_state()["ip_modes"] == ["fixed", "manual", "dhcp"]


def test_the_default_is_still_the_config_address():
    # The long-standing behaviour has to survive an operator who never touches
    # the picker: a station that upgrades must keep producing .2 switches.
    assert cfg.public_state()["next_ip"] == FINAL_IP


def test_fixed_mode_gives_every_switch_the_same_address(monkeypatch):
    cfg.ip_modes.set_mode(mod.MODE_FIXED, "9")
    first, _ = detect_and_configure(monkeypatch)
    second, _ = detect_and_configure(monkeypatch)
    assert first["target_ip"] == second["target_ip"] == "192.168.88.9"


def test_manual_takes_an_octet_or_a_whole_address(monkeypatch):
    for typed in ("57", "192.168.88.57"):
        seen, state = detect_and_configure(monkeypatch, ip_mode="manual",
                                           octet=typed)
        assert "error" not in state
        assert seen["target_ip"] == "192.168.88.57"


def test_a_manual_address_on_another_subnet_is_refused(monkeypatch):
    # The tool writes its own gateway and netmask alongside, so honouring only
    # the last octet of what was typed would strand the switch.
    _, state = detect_and_configure(monkeypatch, ip_mode="manual",
                                    octet="10.0.0.57")
    assert "not on this tool's subnet" in state["error"]
    assert not cfg.state["history"]


def test_manual_with_nothing_typed_is_refused(monkeypatch):
    _, state = detect_and_configure(monkeypatch, ip_mode="manual", octet="")
    assert "Enter an address" in state["error"]
    assert not cfg.state["history"]


def test_dhcp_assigns_nothing(monkeypatch):
    seen, state = detect_and_configure(monkeypatch, ip_mode="dhcp")
    assert "error" not in state
    assert seen["target_ip"] == ""
    assert seen["ip_mode"] == "dhcp"


def test_dhcp_is_refused_without_a_mac(monkeypatch):
    # The MAC is the only way to find a switch again once it has gone somewhere
    # the bench did not choose. Without one the run would provision it and then
    # lose it, so it is refused before the device is touched.
    set_detection(monkeypatch, FACTORY_IP, mac=None)
    poll()
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result())
    state = post_configure(ip_mode="dhcp")
    assert "MAC could not be read" in state["error"]
    assert not cfg.state["history"]


def test_a_dhcp_run_records_no_assigned_address(monkeypatch):
    # The lease belongs to the site's DHCP server. Recording it as ours would
    # be a claim the next reader — a QA label (TEC-352), bench-central — acts
    # on, so `ip` stays empty and where it was found rides along separately.
    detect_and_configure(monkeypatch, ip_mode="dhcp")
    device = cfg.state["history"][0]["device"]
    assert device["ip"] == ""
    assert device["ip_mode"] == "dhcp"
    assert device["reached_at"] == "192.168.88.57"


def test_a_static_run_records_the_mode_too(monkeypatch):
    # Not just the DHCP case: a label or a bench-central reader has to be able
    # to tell "this is where we put it" from "this is where it happened to be".
    detect_and_configure(monkeypatch, ip_mode="manual", octet="57")
    device = cfg.state["history"][0]["device"]
    assert device["ip"] == "192.168.88.57"
    assert device["ip_mode"] == "manual"


def test_the_chosen_mode_sticks_across_switches(monkeypatch):
    # It is a mode the operator switches on once and then works a batch under,
    # so a scripted call that names one changes the selection rather than
    # applying to that unit alone.
    detect_and_configure(monkeypatch, ip_mode="dhcp")
    assert cfg.ip_modes.mode == "dhcp"


def test_a_dhcp_success_message_says_where_the_switch_went(monkeypatch):
    detect_and_configure(monkeypatch, ip_mode="dhcp")
    assert "on DHCP, currently at 192.168.88.57" in cfg.state["message"]


def test_the_pickers_fixed_address_is_probed_by_detection(monkeypatch):
    # An operator who moved the fixed address is telling us where their
    # finished switches now live, so that is where a re-plugged one is looked
    # for — not only at the config's lan_ip.
    cfg.ip_modes.set_mode(mod.MODE_FIXED, "9")
    probed = []
    monkeypatch.setattr(mod.socket, "create_connection",
                        lambda addr, *a, **k: probed.append(addr) or
                        (_ for _ in ()).throw(OSError("nothing there")))
    assert cfg._detect_host() is None
    assert probed == [(FACTORY_IP, 443), ("192.168.88.9", 443), (FINAL_IP, 443)]


# ── config surface the page reads ────────────────────────────────────────────

def test_public_state_carries_what_the_page_shows():
    state = cfg.public_state()
    assert state["ntp_server"] == "192.168.88.10"
    assert state["min_firmware"] == "TSW2_R_00.01.07.1"
    # Only a switch that arrives BELOW the floor needs the image, so its
    # absence is reported rather than blocking anything.
    assert isinstance(state["firmware_found"], bool)
