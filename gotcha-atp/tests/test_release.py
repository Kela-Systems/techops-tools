from pathlib import Path

import pytest
import yaml

from gotcha_atp import release

ROOT = Path(__file__).resolve().parents[1]


def test_checked_in_release_loads():
    r = release.load(ROOT / "release.yaml")
    assert r.name == "2026.10-a"
    assert r.plan.radars["radar_2"] == "192.168.88.52"
    assert r.plan.apu_for("radar_3") == "192.168.88.61"
    assert r.pin("magos.apu_firmware") == "3.1.2"
    assert len(r.sha256) == 64


def test_agreed_pins():
    r = release.load(ROOT / "release.yaml")
    assert r.pin("server.l4t") == "R36.4.3"
    assert r.pin("speaker.outvolume_min") == "1"            # not muted
    assert r.get("hub.integrations") is None                 # digests follow the release (S4.2 vs Fleet)
    assert not release.pinned("TODO sha256") and not release.pinned("")


def test_addresses_cover_the_whole_plan():
    r = release.load(ROOT / "release.yaml")
    ips = set(r.plan.addresses())
    assert {"192.168.88.1", "192.168.88.2", "192.168.88.3", r.plan.camera, "192.168.88.50",
            "192.168.88.53", "192.168.88.60", "192.168.88.61", "192.168.88.70"} <= ips
    assert r.plan.server not in ips


@pytest.mark.parametrize("value,expected", [
    ("", False), (None, False), ("TODO", False), ("todo sha256", False), ("3.1.2", True), (0, True)])
def test_pinned(value, expected):
    assert release.pinned(value) is expected


def _write(tmp_path, mutate):
    data = yaml.safe_load((ROOT / "release.yaml").read_text())
    mutate(data)
    p = tmp_path / "release.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def test_wrong_schema_is_rejected(tmp_path):
    p = _write(tmp_path, lambda d: d.update(schema="gotcha-release/0"))
    with pytest.raises(release.ReleaseError, match="schema"):
        release.load(p)


def test_missing_key_is_named(tmp_path):
    p = _write(tmp_path, lambda d: d["server"].pop("k3s_floor"))
    with pytest.raises(release.ReleaseError, match="server.k3s_floor"):
        release.load(p)


def test_radar_must_be_assigned_once(tmp_path):
    p = _write(tmp_path, lambda d: d["plan"]["apus"].update({"192.168.88.61": ["radar_2"]}))
    with pytest.raises(release.ReleaseError, match="exactly one APU"):
        release.load(p)


def test_bad_ip(tmp_path):
    p = _write(tmp_path, lambda d: d["plan"].update(camera="192.168.88.300"))
    with pytest.raises(release.ReleaseError, match="plan.camera"):
        release.load(p)
