"""Keeping the factory-label password on bench-central (TEC-845).

A Teltonika's factory password is unique per unit and printed on its sticker.
The bench uses it once and replaces it — and it is what the device reverts to
on a factory reset, so throwing it away is how a unit reset in the field
becomes unreachable. These cover the station's half: which runs produce a
retained password, which deliberately produce none, and what the queued record
says.

The negatives carry most of the weight. Retaining the wrong string is worse
than retaining nothing, because it reads as an answer: somebody months from now
tries it on a reset device, it fails, and the store has quietly lied. So the
shared password, an empty field and a run that never identified its device all
have to come out empty, and each has its own case here.

Driven through `execute_run` on a stub configurator rather than through a real
tool, because retention lives in the shared base — the three tools that use it
only flip a flag.
"""
import asyncio
import json

import pytest

from bench_core.bench_ui import BenchConfigurator
from bench_core.central import LABEL_OUTBOX_DIRNAME
from bench_core.label_record import LABEL_RECORD_SCHEMA
from bench_core.run_record import build_run_entry

COLLECTOR = "http://collector.test:8100"
SHARED = "test-shared-pw"          # bench_core.DEFAULT_NEW_PASSWORD

# A real OTD500 sticker (the one in device_label.py's docstring).
LABEL = "SN:6008219573;I:864088065513384;M:2097272B00F7;U:admin;PW:zZ?40*kA;B:015;"
LABEL_PW = "zZ?40*kA"
LABEL_SN = "6008219573"
LABEL_MAC = "20:97:27:2b:00:f7"


class StubBench(BenchConfigurator):
    """The smallest configurator that can complete a run: no device, no
    pipeline, no config file."""

    title = "Retention stub"
    config_filename = "config/stub.config.json"
    log_filename = "stub.log"
    # The shared "teltonika" logger the three real tools use — it is the one
    # carrying the context filter that LOG_LINE_FORMAT's fields come from.
    label_scan_enabled = True
    retain_label_password = True
    record_tool = "otd"

    def initial_state(self) -> dict:
        return {"phase": "waiting", "detected": True, "active_mac": None,
                "busy": False, "message": "", "last_result": None,
                "history": []}

    def hostname_for(self, inputs: dict) -> str:
        return "otd-under-test"

    def build_entry(self, result: dict, inputs: dict, duration: int) -> dict:
        ident = result["identity"]
        return build_run_entry(
            tool="otd", ok=result["ok"], error=result["error"],
            serial=ident.get("serial", "unknown"),
            mac=ident.get("mac", "unknown"),
            model=ident.get("model", "unknown"),
            device={"imei": ident.get("imei", "unknown"),
                    "password_source": self.password_source(inputs,
                                                            "label_password")})


def identified(serial=LABEL_SN, mac=LABEL_MAC, model="OTD500",
               imei="864088065513384"):
    return {"serial": serial, "mac": mac, "model": model, "imei": imei}


def pipeline_result(ok=True, identity=None):
    """What `_do_configure` hands back once the pipeline has run."""
    return {"ok": ok, "hostname": "otd-under-test",
            "identity": identity if identity is not None else identified(),
            "warnings": [], "error": None if ok else "boom",
            "steps": [], "verification": [], "log": ""}


@pytest.fixture
def bench(tmp_path, monkeypatch):
    """A stub bench with central shipping on.

    The env var goes on AFTER construction on purpose: `__init__` would
    otherwise start a real uploader thread, and these tests want the queue on
    disk, not a POST at a hostname that does not resolve.
    """
    station = tmp_path / "tool"
    station.mkdir()
    monkeypatch.delenv("BENCH_CENTRAL_URL", raising=False)
    cfg = StubBench(station)
    monkeypatch.setenv("BENCH_CENTRAL_URL", COLLECTOR)
    monkeypatch.setattr(cfg, "_save_log", lambda entry: None)
    return cfg


def run(cfg, typed="", *, result=None, key="label_password"):
    """One configure run, the way `/api/configure` drives it."""
    monkeyed = result or pipeline_result()
    cfg._do_configure = lambda inputs: monkeyed
    inputs = {"mac": cfg.state.get("active_mac"),
              **cfg.label_password_inputs(typed, key=key)}
    assert asyncio.run(cfg.execute_run(inputs, "otd-under-test")) is True
    return cfg.state["history"][0]


def queued(cfg) -> list[dict]:
    """Every device-label record waiting to go to central."""
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted((cfg.log_dir / LABEL_OUTBOX_DIRNAME).glob("*.json"))]


def scan(cfg):
    cfg.state["active_mac"] = LABEL_MAC
    assert cfg.arm_label(LABEL) == {}


# ── what gets kept ───────────────────────────────────────────────────────────

