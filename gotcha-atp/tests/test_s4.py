"""S4 helpers and rows against unit-shaped data (as read off gotcha-rev3-dev)."""
from pathlib import Path

from gotcha_atp import release
from gotcha_atp.access.creds import Credentials, Redactor
from gotcha_atp.access.session import Unit
from gotcha_atp.benchcentral import BenchCentral
from gotcha_atp.context import Context
from gotcha_atp.fleet import Fleet
from gotcha_atp.stages import s4

ROOT = Path(__file__).resolve().parents[1]
REL = release.load(ROOT / "release.yaml")
ECR = "781540302536.dkr.ecr.il-central-1.amazonaws.com"
GOTCHA = f"{ECR}/integrations/gotcha@sha256:2abaef0fac9765a93a31ca1a5583db73b6b0f4434a1437c9c35e50d7877aecfb"
AGENT = f"{ECR}/vendor/magos-agent@sha256:0ac979ef90f55b153fe4321a82d98e656ed67d57f9d83bdebf2283eb1c09e860"


def _part(role, kind, ip, serial="unknown", firmware="unknown", error=""):
    return {"role": role, "kind": kind, "ip": ip, "serial": serial, "model": "m" if not error else "unknown",
            "firmware": firmware, "mac": "unknown", "error": error, "extra": {}, "identified": not error}


INVENTORY = [
    _part("radar_0", "radar", "192.168.88.50", "14430001-140", "3.1.0"),
    _part("radar_1", "radar", "192.168.88.51", "14430001-121", "3.1.0"),
    _part("radar_2", "radar", "192.168.88.52", "14430001-157", "3.1.0"),
    _part("radar_3", "radar", "192.168.88.53", "14430001-120", "3.0.9"),
    _part("apu_60", "apu", "192.168.88.60", "GSAC598255", "3.1.2"),
    _part("apu_61", "apu", "192.168.88.61", "GSAC657362", "3.1.2"),
    _part("camera", "camera", "192.168.88.139", "CB6280103", "B1.2.01.01.15, 2026-05-14"),
    _part("speaker", "speaker", "192.168.88.70", error="no answer on its web interface"),
    _part("router", "router", "192.168.88.1", "6009545073", "RUTM_R_00.07.24.3"),
    _part("modem", "modem", "192.168.1.1", "6009000001", "OTD5_R_00.07.23.4"),
    _part("switch", "switch", "192.168.88.2", "6010620710", "TSW2_R_00.01.10"),
    _part("poe_switch", "poe_switch", "192.168.88.3", "506188060369", "1.305b251017"),
]


def _ctx(**facts):
    ctx = Context(unit=Unit("gotcha-x"), creds=Credentials(bench_central_url="http://bc", fleet_url="http://fleet",
                                                           fleet_token="t"),
                  redact=Redactor(), release=REL)
    ctx.facts["inventory"] = INVENTORY
    ctx.facts.update(facts)
    return ctx


def _checks(row):
    return {c["label"]: c["ok"] for c in row.detail["checks"]}


def test_version_helpers():
    assert s4.versions_match("RUTM_R_00.07.24.3", "00.07.24.3")
    assert not s4.versions_match("OTD5_R_00.07.23.4", "OTD5_R_00.07.24.3")
    assert s4.version_at_least("TSW2_R_00.01.10", "TSW2_R_00.01.07.1")
    assert not s4.version_at_least("TSW2_R_00.01.07", "TSW2_R_00.01.07.1")
    assert not s4.version_at_least("garbage", "TSW2_R_00.01.07.1")
    # every 1.305bNNNNNN shares the dotted part — the build date decides
    assert not s4.planet_at_least("1.305b251017", "1.305b260324")
    assert s4.planet_at_least("1.305b260324", "1.305b260324")
    assert s4.camera_legacy("1.000.General 00.0.T, build: 2025-04-09")
    assert not s4.camera_legacy("B1.0.22.29.16, 2026-09-03")
    assert str(s4.fw_build_date("B1.0.22.29.16, 2026-09-03")) == "2026-09-03"


def test_image_refs():
    assert s4.digest_of(GOTCHA).startswith("sha256:2abaef")
    assert s4.repo_path(GOTCHA) == "integrations/gotcha"
    assert s4.repo_path("vendor/magos-agent:3.25.6") == "vendor/magos-agent"
    hosting = [{"services": [{"image": "kela-integration://integrations/gotcha"},
                             {"image": "kela-integration://vendor/magos-agent:3.25.6"}]}]
    assert s4.manifest_image_tag(hosting, "vendor/magos-agent") == "3.25.6"
    assert s4.manifest_image_tag(hosting, "vendor/other") is None


def test_firmware_rows_judge_each_part():
    ctx = _ctx()
    r = s4._s45(ctx)
    assert r.state == "fail" and _checks(r)["identical"] is False and _checks(r)["radar_3 .53"] is False
    assert s4._s44(ctx).state == "pass"
    # by build date: the higher-numbered May build is older than the September floor
    cam = s4._s46(ctx)
    assert cam.state == "fail" and _checks(cam) == {"generation": True, "firmware": False}
    net = _checks(s4._s47(ctx))
    assert net == {"router .1": True, "modem 192.168.1.1": False, "switch .2": True, "poe_switch .3": False}
    # unreachable speaker: amber, not fail
    assert s4._s410(ctx).state == "amber"


def test_device_rows_without_discovery():
    ctx = _ctx()
    ctx.facts.pop("inventory")
    for fn in (s4._s44, s4._s45, s4._s46, s4._s47, s4._s49, s4._s410):
        assert fn(ctx).state == "amber"


