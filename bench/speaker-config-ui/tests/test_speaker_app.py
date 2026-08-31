"""State-machine tests for the Provision-ISR speaker configurator
(speaker_app.py).

No hardware/network: detection is faked via `_find_speaker` + `read_device_mac`
and the device pipeline via `_do_configure`.
"""
import asyncio
from pathlib import Path

import pytest

from bench_core.bench_ui import VerifyBody      # /api/verify is shared, TEC-348
from bench_core.run_record import RUN_RECORD_SCHEMA

import speaker_app as mod

cfg = mod.configurator

HOST = "192.168.1.57"


def fake_result(ok=True, serial="TM-CS20-000001-XX", hostname="speaker-70",
                mac="74:f8:db:5f:25:6a"):
    return {
        "ok": ok,
        "hostname": hostname,
        "identity": {"serial": serial, "mac": mac,
                     "model": "Provision-ISR speaker", "firmware": "V3.3.39-PR1"},
        "warnings": [],
        "error": None if ok else "boom",
        "steps": [],
        "verification": [],
        "log": "",
    }


FIXED_IP = "192.168.88.70"


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    cfg.state.update(cfg.initial_state())
    cfg.state["config_loaded"] = True
    # Addressing is the shared store's business (TEC-848); keep the file on
    # disk out of it and start every test from the station default.
    monkeypatch.setattr(cfg.ip_modes, "save", lambda: None)
    cfg.ip_modes.mode = "fixed"
    cfg.ip_modes.fixed_octet = 70
    cfg.ip_modes.cycle_next = 70
    monkeypatch.setattr(cfg, "_save_log", lambda entry: None)


def set_detection(monkeypatch, host, mac="74:f8:db:5f:25:6a"):
    monkeypatch.setattr(cfg, "_find_speaker", lambda: host)
    monkeypatch.setattr(mod, "read_device_mac", lambda *a, **k: mac)


def poll():
    async def run():
        await cfg.poll_once(asyncio.get_running_loop())
    asyncio.run(run())


def run_configure(ok=True, monkeypatch=None, target_ip=FIXED_IP,
                  ip_mode="fixed", result=None):
    monkeypatch.setattr(cfg, "_do_configure",
                        lambda inputs: result or fake_result(ok=ok))
    inputs = {"host": HOST, "mac": "74:f8:db:5f:25:6a", "media_path": "",
              "target_ip": target_ip, "ip_mode": ip_mode,
              "advance_cycle": False}
    asyncio.run(cfg.execute_run(inputs, "test"))


def configure(monkeypatch, **body):
    """POST /api/configure through the real route, with the pipeline stubbed."""
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result())
    monkeypatch.setattr(mod, "resolve_media", lambda cfgdict: Path("alarm.mp3"))
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
    set_detection(monkeypatch, HOST)
    poll()
    assert cfg.state["phase"] == "detected"
    assert cfg.state["active_host"] == HOST
    assert cfg.state["active_mac"] == "74:f8:db:5f:25:6a"

    set_detection(monkeypatch, None)
    poll()
    assert cfg.state["phase"] == "waiting"
    assert cfg.state["active_host"] is None


def test_busy_blocks_detection(monkeypatch):
    cfg.state["busy"] = True
    cfg.state["phase"] = "configuring"
    set_detection(monkeypatch, HOST)
    poll()
    assert cfg.state["phase"] == "configuring"


def test_detection_keeps_finished_phase_while_visible(monkeypatch):
    # After a run the speaker may still answer (now on its static IP); the
    # result must stay on screen instead of flipping back to "detected".
    cfg.state["phase"] = "configured"
    set_detection(monkeypatch, FIXED_IP)
    poll()
    assert cfg.state["phase"] == "configured"


