import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gotcha_atp import stages
from gotcha_atp.benchcentral import BenchCentral
from gotcha_atp.ui.app import create_app


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("GOTCHA_ATP_CONFIG", str(tmp_path / "missing.toml"))
    from gotcha_atp.access import creds
    monkeypatch.setattr(creds, "CONFIG_PATH", tmp_path / "missing.toml")
    from gotcha_atp.access import tailnet
    monkeypatch.setattr(tailnet, "status", lambda: {"BackendState": "Running", "Self": {"Online": True},
                                                     "Peer": {"x": {"DNSName": "kela-gotcha-01-operator.t.ts.net.",
                                                                    "Online": True, "TailscaleIPs": ["100.1.1.1"]},
                                                              "y": {"DNSName": "kela-fob-04-operator.t.ts.net.",
                                                                    "Online": True, "TailscaleIPs": ["100.1.1.2"]}}})
    with TestClient(create_app()) as c:
        yield c


def test_page_and_home(client):
    assert "Gotcha ATP" in client.get("/").text
    for asset in ("app.js", "style.css"):
        assert client.get(f"/static/{asset}").status_code == 200
    h = client.get("/api/home").json()
    assert h["release"]["ok"] is True and h["release"]["name"] == "2026.10-a"
    assert [u["site"] for u in h["units"]] == ["kela-gotcha-01"]   # non-gotcha units filtered out
    assert h["creds"]["ssh_password_set"] is False
    assert h["running"] is False


def test_run_requires_a_connection(client):
    r = client.post("/api/run", json={})
    assert r.status_code == 400 and "connect" in r.json()["detail"]
    assert client.post("/api/answer", json={"id": "S10.1", "answer": "yes"}).status_code == 400
    assert client.get("/api/report/pdf").status_code == 404


def test_catalogue_ids_are_unique_and_cover_the_canvas():
    ids = [s.id for _, s in stages.all_specs()]
    assert len(ids) == len(set(ids))
    for must in ("S0.6", "S1.4", "S2.8", "S3.5", "S4.11", "S5.3b", "S5.10", "S6.3d", "S6.13",
                 "S7.5", "S8.4", "S9.4", "S10.4"):
        assert must in ids
    manual = [s.id for _, s in stages.all_specs() if s.cls == "manual"]
    assert manual == ["S7.5", "S9.0", "S10.1", "S10.2", "S10.3", "S10.4"]


def _record(run_id="r1"):
    return {"schema": "bench-run-record/1", "run_id": run_id, "timestamp": "2026-10-05T07:00:00+00:00"}


def test_upload_queues_when_unreachable_and_drains(tmp_path, monkeypatch):
    bc = BenchCentral("http://central.invalid:8100", outbox=tmp_path / "outbox")

    def down(*a, **k):
        raise httpx.ConnectError("no route")
    monkeypatch.setattr(httpx, "post", down)
    res = bc.post_run(_record())
    assert res["status"] == "queued" and bc.pending() == 1

    calls = []

    def up(url, json=None, timeout=None):
        calls.append(json["run_id"])
        return httpx.Response(409 if len(calls) == 1 else 201)
    monkeypatch.setattr(httpx, "post", up)
    bc.post_run(_record("r2"))
    assert bc.pending() == 1                       # r2 went straight up (409 = already stored)
    assert [r["status"] for r in bc.drain()] == ["uploaded"]
    assert bc.pending() == 0


def test_rejected_record_is_set_aside(tmp_path, monkeypatch):
    bc = BenchCentral("http://central.invalid:8100", outbox=tmp_path / "outbox")
    bc._queue(_record())
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(400, text="no run_id"))
    assert bc.drain()[0]["status"] == "rejected"
    assert bc.pending() == 0 and list((tmp_path / "outbox").glob("*.bad"))
    assert json.loads(next((tmp_path / "outbox").glob("*.bad")).read_text())["run_id"] == "r1"
