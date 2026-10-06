"""S6 — Sensor liveness: is every sensor actually producing data, now.

One sampling window (release.yaml detection_sample_s) feeds most rows, so the
stage costs ~20 s however many rows read from it:
  * each radar's sweep stream through its APU, ws://<apu>/radars/<id>/v1/detections
    (HTTP Basic auth with the device's radar login — how the driver reads it),
    all four in parallel
  * each radar's own stream, ws://<radar>/radar/v1/detections (session cookie):
    an APU-controlled radar is in **raw mode** — it streams to its APU and
    sends no sweeps of its own (field finding, rev3-dev), so this proves the
    radar is up and in the right mode, not that it sweeps
  * MediaMTX byte counters and the int-gotcha pod's metrics, before and after

Field findings folded in (gotcha-rev3-dev, 2026-10-05):
  * Asset.connection_health is hub-internal — not in the gRPC Asset; it is
    read from the newest row of the assets table
  * the gotcha variant publishes no stream-health scores (no stream-inspector),
    so S6.6 judges the streams the hub registered and records a score only
    when there is one
  * kela_magos_rf_to_hub_publish_seconds only fills with MASS processing on;
    release.yaml pins it off, so S6.5 is "not applicable" then
  * a radar's applClientList holds its APU only, not the server
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import shutil
import time
from typing import Any, Iterator, Optional

from websockets.asyncio.client import connect

from ..access import socks_http
from ..access.exec import run_local
from ..context import Context
from ..devices import Magos, Speaker
from ..discovery import friendly_error
from ..model import Checks, Row, RowSpec, StageSpec, amber, result
from ._common import grpc_error, guarded, hub

STAGE = StageSpec(
    id="S6", name="Sensor liveness", depends_on=("S3.5", "S5.1"),
    vantage="hub gRPC + psql (server) + devices (SOCKS on the server session) + MediaMTX",
    rows=(
        RowSpec("S6.1", "Radar sensors healthy",
                "radar-1..4 HEALTHY (WARN / FAIL / OFFLINE / NOT_READY / POWERED_OFF / UNSPECIFIED fail)",
                "critical"),
        RowSpec("S6.2", "Driver session live; camera component healthy",
                "connection_health HEALTHY and updated < 60 s ago; ComponentHealth.camera HEALTHY",
                "critical"),
        RowSpec("S6.3a", "Detection history per radar", "raw_tracks since boot recorded for radar-1..4",
                "critical"),
        RowSpec("S6.3b", "Radar online in raw mode (APU-controlled)",
                "each radar's own stream answers: op_state raw, heartbeats", "critical"),
        RowSpec("S6.3c", "Live sweep sample — through the APU",
                "sweeps > 0 for each radar on its APU's per-radar stream", "critical"),
        RowSpec("S6.3d", "Radar verdict",
                "HEALTHY + sweeping via the APU = PASS ('quiet' when no detections and no history); "
                "not sweeping or not HEALTHY = FAIL", "critical"),
        RowSpec("S6.4", "Radar reachability (driver probes)",
                "no radar probe failure in the int-gotcha log in the last hour", "high"),
        RowSpec("S6.5", "Radar pipeline latency",
                "rf→hub p95 < 1 s for 4 radars (only with MASS processing on; else not applicable)", "high"),
        RowSpec("S6.6", "Video streams registered in the hub",
                "main and thermal each have a native substream; stream-health score recorded when published",
                "critical"),
        RowSpec("S6.7a", "MediaMTX is receiving both streams",
                "every native path ready, H264, bytes increasing during the sample", "critical"),
        RowSpec("S6.7b", "Video actually decodes",
                "≥ 25 frames in 5 s from main and thermal; resolution == release.yaml.camera.streams",
                "high"),
        RowSpec("S6.8", "PTZ telemetry flowing",
                "kela_pose_readings_total{outcome=accepted} increases during the sample", "medium"),
        RowSpec("S6.9", "Speaker reachable and loaded (read-only)",
                "CGI login ok; the music listing is not empty", "high"),
        RowSpec("S6.13", "Speaker output volume set",
                "audio.get result 0; outvolume ≥ release.yaml.speaker.outvolume_min (1 = not muted)", "high"),
        RowSpec("S6.10", "Radar self-reported status (×4)",
                "alerts == []; ethernet 1000; port1 CONNECTED at the planned IP; time synced to .10 "
                "(|offset| < 0.5 s); its APU in applClientList; max temperature < 75 °C", "high"),
        RowSpec("S6.11", "APU self-reported status (×2)",
                "alerts == []; ethernet 1000; port1 CONNECTED at .60 / .61; time synced to .10; "
                "max temperature < 75 °C", "high"),
        RowSpec("S6.12", "Radar / APU link stable during the run",
                "ethernetCarrierDown unchanged since S0.6 (RX/TX error growth recorded)", "high"),
    ),
)

PSQL = "sudo -n k3s kubectl -n kela exec postgresql-0 -c postgresql -- psql -U kela -d kela -At -c"
MTX_API = "http://127.0.0.1:9997/v3/paths/list"          # mediamtx runs host-network on the server
RTSP_PORT = 8554
RADAR_SENSORS = ("radar-1", "radar-2", "radar-3", "radar-4")
DIRECT_SAMPLE_S = 4
PROBE_FAIL_RE = r"Failed to probe radar endpoint|is being marked failed|treating it as unreachable"


# ── parsers (pure; covered by tests/test_s6.py) ─────────────────────────────

def prom_samples(text: str, name: str) -> list[tuple[dict, float]]:
    """Samples of one Prometheus metric: [(labels, value)]."""
    out = []
    for m in re.finditer(rf"^{re.escape(name)}(?:\{{([^}}]*)\}})?\s+(\S+)", text, re.M):
        labels = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1) or ""))
        try:
            out.append((labels, float(m.group(2))))
        except ValueError:
            pass
    return out


def histogram_p95(text: str, name: str, group: str) -> dict[str, tuple[int, Optional[float]]]:
    """{label value of `group`: (count, p95 upper bound in seconds)} from _bucket lines."""
    buckets: dict[str, list[tuple[float, float]]] = {}
    for labels, v in prom_samples(text, name + "_bucket"):
        le = labels.get("le", "+Inf")
        buckets.setdefault(labels.get(group, ""), []).append((math.inf if le == "+Inf" else float(le), v))
    out = {}
    for key, bs in buckets.items():
        bs.sort()
        total = bs[-1][1] if bs else 0
        p95 = None
        if total:
            p95 = next((le for le, cum in bs if cum >= 0.95 * total), math.inf)
        out[key] = (int(total), p95)
    return out


def mtx_paths(data: dict, asset_id: str) -> dict[str, dict]:
    """MediaMTX paths of one asset: {path: {sensor, ready, codecs, width, height, bytes}}."""
    out = {}
    for it in (data or {}).get("items") or []:
        md = it.get("metadata") or {}
        if md.get("assetId") != asset_id or md.get("type", "Native") != "Native":
            continue
        t2 = (it.get("tracks2") or [{}])[0] or {}
        props = t2.get("codecProps") or {}
        out[it["name"]] = {"sensor": md.get("sensorId"), "ready": bool(it.get("ready")),
                           "codecs": it.get("tracks") or [], "width": props.get("width"),
                           "height": props.get("height"),
                           "bytes": int(it.get("bytesReceived") or it.get("inboundBytes") or 0)}
    return out


def ffprobe_frames(text: str) -> tuple[int, Optional[str]]:
    """(frames, 'WxH') from `ffprobe -count_frames -show_entries stream=...` JSON."""
    try:
        st = (json.loads(text).get("streams") or [{}])[0]
    except (ValueError, IndexError):
        return 0, None
    frames = int(st.get("nb_read_frames") or 0)
    res = f"{st['width']}x{st['height']}" if st.get("width") else None
    return frames, res


# ── the shared sample ───────────────────────────────────────────────────────

async def _ws_count(uri: str, seconds: float, proxy: str, headers: dict) -> dict:
    c: dict = {"sweeps": 0, "detections": 0, "heartbeats": 0, "op_state": None, "alerts": [], "error": None}
    deadline = time.monotonic() + seconds
    try:
        async with connect(uri, proxy=proxy, additional_headers=headers, open_timeout=10,
                           max_size=16 << 20) as ws:
            while (left := deadline - time.monotonic()) > 0:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=left)
                except asyncio.TimeoutError:
                    break
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                op, payload = msg.get("op"), msg.get("payload")
                if op == "detections":
                    c["sweeps"] += 1
                    dets = payload.get("detections") if isinstance(payload, dict) else payload
                    c["detections"] += len(dets) if isinstance(dets, list) else 0
                elif op == "heartbeat":
                    c["heartbeats"] += 1
                elif op == "op_state" and isinstance(payload, dict):
                    c["op_state"] = payload.get("state")
                elif op == "alerts" and isinstance(payload, list):
                    c["alerts"] = [a.get("message") for a in payload if isinstance(a, dict)]
    except Exception as e:  # noqa: BLE001
        c["error"] = f"{type(e).__name__}: {e}"
    return c


def _int_pod_ip(ctx: Context) -> str:
    r = ctx.server("sudo -n k3s kubectl -n kela get pods -o jsonpath='{range .items[*]}{.metadata.name} "
                   "{.status.podIP}{\"\\n\"}{end}' | awk '/^int-gotcha-/{print $2; exit}'", 20)
    return r.text


def _snapshot(ctx: Context, pod_ip: str) -> dict:
    s = ctx.server_sections({"mtx": f"curl -s -m 8 {MTX_API}",
                             "metrics": f"curl -s -m 8 http://{pod_ip}:8664/metrics" if pod_ip else "true"}, 30)
    try:
        mtx = json.loads(s["mtx"].out) if s["mtx"].ok else None
    except ValueError:
        mtx = None
    return {"mtx": mtx, "metrics": s["metrics"].out, "t": time.monotonic()}


def _sample(ctx: Context) -> dict:
    if "_s6" in ctx.facts:
        return ctx.facts["_s6"]
    plan = ctx.release.plan
    login = ctx.creds.device("magos")
    smp: dict = {"apu": {}, "direct": {}, "before": None, "after": None, "pod_ip": ""}
    smp["pod_ip"] = _int_pod_ip(ctx)
    smp["before"] = _snapshot(ctx, smp["pod_ip"])
    # session cookies for the radars' own streams — one client per radar: every
    # radar names its cookie _sessionId, so a shared jar would mix them up
    cookies: dict[str, str] = {}
    basic = Magos(None, "", login).basic_header()  # type: ignore[arg-type]
    for rid, ip in plan.radars.items():
        with socks_http.client(ctx.session.socks_url) as http:
            try:
                m = Magos(http, ip, login)
                m.login()
                cookies[rid] = m.cookie_header()
            except Exception as e:  # noqa: BLE001 — an unreachable radar is a row result
                smp["direct"][rid] = {"error": ctx.redact(friendly_error(e))}
    seconds = float(ctx.release.thresholds["detection_sample_s"])
    proxy = ctx.session.socks_url

    async def everything():
        jobs = {}
        for rid in plan.radars:
            apu = plan.apu_for(rid)
            jobs[("apu", rid)] = _ws_count(f"ws://{apu}/radars/{rid}/v1/detections", seconds, proxy, basic)
            if rid in cookies:
                jobs[("direct", rid)] = _ws_count(f"ws://{plan.radars[rid]}/radar/v1/detections",
                                                  DIRECT_SAMPLE_S, proxy, {"Cookie": cookies[rid]})
        res = await asyncio.gather(*jobs.values())
        return dict(zip(jobs, res))

    ctx.log(f"S6: sampling every radar for {seconds:g} s")
    for (kind, rid), counts in asyncio.run(everything()).items():
        if counts.get("error"):
            counts["error"] = ctx.redact(counts["error"])
        smp[kind][rid] = counts
    smp["after"] = _snapshot(ctx, smp["pod_ip"])
    ctx.facts["_s6"] = smp
    return smp


def _asset(ctx: Context):
    """(gRPC Asset of the Gotcha device | None, error)."""
    if "_s6_asset" in ctx.facts:
        return ctx.facts["_s6_asset"]
    from ..access.grpc import pb
    dev = ctx.facts.get("gotcha_device") or {}
    integ = dev.get("integration_id")
    out = (None, "no Gotcha device (S5.1)")
    if integ:
        try:
            apb = pb("kela.asset.v1alpha1.asset_pb2")
            resp = hub(ctx).stub("kela.asset.v1alpha1.asset_service_pb2_grpc", "AssetServiceStub") \
                .ListAssets(apb.ListAssetsRequest(), timeout=20)
            mine = [a for a in resp.assets if a.integration_id == integ]
            out = (mine[0], "") if len(mine) == 1 else (None, f"{len(mine)} assets for the Gotcha integration")
        except Exception as e:  # noqa: BLE001
            out = (None, ctx.redact(grpc_error(e)))
    ctx.facts["_s6_asset"] = out
    return out


def _hs(value: int) -> str:
    from ..access.grpc import pb
    return pb("kela.asset.v1alpha1.asset_pb2").HealthStatus.Name(value).replace("HEALTH_STATUS_", "")


# ── rows ────────────────────────────────────────────────────────────────────

def _s61(ctx: Context) -> Row:
    spec = STAGE.spec("S6.1")
    a, err = _asset(ctx)
    if a is None:
        return amber(spec, err)
    by = {s.sensor_id: s for s in a.sensors}
    c = Checks()
    health = {}
    for sid in RADAR_SENSORS:
        s = by.get(sid)
        if s is None:
            c.add(sid, False, "missing from the asset")
            continue
        h = _hs(s.health.health_status) if s.HasField("health") else "UNSPECIFIED"
        health[sid] = h
        c.add(sid, h == "HEALTHY", h)
    ctx.facts["radar_health"] = health
    return c.row(spec)


def _s62(ctx: Context) -> Row:
    spec = STAGE.spec("S6.2")
    a, err = _asset(ctx)
    if a is None:
        return amber(spec, err)
    c = Checks()
    r = ctx.server(f"{PSQL} \"select json_build_object('conn', data->'connection_health', "
                   f"'age', extract(epoch from now() - time)) from assets where id = '{a.id}' "
                   "and deleted_at is null order by time desc limit 1\"", 40)
    try:
        row = json.loads(r.text)
        conn = (row.get("conn") or {}).get("health_status")
        age = float(row.get("age") or 0)
        c.add("driver session", conn == 1, f"{_hs(conn) if conn is not None else 'unset'}, saved {age:.0f} s ago")
        c.add("last update", age < 60, f"{age:.0f} s ago")
    except (ValueError, TypeError, AttributeError):
        c.add("driver session", None, "hub DB: " + ctx.redact(r.text[:120] or r.err.strip() or "no row"))
    from ..access.grpc import pb
    gpb = pb("kela.ext.gotcha.v1alpha1.gotcha_pb2")
    comp = None
    for e in a.extensions:
        if e.type_url.endswith("kela.ext.gotcha.v1alpha1.ComponentHealth"):
            comp = gpb.ComponentHealth()
            e.Unpack(comp)
    if comp is None:
        c.add("camera component", False, "no ComponentHealth on the asset")
    else:
        c.add("camera component", _hs(comp.camera) == "HEALTHY", _hs(comp.camera))
        c.items.append({"label": "speaker component (recorded)", "ok": True,
                        "actual": _hs(comp.speaker) + " — judged by S6.9 / S6.13 over the speaker's own CGI"})
    return c.row(spec)


def _s63a(ctx: Context) -> Row:
    spec = STAGE.spec("S6.3a")
    a, err = _asset(ctx)
    if a is None:
        return amber(spec, err)
    r = ctx.server(f"boot=$(date -u -d \"$(uptime -s)\" +%FT%TZ); {PSQL} \"select coalesce(json_object_agg("
                   f"source_sensor_id, n), '{{}}') from (select source_sensor_id, count(*) n from raw_tracks "
                   f"where source_asset_id = '{a.id}' and last_detected_at > '$boot' group by 1) t\"", 60)
    try:
        counts = json.loads(r.text) if r.ok else None
    except ValueError:
        counts = None
    if counts is None:
        return amber(spec, "hub DB: " + ctx.redact(r.text[:120] or r.err.strip() or "no answer"))
    ctx.facts["radar_history"] = {sid: int(counts.get(sid, 0)) for sid in RADAR_SENSORS}
    c = Checks()
    for sid in RADAR_SENSORS:
        c.items.append({"label": sid, "ok": True, "actual": f"{counts.get(sid, 0)} tracks since boot"})
    return c.row(spec, counts=counts)


def _s63b(ctx: Context) -> Row:
    spec = STAGE.spec("S6.3b")
    smp = _sample(ctx)
    plan = ctx.release.plan
    c = Checks()
    for rid, ip in plan.radars.items():
        d = smp["direct"].get(rid) or {}
        label = f"{rid} .{ip.rsplit('.', 1)[1]}"
        if d.get("error"):
            c.add(label, False, d["error"])
            continue
        mode = d.get("op_state")
        alive = d.get("heartbeats", 0) > 0 or mode is not None
        c.add(label, alive and mode == "raw",
              f"op_state {mode or '—'}, {d.get('heartbeats', 0)} heartbeats in {DIRECT_SAMPLE_S} s"
              + ("" if mode in (None, "raw") else " (want raw: the APU must control it)"))
    return c.row(spec)


def _s63c(ctx: Context) -> Row:
    spec = STAGE.spec("S6.3c")
    smp = _sample(ctx)
    plan = ctx.release.plan
    secs = ctx.release.thresholds["detection_sample_s"]
    c = Checks()
    for rid in plan.radars:
        d = smp["apu"].get(rid) or {}
        label = f"{rid} via .{plan.apu_for(rid).rsplit('.', 1)[1]}"
        if d.get("error"):
            c.add(label, False, d["error"])
            continue
        c.add(label, d.get("sweeps", 0) > 0,
              f"{d.get('sweeps', 0)} sweeps, {d.get('detections', 0)} detections in {secs:g} s")
    return c.row(spec, apu=smp["apu"])


def _s63d(ctx: Context) -> Row:
    spec = STAGE.spec("S6.3d")
    smp = _sample(ctx)
    health = ctx.facts.get("radar_health") or {}
    history = ctx.facts.get("radar_history") or {}
    c, quiet = Checks(), []
    # The driver names sensors by position in setup_info.radars (radar-N = N-th entry).
    declared = [str(r.get("instanceId")) for r in
                ((ctx.facts.get("gotcha_device") or {}).get("setup_info") or {}).get("radars") or []]
    order = declared if sorted(declared) == sorted(ctx.release.plan.radars) else list(ctx.release.plan.radars)
    for i, rid in enumerate(order, 1):
        sid = f"radar-{i}"
        h = health.get(sid)
        d = smp["apu"].get(rid) or {}
        sweeps, dets = d.get("sweeps", 0), d.get("detections", 0)
        if h is None:
            c.add(rid, None, "hub health unknown (S6.1)")
        elif h != "HEALTHY":
            c.add(rid, False, f"hub reports {h}")
        elif d.get("error") or not sweeps:
            c.add(rid, False, "not sweeping — responsive but not operational")
        elif not dets and not history.get(sid):
            quiet.append(rid)
            c.add(rid, True, "PASS, quiet (no detections now, no history since boot) — walk test offered")
        else:
            c.add(rid, True, f"PASS ({sweeps} sweeps, {dets} detections, {history.get(sid, '?')} tracks since boot)")
    if quiet:
        ctx.facts["quiet_radars"] = quiet
    return c.row(spec)


def _s64(ctx: Context) -> Row:
    spec = STAGE.spec("S6.4")
    r = ctx.server("dep=$(sudo -n k3s kubectl -n kela get deploy -o name | grep '/int-gotcha-' | head -1); "
                   f"sudo -n k3s kubectl -n kela logs $dep -c svc-0 --since=1h 2>&1 | grep -E '{PROBE_FAIL_RE}' | tail -n 20", 60)
    lines = [ln for ln in r.out.splitlines() if ln.strip()]
    if not lines:
        return result(spec, "no radar probe failures in the last hour", True)
    c = Checks()
    hosts: dict[str, int] = {}
    for ln in lines:
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)(?::\d+)?|Radar (\S+)", ln)
        key = (m.group(1) or m.group(2)) if m else "radar"
        hosts[key] = hosts.get(key, 0) + 1
    for host, n in hosts.items():
        c.add(host, False, f"{n} probe failure(s) in the last hour")
    return c.row(spec, lines=lines)


def _s65(ctx: Context) -> Row:
    spec = STAGE.spec("S6.5")
    smp = _sample(ctx)
    mass = ctx.release.get("hub.integration_config", {}).get("mass_processing_enabled")
    hist = histogram_p95(smp["after"]["metrics"], "kela_magos_rf_to_hub_publish_seconds", "radar_uid")
    hist = {k: v for k, v in hist.items() if v[0]}
    if not hist:
        if mass is False:
            return result(spec, "not applicable — the latency histogram only fills with MASS processing on "
                          "(release.yaml pins it off)", True)
        return amber(spec, "kela_magos_rf_to_hub_publish_seconds has no samples")
    c = Checks()
    for uid, (n, p95) in sorted(hist.items()):
        c.add(uid or "radar", p95 is not None and p95 < 1.0, f"p95 ≤ {p95:g} s over {n} detections")
    return c.row(spec)


def _s66(ctx: Context) -> Row:
    spec = STAGE.spec("S6.6")
    a, err = _asset(ctx)
    if a is None:
        return amber(spec, err)
    from ..access.grpc import pb
    natives = {ss.sensor_id: [x for x in ss.substreams if "NATIVE" in pb("kela.asset.v1alpha1.asset_pb2")
                              .SubstreamType.Name(x.type)] for ss in a.sensor_streams}
    scores: dict[str, Optional[int]] = {}
    try:
        vpb = pb("kela.media.v1alpha1.video_pb2")
        st = hub(ctx).stub("kela.media.v1alpha1.video_pb2_grpc", "VideoServiceStub") \
            .WatchStreamHealth(vpb.WatchStreamHealthRequest(), timeout=8)
        first = next(st)
        st.cancel()
        for s in first.streams:
            if s.asset_id == a.id and s.HasField("score"):
                scores[s.sensor_id] = min(scores.get(s.sensor_id, 100), s.score)
    except Exception as e:  # noqa: BLE001
        scores = {"error": ctx.redact(grpc_error(e))}  # type: ignore[dict-item]
    floor = ctx.release.thresholds["stream_health_min"]
    c = Checks()
    for sensor in ("main", "thermal"):
        subs = natives.get(sensor) or []
        res = ", ".join(f"{x.expected_resolution.width}x{x.expected_resolution.height}" for x in subs)
        c.add(sensor, bool(subs), f"{len(subs)} native substream(s) {res}" if subs else "no native substream")
        if sensor in scores:
            c.add(f"{sensor} health score", scores[sensor] >= floor, f"{scores[sensor]} (min {floor})")
    if not any(k in scores for k in ("main", "thermal")):
        c.items.append({"label": "health score (recorded)", "ok": True,
                        "actual": "not published on this variant" if "error" not in scores else scores["error"]})
    ctx.facts["video_natives"] = {k: [x.path for x in v] for k, v in natives.items()}
    return c.row(spec)


def _s67a(ctx: Context) -> Row:
    spec = STAGE.spec("S6.7a")
    a, err = _asset(ctx)
    if a is None:
        return amber(spec, err)
    smp = _sample(ctx)
    b, e = smp["before"]["mtx"], smp["after"]["mtx"]
    if e is None:
        return amber(spec, f"MediaMTX API ({MTX_API}) did not answer on the server")
    before, after = mtx_paths(b or {}, a.id), mtx_paths(e, a.id)
    secs = smp["after"]["t"] - smp["before"]["t"]
    c = Checks()
    if not after:
        return result(spec, "MediaMTX has no path for this asset", False)
    for path, p in sorted(after.items(), key=lambda kv: (kv[1]["sensor"] or "", kv[0])):
        grew = p["bytes"] - (before.get(path) or {}).get("bytes", p["bytes"])
        label = f"{p['sensor']} {p['width']}x{p['height']}"
        ok = p["ready"] and "H264" in p["codecs"] and grew > 0
        c.add(label, ok, ("ready" if p["ready"] else "NOT ready") + f", {'/'.join(p['codecs']) or 'no track'}, "
              f"+{grew / 1e6:.1f} MB in {secs:.0f} s")
    return c.row(spec)


def _s67b(ctx: Context) -> Row:
    spec = STAGE.spec("S6.7b")
    if shutil.which("ffprobe") is None:
        return amber(spec, "ffprobe is not installed on this laptop (brew install ffmpeg)")
    natives = ctx.facts.get("video_natives") or {}
    if not natives:
        return amber(spec, "no video paths (S6.6)")
    try:
        lport = ctx.session.forward("127.0.0.1", RTSP_PORT)
    except Exception as e:  # noqa: BLE001
        return amber(spec, ctx.redact(f"RTSP tunnel failed: {e}"))
    want = ctx.release.get("camera.streams") or {}
    c = Checks()
    for sensor in ("main", "thermal"):
        paths = natives.get(sensor) or []
        if not paths:
            c.add(sensor, False, "no native path")
            continue
        r = run_local(["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "v:0",
                       "-read_intervals", "%+5", "-count_frames", "-show_entries",
                       "stream=width,height,nb_read_frames", "-of", "json",
                       f"rtsp://127.0.0.1:{lport}/{paths[0]}"], timeout=40)
        frames, res = ffprobe_frames(r.out)
        if not frames:
            c.add(sensor, False, "no frames decoded" + (f" ({r.err.strip().splitlines()[-1][:80]})" if r.err.strip() else ""))
            continue
        exp = want.get(sensor)
        ok_res = (res == exp) if exp else True
        c.add(sensor, frames >= 25 and ok_res,
              f"{frames} frames in 5 s at {res}" + ("" if ok_res else f" (release.yaml wants {exp})"))
    return c.row(spec)


def _s68(ctx: Context) -> Row:
    spec = STAGE.spec("S6.8")
    smp = _sample(ctx)

    def total(text: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for labels, v in prom_samples(text, "kela_pose_readings_total"):
            if labels.get("outcome") == "accepted" and labels.get("entity_id"):
                out[labels.get("sensor_id", "")] = out.get(labels.get("sensor_id", ""), 0) + v
        return out

    b, e = total(smp["before"]["metrics"]), total(smp["after"]["metrics"])
    if not e:
        return amber(spec, "kela_pose_readings_total not exposed by the int-gotcha pod")
    secs = smp["after"]["t"] - smp["before"]["t"]
    c = Checks()
    for sensor, v in sorted(e.items()):
        grew = v - b.get(sensor, v)
        c.add(sensor, grew > 0, f"+{grew:.0f} accepted pose readings in {secs:.0f} s")
    return c.row(spec)


def _speaker(ctx: Context) -> Any:
    if "_s6_spk" not in ctx.facts:
        out: dict = {}
        with socks_http.client(ctx.session.socks_url) as http:
            spk = Speaker(http, ctx.release.plan.speaker, ctx.creds.device("speaker"))
            try:
                spk.login()
                out["music"] = spk.get("musicfile.get")
                try:
                    out["audio"] = spk.get("audio.get")
                except Exception as e:  # noqa: BLE001
                    out["audio_error"] = ctx.redact(friendly_error(e))
            except Exception as e:  # noqa: BLE001 — an unreachable speaker is a row result
                out["error"] = ctx.redact(friendly_error(e))
        ctx.facts["_s6_spk"] = out
    return ctx.facts["_s6_spk"]


def _volume(audio: dict) -> Optional[int]:
    """outvolume in either the flat or the nested shape (as the driver's extract_volume_percent)."""
    for d in (audio, audio.get("audio") if isinstance(audio.get("audio"), dict) else None):
        if isinstance(d, dict) and "outvolume" in d:
            try:
                return int(float(d["outvolume"]))
            except (TypeError, ValueError):
                return None
    return None


def _s69(ctx: Context) -> Row:
    spec = STAGE.spec("S6.9")
    s = _speaker(ctx)
    if s.get("error"):
        return result(spec, s["error"], False)
    files = s.get("music", {}).get("musicfile") or []
    names = [f.get("name") or f.get("filename") or str(f) for f in files if f] if isinstance(files, list) else []
    return result(spec, f"login ok; {len(names)} file(s): {', '.join(names[:5])}" if names else "login ok; no audio files loaded",
                  bool(names))


def _s613(ctx: Context) -> Row:
    spec = STAGE.spec("S6.13")
    s = _speaker(ctx)
    if s.get("error"):
        return amber(spec, "speaker unreachable (S6.9)")
    if s.get("audio_error"):
        return result(spec, s["audio_error"], False)
    vol = _volume(s.get("audio") or {})
    want = ctx.release.pin("speaker.outvolume_min")
    if vol is None:
        return result(spec, "audio.get returned no outvolume", False)
    if want is None:
        return result(spec, f"outvolume {vol} (no minimum pinned)", True)
    return result(spec, f"outvolume {vol} (min {want})", vol >= float(want))


def _status_of(ctx: Context, ip: str, family: str) -> Any:
    cache = ctx.facts.setdefault("_s6_status", {})
    if ip not in cache:
        with socks_http.client(ctx.session.socks_url) as http:
            m = Magos(http, ip, ctx.creds.device(family))
            try:
                m.login()
                cache[ip] = m.system_status()
            except Exception as e:  # noqa: BLE001 — an unreachable device is a row result
                cache[ip] = ctx.redact(friendly_error(e))
    return cache[ip]


def _self_status(st: dict, want_ip: str, thresholds: dict, server: str, apu: Optional[str] = None) -> list[tuple]:
    """[(label, ok, actual)] for one Magos device's systemStatus."""
    out = []
    alerts = st.get("alerts") or []
    out.append(("alerts", not alerts, "none" if not alerts else "; ".join(
        str(a.get("message") if isinstance(a, dict) else a) for a in alerts)))
    sp = st.get("ethernetSpeed")
    out.append(("ethernet", sp == thresholds["magos_ethernet_speed"], f"{sp} Mb/s"))
    p1 = (st.get("netInterfaces") or {}).get("port1") or {}
    out.append(("port1", p1.get("deviceState") == "CONNECTED" and p1.get("ip4Address") == want_ip,
                f"{p1.get('deviceState', '—')} at {p1.get('ip4Address', '—')}"))
    target = str(st.get("timeSyncTarget") or "")
    off = st.get("timeSyncOffset")
    ok_t = bool(st.get("timeSyncOk")) and st.get("timeSyncDetail") == "synced" and server in target
    if isinstance(off, (int, float)):
        ok_t = ok_t and abs(off) < thresholds["magos_time_sync_offset_s"]
    out.append(("time", ok_t, f"{st.get('timeSyncDetail', '—')} to {target.strip('<>') or '—'}"
                + (f", offset {off * 1000:+.1f} ms" if isinstance(off, (int, float)) else "")))
    if apu:
        clients = [c.get("ip") for c in st.get("applClientList") or [] if isinstance(c, dict)]
        out.append(("client", apu in clients, ("APU " + apu) if apu in clients else f"clients: {', '.join(clients) or 'none'}"))
    temps = [t for t in st.get("perfTemperature") or [] if isinstance(t, (int, float))]
    if temps:
        out.append(("temperature", max(temps) < thresholds["magos_temp_max_c"], f"max {max(temps):g} °C"))
    return out


def _s610(ctx: Context) -> Row:
    spec = STAGE.spec("S6.10")
    plan, th = ctx.release.plan, ctx.release.thresholds
    c = Checks()
    for rid, ip in plan.radars.items():
        st = _status_of(ctx, ip, "magos")
        tag = f"{rid} .{ip.rsplit('.', 1)[1]}"
        if isinstance(st, str):
            c.add(tag, False, st)
            continue
        for label, ok, actual in _self_status(st, ip, th, plan.server, plan.apu_for(rid)):
            c.add(f"{tag} {label}", ok, actual)
    return c.row(spec)


def _s611(ctx: Context) -> Row:
    spec = STAGE.spec("S6.11")
    plan, th = ctx.release.plan, ctx.release.thresholds
    c = Checks()
    for ip in plan.apus:
        st = _status_of(ctx, ip, "apu")
        tag = f"APU .{ip.rsplit('.', 1)[1]}"
        if isinstance(st, str):
            c.add(tag, False, st)
            continue
        for label, ok, actual in _self_status(st, ip, th, plan.server):
            c.add(f"{tag} {label}", ok, actual)
    return c.row(spec)


def _s612(ctx: Context) -> Row:
    spec = STAGE.spec("S6.12")
    base = ctx.facts.get("magos_status") or {}
    if not base:
        return amber(spec, "no S0.6 baseline (device discovery did not read the radars / APUs)")
    plan = ctx.release.plan
    c = Checks()
    for ip in [*plan.radars.values(), *plan.apus]:
        b, now = base.get(ip), ctx.facts.get("_s6_status", {}).get(ip)
        tag = f".{ip.rsplit('.', 1)[1]}"
        if not isinstance(b, dict) or not isinstance(now, dict):
            c.add(tag, None, "no reading at S0.6 or now")
            continue
        d = {k: int(now.get(k) or 0) - int(b.get(k) or 0)
             for k in ("ethernetCarrierDown", "ethernetRXErrors", "ethernetTXErrors")}
        errs = d["ethernetRXErrors"] + d["ethernetTXErrors"]
        c.add(tag, d["ethernetCarrierDown"] == 0,
              f"carrier down +{d['ethernetCarrierDown']} (total {now.get('ethernetCarrierDown')})"
              + (f", RX/TX errors +{errs} (recorded)" if errs else ""))
    return c.row(spec)


def run(ctx: Context) -> Iterator[Row]:
    try:
        yield from guarded(ctx, STAGE, (_s61, _s62, _s63a, _s63b, _s63c, _s63d, _s64, _s65, _s66, _s67a,
                                        _s67b, _s68, _s69, _s613, _s610, _s611, _s612))
    finally:
        for k in ("_s6", "_s6_asset", "_s6_spk", "_s6_status"):
            ctx.facts.pop(k, None)