def test_configure_records_history(monkeypatch):
    result = fake_result()
    result["ip"] = FIXED_IP
    run_configure(monkeypatch=monkeypatch, result=result)
    assert cfg.state["phase"] == "configured"
    entry = cfg.state["history"][0]
    assert entry["schema"] == RUN_RECORD_SCHEMA
    assert entry["tool"] == "speaker"
    assert entry["status"] == "ok"
    assert entry["serial"] == "TM-CS20-000001-XX"
    assert entry["device"]["ip"] == FIXED_IP
    assert entry["device"]["from_host"] == HOST


def test_failed_configure_sets_error(monkeypatch):
    run_configure(ok=False, monkeypatch=monkeypatch)
    assert cfg.state["phase"] == "error"
    assert cfg.state["history"][0]["status"] == "error"


def test_hostname_uses_target_ip_octet():
    assert cfg.hostname_for({"target_ip": FIXED_IP}) == "speaker-70"


# ── address modes (TEC-848) ──────────────────────────────────────────────────

def test_all_four_modes_are_offered():
    assert cfg.public_state()["ip_modes"] == ["fixed", "cycle", "manual", "dhcp"]


def test_the_default_is_still_the_config_address(monkeypatch):
    """A bench that never touches the picker must behave exactly as it did
    before the modes existed: every speaker to static.ip."""
    set_detection(monkeypatch, HOST)
    poll()
    _out, captured = configure(monkeypatch)
    assert captured["inputs"]["target_ip"] == FIXED_IP
    assert captured["inputs"]["ip_mode"] == "fixed"


def test_cycle_alternates_between_70_and_71(monkeypatch):
    cfg.ip_modes.set_mode("cycle")
    assert cfg.ip_modes.cycle_next == 70
    cfg.ip_modes.advance_cycle()
    assert cfg.ip_modes.cycle_next == 71
    cfg.ip_modes.advance_cycle()
    assert cfg.ip_modes.cycle_next == 70   # a site takes two, so it wraps here


def test_a_cycled_run_burns_its_number_only_on_success(monkeypatch):
    cfg.ip_modes.set_mode("cycle")
    set_detection(monkeypatch, HOST)
    poll()
    _out, captured = configure(monkeypatch)
    assert captured["inputs"]["target_ip"] == "192.168.88.70"
    assert cfg.ip_modes.cycle_next == 71

    cfg.ip_modes.cycle_next = 71
    run_configure(ok=False, monkeypatch=monkeypatch, ip_mode="cycle")
    assert cfg.ip_modes.cycle_next == 71


def test_manual_takes_an_octet_or_a_whole_address(monkeypatch):
    # The two spellings of one address, asserted against each other rather than
    # against a literal: the range is the STATION's to set in speaker.config.json
    # (gitignored), so a test that named an octet would break the day an
    # operator retuned it, and would be claiming something about their bench
    # rather than about this code.
    policy = cfg.ip_modes.policy
    octet = policy.octet_min
    want = f"{policy.prefix}.{octet}"

    set_detection(monkeypatch, HOST)
    poll()
    _out, captured = configure(monkeypatch, ip_mode="manual", octet=str(octet))
    assert captured["inputs"]["target_ip"] == want

    poll()
    _out, captured = configure(monkeypatch, ip_mode="manual", octet=want)
    assert captured["inputs"]["target_ip"] == want


def test_a_manual_address_outside_the_configured_range_is_refused(monkeypatch):
    policy = cfg.ip_modes.policy
    set_detection(monkeypatch, HOST)
    poll()
    out, captured = configure(monkeypatch, ip_mode="manual",
                              octet=str(policy.octet_max + 1))
    assert "outside the allowed range" in out["error"]
    assert "inputs" not in captured


def test_a_manual_address_on_another_subnet_is_refused(monkeypatch):
    # Refused rather than quietly reduced to its last octet: the tool writes
    # its own gateway and netmask alongside, so honouring half of what was
    # typed would strand the speaker.
    set_detection(monkeypatch, HOST)
    poll()
    out, captured = configure(monkeypatch, ip_mode="manual", octet="10.0.0.70")
    assert "not on this tool's subnet" in out["error"]
    assert "inputs" not in captured


