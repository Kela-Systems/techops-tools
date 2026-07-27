"""Tests for the shared per-run JSON writer (bench_core.bench_ui.save_run_record,
TEC-572) — the one function both bench bases (BenchConfigurator and MagosBench)
write their run records through."""
import json
from datetime import datetime
from pathlib import Path

from bench_core.bench_ui import save_run_record
from bench_core.run_record import build_run_entry


def test_writes_record_and_returns_path(tmp_path):
    entry = build_run_entry(tool="speaker", ok=True, serial="SN-9")
    path = save_run_record(tmp_path, entry, name_stem="speaker-70")

    assert path is not None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["run_id"] == entry["run_id"]
    assert data["timestamp"] == entry["timestamp"]

    # Filename: {ts}_{stem}_{status}.json, timestamp taken from the record
    # itself so the name and the content always agree.
    ts = datetime.fromisoformat(entry["timestamp"])
    assert Path(path).name == f"{ts.strftime('%Y%m%d-%H%M%S')}_speaker-70_ok.json"


def test_prefix_extra_payload_and_slugged_stem(tmp_path):
    # The MagosBench call shape: filename prefix, serial stem (may be missing),
    # and the raw device payloads appended to the JSON only.
    entry = build_run_entry(tool="magos-apu", ok=False, error="boom")
    path = save_run_record(tmp_path, entry, name_stem=None, prefix="apu_",
                           extra={"raw_identity_payloads": {"info": {"a": 1}}})

    name = Path(path).name
    assert name.startswith("apu_")
    assert name.endswith("_unknown_error.json")  # None stem slugs to "unknown"
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["raw_identity_payloads"] == {"info": {"a": 1}}
    assert "raw_identity_payloads" not in entry  # extra goes to the file, not the entry


def test_record_without_timestamp_gets_stamped(tmp_path):
    entry = build_run_entry(tool="otd", ok=True)
    del entry["timestamp"]  # a pre-TEC-572 caller
    path = save_run_record(tmp_path, entry, name_stem="otd-x")
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert datetime.fromisoformat(data["timestamp"]).tzinfo is not None


def test_failed_write_returns_none(tmp_path):
    entry = build_run_entry(tool="otd", ok=True)
    missing_dir = tmp_path / "does-not-exist"
    assert save_run_record(missing_dir, entry, name_stem="x") is None
