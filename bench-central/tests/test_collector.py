"""Tests for the central collector (collector.py, TEC-574): the ingest
contract the station uploader relies on (201/409/400), durable storage
across restarts, and the read endpoints engineers browse with."""
import pytest
from fastapi.testclient import TestClient

from bench_core.run_record import build_run_entry, parse_run_record

from collector import create_app


def make_record(**over):
    """A canonical record the way a station ships it: built by
    build_run_entry() and stamped with provenance."""
    kwargs = {"tool": "otd", "ok": True, "serial": "SN-1", "mac": "aa:bb",
              "model": "OTD500", "device": {"hostname": "otd-x"}}
    stamps = {"operator": "Dana K", "station_id": "bench-1",
              "bench_version": "abc1234"}
    for key, value in over.items():
        (kwargs if key in kwargs or key in ("ok", "error") else stamps)[key] = value
    entry = build_run_entry(**kwargs)
    entry.update(stamps)
    if "timestamp" in over:
        entry["timestamp"] = over["timestamp"]
    return entry


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path / "runs.db"))


# ── the ingest contract (what the TEC-573 uploader relies on) ─────────────────

def test_ingest_stores_and_roundtrips(client):
    record = make_record()
    resp = client.post("/api/v1/runs", json=record)
    assert resp.status_code == 201
    assert resp.json() == {"stored": record["run_id"]}
    # The full record comes back intact — the audit trail keeps everything.
    assert client.get(f"/api/v1/runs/{record['run_id']}").json() \
        == parse_run_record(record)


def test_duplicate_run_id_is_409(client):
    record = make_record()
    assert client.post("/api/v1/runs", json=record).status_code == 201
    assert client.post("/api/v1/runs", json=record).status_code == 409
    # Still exactly one copy stored.
    assert client.get("/api/v1/health").json()["runs"] == 1


def test_record_without_run_id_is_400(client):
    record = make_record()
    del record["run_id"]
    assert client.post("/api/v1/runs", json=record).status_code == 400


def test_non_object_body_is_4xx(client):
    assert client.post("/api/v1/runs", json=[1, 2, 3]).status_code == 422


def test_survives_restart(tmp_path):
    db = tmp_path / "runs.db"
    record = make_record()
    assert TestClient(create_app(db)).post("/api/v1/runs",
                                           json=record).status_code == 201
    # A fresh app instance on the same file = a service/instance restart.
    reborn = TestClient(create_app(db))
    assert reborn.get(f"/api/v1/runs/{record['run_id']}").status_code == 200
    assert reborn.get("/api/v1/health").json()["runs"] == 1


# ── the read side ─────────────────────────────────────────────────────────────

def _seed(client):
    records = [
        make_record(serial="SN-A", station_id="bench-1",
                    timestamp="2026-07-01T08:00:00+00:00"),
        make_record(serial="SN-B", station_id="bench-2", tool="rutm",
                    timestamp="2026-07-15T08:00:00+00:00"),
        make_record(serial="SN-C", station_id="bench-1", ok=False,
                    error="boom", timestamp="2026-07-20T08:00:00+00:00"),
    ]
    for record in records:
        assert client.post("/api/v1/runs", json=record).status_code == 201
    return records


def test_list_newest_first_with_filters(client):
    _seed(client)

    listed = client.get("/api/v1/runs").json()
    assert listed["count"] == 3
    assert [r["serial"] for r in listed["runs"]] == ["SN-C", "SN-B", "SN-A"]
    assert "record" not in listed["runs"][0]  # summaries, not full blobs

    by_station = client.get("/api/v1/runs?station_id=bench-1").json()
    assert {r["serial"] for r in by_station["runs"]} == {"SN-A", "SN-C"}

    errors = client.get("/api/v1/runs?status=error").json()
    assert [r["serial"] for r in errors["runs"]] == ["SN-C"]
    assert errors["runs"][0]["error"] == "boom"

    july_middle = client.get(
        "/api/v1/runs?since=2026-07-10&until=2026-07-16").json()
    assert [r["serial"] for r in july_middle["runs"]] == ["SN-B"]

    assert client.get("/api/v1/runs?limit=2").json()["count"] == 2
    assert client.get("/api/v1/runs?limit=5000").status_code == 422


def test_verified_comes_back_as_bool(client):
    record = make_record()
    record["verified"] = False
    record["verify_detail"] = "no answer at the new IP"
    client.post("/api/v1/runs", json=record)
    assert client.get("/api/v1/runs").json()["runs"][0]["verified"] is False


def test_unknown_run_is_404(client):
    assert client.get("/api/v1/runs/not-a-run").status_code == 404


def test_health(client):
    health = client.get("/api/v1/health").json()
    assert health["ok"] is True
    assert health["runs"] == 0
    assert health["db_bytes"] > 0