def test_manual_with_nothing_typed_is_refused(monkeypatch):
    set_detection(monkeypatch, HOST)
    poll()
    out, captured = configure(monkeypatch, ip_mode="manual")
    assert "Enter an address" in out["error"]
    assert "inputs" not in captured


def test_dhcp_assigns_nothing(monkeypatch):
    set_detection(monkeypatch, HOST)
    poll()
    _out, captured = configure(monkeypatch, ip_mode="dhcp")
    assert captured["inputs"]["target_ip"] == ""
    assert captured["inputs"]["ip_mode"] == "dhcp"
    assert "DHCP" in captured["label"]


def test_dhcp_is_refused_without_a_mac(monkeypatch):
    """Left on DHCP the speaker may move, and its MAC is the only way back to
    it — so a run that never read one could confirm nothing afterwards."""
    set_detection(monkeypatch, HOST, mac=None)
    poll()
    out, captured = configure(monkeypatch, ip_mode="dhcp")
    assert "MAC" in out["error"]
    assert "inputs" not in captured


def test_a_dhcp_run_records_no_assigned_address(monkeypatch):
    """The lease is the site DHCP server's, so there is no address of ours to
    record — a QA label (TEC-352) must print DHCP, not a made-up .70."""
    result = fake_result(hostname="speaker-dhcp")
    result["ip"] = ""
    result["ip_mode"] = "dhcp"
    result["reached_at"] = "192.168.1.57"
    run_configure(monkeypatch=monkeypatch, target_ip="", ip_mode="dhcp",
                  result=result)
    dev = cfg.state["history"][0]["device"]
    assert dev["ip"] == ""
    assert dev["ip_mode"] == "dhcp"
    assert dev["reached_at"] == "192.168.1.57"


def test_a_static_run_records_the_mode_too(monkeypatch):
    result = fake_result()
    result["ip"] = "192.168.88.71"
    result["ip_mode"] = "cycle"
    run_configure(monkeypatch=monkeypatch, result=result)
    dev = cfg.state["history"][0]["device"]
    assert dev["ip"] == "192.168.88.71"
    assert dev["ip_mode"] == "cycle"


# ── /api/verify (TEC-348) ────────────────────────────────────────────────────

def verify_result(ok=True, verification=None):
    result = fake_result(ok=ok)
    result["name"] = result["hostname"]
    result["ip"] = "192.168.88.70"
    result["verification"] = verification or []
    return result


def route(path):
    return next(r for r in mod.app.routes if getattr(r, "path", None) == path)


def post_verify(**body):
    return asyncio.run(route("/api/verify").endpoint(VerifyBody(**body)))


def test_the_verify_route_exists_because_the_tool_opted_in():
    assert cfg.verify_supported is True
    assert "POST" in route("/api/verify").methods
    assert cfg.public_state()["verify_supported"] is True


def test_verify_refuses_when_no_speaker_is_detected():
    assert post_verify() == {"error": "No device is currently detected."}


def test_verify_records_a_verify_run(monkeypatch):
    monkeypatch.setattr(cfg, "_do_verify", lambda inputs: verify_result())
    set_detection(monkeypatch, "192.168.88.70")
    poll()
    assert "error" not in post_verify()
    entry = cfg.state["history"][0]
    assert entry["kind"] == "verify"
    assert entry["tool"] == "speaker"
    assert cfg.state["phase"] == "verified"


def test_verify_reaches_the_speaker_where_the_scan_found_it(monkeypatch):
    # The whole "reached at" row depends on this: the pipeline must be pointed
    # at the address detection actually found, not at the target static IP.
    seen = {}
    monkeypatch.setattr(cfg, "_do_verify",
                        lambda inputs: seen.update(inputs) or verify_result())
    set_detection(monkeypatch, HOST)
    poll()
    post_verify()
    assert seen["host"] == HOST


