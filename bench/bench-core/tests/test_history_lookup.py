"""Recovering what a finished unit was supposed to be (bench_core.history, TEC-348).

The operator pressing Verify on a provisioned RUTM08 cannot be asked what site
name was typed when it was configured — that is the whole point of the mode. So
the expectation is read back out of the run record the configure run wrote.

Two properties matter more than the happy path:

* a **verify** record must never be accepted as the source of an expectation,
  or a wrong expectation launders itself into looking authoritative on the next
  sweep;
* a unit with **no** configure record must FAIL, not skip. Skipping is how you
  get a green table for a box nobody ever provisioned, which is the exact fault
  class (a check that cannot fail) this issue exists to remove.
"""
import json
import time

import pytest

from bench_core import history
from bench_core.run_record import build_run_entry


def write_record(log_dir, *, serial="SN-1", mac="aa:bb:cc:dd:ee:ff",
                 tool="rutm", kind="configure", device=None, name=None,
                 age_sec=0.0):
    """Drop one per-run JSON into `log_dir` the way save_run_record does."""
    entry = build_run_entry(tool=tool, ok=True, kind=kind, serial=serial,
                            mac=mac, device=device or {"hostname": "rut-haifa",
                                                       "site_name": "haifa"})
    path = log_dir / (name or f"{kind}_{serial}_{time.time_ns()}.json")
    path.write_text(json.dumps(entry), encoding="utf-8")
    if age_sec:
        old = time.time() - age_sec
        import os
        os.utime(path, (old, old))
    return entry


@pytest.fixture
def logs(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def no_central(monkeypatch):
    """Central shipping off unless a test turns it on — otherwise a developer
    with BENCH_CENTRAL_URL exported would have these tests hit a real host."""
    monkeypatch.delenv("BENCH_CENTRAL_URL", raising=False)


# ── the local lookup ─────────────────────────────────────────────────────────

def test_finds_the_configure_run_by_serial(logs):
    write_record(logs, serial="SN-1", device={"hostname": "rut-haifa",
                                              "site_name": "haifa"})
    found = history.find_last_configure_run(logs, serial="SN-1", tool="rutm")
    assert found["device"]["hostname"] == "rut-haifa"


def test_falls_back_to_the_mac_when_the_serial_is_unreadable(logs):
    # A device whose serial the REST identity read couldn't produce still has a
    # LAN MAC, read independently over ARP, and it survives a provision.
    write_record(logs, serial="SN-1", mac="AA:BB:CC:DD:EE:FF")
    found = history.find_last_configure_run(logs, serial="unknown",
                                            mac="aa:bb:cc:dd:ee:ff", tool="rutm")
    assert found is not None


def test_unknown_never_matches_unknown(logs):
    # Two devices that both failed their identity read are not the same device.
    write_record(logs, serial="unknown", mac="unknown")
    assert history.find_last_configure_run(logs, serial="unknown",
                                           mac="unknown", tool="rutm") is None


def test_a_verify_record_is_not_a_source_of_truth(logs):
    # The only record for this unit is a previous verify sweep. It carries no
    # fresh intent — it was itself checked against something — so it must not
    # be treated as evidence the unit was ever configured.
    write_record(logs, serial="SN-1", kind="verify")
    assert history.find_last_configure_run(logs, serial="SN-1",
                                           tool="rutm") is None


def test_the_newest_configure_run_wins(logs):
    write_record(logs, serial="SN-1", age_sec=3600,
                 device={"hostname": "rut-old", "site_name": "old"})
    write_record(logs, serial="SN-1",
                 device={"hostname": "rut-new", "site_name": "new"})
    found = history.find_last_configure_run(logs, serial="SN-1", tool="rutm")
    assert found["device"]["hostname"] == "rut-new"


def test_a_verify_run_does_not_shadow_the_configure_run_underneath_it(logs):
    # The realistic sweep: configure, then verify, then verify again. The
    # newest record is a verify one and must be walked past, not stopped at.
    write_record(logs, serial="SN-1", age_sec=600,
                 device={"hostname": "rut-haifa", "site_name": "haifa"})
    write_record(logs, serial="SN-1", kind="verify",
                 device={"hostname": "rut-haifa"})
    found = history.find_last_configure_run(logs, serial="SN-1", tool="rutm")
    assert found["kind"] == "configure"
    assert found["device"]["site_name"] == "haifa"


def test_another_tools_record_for_the_same_serial_is_ignored(logs):
    write_record(logs, serial="SN-1", tool="tsw", device={"ip": "192.168.88.2"})
    assert history.find_last_configure_run(logs, serial="SN-1",
                                           tool="rutm") is None


def test_a_corrupt_file_does_not_stop_the_scan(logs):
    (logs / "20260101-000000_torn_ok.json").write_text("{not json",
                                                       encoding="utf-8")
    write_record(logs, serial="SN-1", age_sec=60)
    assert history.find_last_configure_run(logs, serial="SN-1",
                                           tool="rutm") is not None


def test_a_missing_log_dir_is_not_an_error(tmp_path):
    assert history.find_last_configure_run(tmp_path / "nope",
                                           serial="SN-1") is None


def test_a_legacy_record_from_before_the_verify_mode_counts_as_configure(logs):
    # Records already on operator machines have no `kind`. parse_run_record
    # reads them as configure runs, so the whole existing fleet is verifiable
    # rather than every unit reporting "no prior run".
    entry = build_run_entry(tool="rutm", ok=True, serial="SN-1",
                            device={"hostname": "rut-haifa"})
    del entry["kind"]
    (logs / "20260101-000000_rut-haifa_ok.json").write_text(json.dumps(entry),
                                                            encoding="utf-8")
    assert history.find_last_configure_run(logs, serial="SN-1",
                                           tool="rutm") is not None


# ── the central lookup ───────────────────────────────────────────────────────

class FakeCentral:
    """Stands in for `requests`, recording the queries the lookup makes.

    `full` is either one record (every run_id resolves to it) or a dict keyed
    by run_id, for the tests that need the listing and the records to disagree.
    """

    def __init__(self, runs=(), full=None, boom=False):
        self.runs = list(runs)
        self.full = full
        self.boom = boom
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params or {}))
        if self.boom:
            raise OSError("collector unreachable")
        if url.endswith("/runs"):
            return _Resp({"runs": self.runs})
        if isinstance(self.full, dict) and "run_id" not in (self.full or {}):
            return _Resp(self.full[url.rsplit("/", 1)[-1]])
        return _Resp(self.full)


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_central_is_asked_only_for_configure_runs(monkeypatch):
    monkeypatch.setenv("BENCH_CENTRAL_URL", "http://central:8100")
    full = build_run_entry(tool="rutm", ok=True, serial="SN-1",
                           device={"hostname": "rut-golan"})
    fake = FakeCentral(runs=[{"run_id": "r-1"}], full=full)
    monkeypatch.setattr(history, "requests", fake)

    found = history.fetch_last_configure_run(serial="SN-1", tool="rutm")
    assert found["device"]["hostname"] == "rut-golan"
    # The filter is what keeps a previous sweep's verify record from coming
    # back as if it were the configure run.
    assert fake.calls[0][1]["kind"] == "configure"
    assert fake.calls[0][1]["serial"] == "SN-1"
    # Second hop: the summary columns don't carry the per-family device block.
    assert fake.calls[1][0].endswith("/api/v1/runs/r-1")


