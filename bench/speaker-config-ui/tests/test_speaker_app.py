"""State-machine tests for the Provision-ISR speaker configurator
(speaker_app.py).

No hardware/network: detection is faked via `_find_speaker` + `read_device_mac`
and the device pipeline via `_do_configure`.
"""
import asyncio

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


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    cfg.state.update(cfg.initial_state())
    cfg.state["config_loaded"] = True
    monkeypatch.setattr(cfg, "_save_log", lambda entry: None)


def set_detection(monkeypatch, host, mac="74:f8:db:5f:25:6a"):
    monkeypatch.setattr(cfg, "_find_speaker", lambda: host)
    monkeypatch.setattr(mod, "read_device_mac", lambda *a, **k: mac)


def poll():
    async def run():
        await cfg.poll_once(asyncio.get_running_loop())
    asyncio.run(run())


def run_configure(ok=True, monkeypatch=None):
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result(ok=ok))
    inputs = {"host": HOST, "mac": "74:f8:db:5f:25:6a", "media_path": ""}
    asyncio.run(cfg.execute_run(inputs, "test"))


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
    set_detection(monkeypatch, cfg._target_ip())
    poll()
    assert cfg.state["phase"] == "configured"


def test_configure_records_history(monkeypatch):
    run_configure(ok=True, monkeypatch=monkeypatch)
    assert cfg.state["phase"] == "configured"
    entry = cfg.state["history"][0]
    assert entry["schema"] == RUN_RECORD_SCHEMA
    assert entry["tool"] == "speaker"
    assert entry["status"] == "ok"
    assert entry["serial"] == "TM-CS20-000001-XX"
    assert entry["device"]["ip"] == cfg._target_ip()
    assert entry["device"]["from_host"] == HOST


def test_failed_configure_sets_error(monkeypatch):
    run_configure(ok=False, monkeypatch=monkeypatch)
    assert cfg.state["phase"] == "error"
    assert cfg.state["history"][0]["status"] == "error"


def test_hostname_uses_target_ip_octet():
    assert cfg.hostname_for({}) == f"speaker-{cfg._target_ip().rsplit('.', 1)[-1]}"


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
