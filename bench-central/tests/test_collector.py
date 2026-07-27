"""Tests for the central collector (collector.py, TEC-574): the ingest
contract the station uploader relies on (201/409/400), durable storage
across restarts, and the read endpoints the dashboard (TEC-575) and
engineers browse with."""
import json
import sqlite3

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
                    device={"hostname": "otd-haifa", "site_name": "haifa"},
                    timestamp="2026-07-01T08:00:00+00:00"),
        make_record(serial="SN-B", station_id="bench-2", tool="rutm",
                    device={"hostname": "rut-golan", "site_name": "golan"},
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


def test_site_and_hostname_are_queryable(client):
    _seed(client)
    summary = client.get("/api/v1/runs?serial=SN-A").json()["runs"][0]
    assert summary["site"] == "haifa"
    assert summary["hostname"] == "otd-haifa"
    by_site = client.get("/api/v1/runs?site=golan").json()
    assert [r["serial"] for r in by_site["runs"]] == ["SN-B"]


def test_q_substring_search(client):
    _seed(client)
    # Matches across serial, hostname, site, operator — case per SQLite LIKE.
    assert [r["serial"] for r in
            client.get("/api/v1/runs?q=golan").json()["runs"]] == ["SN-B"]
    assert [r["serial"] for r in
            client.get("/api/v1/runs?q=SN-").json()["runs"]] \
        == ["SN-C", "SN-B", "SN-A"]
    # LIKE wildcards in the needle are literals, not wildcards.
    assert client.get("/api/v1/runs?q=%25").json()["total"] == 0


def test_offset_pagination(client):
    _seed(client)
    page = client.get("/api/v1/runs?limit=2").json()
    assert page["total"] == 3 and page["count"] == 2
    rest = client.get("/api/v1/runs?limit=2&offset=2").json()
    assert rest["total"] == 3 and [r["serial"] for r in rest["runs"]] == ["SN-A"]


def test_filters_endpoint(client):
    _seed(client)
    filters = client.get("/api/v1/filters").json()
    assert filters["stations"] == ["bench-1", "bench-2"]
    assert filters["tools"] == ["otd", "rutm"]
    assert filters["sites"] == ["golan", "haifa"]
    assert filters["operators"] == ["Dana K"]


def test_dashboard_is_served_at_root(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Bench Central" in resp.text


def test_migration_backfills_site_and_hostname(tmp_path):
    # A database created by the pre-dashboard collector (TEC-574): no
    # site/hostname columns; those values live only inside the record JSON.
    db = tmp_path / "runs.db"
    record = make_record(device={"hostname": "otd-old", "site_name": "eilat"})
    row_json = json.dumps(parse_run_record(record))
    with sqlite3.connect(db) as conn:
        conn.execute("""CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, received_at TEXT NOT NULL, timestamp TEXT,
            tool TEXT, status TEXT, serial TEXT, mac TEXT, model TEXT,
            firmware TEXT, duration_s INTEGER, verified INTEGER, error TEXT,
            operator TEXT, station_id TEXT, bench_version TEXT,
            record TEXT NOT NULL)""")
        conn.execute(
            "INSERT INTO runs (run_id, received_at, timestamp, tool, status, "
            "serial, record) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (record["run_id"], "2026-07-27T00:00:00+00:00",
             record["timestamp"], "otd", "ok", record["serial"], row_json))

    client = TestClient(create_app(db))  # init_db migrates on startup
    summary = client.get("/api/v1/runs").json()["runs"][0]
    assert summary["site"] == "eilat"
    assert summary["hostname"] == "otd-old"
    assert [r["serial"] for r in
            client.get("/api/v1/runs?site=eilat").json()["runs"]] == ["SN-1"]


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
    # The collector's own repo revision — comparable with bench_version.
    assert isinstance(health["version"], str) and health["version"]