def test_a_collector_that_ignores_the_filter_is_not_trusted(monkeypatch):
    """The deployed case, found by asking the live collector (2026-08-30).

    An older collector has no `kind` column and no `kind` query parameter, and
    FastAPI ignores parameters a route never declared — so the filter silently
    becomes "the newest run of any kind". The first sweep of a unit puts a
    verify record at the top of that list, and accepting it would make the next
    sweep check the unit against its own previous sweep.
    """
    monkeypatch.setenv("BENCH_CENTRAL_URL", "http://central:8100")
    sweep = build_run_entry(tool="rutm", ok=True, kind="verify", serial="SN-1",
                            device={"hostname": "rut-golan"})
    configured = build_run_entry(tool="rutm", ok=True, serial="SN-1",
                                 device={"hostname": "rut-golan"})
    # No `kind` in the summary rows — that is what an old collector returns.
    monkeypatch.setattr(history, "requests", FakeCentral(
        runs=[{"run_id": "sweep"}, {"run_id": "configured"}],
        full={"sweep": sweep, "configured": configured}))

    found = history.fetch_last_configure_run(serial="SN-1", tool="rutm")
    assert found["kind"] == "configure"
    assert found["run_id"] == configured["run_id"]


def test_only_sweeps_and_no_configure_run_is_a_miss(monkeypatch):
    # Nothing to launder from: the honest answer is "no configure record",
    # which surfaces as the failing `prior run` row.
    monkeypatch.setenv("BENCH_CENTRAL_URL", "http://central:8100")
    sweep = build_run_entry(tool="rutm", ok=True, kind="verify", serial="SN-1")
    monkeypatch.setattr(history, "requests", FakeCentral(
        runs=[{"run_id": "s1"}], full={"s1": sweep}))
    assert history.fetch_last_configure_run(serial="SN-1", tool="rutm") is None


def test_a_verify_row_in_the_summary_costs_no_second_request(monkeypatch):
    # A current collector reports `kind` in the summary, so the record itself
    # never has to be fetched to be rejected.
    monkeypatch.setenv("BENCH_CENTRAL_URL", "http://central:8100")
    configured = build_run_entry(tool="rutm", ok=True, serial="SN-1")
    fake = FakeCentral(runs=[{"run_id": "s1", "kind": "verify"},
                             {"run_id": "c1", "kind": "configure"}],
                       full={"c1": configured})
    monkeypatch.setattr(history, "requests", fake)

    assert history.fetch_last_configure_run(serial="SN-1", tool="rutm") is not None
    fetched = [url for url, _ in fake.calls if "/runs/" in url]
    assert fetched == ["http://central:8100/api/v1/runs/c1"]


