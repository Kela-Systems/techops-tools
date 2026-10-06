"""S3 — Cluster & hub. kubectl on the server session (one batched round-trip),
the hub over gRPC through a -L tunnel, the operator's view of the C2.

The required-workload list (S3.4) is what kela's stack deploys for the gotcha
variant (deployment/kela-stack-ts: variants/core.ts + variants/gotcha.ts),
checked against a converged rev3 unit. The gotcha-site-seed job is not required
to be present: it deletes itself an hour after finishing
(ttlSecondsAfterFinished 3600); only a *failed* one is a finding.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Iterator, Optional

from ..access.exec import AccessError
from ..context import Context
from ..model import Checks, Row, RowSpec, StageSpec, amber, result
from ._common import grpc_error, guarded, hub

STAGE = StageSpec(
    id="S3", name="Cluster & hub", depends_on=("S2.7",),
    vantage="server (kubectl via ssh) + hub gRPC via -L",
    rows=(
        RowSpec("S3.1", "Node Ready, controller converged", "Ready; converged; applied == desired",
                "critical"),
        RowSpec("S3.2", "All pods Running, Ready and stable",
                "all Running/Ready; every container up ≥ release.yaml pod_stable_min_minutes; "
                "earlier restarts recorded", "critical"),
        RowSpec("S3.3", "No stuck or failing pods",
                "0 ImagePullBackOff / CrashLoopBackOff / ContainerStatusUnknown / Unknown", "critical"),
        RowSpec("S3.4", "Required workloads present",
                "every gotcha-variant workload present and ready; int-gotcha-<id> with the magos-agent "
                "sidecar; gotcha-site-seed not failed; other workloads recorded", "critical"),
        RowSpec("S3.5", "Hub gRPC serving", "grpc.health.v1 Health/Check == SERVING", "critical"),
        RowSpec("S3.6", "Hub is the Gotcha product profile",
                "PRODUCT_SERVICE_PROFILE_GOTCHA; site_id == build-info site", "critical",
                depends_on=("S3.5",)),
        RowSpec("S3.7", "Integration pod health endpoint", ":8663/health 200; :8664/metrics present",
                "high"),
        RowSpec("S3.8", "C2 reachable as the operator sees it",
                "HTTP 200 from https://kela.local/; certificate trusted", "critical"),
    ),
)

KUBECTL = "sudo -n k3s kubectl"
NS = "kela"

# (namespace, kind, name) — the gotcha variant's workloads.
REQUIRED: tuple[tuple[str, str, str], ...] = (
    (NS, "Deployment", "hub-server"),
    (NS, "Deployment", "c2-frontend"),
    (NS, "Deployment", "device-controller"),
    (NS, "Deployment", "fusion-service"),
    (NS, "Deployment", "map-server"),
    (NS, "Deployment", "integration-orchestrator"),
    (NS, "Deployment", "qgis-server"),
    (NS, "Deployment", "mediamtx"),
    (NS, "Deployment", "export-orchestrator"),       # data-export
    (NS, "Deployment", "dex"),
    (NS, "Deployment", "triton-inference-server"),
    (NS, "Deployment", "video-analytics-rs"),
    (NS, "Deployment", "calibration-service"),
    (NS, "Deployment", "object-follower"),
    (NS, "StatefulSet", "postgresql"),
    ("monitoring", "Deployment", "grafana"),          # observability
    ("monitoring", "Deployment", "prometheus-server"),
    ("monitoring", "StatefulSet", "loki"),
    ("monitoring", "StatefulSet", "tempo"),
    ("monitoring", "DaemonSet", "alloy"),
)
INTEGRATION_PREFIX = "int-gotcha-"
SITE_SEED_JOB = "gotcha-site-seed"

STUCK_WAITING = {"ImagePullBackOff", "ErrImagePull", "CrashLoopBackOff", "CreateContainerConfigError",
                 "CreateContainerError", "InvalidImageName", "RunContainerError"}

COMMANDS = {
    "nodes": f"{KUBECTL} get nodes -o json",
    "pods": f"{KUBECTL} get pods -A -o json",
    "workloads": f"{KUBECTL} get deploy,sts,ds,job -A -o json",
    "converged": "sudo -n journalctl -u kela-node-controller -b --no-pager -o short-iso "
                 "| grep -i 'system converged' | tail -n 1",
    # The server's clock, so container uptimes do not depend on the laptop's.
    "now": "date -u +%s",
}


# ── parsers (pure; covered by tests/test_s3.py) ─────────────────────────────

def _ts(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def node_ready(nodes: dict) -> list[tuple[str, bool]]:
    out = []
    for n in nodes.get("items") or []:
        cond = {c.get("type"): c.get("status") for c in (n.get("status") or {}).get("conditions") or []}
        out.append((n["metadata"]["name"], cond.get("Ready") == "True"))
    return out


def _age(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m} min" if m < 120 else f"{m // 60} h" if m < 48 * 60 else f"{m // 1440} d"


def pod_problems(pods: dict, now: Optional[float], stable_min: float = 30) -> dict:
    """What S3.2 / S3.3 judge, from `kubectl get pods -A -o json`:

    not_ready  'ns/pod why'            phase not Running, or a container not ready
    young      'ns/pod/container why'  running for less than `stable_min` minutes
    stuck      'ns/pod[/container] why'
    restart_moments  {'YYYY-MM-DD HH:MM': containers} — when earlier restarts
               happened. Restarts cluster at one moment (boot-time start-up,
               a k3s / containerd restart) and say nothing about stability now,
               so they are recorded, not judged.
    shortest   (seconds, 'ns/pod/container') — the youngest running container
    """
    not_ready, young, stuck = [], [], []
    moments: dict[str, int] = {}
    shortest: Optional[tuple[float, str]] = None
    for p in pods.get("items") or []:
        meta, st = p["metadata"], p.get("status") or {}
        name = f"{meta['namespace']}/{meta['name']}"
        phase = st.get("phase")
        if phase == "Succeeded":
            continue
        if phase in ("Unknown", "Failed"):
            stuck.append(f"{name} phase {phase}" + (f" ({st.get('reason')})" if st.get("reason") else ""))
            continue
        statuses = st.get("containerStatuses") or []
        if phase != "Running" or not statuses or not all(c.get("ready") for c in statuses):
            nr = [c["name"] for c in statuses if not c.get("ready")]
            not_ready.append(f"{name} {phase}" + (f", not ready: {', '.join(nr)}" if nr else ""))
        for c in statuses:
            waiting = ((c.get("state") or {}).get("waiting") or {}).get("reason")
            term = (c.get("lastState") or {}).get("terminated") or {}
            if waiting in STUCK_WAITING:
                stuck.append(f"{name}/{c['name']} {waiting}")
            if term.get("reason") == "ContainerStatusUnknown" or \
                    ((c.get("state") or {}).get("terminated") or {}).get("reason") == "ContainerStatusUnknown":
                stuck.append(f"{name}/{c['name']} ContainerStatusUnknown")
            finished = _ts(term.get("finishedAt"))
            if c.get("restartCount") and finished is not None:
                when = datetime.fromtimestamp(finished, timezone.utc).strftime("%Y-%m-%d %H:%M")
                moments[when] = moments.get(when, 0) + 1
            started = _ts(((c.get("state") or {}).get("running") or {}).get("startedAt"))
            if started is not None and now is not None:
                up = now - started
                if shortest is None or up < shortest[0]:
                    shortest = (up, f"{name}/{c['name']}")
                if up < stable_min * 60:
                    young.append(f"{name}/{c['name']} up only {_age(up)}"
                                 + (f" ({c.get('restartCount')} restarts)" if c.get("restartCount") else ""))
    return {"not_ready": not_ready, "young": young, "stuck": stuck,
            "restart_moments": dict(sorted(moments.items())), "shortest": shortest}


def workload_index(workloads: dict) -> dict[tuple[str, str, str], dict]:
    return {(w["metadata"]["namespace"], w["kind"], w["metadata"]["name"]): w
            for w in workloads.get("items") or []}


def workload_ready(w: dict) -> tuple[bool, str]:
    kind, st, spec = w["kind"], w.get("status") or {}, w.get("spec") or {}
    if kind == "DaemonSet":
        want, got = st.get("desiredNumberScheduled", 0), st.get("numberReady", 0)
    elif kind == "Job":
        ok = bool(st.get("succeeded")) and not st.get("failed")
        return ok, "Completed" if st.get("succeeded") else ("Failed" if st.get("failed") else "running")
    else:
        want, got = spec.get("replicas", 1), st.get("readyReplicas", 0)
    return got >= want, f"{got}/{want} ready"


# ── rows ────────────────────────────────────────────────────────────────────

def _k(ctx: Context) -> dict:
    """kubectl reads, once per stage."""
    if "_s3" not in ctx.facts:
        raw = ctx.server_sections(COMMANDS, timeout=90)
        k: dict = {"raw": raw}
        for name in ("nodes", "pods", "workloads"):
            try:
                k[name] = json.loads(raw[name].out) if raw[name].ok else None
            except ValueError:
                k[name] = None
        try:
            k["now"] = float(raw["now"].text)
        except ValueError:
            k["now"] = None
        ctx.facts["_s3"] = k
    return ctx.facts["_s3"]


def _kubectl_error(k: dict, name: str) -> str:
    r = k["raw"][name]
    if "a password is required" in r.out:
        return "sudo on the server asked for a password (kela needs passwordless sudo)"
    return f"kubectl get {name} failed: " + ((r.text.splitlines() or [f'rc {r.rc}'])[-1][:140])


def _s31(ctx: Context) -> Row:
    spec = STAGE.spec("S3.1")
    k = _k(ctx)
    if k["nodes"] is None:
        return amber(spec, ctx.redact(_kubectl_error(k, "nodes")))
    c = Checks()
    nodes = node_ready(k["nodes"])
    bad = [n for n, ok in nodes if not ok]
    c.add("node", bool(nodes) and not bad, f"{len(nodes)} node(s) Ready" if not bad else "NOT Ready: " + ", ".join(bad))
    line = k["raw"]["converged"].text
    m = re.search(r"^(\S+).*system converged(?:.*version=(\S+))?", line)
    c.add("node-controller", bool(m), f"'system converged' at {m.group(1)}" + (f", version {m.group(2)}" if m.group(2) else "")
          if m else "no 'system converged' since boot")
    node, target = ctx.facts.get("fleet_node"), ctx.facts.get("fleet_desired")
    if node and target:
        a, d = node.get("appliedSystemConfigurationVersion"), target["version"]
        c.add("Fleet applied == desired", a == d,
              f"{a} / {d}" + (" (pinned)" if target["pinned"] else " (not pinned — follows the latest staged)"))
    else:
        c.add("Fleet applied == desired", None, "not read (S0.5)")
    return c.row(spec)


def _s32(ctx: Context) -> Row:
    spec = STAGE.spec("S3.2")
    k = _k(ctx)
    if k["pods"] is None:
        return amber(spec, ctx.redact(_kubectl_error(k, "pods")))
    # S9's re-check after a power cut waives the age rule: every container is minutes old.
    stable_min = ctx.facts.get("pod_stable_min_minutes", ctx.release.thresholds["pod_stable_min_minutes"])
    probs = pod_problems(k["pods"], k["now"], stable_min)
    total = sum(1 for p in k["pods"].get("items") or [] if (p.get("status") or {}).get("phase") != "Succeeded")
    c = Checks()
    # Problems get one line each; a healthy cluster is two lines.
    for entry in probs["not_ready"]:
        name, _, why = entry.partition(" ")
        c.add(name, False, why)
    if not probs["not_ready"]:
        c.add("running & ready", True, f"all {total} pods")
    if k["now"] is None:
        c.add("stable", None, "server clock unreadable")
    for entry in probs["young"]:
        name, _, why = entry.partition(" ")
        c.add(name, False, f"{why} (want ≥ {stable_min} min)")
    if k["now"] is not None and not probs["young"] and probs["shortest"]:
        c.add("stable", True, f"every container up ≥ {stable_min} min (shortest {_age(probs['shortest'][0])})")
    moments = probs["restart_moments"]
    if moments:
        n = sum(moments.values())
        top = sorted(moments.items(), key=lambda kv: -kv[1])[:3]
        c.items.append({"label": "earlier restarts (recorded)", "ok": True,
                        "actual": f"{n} containers, last restarted at " +
                                  ", ".join(f"{when} UTC ×{cnt}" for when, cnt in top) +
                                  (" …" if len(moments) > 3 else "")})
    return c.row(spec, **probs)


def _s33(ctx: Context) -> Row:
    spec = STAGE.spec("S3.3")
    k = _k(ctx)
    if k["pods"] is None:
        return amber(spec, ctx.redact(_kubectl_error(k, "pods")))
    stuck = pod_problems(k["pods"], k["now"])["stuck"]
    if not stuck:
        return result(spec, "0 stuck pods", True)
    c = Checks()
    for entry in stuck:
        name, _, why = entry.partition(" ")
        c.add(name, False, why)
    return c.row(spec, stuck=stuck)


def _s34(ctx: Context) -> Row:
    spec = STAGE.spec("S3.4")
    k = _k(ctx)
    if k["workloads"] is None:
        return amber(spec, ctx.redact(_kubectl_error(k, "workloads")))
    idx = workload_index(k["workloads"])
    c = Checks()
    for ns, kind, name in REQUIRED:
        w = idx.get((ns, kind, name))
        if w is None:
            c.add(name, False, f"missing ({kind} in {ns})")
        else:
            ok, why = workload_ready(w)
            c.add(name, ok, why)
    ints = [w for (ns, kind, name), w in idx.items()
            if ns == NS and kind == "Deployment" and name.startswith(INTEGRATION_PREFIX)]
    if not ints:
        c.add("int-gotcha-<id>", False, "no integration deployment")
    for w in ints:
        ok, why = workload_ready(w)
        images = [x.get("image", "") for x in w["spec"]["template"]["spec"].get("containers") or []]
        sidecar = any("magos-agent" in i for i in images)
        c.add(w["metadata"]["name"], ok and sidecar, why + ("" if sidecar else ", no magos-agent sidecar"))
    seeds = [w for (ns, kind, name), w in idx.items() if kind == "Job" and name.startswith(SITE_SEED_JOB)]
    failed = [w["metadata"]["name"] for w in seeds if (w.get("status") or {}).get("failed")]
    c.add(SITE_SEED_JOB, not failed, "FAILED: " + ", ".join(failed) if failed else
          ("Completed" if seeds else "ran and was cleaned up (ttl 1 h) — not judged"))
    known = {(ns, name) for ns, _, name in REQUIRED} | {(NS, w["metadata"]["name"]) for w in ints}
    extra = sorted(name for (ns, kind, name) in idx
                   if ns == NS and kind in ("Deployment", "StatefulSet") and (ns, name) not in known)
    bare = sorted(p["metadata"]["name"] for p in (k["pods"] or {}).get("items") or []
                  if p["metadata"]["namespace"] == NS and not p["metadata"].get("ownerReferences"))
    if extra or bare:
        c.items.append({"label": "other kela workloads (recorded)", "ok": True,
                        "actual": ", ".join(extra + [f"{b} (bare pod)" for b in bare])})
    return c.row(spec, extra=extra, bare_pods=bare)


def _s35(ctx: Context) -> Row:
    spec = STAGE.spec("S3.5")
    try:
        state = hub(ctx).health()
    except AccessError as e:
        return result(spec, ctx.redact(str(e)), False)
    except Exception as e:  # noqa: BLE001 — grpc errors carry the reason in their text
        return result(spec, ctx.redact(grpc_error(e)), False)
    return result(spec, state, state == "SERVING")


def _s36(ctx: Context) -> Row:
    spec = STAGE.spec("S3.6")
    from ..access.grpc import pb
    try:
        sys_pb = pb("kela.system.v1alpha1.system_pb2")
        st = hub(ctx).stub("kela.system.v1alpha1.system_pb2_grpc", "SystemServiceStub") \
            .GetStatus(sys_pb.GetStatusRequest(), timeout=15)
    except Exception as e:  # noqa: BLE001
        return result(spec, ctx.redact(grpc_error(e)), False)
    profile = sys_pb.ProductServiceProfile.Name(st.product_service_profile)
    ctx.facts["hub_status"] = {"version": st.version, "site_id": st.site_id, "profile": profile}
    bi = ((ctx.facts.get("identity") or {}).get("server") or {}).get("build_info") or {}
    want_site = bi.get("site")
    c = Checks()
    c.add("profile", profile == "PRODUCT_SERVICE_PROFILE_GOTCHA", profile.replace("PRODUCT_SERVICE_PROFILE_", ""))
    if want_site:
        c.add("site_id", st.site_id == want_site, f"{st.site_id or 'empty'} (build-info {want_site})")
    else:
        c.add("site_id", None, f"{st.site_id or 'empty'} — no build-info site to compare")
    c.items.append({"label": "version (recorded)", "ok": True, "actual": st.version or "—"})
    return c.row(spec, **ctx.facts["hub_status"])


def _s37(ctx: Context) -> Row:
    spec = STAGE.spec("S3.7")
    k = _k(ctx)
    if k["pods"] is None:
        return amber(spec, ctx.redact(_kubectl_error(k, "pods")))
    pods = [p for p in k["pods"].get("items") or []
            if p["metadata"]["namespace"] == NS and p["metadata"]["name"].startswith(INTEGRATION_PREFIX)
            and (p.get("status") or {}).get("phase") == "Running"]
    if not pods:
        return result(spec, "no running int-gotcha pod", False)
    c = Checks()
    for p in pods:
        ip = (p.get("status") or {}).get("podIP")
        name = p["metadata"]["name"]
        r = ctx.server_sections({
            "health": f"curl -s -m 5 -o /dev/null -w '%{{http_code}}' http://{ip}:8663/health",
            "metrics": f"curl -s -m 5 http://{ip}:8664/metrics | grep -c '^kela_'",
        }, timeout=20)
        code = r["health"].text
        c.add(f"{name} /health", code == "200", f"HTTP {code or 'no answer'}")
        n = r["metrics"].text
        c.add(f"{name} /metrics", n.isdigit() and int(n) > 0, f"{n or 0} kela_ series")
    return c.row(spec)


def _s38(ctx: Context) -> Row:
    spec = STAGE.spec("S3.8")
    if not ctx.session.has_operator:
        return amber(spec, "no operator session — " + ctx.redact(ctx.session.operator_error))
    r = ctx.operator("curl -sS -m 10 -o /dev/null -w '%{http_code}' https://kela.local/; echo \" rc=$?\"", 20)
    m = re.search(r"(\d{3})?\s*rc=(\d+)", r.out)
    code, rc = (m.group(1), int(m.group(2))) if m else (None, r.rc)
    if rc == 60:
        return result(spec, "certificate NOT trusted by the operator station (curl error 60)", False)
    if rc != 0:
        why = (r.err or r.out).strip().splitlines()
        return result(spec, f"no answer from https://kela.local/ (curl rc {rc}"
                      + (f": {why[0][:100]}" if why else "") + ")", False)
    return result(spec, f"HTTP {code}, certificate trusted", code == "200")


def run(ctx: Context) -> Iterator[Row]:
    try:
        yield from guarded(ctx, STAGE, (_s31, _s32, _s33, _s34, _s35, _s36, _s37, _s38))
    finally:
        ctx.facts.pop("_s3", None)
