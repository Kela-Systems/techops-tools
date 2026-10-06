"""S6 parsers and the self-status rule, against output shapes read off gotcha-rev3-dev."""
from pathlib import Path

from gotcha_atp import release
from gotcha_atp.stages import s6

ROOT = Path(__file__).resolve().parents[1]
REL = release.load(ROOT / "release.yaml")

METRICS = """# HELP kela_pose_readings_total x
kela_pose_readings_total{entity_id="",outcome="accepted",sensor_id="main"} 1.0
kela_pose_readings_total{entity_id="61c9",outcome="accepted",sensor_id="main"} 3.41058e+06
kela_pose_readings_total{entity_id="61c9",outcome="stalled",sensor_id="main"} 37.0
kela_magos_rf_to_hub_publish_seconds_bucket{radar_uid="d-radar-1",le="0.5"} 90
kela_magos_rf_to_hub_publish_seconds_bucket{radar_uid="d-radar-1",le="1.0"} 99
kela_magos_rf_to_hub_publish_seconds_bucket{radar_uid="d-radar-1",le="+Inf"} 100
"""


def test_prom_and_histogram():
    acc = [v for labels, v in s6.prom_samples(METRICS, "kela_pose_readings_total")
           if labels["outcome"] == "accepted" and labels["entity_id"]]
    assert acc == [3.41058e+06]
    assert s6.histogram_p95(METRICS, "kela_magos_rf_to_hub_publish_seconds", "radar_uid") == {"d-radar-1": (100, 1.0)}
    assert s6.histogram_p95("", "kela_magos_rf_to_hub_publish_seconds", "radar_uid") == {}


def test_mtx_paths_and_ffprobe():
    data = {"items": [
        {"name": "hub/asset/A/sensor/main/stream/1", "ready": True, "tracks": ["H264"], "bytesReceived": 100,
         "tracks2": [{"codec": "H264", "codecProps": {"width": 1920, "height": 1080}}],
         "metadata": {"assetId": "A", "sensorId": "main", "type": "Native"}},
        {"name": "hub/asset/B/sensor/main/stream/9", "ready": True, "tracks": ["H264"],
         "metadata": {"assetId": "B", "sensorId": "main", "type": "Native"}},
    ]}
    p = s6.mtx_paths(data, "A")
    assert list(p) == ["hub/asset/A/sensor/main/stream/1"]
    assert p["hub/asset/A/sensor/main/stream/1"] == {"sensor": "main", "ready": True, "codecs": ["H264"],
                                                     "width": 1920, "height": 1080, "bytes": 100}
    assert s6.ffprobe_frames('{"streams":[{"width":1280,"height":1024,"nb_read_frames":"123"}]}') == (123, "1280x1024")
    assert s6.ffprobe_frames("not json") == (0, None)


def test_self_status_rule():
    st = {"alerts": [], "ethernetSpeed": 1000, "timeSyncOk": True, "timeSyncDetail": "synced",
          "timeSyncTarget": "<192.168.88.10>", "timeSyncOffset": -4e-05,
          "applClientList": [{"ip": "192.168.88.60"}], "perfTemperature": [41, 43],
          "netInterfaces": {"port1": {"deviceState": "CONNECTED", "ip4Address": "192.168.88.50"}}}
    rows = s6._self_status(st, "192.168.88.50", REL.thresholds, REL.plan.server, "192.168.88.60")
    assert all(ok for _, ok, _ in rows), rows
    bad = dict(st, ethernetSpeed=100, timeSyncTarget="<pool.ntp.org>", perfTemperature=[80],
               applClientList=[{"ip": "192.168.88.61"}])
    failed = [label for label, ok, _ in s6._self_status(bad, "192.168.88.50", REL.thresholds, REL.plan.server,
                                                         "192.168.88.60") if not ok]
    assert failed == ["ethernet", "time", "client", "temperature"]


def test_speaker_volume_shapes():
    assert s6._volume({"outvolume": "60"}) == 60
    assert s6._volume({"audio": {"outvolume": 45}}) == 45
    assert s6._volume({}) is None