def test_a_legacy_central_record_with_no_kind_still_counts(monkeypatch):
    # Every record uploaded before the mode existed. Rejecting these would make
    # the entire already-provisioned fleet unverifiable.
    monkeypatch.setenv("BENCH_CENTRAL_URL", "http://central:8100")
    legacy = build_run_entry(tool="rutm", ok=True, serial="SN-1",
                             device={"hostname": "rut-haifa"})
    del legacy["kind"]
    monkeypatch.setattr(history, "requests", FakeCentral(
        runs=[{"run_id": "r-1"}], full={"r-1": legacy}))
    found = history.fetch_last_configure_run(serial="SN-1", tool="rutm")
    assert found["device"]["hostname"] == "rut-haifa"


def test_central_off_is_not_consulted(monkeypatch):
    monkeypatch.setattr(history, "requests", FakeCentral(boom=True))
    assert history.fetch_last_configure_run(serial="SN-1", tool="rutm") is None


def test_an_unreachable_collector_falls_through_rather_than_raising(monkeypatch):
    # The bench must stay usable when the tailnet is down.
    monkeypatch.setenv("BENCH_CENTRAL_URL", "http://central:8100")
    monkeypatch.setattr(history, "requests", FakeCentral(boom=True))
    assert history.fetch_last_configure_run(serial="SN-1", tool="rutm") is None


def test_central_with_no_record_for_this_unit(monkeypatch):
    monkeypatch.setenv("BENCH_CENTRAL_URL", "http://central:8100")
    monkeypatch.setattr(history, "requests", FakeCentral(runs=[]))
    assert history.fetch_last_configure_run(serial="SN-1", tool="rutm") is None


# ── resolve_expected: the rule that a miss must fail ─────────────────────────

def test_a_local_record_supplies_the_expectations(logs):
    write_record(logs, serial="SN-1", device={"hostname": "rut-haifa",
                                              "site_name": "haifa"})
    expected, prior, source = history.resolve_expected(
        logs, serial="SN-1", tool="rutm")
    assert expected["hostname"] == "rut-haifa"
    assert prior is None
    assert source == "station"


def test_no_record_anywhere_produces_a_failing_row(logs):
    expected, prior, source = history.resolve_expected(
        logs, serial="SN-1", mac="aa:bb", tool="rutm")
    assert expected == {}
    assert source == "none"
    # Not ok=None. A skipped row is how a green table gets printed for a device
    # nobody provisioned; TEC-352 prints a label on verified is True.
    assert prior["ok"] is False
    assert prior["item"] == "prior run"
    assert "SN-1" in prior["actual"]


def test_the_failing_row_says_whether_central_was_even_reachable(logs,
                                                                monkeypatch):
    _, without, _ = history.resolve_expected(logs, serial="SN-1", tool="rutm")
    assert "bench-central is not configured" in without["actual"]

    monkeypatch.setenv("BENCH_CENTRAL_URL", "http://central:8100")
    monkeypatch.setattr(history, "requests", FakeCentral(runs=[]))
    _, with_central, _ = history.resolve_expected(logs, serial="SN-1", tool="rutm")
    assert "bench-central" in with_central["actual"]
    assert "not configured" not in with_central["actual"]


def test_an_operator_override_beats_the_record(logs):
    write_record(logs, serial="SN-1", device={"hostname": "rut-typo",
                                              "site_name": "typo"})
    expected, prior, source = history.resolve_expected(
        logs, serial="SN-1", tool="rutm", overrides={"site_name": "haifa"})
    assert expected["site_name"] == "haifa"
    assert expected["hostname"] == "rut-typo"   # untouched fields still apply
    assert prior is None
    assert source == "override"


def test_an_override_alone_is_enough_to_state_the_intent(logs):
    # No record at all, but the operator named the site. That is a stronger
    # claim than a lookup, so it stands on its own without the failing row.
    expected, prior, source = history.resolve_expected(
        logs, serial="SN-1", tool="rutm", overrides={"site_name": "haifa"})
    assert expected == {"site_name": "haifa"}
    assert prior is None
    assert source == "override"


def test_blank_overrides_are_not_overrides(logs):
    # The form posts empty strings for fields the operator left alone; those
    # must not count as "the operator stated the intent".
    _, prior, source = history.resolve_expected(
        logs, serial="SN-1", tool="rutm", overrides={"site_name": "  "})
    assert prior is not None and source == "none"


def test_central_is_used_when_the_unit_was_provisioned_on_another_bench(logs,
                                                                       monkeypatch):
    monkeypatch.setenv("BENCH_CENTRAL_URL", "http://central:8100")
    full = build_run_entry(tool="rutm", ok=True, serial="SN-1",
                           device={"hostname": "rut-golan"})
    monkeypatch.setattr(history, "requests",
                        FakeCentral(runs=[{"run_id": "r-1"}], full=full))
    expected, prior, source = history.resolve_expected(
        logs, serial="SN-1", tool="rutm")
    assert expected["hostname"] == "rut-golan"
    assert prior is None
    assert source == "central"
