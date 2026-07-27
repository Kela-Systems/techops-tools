"""Tests for the canonical run-record schema (bench_core.run_record, TEC-346).

The legacy fixtures below mirror the exact entry shapes each tool's
`build_entry` emitted BEFORE the schema existed — parse_run_record must keep
reading the per-run JSON files already sitting in logs/ on operator machines.
"""
import uuid
from datetime import datetime

import pytest

from bench_core.run_record import (
    RUN_RECORD_SCHEMA,
    build_run_entry,
    parse_run_record,
    verification_outcome,
)


# ── build_run_entry ───────────────────────────────────────────────────────────

def test_build_entry_core_shape():
    entry = build_run_entry(tool="otd", ok=True, serial="SN-1", mac="aa:bb",
                            model="OTD500", firmware="07.20", duration_s=95,
                            device={"hostname": "otd-haifa", "site_name": "haifa"})
    assert entry["schema"] == RUN_RECORD_SCHEMA
    assert entry["tool"] == "otd"
    assert entry["status"] == "ok"
    assert entry["error"] is None
    assert entry["duration_s"] == 95
    assert entry["device"] == {"hostname": "otd-haifa", "site_name": "haifa"}
    # containers default to empty, never None
    assert entry["verification"] == [] and entry["warnings"] == [] and entry["steps"] == []


def test_build_entry_mints_run_id_and_timestamp():
    a = build_run_entry(tool="otd", ok=True)
    b = build_run_entry(tool="otd", ok=True)
    uuid.UUID(a["run_id"])                       # well-formed
    assert a["run_id"] != b["run_id"]            # unique per run (upload dedup key)
    ts = datetime.fromisoformat(a["timestamp"])
    assert ts.tzinfo is not None                 # timezone-aware UTC
    assert a["time"] == ts.strftime("%H:%M:%S")  # UI column agrees with the core stamp


def test_build_entry_failure_status():
    entry = build_run_entry(tool="rutm", ok=False, error="boom")
    assert entry["status"] == "error"
    assert entry["error"] == "boom"


def test_build_entry_rejects_unknown_tool():
    with pytest.raises(ValueError):
        build_run_entry(tool="toaster", ok=True)


def test_build_entry_derives_verified_from_checks():
    checks = [{"item": "hostname", "ok": True}, {"item": "RMS", "ok": None}]
    entry = build_run_entry(tool="otd", ok=True, verification=checks)
    assert entry["verified"] is True
    assert entry["verify_detail"] is None

    checks = [{"item": "hostname", "ok": True}, {"item": "Tailscale", "ok": False}]
    entry = build_run_entry(tool="otd", ok=False, verification=checks)
    assert entry["verified"] is False
    assert "Tailscale" in entry["verify_detail"]


def test_build_entry_explicit_verified_wins():
    entry = build_run_entry(tool="magos-radar", ok=True,
                            verified=False, verify_detail="no answer at the new IP")
    assert entry["verified"] is False
    assert entry["verify_detail"] == "no answer at the new IP"


def test_verification_outcome_no_scoped_checks_is_none():
    assert verification_outcome([]) == (None, None)
    assert verification_outcome([{"item": "RMS", "ok": None}]) == (None, None)


# ── parse_run_record: canonical records ───────────────────────────────────────

def test_parse_canonical_roundtrip():
    entry = build_run_entry(tool="speaker", ok=True, serial="SN-9",
                            device={"hostname": "speaker-70", "ip": "192.168.88.70"})
    entry.update({"operator": "Dana K", "station_id": "bench-1",
                  "bench_version": "abc1234", "log_file": "/tmp/x.json"})
    parsed = parse_run_record(entry)
    assert parsed == entry


def test_parse_fills_missing_run_id_and_timestamp():
    # A canonical record written before run_id/timestamp joined the core must
    # still parse, with both keys present (None) so consumers can rely on them.
    rec = build_run_entry(tool="otd", ok=True)
    del rec["run_id"], rec["timestamp"]
    parsed = parse_run_record(rec)
    assert parsed["run_id"] is None
    assert parsed["timestamp"] is None


# ── parse_run_record: legacy (pre-schema) records, one per family ─────────────

