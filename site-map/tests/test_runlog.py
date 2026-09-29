"""The transcript of a run, and the one thing it must never contain.

Every failure this tool has had was a silent one: `hostname` returning rc
127 on BusyBox, an ssh refused for a username rather than a key, a
neighbour table full of k3s pod addresses, a host answering with the wrong
MAC. All of them are obvious in a transcript and invisible in a result,
which is what these files are for.
"""
import json

import pytest

import runlog


@pytest.fixture
def log(tmp_path):
    return runlog.RunLog(
        request={"server": "100.64.242.104", "subnet": None, "via": "router"},
        secrets=("hunter2", "station-pw"),
        directory=tmp_path / "runs",
    )


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


# -- secrets ------------------------------------------------------------

def test_a_password_never_reaches_the_file(log):
    log.command("router", "login --password hunter2", rc=0,
                output="authenticated as root with hunter2")
    log.note("tried hunter2")
    record = read(log.finish())
    blob = json.dumps(record)
    assert "hunter2" not in blob
    assert "station-pw" not in blob
    assert blob.count("[redacted]") >= 3, "and greppable as plain ASCII"


def test_a_secret_inside_an_exception_is_redacted_too(log):
    # A traceback can carry a password that was a function argument.
    try:
        raise RuntimeError("auth failed for root/hunter2")
    except RuntimeError as exc:
        record = read(log.finish(error=exc))
    assert "hunter2" not in json.dumps(record)
    assert record["ok"] is False
    assert record["error"]["type"] == "RuntimeError"


def test_a_very_short_secret_is_not_redacted_everywhere(log):
    # Redacting a 1-2 character secret would scrub half the transcript, and
    # a password that short is not the thing being protected here.
    log.secrets = ("a",)
    log.command("router", "cat /tmp/dhcp.leases", rc=0, output="a lease table")
    assert "lease table" in json.dumps(read(log.finish()))


# -- what a reader needs ------------------------------------------------

def test_each_command_carries_its_exit_status_and_duration(log):
    log.command("router", "hostname", rc=127, seconds=0.35,
                output="", error="ash: hostname: not found")
    step = read(log.finish())["steps"][0]
    assert step["rc"] == 127
    assert step["seconds"] == 0.35
    assert "not found" in step["error"]


def test_output_is_capped_and_says_when_it_was(log):
    log.command("router", "ip neigh show", rc=0, output="x" * (runlog.MAX_OUTPUT + 50))
    step = read(log.finish())["steps"][0]
    assert len(step["output"]) == runlog.MAX_OUTPUT
    assert step["output_truncated"] is True
    assert step["output_bytes"] == runlog.MAX_OUTPUT + 50


def test_the_result_records_evidence_per_device_not_just_names(log):
    site = {
        "name": "kela-fob-03",
        "subnet": "192.168.88.0/24",
        "coverage": {"model": {"proven": 2, "total": 10}},
        "nodes": {"router": {"addr": "192.168.88.1", "mac": "20:97:27:36:55:ec",
                             "model": "Teltonika RUTM08",
                             "evidence": {"*": "device-api"},
                             "interfaces": [{"name": "br-lan"}]}},
        "topology": {"router": "router", "switches": [], "counts": {}},
    }
    record = read(log.finish(site=site, answered=9, swept=254))
    assert record["result"]["coverage"] == {"model": "2/10"}
    device = record["result"]["devices"][0]
    assert device["evidence"] == {"*": "device-api"}
    assert record["answered"] == 9 and record["swept"] == 254


def test_the_request_is_recorded_including_an_absent_subnet(log):
    record = read(log.finish())
    assert record["request"]["subnet"] is None, "so 'read from the router' is visible"
    assert record["request"]["server"] == "100.64.242.104"


def test_a_timed_command_records_a_raised_exception(log):
    with pytest.raises(ValueError):
        with log.timed("router", "ip neigh show") as timer:
            raise ValueError("boom")
    step = read(log.finish())["steps"][0]
    assert "ValueError: boom" in step["error"]
    assert step["seconds"] is not None


# -- housekeeping -------------------------------------------------------

def test_runs_are_listed_oldest_first(tmp_path):
    directory = tmp_path / "runs"
    for i in range(3):
        runlog.RunLog(request={"n": i}, directory=directory,
                      started=f"2026-09-1{i}T00:00:00+00:00").finish()
    runs = runlog.list_runs(directory)
    assert [r["request"]["n"] for _, r in runs] == [0, 1, 2]


def test_old_runs_are_pruned(tmp_path):
    directory = tmp_path / "runs"
    directory.mkdir()
    for i in range(12):
        (directory / f"2026091{i:02d}-x.json").write_text("{}")
    assert runlog.prune(directory, keep=5) == 7
    assert len(list(directory.glob("*.json"))) == 5


def test_an_unwritable_directory_does_not_fail_the_survey(log, monkeypatch):
    # A survey must never fail because its log could not be written.
    def boom(*args, **kwargs):
        raise OSError("read-only file system")
    monkeypatch.setattr(runlog.Path, "mkdir", boom)
    assert log.finish() is None


def test_a_corrupt_run_file_is_skipped_rather_than_crashing_the_listing(tmp_path):
    directory = tmp_path / "runs"
    directory.mkdir()
    (directory / "20260101-good.json").write_text('{"ok": true}')
    (directory / "20260102-bad.json").write_text("{not json")
    assert len(runlog.list_runs(directory)) == 1
