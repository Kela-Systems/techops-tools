"""S3 parsers and rows against kubectl-shaped fixtures."""
import json
from pathlib import Path

from gotcha_atp import release
from gotcha_atp.access.creds import Credentials, Redactor
from gotcha_atp.access.exec import ExecResult
from gotcha_atp.access.session import Unit
from gotcha_atp.context import Context
from gotcha_atp.stages import s3

ROOT = Path(__file__).resolve().parents[1]
NOW = 1759672800.0            # 2025-10-05T14:00:00Z
LONG_AGO = "2025-10-04T15:00:00Z"


def _pod(ns, name, phase="Running", ready=True, restarts=0, finished=None, waiting=None, owner=True,
         started=LONG_AGO):
    cs = {"name": name.split("-")[0], "ready": ready, "restartCount": restarts,
          "state": {"waiting": {"reason": waiting}} if waiting else {"running": {"startedAt": started}},
          "lastState": {"terminated": {"finishedAt": finished, "reason": "Error"}} if finished else {}}
    meta = {"namespace": ns, "name": name}
    if owner:
        meta["ownerReferences"] = [{"kind": "ReplicaSet"}]
    return {"metadata": meta, "status": {"phase": phase, "containerStatuses": [cs], "podIP": "10.42.0.82"}}


PODS = {"items": [
    _pod("kela", "hub-server-1"),
    _pod("kela", "dex-1", restarts=27, finished="2025-09-28T07:44:00Z"),          # old restart: recorded
    _pod("kela", "qgis-server-1", restarts=26, finished="2025-09-28T07:44:30Z"),  # same moment
    _pod("kela", "video-1", restarts=3, finished="2025-10-05T13:50:00Z",
         started="2025-10-05T13:50:05Z"),                                       # up 10 min: not stable
    _pod("kela", "map-server-1", ready=False),
    _pod("kela", "fusion-1", waiting="CrashLoopBackOff", ready=False),
    _pod("kela", "seed-1", phase="Succeeded"),
    _pod("kela", "radar-bench", owner=False),
    _pod("kela", "int-gotcha-8e6d6910-abc"),
]}
STABLE_PODS = {"items": [p for p in PODS["items"]
                         if p["metadata"]["name"] in ("hub-server-1", "dex-1", "qgis-server-1")]}


def _w(kind, ns, name, ready=1, want=1, images=None, **status):
    st = {"readyReplicas": ready} if kind in ("Deployment", "StatefulSet") else status
    if kind == "DaemonSet":
        st = {"desiredNumberScheduled": want, "numberReady": ready}
    spec = {"replicas": want, "template": {"spec": {"containers": [{"image": i} for i in images or ["x"]]}}}
    return {"kind": kind, "metadata": {"namespace": ns, "name": name}, "spec": spec, "status": st}


def _workloads(drop=(), seed_failed=False):
    items = [_w(kind, ns, name) for ns, kind, name in s3.REQUIRED if name not in drop]
    items.append(_w("Deployment", "kela", "int-gotcha-8e6d6910", images=["integrations/gotcha@sha", "vendor/magos-agent@sha"]))
    items.append(_w("Deployment", "kela", "minio"))
    if seed_failed:
        items.append({"kind": "Job", "metadata": {"namespace": "kela", "name": "gotcha-site-seed"},
                      "spec": {}, "status": {"failed": 1}})
    return {"items": items}


def test_pod_problems():
    p = s3.pod_problems(PODS, NOW, 30)
    assert [x.split()[0] for x in p["young"]] == ["kela/video-1/video"]      # old restarts are not judged
    assert p["restart_moments"] == {"2025-09-28 07:44": 2, "2025-10-05 13:50": 1}
    assert any(x.startswith("kela/map-server-1") for x in p["not_ready"])
    assert p["stuck"] == ["kela/fusion-1/fusion CrashLoopBackOff"]


def test_workload_ready():
    assert s3.workload_ready(_w("Deployment", "kela", "a", ready=0, want=1)) == (False, "0/1 ready")
    assert s3.workload_ready(_w("DaemonSet", "monitoring", "alloy", ready=1, want=1))[0]


class _Session:
    has_operator = False
    operator_error = "no x-operator peer on the tailnet"

    def __init__(self, workloads, pods=PODS):
        self.out = {"nodes": json.dumps({"items": [{"metadata": {"name": "n1"}, "status": {
                        "conditions": [{"type": "Ready", "status": "True"}]}}]}),
                    "pods": json.dumps(pods), "workloads": json.dumps(workloads),
                    "converged": "2026-10-05T16:57:25+0300 host kela-node-controller[1]: INFO system converged version=191",
                    "now": str(int(NOW))}

    def sections(self, target, commands, timeout=60):
        return {k: ExecResult(0, self.out.get(k, "")) for k in commands}


def _ctx(workloads, pods=PODS):
    return Context(unit=Unit("gotcha-x"), creds=Credentials(), redact=Redactor(),
                   release=release.load(ROOT / "release.yaml"), session=_Session(workloads, pods))


def test_s32_stable_cluster_with_old_restarts_passes():
    r = s3._s32(_ctx(_workloads(), STABLE_PODS))
    assert r.state == "pass"
    by = {c["label"]: c["actual"] for c in r.detail["checks"]}
    assert by["stable"].startswith("every container up ≥ 30 min")
    assert "2025-09-28 07:44 UTC ×2" in by["earlier restarts (recorded)"]


def test_s3_rows():
    ctx = _ctx(_workloads())
    # an unpinned node (desired None, as on gotcha-rev3-prototype) is judged
    # against the latest staged version S0.5 resolved
    ctx.facts["fleet_node"] = {"appliedSystemConfigurationVersion": 199, "desiredSystemConfigurationVersion": None}
    ctx.facts["fleet_desired"] = {"version": 199, "pinned": False}
    assert s3._s31(ctx).state == "pass"
    r32 = s3._s32(ctx)
    labels = [c["label"] for c in r32.detail["checks"] if c["ok"] is False]
    assert r32.state == "fail" and labels == ["kela/map-server-1", "kela/fusion-1", "kela/video-1/video"]
    assert s3._s33(ctx).state == "fail"
    r34 = s3._s34(ctx)
    assert r34.state == "pass"
    other = [c for c in r34.detail["checks"] if c["label"].startswith("other kela workloads")]
    assert other and "minio" in other[0]["actual"] and "radar-bench (bare pod)" in other[0]["actual"]
    assert s3._s38(ctx).state == "amber"


def test_s34_missing_and_failed_seed():
    r = s3._s34(_ctx(_workloads(drop=("dex",), seed_failed=True)))
    bad = {c["label"]: c["actual"] for c in r.detail["checks"] if c["ok"] is False}
    assert r.state == "fail" and bad["dex"].startswith("missing") and bad["gotcha-site-seed"].startswith("FAILED")