def test_s42_staged_running_and_agent(monkeypatch):
    ctx = _ctx(_s4={"errors": {}, "node_controller": "0.3.56", "k3s": "v1.36.2+k3s1",
                    "images": {"integrations/gotcha": GOTCHA, "vendor/magos-agent": AGENT},
                    "manifest": [{"services": [{"image": "kela-integration://vendor/magos-agent:3.25.6"}]}],
                    "pods": [{"name": "int-gotcha-8e6d6910-86f9b97c5-8lk4c", "image_volumes": [],
                              "containers": [{"name": "svc-0", "image": GOTCHA, "image_id": GOTCHA},
                                             {"name": "svc-1", "image": AGENT, "image_id": AGENT}]}]})
    from gotcha_atp.access.exec import ExecResult
    label = '{"info": {"imageSpec": {"config": {"Labels": {"org.opencontainers.image.version": "3.25.6"}}}}}'
    monkeypatch.setattr(Context, "server", lambda self, script, timeout=30: ExecResult(0, label, ""))
    r = s4._s42(ctx)
    ch = _checks(r)
    # no Fleet node (S0.5) → the staged digests are recorded, not compared
    assert ch["integrations/gotcha"] is None and r.state == "amber"
    assert ch["svc-0 integrations/gotcha"] and ch["svc-1 vendor/magos-agent"]
    assert ch["magos-agent (manifest)"] and ch["magos-agent (running image)"]


def test_s43_against_pin_and_fleet(monkeypatch):
    ctx = _ctx(_s4={"errors": {}, "node_controller": "0.3.56", "k3s": "v1.36.2+k3s1"},
               fleet_node={"model": "NRU-230S_2026-01-30_1427", "role": "server",
                           "desiredSystemConfigurationVersion": 191, "siteId": "gotcha-x"})
    monkeypatch.setattr(Fleet, "system_configuration", lambda self, m, r, v: {
        "k3s": {"version": "v1.36.2+k3s1"}, "nodeController": {"version": "0.3.56"}})
    ch = _checks(s4._s43(ctx))
    assert ch["node-controller == Fleet desired"] and ch["k3s == Fleet desired"]
    assert ch["node-controller ≥ release.yaml floor"] and ch["k3s ≥ release.yaml floor"]
    ctx.facts["_s4"]["k3s"] = "v1.35.5+k3s1"
    ch = _checks(s4._s43(ctx))
    assert ch["k3s == Fleet desired"] is False and ch["k3s ≥ release.yaml floor"] is False
    ctx.facts.pop("fleet_node")
    assert _checks(s4._s43(ctx))["k3s == Fleet desired"] is None


def test_k3s_floor_counts_the_build():
    assert s4.k3s_key("v1.36.2+k3s2") > s4.k3s_key("v1.36.2+k3s1") > s4.k3s_key("v1.35.9+k3s3")


def test_s49_bench_records(monkeypatch):
    runs = {"14430001-140": [{"tool": "magos-radar", "timestamp": "2026-07-14T09:59:10", "verified": True}],
            "6009545073": [{"tool": "rutm", "timestamp": "2026-07-01T00:00:00", "verified": False, "status": "failed"}]}
    monkeypatch.setattr(BenchCentral, "runs_for_serial", lambda self, serial, kind="configure", limit=20: runs.get(serial, []))
    ch = _checks(s4._s49(_ctx()))
    assert ch["radar_0 .50"] is True
    assert ch["router .1"] is False          # last record not verified
    assert ch["apu_60 .60"] is False         # never benched
    assert ch["speaker .70"] is None         # no serial discovered


def test_s48_operator():
    assert s4._s48(_ctx(identity={"operator": None})).state == "amber"
    want = REL.pin("operator.setup_version")
    ctx = _ctx(identity={"operator": {"build_info": {"setup_version": want}}})
    assert s4._s48(ctx).state == "pass"


def test_s42_against_fleet_images_report(monkeypatch):
    from gotcha_atp.access.exec import ExecResult
    ctx = _ctx(_s4={"errors": {}, "node_controller": "0.3.56", "k3s": "v1.36.2+k3s1",
                    "images": {"integrations/gotcha": GOTCHA, "vendor/magos-agent": AGENT},
                    "manifest": [{"services": [{"image": "kela-integration://vendor/magos-agent:3.25.6"}]}],
                    "pods": []},
               fleet_node={"id": "n1", "siteId": "gotcha-x"})
    monkeypatch.setattr(Context, "server", lambda self, script, timeout=30: ExecResult(1, "", ""))
    monkeypatch.setattr(Fleet, "site_release", lambda self, site: {"desired": {"tag": "gotcha-rc-28.9"}})
    report = {"bundleTag": "gotcha-rc-28.9", "images": [
        {"role": "integration", "name": "integrations/gotcha", "image": GOTCHA},
        {"role": "integration", "name": "vendor/magos-agent", "image": f"{ECR}/vendor/magos-agent@sha256:other"}]}
    monkeypatch.setattr(Fleet, "node", lambda self, node_id: {"imagesReport": report})
    ch = _checks(s4._s42(ctx))
    assert ch["staged bundle"] is True and ch["integrations/gotcha"] is True
    assert ch["vendor/magos-agent"] is False          # published digest ≠ what the node reported
    report["bundleTag"] = "dev-some-branch"
    assert _checks(s4._s42(ctx))["staged bundle"] is False
