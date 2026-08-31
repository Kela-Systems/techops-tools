"""Tests for station-side central shipping (bench_core.central, TEC-573):
the outbox spool fed by save_run_record() and the uploader that drains it."""
import json
from pathlib import Path

import pytest
import requests

from bench_core import central
from bench_core.bench_ui import save_run_record
from bench_core.central import CentralUploader, spool_run_record, start_central_uploader
from bench_core.run_record import build_run_entry

COLLECTOR = "http://collector.test:8100"


@pytest.fixture
def shipping_on(monkeypatch):
    monkeypatch.setenv("BENCH_CENTRAL_URL", COLLECTOR)


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


# ── spooling ──────────────────────────────────────────────────────────────────

def test_no_spool_when_shipping_is_off(tmp_path, monkeypatch):
    monkeypatch.delenv("BENCH_CENTRAL_URL", raising=False)
    spool_run_record(tmp_path, build_run_entry(tool="otd", ok=True))
    assert not (tmp_path / "outbox").exists()
    assert start_central_uploader(tmp_path) is None


def test_save_run_record_spools_without_raw_payloads(tmp_path, shipping_on):
    # The MagosBench call shape: raw payloads ride along as `extra`, which must
    # reach the local per-run JSON but never the outbox (decided 2026-07-26).
    entry = build_run_entry(tool="magos-radar", ok=True, serial="SN-7")
    local = save_run_record(tmp_path, entry, name_stem="SN-7", prefix="",
                            extra={"raw_identity_payloads": {"secret": "stuff"}})

    assert "raw_identity_payloads" in json.loads(
        Path(local).read_text(encoding="utf-8"))
    queued = list((tmp_path / "outbox").glob("*.json"))
    assert len(queued) == 1
    assert queued[0].name.endswith(f"_{entry['run_id']}.json")
    shipped = json.loads(queued[0].read_text(encoding="utf-8"))
    assert "raw_identity_payloads" not in shipped
    assert shipped["run_id"] == entry["run_id"]


def test_spool_survives_unwritable_dir(tmp_path, shipping_on):
    # A file where the outbox dir should be — mkdir fails; the run must not.
    (tmp_path / "outbox").write_text("in the way", encoding="utf-8")
    spool_run_record(tmp_path, build_run_entry(tool="otd", ok=True))  # no raise


# ── draining ──────────────────────────────────────────────────────────────────

def _queue(tmp_path, n=1):
    """Spool `n` records; returns the outbox dir and the entries in order."""
    entries = [build_run_entry(tool="otd", ok=True, serial=f"SN-{i}")
               for i in range(n)]
    outbox = tmp_path / "outbox"
    outbox.mkdir(exist_ok=True)
    for i, entry in enumerate(entries):
        # Fixed, ascending stamps so oldest-first ordering is deterministic.
        (outbox / f"20260727-00000{i}_{entry['run_id']}.json").write_text(
            json.dumps(entry), encoding="utf-8")
    return outbox, entries


def _uploader(outbox):
    # The uploader is given the tool's logs/ directory and finds its queues
    # under it, so hand it the outbox's parent.
    return CentralUploader(outbox.parent, COLLECTOR)


def test_drain_uploads_oldest_first_and_deletes(tmp_path, monkeypatch):
    outbox, entries = _queue(tmp_path, n=3)
    posted = []

    def fake_post(url, json=None, timeout=None):
        assert url == COLLECTOR + "/api/v1/runs"
        posted.append(json["serial"])
        return FakeResponse(201)

    monkeypatch.setattr(central.requests, "post", fake_post)
    assert _uploader(outbox).drain_once() is True
    assert posted == ["SN-0", "SN-1", "SN-2"]
    assert list(outbox.iterdir()) == []


def test_duplicate_409_counts_as_accepted(tmp_path, monkeypatch):
    outbox, _ = _queue(tmp_path)
    monkeypatch.setattr(central.requests, "post",
                        lambda *a, **k: FakeResponse(409))
    assert _uploader(outbox).drain_once() is True
    assert list(outbox.iterdir()) == []


def test_unreachable_keeps_records_and_backs_off(tmp_path, monkeypatch):
    outbox, _ = _queue(tmp_path)

    def fake_post(*a, **k):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(central.requests, "post", fake_post)
    assert _uploader(outbox).drain_once() is False
    assert len(list(outbox.glob("*.json"))) == 1  # nothing lost


def test_collector_5xx_keeps_records_and_backs_off(tmp_path, monkeypatch):
    outbox, _ = _queue(tmp_path)
    monkeypatch.setattr(central.requests, "post",
                        lambda *a, **k: FakeResponse(503))
    assert _uploader(outbox).drain_once() is False
    assert len(list(outbox.glob("*.json"))) == 1


def test_rejected_record_is_quarantined_not_poison(tmp_path, monkeypatch):
    outbox, _ = _queue(tmp_path, n=2)
    codes = iter([400, 201])
    monkeypatch.setattr(central.requests, "post",
                        lambda *a, **k: FakeResponse(next(codes)))
    assert _uploader(outbox).drain_once() is True
    # The rejected record is set aside; the one behind it still shipped.
    assert len(list(outbox.glob("*.json.bad"))) == 1
    assert list(outbox.glob("*.json")) == []


def test_corrupt_file_is_quarantined(tmp_path, monkeypatch):
    outbox, _ = _queue(tmp_path)
    (outbox / "00000000-000000_torn.json").write_text("{not json",
                                                      encoding="utf-8")
    monkeypatch.setattr(central.requests, "post",
                        lambda *a, **k: FakeResponse(200))
    assert _uploader(outbox).drain_once() is True
    assert len(list(outbox.glob("*.json.bad"))) == 1
    assert list(outbox.glob("*.json")) == []


def test_missing_outbox_dir_is_fine(tmp_path):
    assert _uploader(tmp_path / "outbox").drain_once() is True
