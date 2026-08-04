"""Tests for the fleet half of bench-central (fleet.py): pinning a release,
serving code bundles built with `git archive`, station check-ins, and the
installer endpoints. The station half (bench/scripts/updater.py) has its own
suite under bench/tests/."""
import io
import json
import subprocess
import zipfile

import pytest
from fastapi.testclient import TestClient

from collector import create_app


def git(repo, *args):
    out = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com",
         *args], cwd=str(repo), capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A minimal repo shaped like techops-tools: a bench/ subtree with a
    couple of files, one of them executable, plus the installers."""
    repo = tmp_path / "repo"
    bench = repo / "bench"
    (bench / "otd-config-ui").mkdir(parents=True)
    (bench / "scripts").mkdir()
    (bench / "scripts" / "updater.py").write_text("print('updater')\n")
    (bench / "otd-config-ui" / "otd_app.py").write_text("print('otd')\n")
    (bench / "start-bench.sh").write_text("#!/usr/bin/env bash\necho hi\n")
    (bench / "start-bench.sh").chmod(0o755)
    (bench / "scripts" / "setup-station.sh").write_text("echo installer v1\n")
    (bench / "scripts" / "setup-station.ps1").write_text("Write-Host 'installer v1'\n")
    git(repo, "init", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "first")
    return repo


@pytest.fixture
def client(tmp_path, repo):
    return TestClient(create_app(tmp_path / "runs.db", repo_root=repo))


def sha_of(repo, ref="main"):
    return git(repo, "rev-parse", ref)


# ── pinning ───────────────────────────────────────────────────────────────────

def test_nothing_pinned_initially(client):
    assert client.get("/api/v1/fleet/desired").json() \
        == {"version": None, "ref": None, "pinned_at": None}


def test_pin_resolves_ref_to_sha(client, repo):
    resp = client.post("/api/v1/fleet/desired", json={"ref": "main"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == sha_of(repo)
    assert body["ref"] == "main"
    assert body["pinned_at"]
    # GET agrees, and it survives a restart (state is in the database).
    assert client.get("/api/v1/fleet/desired").json() == body


def test_pin_unknown_ref_is_400(client):
    resp = client.post("/api/v1/fleet/desired", json={"ref": "no-such-branch"})
    assert resp.status_code == 400
    assert "unknown ref" in resp.json()["detail"]
    # And nothing got pinned.
    assert client.get("/api/v1/fleet/desired").json()["version"] is None


def test_pin_without_ref_is_400(client):
    assert client.post("/api/v1/fleet/desired", json={}).status_code == 400


def test_repin_moves_the_pin_and_rollback_works(client, repo):
    first = sha_of(repo)
    (repo / "bench" / "scripts" / "updater.py").write_text("print('updater v2')\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "second")
    second = sha_of(repo)

    client.post("/api/v1/fleet/desired", json={"ref": "main"})
    assert client.get("/api/v1/fleet/desired").json()["version"] == second
    # Rollback = pin the previous sha explicitly.
    assert client.post("/api/v1/fleet/desired",
                       json={"ref": first}).status_code == 200
    assert client.get("/api/v1/fleet/desired").json()["version"] == first


# ── bundles ───────────────────────────────────────────────────────────────────

def test_bundle_contains_bench_subtree_and_stamp(client, repo):
    sha = client.post("/api/v1/fleet/desired",
                      json={"ref": "main"}).json()["version"]
    resp = client.get(f"/api/v1/fleet/bundle/{sha}.zip")
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = set(zf.namelist())
        # Paths are relative to bench/ — the zip extracts straight over the
        # station's bench folder.
        assert "scripts/updater.py" in names
        assert "otd-config-ui/otd_app.py" in names
        assert not any(n.startswith("bench/") for n in names)
        stamp = json.loads(zf.read(".bench-build.json"))
        assert stamp["version"] == sha
        assert stamp["built_at"]
        # git archive keeps the executable bit; the updater restores it.
        info = zf.getinfo("start-bench.sh")
        assert (info.external_attr >> 16) & 0o111


def test_bundle_rebuilds_after_cache_clear(tmp_path, repo):
    db = tmp_path / "runs.db"
    client = TestClient(create_app(db, repo_root=repo))
    sha = client.post("/api/v1/fleet/desired",
                      json={"ref": "main"}).json()["version"]
    cached = db.parent / "bundles" / f"{sha}.zip"
    assert cached.exists()
    cached.unlink()
    assert client.get(f"/api/v1/fleet/bundle/{sha}.zip").status_code == 200
    assert cached.exists()


def test_bundle_name_must_be_a_full_sha(client):
    assert client.get("/api/v1/fleet/bundle/main.zip").status_code == 400
    assert client.get("/api/v1/fleet/bundle/deadbeef.zip").status_code == 400


def test_bundle_for_nonexistent_sha_is_404(client):
    assert client.get(f"/api/v1/fleet/bundle/{'0' * 40}.zip").status_code == 404


# ── check-ins + fleet listing ─────────────────────────────────────────────────

def test_checkin_upserts_and_returns_desired(client, repo):
    pinned = client.post("/api/v1/fleet/desired",
                         json={"ref": "main"}).json()["version"]
    resp = client.post("/api/v1/fleet/checkin", json={
        "station_id": "bench-1", "hostname": "BENCH-PC-1",
        "platform": "Windows", "version": "aaaa"})
    assert resp.status_code == 200
    # The response carries the pin, so a station learns the desired version
    # from its own check-in.
    assert resp.json() == {"ok": True, "desired": pinned}

    first = client.get("/api/v1/fleet/stations").json()["stations"][0]
    client.post("/api/v1/fleet/checkin", json={
        "station_id": "bench-1", "hostname": "BENCH-PC-1",
        "platform": "Windows", "version": pinned})
    listing = client.get("/api/v1/fleet/stations").json()
    assert len(listing["stations"]) == 1  # upsert, not a second row
    again = listing["stations"][0]
    assert again["version"] == pinned
    assert again["first_seen"] == first["first_seen"]
    assert again["last_seen"] >= first["last_seen"]
    assert listing["desired"]["version"] == pinned


def test_checkin_without_station_id_is_400(client):
    assert client.post("/api/v1/fleet/checkin",
                       json={"hostname": "x"}).status_code == 400


def test_stations_listing_is_sorted(client):
    for sid in ("bench-2", "bench-1"):
        client.post("/api/v1/fleet/checkin", json={"station_id": sid})
    listing = client.get("/api/v1/fleet/stations").json()
    assert [s["station_id"] for s in listing["stations"]] \
        == ["bench-1", "bench-2"]
    assert listing["desired"]["version"] is None


def test_fleet_state_survives_restart(tmp_path, repo):
    db = tmp_path / "runs.db"
    client = TestClient(create_app(db, repo_root=repo))
    sha = client.post("/api/v1/fleet/desired",
                      json={"ref": "main"}).json()["version"]
    client.post("/api/v1/fleet/checkin", json={"station_id": "bench-1"})

    reborn = TestClient(create_app(db, repo_root=repo))
    assert reborn.get("/api/v1/fleet/desired").json()["version"] == sha
    assert len(reborn.get("/api/v1/fleet/stations").json()["stations"]) == 1


# ── installers ────────────────────────────────────────────────────────────────

def test_installers_served_from_the_pinned_sha(client, repo):
    client.post("/api/v1/fleet/desired", json={"ref": "main"})
    # Change the working tree AFTER pinning — the served installer must still
    # be the pinned one, so installer and installed code always match.
    (repo / "bench" / "scripts" / "setup-station.sh").write_text("echo installer v2\n")
    assert client.get("/setup.sh").text == "echo installer v1\n"
    assert client.get("/setup.ps1").text == "Write-Host 'installer v1'\n"


def test_installers_fall_back_to_working_tree_before_first_pin(client):
    # Bootstrap of the very first station: nothing pinned yet.
    assert "installer v1" in client.get("/setup.sh").text
