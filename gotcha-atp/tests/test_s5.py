"""S5 helpers and rows against hub-shaped data (as read off gotcha-rev3-dev)."""
import math
from pathlib import Path

from gotcha_atp import release
from gotcha_atp.access.creds import Credentials, Redactor
from gotcha_atp.access.session import Unit
from gotcha_atp.context import Context
from gotcha_atp.stages import s5

ROOT = Path(__file__).resolve().parents[1]
REL = release.load(ROOT / "release.yaml")


def _radar(i, apu, pan):
    return {"instanceId": f"radar_{i}", "IpAddress": apu, "pan": pan, "tilt": 30.0, "model": "AR-300-F"}


DEVICE = {
    "id": "28ce5a21-0000", "name": "Gotcha", "integration_id": "8e6d6910-0000",
    "setup_info": {
        "latitude": 32.4665, "longitude": 35.0012, "altitude": 75.26, "heading": -82.0,
        "radars": [_radar(0, "192.168.88.60", 45.0), _radar(1, "192.168.88.60", 315.0),
                   _radar(2, "192.168.88.61", 225.0), _radar(3, "192.168.88.61", 135.0)],
        "radar_credentials": {"userName": "admin", "password": "s3cret"},
        "camera": {"host": REL.plan.camera, "password": "c4m"},
        "speaker": {"host": "192.168.88.70", "password": "sp"},
    },
}
INTEGRATION = {"id": "8e6d6910-0000", "manifest": "Gotcha", "version": "0.0.0",
               "config": {"position_uncertainty_up_m": 10.0},
               "schema": {"properties": {"mass_processing_enabled": {"default": False},
                                         "apu_sweep_recovery_enabled": {"default": False},
                                         "stream_apu_raw_detections": {"default": False},
                                         "apu_no_sweep_timeout_minutes": {"default": 2}}}}


def _ctx(device=DEVICE, apu=None):
    ctx = Context(unit=Unit("gotcha-x"), creds=Credentials(), redact=Redactor(), release=REL)
    ctx.facts["_s5"] = {"error": "", "integrations": [INTEGRATION], "gotcha": [INTEGRATION],
                        "devices": [device] if device else []}
    ctx.facts["_s5_apu"] = apu if apu is not None else {
        "192.168.88.60": {"radars": [{"radar_id": "radar_0", "remote_base_url": "http://192.168.88.50"},
                                     {"radar_id": "radar_1", "remote_base_url": "http://192.168.88.51/"}]},
        "192.168.88.61": {"radars": [{"radar_id": "radar_2", "remote_base_url": "http://192.168.88.52"},
                                     {"radar_id": "radar_3", "remote_base_url": "http://192.168.88.53",
                                      "range_gates": [1, 2]}]},
    }
    return ctx


def test_pan_rule():
    quad, cardinal = s5.pan_quadrants({"a": 45, "b": -45, "c": -135, "d": 135})
    assert sorted(quad.values()) == [0, 1, 2, 3] and not cardinal
    quad, cardinal = s5.pan_quadrants({"a": 90, "b": 10, "c": 20, "d": 200})
    assert cardinal == ["a"] and len(set(quad.values())) < 4
    assert s5.angle_diff(315, -45) == 0 and s5.angle_diff(10, 350) == 20


def test_quat_ypr():
    half = math.radians(45) / 2
    yaw, pitch, roll = s5.quat_ypr({"z": math.sin(half), "w": math.cos(half)})
    assert round(yaw, 6) == 45 and abs(pitch) < 1e-9 and abs(roll) < 1e-9


def test_effective_config():
    assert s5.effective_config({"a": 1}, {"properties": {"a": {"default": 2}}}, "a") == (1, "set")
    assert s5.effective_config({}, {"properties": {"a": {"default": 2}}}, "a") == (2, "default")
    assert s5.effective_config({}, {}, "a") == (None, "unset")


def test_rows_on_a_healthy_device():
    ctx = _ctx()
    for fn in (s5._s51, s5._s52, s5._s53, s5._s54, s5._s55, s5._s56, s5._s57, s5._s59):
        r = fn(ctx)
        assert r.state == "pass", (r.id, r.actual)
    r = s5._s53b(ctx)
    assert r.state == "fail" and "radar_3 range gates=[1, 2]" in r.actual
    assert s5._s510(ctx).state == "amber"                       # calibration_revision 0


def test_wrong_plan_and_apu_login():
    dev = {**DEVICE, "setup_info": {**DEVICE["setup_info"],
                                    "radars": [_radar(0, "192.168.88.61", 45.0), _radar(1, "192.168.88.60", 90.0),
                                               _radar(2, "192.168.88.61", 225.0), _radar(3, "192.168.88.61", 135.0)]}}
    ctx = _ctx(dev, apu={"192.168.88.60": "192.168.88.60 login refused (HTTP 401) — set the APU login",
                         "192.168.88.61": {"radars": [{"radar_id": "radar_2", "remote_base_url": "http://192.168.88.99"}]}})
    assert s5._s52(ctx).state == "fail"
    assert s5._s55(ctx).state == "fail"
    r = s5._s53(ctx)
    bad = {c["label"]: c["ok"] for c in r.detail["checks"]}
    assert r.state == "fail" and bad["APU .60"] is None and bad["radar_2 on .61"] is False and bad["radar_3 on .61"] is False


def test_no_device():
    ctx = _ctx(device=None)
    assert s5._s51(ctx).state == "fail"
    assert s5._s52(ctx).state == "amber"


def test_calibrated_device():
    half = math.radians(45) / 2
    q = {"z": math.sin(half), "w": math.cos(half)}
    dev = {**DEVICE, "calibration_revision": "3", "applied_calibration_revision": "3",
           "sensor_to_asset_calibrations": {f"radar-{i}": {"rotation_sensor_to_asset": q,
                                                           "source": "CALIBRATION_SOURCE_SOLVED"} for i in range(1, 5)}}
    r = s5._s510(_ctx(dev))
    assert r.state == "pass", r.actual
    assert "yaw 45.0°" in r.actual