def _legacy_common(**over):
    base = {"mac": "aa:bb", "serial": "SN-1", "model": "X", "status": "ok",
            "error": None, "steps": [], "log": "", "time": "10:00:00",
            "operator": "Dana K", "station_id": "bench-1", "bench_version": "abc1234"}
    base.update(over)
    return base


def test_parse_legacy_otd():
    rec = _legacy_common(hostname="otd-haifa", site_name="haifa", model="OTD500",
                         firmware="07.20", imei="35000...", warnings=[],
                         verification=[{"item": "hostname", "ok": True}],
                         duration_s=120)
    parsed = parse_run_record(rec)
    assert parsed["schema"] == RUN_RECORD_SCHEMA
    assert parsed["tool"] == "otd"
    assert parsed["device"] == {"hostname": "otd-haifa", "site_name": "haifa",
                                "imei": "35000..."}
    assert parsed["verified"] is True          # derived from the check list
    assert parsed["operator"] == "Dana K"      # provenance passes through
    assert parsed["run_id"] is None            # pre-run_id record: key present, empty


def test_parse_legacy_rutm():
    rec = _legacy_common(hostname="rut-haifa", site_name="haifa", model="RUTM08",
                         firmware="07.20", verification=[], duration_s=80)
    parsed = parse_run_record(rec)
    assert parsed["tool"] == "rutm"
    assert parsed["device"]["site_name"] == "haifa"
    assert parsed["verified"] is None          # nothing was verified


def test_parse_legacy_raythink():
    rec = _legacy_common(hostname="raythink-30", profile="lan", ip="192.168.88.30",
                         model="Raythink", firmware="1.0", verification=[],
                         duration_s=60)
    parsed = parse_run_record(rec)
    assert parsed["tool"] == "raythink"
    assert parsed["device"] == {"hostname": "raythink-30", "profile": "lan",
                                "ip": "192.168.88.30"}


def test_parse_legacy_speaker():
    rec = _legacy_common(hostname="speaker-70", ip="192.168.88.70",
                         from_host="192.168.2.238", model="Provision-ISR speaker",
                         firmware="V3.3.39", verification=[], duration_s=45)
    parsed = parse_run_record(rec)
    assert parsed["tool"] == "speaker"
    assert parsed["device"]["from_host"] == "192.168.2.238"


def test_parse_legacy_magos_radar():
    rec = _legacy_common(channel="2", ip="192.168.88.52/24", from_host="192.168.40.50",
                         ntp="pool.ntp.org", timezone="Asia/Jerusalem",
                         model="AR-300", verified=True, verify_detail=None)
    parsed = parse_run_record(rec)
    assert parsed["tool"] == "magos-radar"
    assert parsed["device"]["channel"] == "2"
    assert parsed["verified"] is True          # kept, not re-derived
    assert parsed["firmware"] is None          # family never reported one
    assert parsed["duration_s"] is None        # family never timed runs


def test_parse_legacy_magos_radar_without_channel():
    # Not every radar record has a `channel` key — it must still be recognised
    # as a radar (NOT fall through to speaker) via the other Magos-only keys.
    rec = _legacy_common(ip="192.168.88.55/24", from_host="192.168.40.50",
                         ntp="pool.ntp.org", timezone="Asia/Jerusalem",
                         model="AR-300", verified=True, verify_detail=None)
    parsed = parse_run_record(rec)
    assert parsed["tool"] == "magos-radar"
    assert "channel" not in parsed["device"]
    assert parsed["device"]["ip"] == "192.168.88.55/24"


def test_parse_legacy_magos_apu():
    rec = _legacy_common(channel="1", ip="192.168.88.61", radar_ip="192.168.88.51",
                         from_host="192.168.40.60", ntp="pool.ntp.org",
                         timezone="Asia/Jerusalem", model="MSA1588APU",
                         verified=False, verify_detail="no answer")
    parsed = parse_run_record(rec)
    assert parsed["tool"] == "magos-apu"
    assert parsed["device"]["radar_ip"] == "192.168.88.51"
    assert parsed["verified"] is False
    assert parsed["verify_detail"] == "no answer"
