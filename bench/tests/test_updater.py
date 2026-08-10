"""Tests for the station updater (updater.py): safe bundle extraction, the
converge-to-pin flow's fail-open behavior, dev-checkout detection, and the
plumbing modes the launchers/installers call (--print-*, --seed-configs).
Runs with no network and no bench-central — everything is faked."""
import io
import json
import os
import urllib.error
import zipfile

import pytest

import updater


@pytest.fixture
def root(tmp_path, monkeypatch):
    """Point the updater at a scratch bench root."""
    monkeypatch.setattr(updater, "ROOT", tmp_path)
    monkeypatch.setattr(updater, "STATION_FILE", tmp_path / ".bench-station.json")
    monkeypatch.setattr(updater, "STAMP_FILE", tmp_path / ".bench-build.json")
    return tmp_path


def make_bundle(path, files, version="a" * 40):
    """A zip shaped like a bench-central bundle: relative paths, modes in
    external_attr, the .bench-build.json stamp included."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, (content, mode) in files.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            zf.writestr(info, content)
        zf.writestr(".bench-build.json",
                    json.dumps({"version": version, "built_at": "now"}))
    return path


# ── extraction ────────────────────────────────────────────────────────────────

def test_apply_bundle_writes_files_and_restores_modes(root, tmp_path):
    bundle = make_bundle(tmp_path / "b.zip", {
        "updater.py": ("print('v2')\n", 0o644),
        "start-bench.sh": ("#!/bin/bash\n", 0o755),
        "otd-config-ui/otd_app.py": ("print('otd')\n", 0o644),
    })
    changed = updater.apply_bundle(bundle)
    assert changed == 4  # 3 files + the stamp
    assert (root / "otd-config-ui" / "otd_app.py").read_text() == "print('otd')\n"
    assert os.stat(root / "start-bench.sh").st_mode & 0o111  # exec bit back
    assert updater.local_version() == "a" * 40


def test_apply_bundle_preserves_local_state(root, tmp_path):
    # Gitignored files (configs, station identity) are never IN a bundle —
    # extraction must leave whatever is on disk alone.
    config = root / "otd-config-ui" / "config" / "site.config.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"real": "secrets"}')
    (root / ".bench-station.json").write_text('{"station_id": "bench-1"}')
    updater.apply_bundle(make_bundle(tmp_path / "b.zip", {
        "otd-config-ui/otd_app.py": ("print('otd')\n", 0o644)}))
    assert config.read_text() == '{"real": "secrets"}'
    assert (root / ".bench-station.json").read_text() \
        == '{"station_id": "bench-1"}'


def test_apply_bundle_leaves_unchanged_files_alone(root, tmp_path):
    # Unchanged files are not rewritten (same inode) — only real changes
    # touch disk, so a launch-time update is a near-no-op when current.
    files = {"updater.py": ("same\n", 0o644)}
    updater.apply_bundle(make_bundle(tmp_path / "b1.zip", files))
    inode = os.stat(root / "updater.py").st_ino
    changed = updater.apply_bundle(make_bundle(tmp_path / "b2.zip", files))
    assert changed == 0
    assert os.stat(root / "updater.py").st_ino == inode


def test_extract_member_rejects_escaping_paths(root, tmp_path):
    bundle = tmp_path / "evil.zip"
    with zipfile.ZipFile(bundle, "w") as zf:
        zf.writestr("../evil.txt", "boom")
    with zipfile.ZipFile(bundle) as zf:
        with pytest.raises(ValueError):
            updater._extract_member(zf, zf.infolist()[0])
    assert not (root.parent / "evil.txt").exists()


# ── the converge flow (all fail-open) ─────────────────────────────────────────

def test_update_downloads_and_applies_when_stamp_differs(root, tmp_path,
                                                         monkeypatch):
    sha = "b" * 40
    buf = io.BytesIO()
    make_bundle(buf, {"updater.py": ("print('v2')\n", 0o644)}, version=sha)

    monkeypatch.setattr(updater, "_api",
                        lambda base, path, **kw: {"version": sha})

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    requested = []

    def fake_urlopen(url, timeout=None):
        requested.append(url)
        return FakeResponse(buf.getvalue())

    monkeypatch.setattr(updater.urllib.request, "urlopen", fake_urlopen)
    updater.update("http://central:8100")
    assert requested == [f"http://central:8100/api/v1/fleet/bundle/{sha}.zip"]
    assert (root / "updater.py").read_text() == "print('v2')\n"
    assert updater.local_version() == sha
    # No leftover temp downloads.
    assert not list(root.glob("*.bundle.zip"))


def test_update_noop_when_already_on_the_pin(root, monkeypatch):
    sha = "c" * 40
    (root / ".bench-build.json").write_text(json.dumps({"version": sha}))
    monkeypatch.setattr(updater, "_api",
                        lambda base, path, **kw: {"version": sha})
    monkeypatch.setattr(updater.urllib.request, "urlopen",
                        lambda *a, **kw: pytest.fail("must not download"))
    updater.update("http://central:8100")


def test_update_fail_open_when_central_unreachable(root, monkeypatch):
    def down(base, path, **kw):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(updater, "_api", down)
    updater.update("http://central:8100")  # must not raise


def test_update_respects_bench_no_pull(root, monkeypatch):
    monkeypatch.setenv("BENCH_NO_PULL", "1")
    monkeypatch.setattr(updater, "_api",
                        lambda *a, **kw: pytest.fail("must not touch central"))
    updater.update("http://central:8100")


def test_checkin_is_best_effort(root, monkeypatch):
    def down(base, path, **kw):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(updater, "_api", down)
    updater.checkin("http://central:8100", "bench-1")  # must not raise


def test_checkin_reports_the_stamped_version(root, monkeypatch):
    (root / ".bench-build.json").write_text(json.dumps({"version": "d" * 40}))
    sent = {}

    def capture(base, path, payload=None, **kw):
        sent.update(payload)
        return {"ok": True}
    monkeypatch.setattr(updater, "_api", capture)
    updater.checkin("http://central:8100", "bench-1")
    assert sent["station_id"] == "bench-1"
    assert sent["version"] == "d" * 40


# ── identity + plumbing ───────────────────────────────────────────────────────

def test_dev_checkout_detected_here_or_one_level_up(root):
    assert not updater.is_dev_checkout()
    (root / ".git").mkdir()
    assert updater.is_dev_checkout()


def test_short_version_prefers_the_stamp(root):
    (root / ".bench-build.json").write_text(
        json.dumps({"version": "e" * 40}))
    assert updater.short_version() == "e" * 12


def test_short_version_unknown_without_stamp_or_git(root):
    assert updater.short_version() == "unknown"


def test_station_config_tolerates_a_utf8_bom(root):
    # Windows PowerShell 5.1's `Set-Content -Encoding UTF8` (the original
    # setup-station.ps1) prepends a BOM; the station file must still parse,
    # or the station silently loses its central URL and never self-updates.
    (root / ".bench-station.json").write_bytes(
        b'\xef\xbb\xbf{"station_id": "bench-1", '
        b'"central_url": "http://central:8100"}')
    cfg = updater.station_config()
    assert cfg["station_id"] == "bench-1"
    assert cfg["central_url"] == "http://central:8100"


def test_station_config_env_overrides_the_file(root, monkeypatch):
    (root / ".bench-station.json").write_text(json.dumps(
        {"station_id": "bench-1", "central_url": "http://file:8100"}))
    assert updater.station_config()["central_url"] == "http://file:8100"
    monkeypatch.setenv("BENCH_CENTRAL_URL", "http://env:8100")
    monkeypatch.setenv("BENCH_STATION_ID", "bench-9")
    cfg = updater.station_config()
    assert cfg == {"station_id": "bench-9", "central_url": "http://env:8100"}


def test_seed_configs_copies_missing_templates_only(root, capsys):
    config = root / "otd-config-ui" / "config"
    config.mkdir(parents=True)
    (config / "site.config.example.json").write_text('{"x": "xxxxx"}')
    have = root / "rutm-config-ui" / "config"
    have.mkdir(parents=True)
    (have / "rutm.config.example.json").write_text('{"t": "template"}')
    (have / "rutm.config.json").write_text('{"t": "real"}')
    # Templates inside local-state trees must not be seeded.
    venv = root / ".venv" / "config"
    venv.mkdir(parents=True)
    (venv / "pkg.config.example.json").write_text("{}")

    updater.seed_configs()
    assert (config / "site.config.json").read_text() == '{"x": "xxxxx"}'
    assert (have / "rutm.config.json").read_text() == '{"t": "real"}'
    assert not (venv / "pkg.config.json").exists()