def test_a_scanned_password_is_queued_with_the_whole_label(bench):
    scan(bench)
    entry = run(bench)

    record, = queued(bench)
    assert record["schema"] == LABEL_RECORD_SCHEMA
    assert record["serial"] == LABEL_SN
    assert record["password"] == LABEL_PW
    assert record["source"] == "scan"
    assert record["tool"] == "otd"
    assert record["mac"] == "2097272b00f7"
    assert record["model"] == "OTD500"
    # Off the sticker, not off the device — a typed run has none of these.
    assert record["username"] == "admin"
    assert record["batch"] == "015"
    assert record["imei"] == "864088065513384"
    # Cross-references back to the run it was read during.
    assert record["run_id"] == entry["run_id"]
    assert record["captured_at"] == entry["timestamp"]
    assert record["station_id"] == entry["station_id"]


def test_a_typed_password_is_kept_too(bench):
    # The scanner is the happy path, not the only one: a sticker can be
    # scuffed, and TEC-845's capture point is "op entry and/or the QR scan".
    run(bench, "typed-in-by-hand")

    record, = queued(bench)
    assert record["password"] == "typed-in-by-hand"
    assert record["source"] == "typed"
    assert record["serial"] == LABEL_SN     # off the device, not off a label
    assert record["batch"] == ""            # nothing typed carries one


def test_a_failed_run_still_keeps_a_scanned_password(bench):
    # The most valuable case there is: the unit that did not finish is the one
    # somebody will have to get back into.
    scan(bench)
    entry = run(bench, result=pipeline_result(ok=False, identity={}))

    assert entry["status"] == "error"
    record, = queued(bench)
    # The device never answered, so every field here came off the QR — which
    # is exactly why a scan can be keyed when a typed password cannot.
    assert record["serial"] == LABEL_SN
    assert record["password"] == LABEL_PW
    assert record["mac"] == "2097272b00f7"
    assert record["model"] == ""


def test_a_rerun_replaces_its_own_queue_entry(bench):
    # Named after the serial, so a unit read twice before the queue drains
    # ships the reading the operator ended up with, not both in a race.
    run(bench, "first-attempt")
    run(bench, "corrected")

    record, = queued(bench)
    assert record["password"] == "corrected"


# ── what deliberately gets nothing ───────────────────────────────────────────

def test_the_stations_shared_password_is_not_retained(bench):
    # An operator getting back into a finished unit types the SHARED password.
    # Storing that as the factory one would overwrite the real answer with a
    # password we already have everywhere.
    entry = run(bench, SHARED)

    assert entry["device"]["password_source"] == "typed"
    assert queued(bench) == []


def test_a_rerun_on_the_shared_password_retains_nothing(bench):
    # Empty field: the pipeline falls back to the shared password, so there
    # was never a factory password in play.
    entry = run(bench)

    assert entry["device"]["password_source"] == "shared-fallback"
    assert queued(bench) == []


def test_nothing_is_kept_when_the_run_never_identified_the_device(bench):
    # Typed password, no login, no scan: nothing to key a row on — and the
    # password is most likely wrong, since that is usually why a login fails.
    run(bench, "probably-a-typo", result=pipeline_result(ok=False, identity={}))

    assert queued(bench) == []


def test_a_verify_run_retains_nothing(bench):
    # A verify pass takes no password from the operator at all (TEC-348).
    bench.verify_supported = True
    bench.verify_pipeline = lambda client, run_cfg, inputs: pipeline_result()
    bench._do_verify = lambda inputs: pipeline_result()
    inputs = bench.verify_inputs(type("Body", (), {"expected": {}})())
    assert asyncio.run(bench.execute_verify(inputs, "otd-under-test")) is True

    assert queued(bench) == []


def test_a_tool_that_did_not_opt_in_keeps_nothing(bench, monkeypatch):
    # Reading a password off a sticker is not a decision to retain it: the
    # device scope was settled per family, so the switch is its own.
    monkeypatch.setattr(type(bench), "retain_label_password", False)
    scan(bench)
    run(bench)

    assert not (bench.log_dir / LABEL_OUTBOX_DIRNAME).exists()


def test_nothing_leaves_a_station_with_no_collector(bench, monkeypatch):
    monkeypatch.delenv("BENCH_CENTRAL_URL", raising=False)
    scan(bench)
    run(bench)

    assert not (bench.log_dir / LABEL_OUTBOX_DIRNAME).exists()


# ── the line the run record still holds ──────────────────────────────────────

def test_the_run_record_carries_the_provenance_but_not_the_password(bench):
    # TEC-349's rule is unchanged by TEC-845: run records feed the read-only
    # dashboard and get copied per run, so the password rides its own channel.
    scan(bench)
    entry = run(bench)

    assert entry["device"]["password_source"] == "scan"
    assert LABEL_PW not in json.dumps(entry)