def test_verify_passes_the_configured_media_file(monkeypatch, tmp_path):
    media = tmp_path / "alarm.mp3"
    media.write_bytes(b"x")
    monkeypatch.setattr(mod, "resolve_media", lambda cfgdict: media)
    seen = {}
    monkeypatch.setattr(cfg, "_do_verify",
                        lambda inputs: seen.update(inputs) or verify_result())
    set_detection(monkeypatch, HOST)
    poll()
    post_verify()
    assert seen["media_path"] == str(media)


def test_a_missing_media_file_does_not_block_a_verify(monkeypatch):
    # Configure refuses to run without it; verify must not. The file's absence
    # from this bench PC says nothing about the speaker in front of us.
    monkeypatch.setattr(mod, "resolve_media",
                        lambda cfgdict: (_ for _ in ()).throw(
                            mod.SpeakerError("media file not found")))
    seen = {}
    monkeypatch.setattr(cfg, "_do_verify",
                        lambda inputs: seen.update(inputs) or verify_result())
    set_detection(monkeypatch, HOST)
    poll()
    assert "error" not in post_verify()
    assert seen["media_path"] == ""


def test_verify_asks_for_no_password(monkeypatch):
    assert set(VerifyBody.model_fields) == {"expected"}


def test_a_verify_run_cannot_start_while_a_configure_run_is_going(monkeypatch):
    set_detection(monkeypatch, HOST)
    poll()
    cfg.state["busy"] = True
    assert post_verify() == {"error": "A run is already in progress."}


def test_verify_runs_are_counted_separately(monkeypatch):
    set_detection(monkeypatch, HOST)
    poll()
    monkeypatch.setattr(cfg, "_do_verify", lambda inputs: verify_result())
    post_verify()
    assert cfg.counts() == {"done": 0, "error": 0,
                            "verified": 1, "verify_failed": 0}


# ── verify against the address THIS unit was given (TEC-848) ─────────────────

def test_verify_states_no_address_by_default(monkeypatch):
    """The point of the mode: the operator holding a finished speaker knows
    nothing about which address it got. None means "use its record"."""
    seen = {}
    monkeypatch.setattr(cfg, "_do_verify",
                        lambda inputs: seen.update(inputs) or verify_result())
    set_detection(monkeypatch, FIXED_IP)
    poll()
    post_verify()
    assert seen["target_ip"] is None
    assert seen["ip_mode"] == ""


def test_an_operator_can_state_the_address(monkeypatch):
    seen = {}
    monkeypatch.setattr(cfg, "_do_verify",
                        lambda inputs: seen.update(inputs) or verify_result())
    set_detection(monkeypatch, "192.168.88.71")
    poll()
    post_verify(expected={"ip": "71"})
    assert seen["target_ip"] == "192.168.88.71"


def test_an_operator_can_state_that_it_was_left_on_dhcp(monkeypatch):
    # An absent address and "this one is on DHCP" are different claims, and
    # only the second one means "expect no bench-assigned address".
    seen = {}
    monkeypatch.setattr(cfg, "_do_verify",
                        lambda inputs: seen.update(inputs) or verify_result())
    set_detection(monkeypatch, HOST)
    poll()
    post_verify(expected={"ip_mode": "dhcp"})
    assert seen["ip_mode"] == "dhcp"
    assert seen["target_ip"] == ""


def test_an_off_subnet_stated_address_is_dropped_rather_than_trusted(monkeypatch):
    # An expectation no speaker on this bench could meet would fail every unit
    # it was typed against; falling back to the record is the honest reading.
    seen = {}
    monkeypatch.setattr(cfg, "_do_verify",
                        lambda inputs: seen.update(inputs) or verify_result())
    set_detection(monkeypatch, FIXED_IP)
    poll()
    post_verify(expected={"ip": "10.0.0.70"})
    assert seen["target_ip"] is None
