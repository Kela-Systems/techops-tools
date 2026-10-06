"""S5 — Hub config vs plan. What the hub holds for the unit, against
release.yaml's plan and the hub's own rules.

Sources (all reads):
  * the hub DB (kubectl exec psql, user kela): the Gotcha integration, its
    manifest and integration config
  * DeviceService.ListDevices(integration_id): the device — setup_info
    (radars, credentials, pose, camera, speaker) and its calibrations
  * ConfigService.GetConfig: hub.map → initial_position, hub.projection →
    dem_radius_km (the old site_configs table is gone; its sections live under
    their own config owners)
  * each APU's /apu/v1/settings (dashboard login, else HTTP Basic auth — the
    way the driver reaches it; [devices.apu], falling back to the radar login)

The driver names radar sensors by position: the N-th entry of
setup_info.radars is sensor radar-N (kela_gotcha/radar.py radar_sensor_id).
"""
from __future__ import annotations

import json
import math
from typing import Any, Iterator, Optional
from urllib.parse import urlparse

from ..access import socks_http
from ..context import Context
from ..devices import DeviceError, Magos
from ..model import Checks, Row, RowSpec, StageSpec, amber, result
from ._common import grpc_error, guarded, hub

STAGE = StageSpec(
    id="S5", name="Hub config vs plan", depends_on=("S3.5",),
    vantage="hub gRPC + psql (kubectl exec) + APU API",
    rows=(
        RowSpec("S5.1", "Gotcha integration and device exist",
                "exactly one Gotcha integration with exactly one device", "critical"),
        RowSpec("S5.2", "Radar array matches plan", "4 radars; radar_0/1 → .60; radar_2/3 → .61",
                "critical", "read-back", depends_on=("S5.1",)),
        RowSpec("S5.3", "APU → radar assignment matches plan",
                "radar_0→.50 radar_1→.51 on .60; radar_2→.52 radar_3→.53 on .61",
                "critical", "read-back"),
        RowSpec("S5.3b", "APU radar filters disabled",
                "range_gates and detector_threshold null on all 4 radar entries (both APUs)",
                "high", "read-back"),
        RowSpec("S5.4", "Radar credentials set", "setup_info.radar_credentials userName and password non-empty",
                "critical", "read-back", depends_on=("S5.1",)),
        RowSpec("S5.5", "Declared pan angles are sweep-assignable",
                "4 radars → 4 distinct quadrants, no cardinal values; offset from nominal recorded",
                "high", "read-back", depends_on=("S5.1",)),
        RowSpec("S5.10", "Radar sensor-to-asset calibration consistent (if present)",
                "revision 0 → amber 'not calibrated'; revision > 0 → all 4 radars SOLVED/MANUAL, "
                "applied == revision, no lags, tilt spread ≤ 15°; solved yaw vs declared pan recorded",
                "medium", "read-back", depends_on=("S5.1",)),
        RowSpec("S5.6", "Unit pose present (recorded, not judged)",
                "lat/lon/alt/heading present and finite; flagged when still the site-seed placeholder",
                "low", "read-back", depends_on=("S5.1",)),
        RowSpec("S5.7", "Camera and speaker addresses",
                "camera at plan.camera (NTP .10 or the driver default); speaker at plan.speaker",
                "high", "read-back", depends_on=("S5.1",)),
        RowSpec("S5.8", "Site config sane", "hub.map initial_position set; hub.projection dem_radius_km > 0",
                "high", "read-back"),
        RowSpec("S5.9", "Integration config at expected values",
                "== release.yaml.hub.integration_config (stored value, else the manifest default)",
                "medium", "read-back", depends_on=("S5.1",)),
    ),
)

PSQL = "sudo -n k3s kubectl -n kela exec postgresql-0 -c postgresql -- psql -U kela -d kela -At -c"
INTEGRATIONS_SQL = (
    "select coalesce(json_agg(json_build_object('id', i.id, 'manifest', m.name, 'version', m.version, "
    "'config', i.integration_config, 'schema', m.integration_config_schema)), '[]') "
    "from integrations i join manifests m on m.id = i.manifest_id")
# hub/site-seed's placeholder pose (canvas S5.6): every unit starts here.
SEED_POSE = (32.8667, 35.7034, 325.0, 0.0)
FILTER_FIELDS = (("range_gates", "range gates"), ("detector_threshold", "detector threshold"))
CAL_SOURCES_OK = ("CALIBRATION_SOURCE_SOLVED", "CALIBRATION_SOURCE_MANUAL")


# ── pure helpers (covered by tests/test_s5.py) ──────────────────────────────

def pan_quadrants(pans: dict[str, float]) -> tuple[dict[str, int], list[str]]:
    """{radar: quadrant 0–3} and the radars sitting on a cardinal heading —
    C2's radarPanelOrder() rule: each radar in its own 90° sector, none on
    0/90/180/270 where the sector is ambiguous."""
    quad, cardinal = {}, []
    for rid, pan in pans.items():
        p = pan % 360
        if math.isclose(p % 90, 0, abs_tol=1e-6) or math.isclose(p % 90, 90, abs_tol=1e-6):
            cardinal.append(rid)
        quad[rid] = int(p // 90) % 4
    return quad, cardinal


def angle_diff(a: float, b: float) -> float:
    """Signed smallest difference a − b in degrees, in (−180, 180]."""
    d = (a - b) % 360
    return d - 360 if d > 180 else d


def quat_ypr(q: dict) -> tuple[float, float, float]:
    """Yaw, pitch, roll (degrees, ZYX) of a {x, y, z, w} quaternion."""
    x, y, z, w = (float(q.get(k, 0.0)) for k in ("x", "y", "z", "w"))
    yaw = math.degrees(math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)))
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x)))))
    roll = math.degrees(math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y)))
    return yaw, pitch, roll


def effective_config(stored: Optional[dict], schema: Optional[dict], key: str) -> tuple[Any, str]:
    """(value, where it came from): the stored integration config, else the
    manifest schema default, else unset."""
    if stored and key in stored:
        return stored[key], "set"
    props = (schema or {}).get("properties") or {}
    if key in props and "default" in props[key]:
        return props[key]["default"], "default"
    return None, "unset"


def _host(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""


# ── data, read once per stage ───────────────────────────────────────────────

def _data(ctx: Context) -> dict:
    if "_s5" in ctx.facts:
        return ctx.facts["_s5"]
    d: dict = {"error": "", "integrations": [], "gotcha": [], "devices": []}
    r = ctx.server(f"{PSQL} \"{INTEGRATIONS_SQL}\"", timeout=40)
    try:
        d["integrations"] = json.loads(r.text) if r.ok else []
        if not r.ok:
            d["error"] = "hub DB: " + ctx.redact((r.text.splitlines() or [f"rc {r.rc}"])[-1][:140])
    except ValueError:
        d["error"] = "hub DB returned no JSON"
    d["gotcha"] = [i for i in d["integrations"] if "gotcha" in str(i.get("manifest", "")).lower()]
    if len(d["gotcha"]) == 1:
        from google.protobuf.json_format import MessageToDict
        from ..access.grpc import pb
        try:
            dpb = pb("kela.device.v1alpha1.device_pb2")
            resp = hub(ctx).stub("kela.device.v1alpha1.device_pb2_grpc", "DeviceServiceStub").ListDevices(
                dpb.ListDevicesRequest(integration_id=d["gotcha"][0]["id"]), timeout=20)
            d["devices"] = [MessageToDict(x, preserving_proto_field_name=True) for x in resp.devices]
        except Exception as e:  # noqa: BLE001
            d["error"] = "ListDevices: " + ctx.redact(grpc_error(e))
    for si in (x.get("setup_info") or {} for x in d["devices"]):
        for k in ("password",):
            if isinstance(si.get("radar_credentials"), dict) and si["radar_credentials"].get(k):
                ctx.redact.add(str(si["radar_credentials"][k]))
            for part in ("camera", "speaker"):
                if isinstance(si.get(part), dict) and si[part].get(k):
                    ctx.redact.add(str(si[part][k]))
    ctx.facts["_s5"] = d
    if len(d["devices"]) == 1:
        ctx.facts["gotcha_device"] = d["devices"][0]
    return d


def _device(ctx: Context) -> Optional[dict]:
    d = _data(ctx)
    return d["devices"][0] if len(d["devices"]) == 1 else None


def _apu_settings(ctx: Context) -> dict[str, Any]:
    """{apu ip: settings dict | error string}, read once."""
    if "_s5_apu" in ctx.facts:
        return ctx.facts["_s5_apu"]
    out: dict[str, Any] = {}
    with socks_http.client(ctx.session.socks_url) as http:
        for ip in ctx.release.plan.apus:
            m = Magos(http, ip, ctx.creds.device("apu"))
            try:
                m.login()
                out[ip] = m.apu_settings()
            except Exception as e:  # noqa: BLE001 — an unreachable APU is a row result
                from ..discovery import friendly_error
                msg = ctx.redact(friendly_error(e) if not isinstance(e, DeviceError) else str(e))
                if "login refused" in msg:
                    msg += " — check the radar login in [devices.magos] (or set [devices.apu]) in ~/.config/gotcha-atp/config.toml"
                out[ip] = msg
    ctx.facts["_s5_apu"] = out
    return out


# ── rows ────────────────────────────────────────────────────────────────────

def _s51(ctx: Context) -> Row:
    spec = STAGE.spec("S5.1")
    d = _data(ctx)
    if d["error"] and not d["gotcha"]:
        return amber(spec, d["error"])
    c = Checks()
    g = d["gotcha"]
    c.add("Gotcha integration", len(g) == 1,
          f"{g[0]['manifest']} {g[0].get('version') or ''} ({g[0]['id'][:8]})" if len(g) == 1
          else f"{len(g)} found" + (f": {', '.join(x['manifest'] for x in g)}" if g else ""))
    if len(g) == 1:
        if d["error"]:
            c.add("device", None, d["error"])
        else:
            c.add("device", len(d["devices"]) == 1,
                  f"{d['devices'][0].get('name')} ({d['devices'][0].get('id', '')[:8]})" if len(d["devices"]) == 1
                  else f"{len(d['devices'])} devices")
    return c.row(spec)


def _need_device(ctx: Context, spec: RowSpec) -> Optional[Row]:
    return None if _device(ctx) else amber(spec, "no single Gotcha device (S5.1)")


def _s52(ctx: Context) -> Row:
    spec = STAGE.spec("S5.2")
    if (r := _need_device(ctx, spec)):
        return r
    radars = (_device(ctx).get("setup_info") or {}).get("radars") or []
    plan = ctx.release.plan
    c = Checks()
    c.add("count", len(radars) == 4, f"{len(radars)} radars")
    for i, rd in enumerate(radars, 1):
        rid = str(rd.get("instanceId") or f"#{i}")
        want = plan.apu_for(rid)
        got = rd.get("IpAddress")
        c.add(rid, (got == want) if want else False,
              f"→ {got}" + ("" if got == want else f" (want {want})" if want else " (not in the plan)"))
    return c.row(spec)


def _s53(ctx: Context) -> Row:
    spec = STAGE.spec("S5.3")
    plan = ctx.release.plan
    c = Checks()
    for ip, want_ids in plan.apus.items():
        st = _apu_settings(ctx)[ip]
        if isinstance(st, str):
            c.add(f"APU .{ip.rsplit('.', 1)[1]}", None, st)
            continue
        entries = st.get("radars") if isinstance(st, dict) else None
        if not isinstance(entries, list):
            c.add(f"APU .{ip.rsplit('.', 1)[1]}", None, "does not report its radar settings")
            continue
        got = {str(e.get("radar_id")): _host(str(e.get("remote_base_url") or "")) for e in entries if isinstance(e, dict)}
        for rid in want_ids:
            want = plan.radars[rid]
            c.add(f"{rid} on .{ip.rsplit('.', 1)[1]}", got.get(rid) == want,
                  f"→ {got.get(rid) or 'not assigned'}" + ("" if got.get(rid) == want else f" (want {want})"))
        extra = sorted(set(got) - set(want_ids))
        if extra:
            c.add(f"APU .{ip.rsplit('.', 1)[1]} extra", False, "also controls " + ", ".join(extra))
    return c.row(spec)


def _s53b(ctx: Context) -> Row:
    spec = STAGE.spec("S5.3b")
    c = Checks()
    for ip in ctx.release.plan.apus:
        st = _apu_settings(ctx)[ip]
        label = f"APU .{ip.rsplit('.', 1)[1]}"
        entries = st.get("radars") if isinstance(st, dict) else None
        if isinstance(st, str) or not isinstance(entries, list):
            c.add(label, None, st if isinstance(st, str) else "does not report its radar settings")
            continue
        on = [f"{e.get('radar_id')} {name}={e[key]}" for e in entries if isinstance(e, dict)
              for key, name in FILTER_FIELDS if e.get(key) is not None]
        c.add(label, not on, "disabled on all radars" if not on else "set: " + ", ".join(on))
    return c.row(spec)


def _s54(ctx: Context) -> Row:
    spec = STAGE.spec("S5.4")
    if (r := _need_device(ctx, spec)):
        return r
    rc = (_device(ctx).get("setup_info") or {}).get("radar_credentials") or {}
    c = Checks()
    c.add("userName", bool(rc.get("userName")), rc.get("userName") or "empty")
    c.add("password", bool(rc.get("password")), "set" if rc.get("password") else "empty")
    return c.row(spec)


def _s55(ctx: Context) -> Row:
    spec = STAGE.spec("S5.5")
    if (r := _need_device(ctx, spec)):
        return r
    radars = (_device(ctx).get("setup_info") or {}).get("radars") or []
    pans = {str(rd.get("instanceId") or f"#{i}"): float(rd.get("pan", 0.0)) for i, rd in enumerate(radars, 1)}
    quad, cardinal = pan_quadrants(pans)
    nominal = ctx.release.plan.pan_nominal_deg
    c = Checks()
    dup = len(set(quad.values())) != len(quad)
    c.add("quadrants", len(quad) == 4 and not dup,
          "4 distinct" if len(quad) == 4 and not dup else f"{len(set(quad.values()))} distinct for {len(quad)} radars")
    for rid, pan in pans.items():
        off = f", {angle_diff(pan, nominal[rid]):+.0f}° from nominal" if rid in nominal else ""
        c.add(rid, rid not in cardinal, f"pan {pan:g}° (Q{quad[rid] + 1}{off})" + (" — on a cardinal heading" if rid in cardinal else ""))
    return c.row(spec, pans=pans)


def _s510(ctx: Context) -> Row:
    spec = STAGE.spec("S5.10")
    if (r := _need_device(ctx, spec)):
        return r
    dev = _device(ctx)
    rev = int(dev.get("calibration_revision") or 0)
    if rev == 0:
        return amber(spec, "not calibrated — the sensor-to-asset sweep has not been run on this unit")
    radars = (dev.get("setup_info") or {}).get("radars") or []
    cals = dev.get("sensor_to_asset_calibrations") or {}
    applied = dev.get("applied_calibration_revision")
    c = Checks()
    c.add("applied revision", applied is not None and int(applied) == rev, f"{applied} / {rev}")
    lags = dev.get("calibration_lags") or []
    c.add("lags", not lags, "none" if not lags else ", ".join(f"{x.get('sensor_id')} {x.get('reason', '')}" for x in lags))
    pitches = []
    for i, rd in enumerate(radars, 1):
        sid, rid = f"radar-{i}", str(rd.get("instanceId") or f"#{i}")
        cal = cals.get(sid)
        if not cal:
            c.add(sid, False, f"no calibration ({rid})")
            continue
        yaw, pitch, roll = quat_ypr(cal.get("rotation_sensor_to_asset") or {})
        pitches.append(pitch)
        src = cal.get("source", "CALIBRATION_SOURCE_UNSPECIFIED")
        c.add(sid, src in CAL_SOURCES_OK,
              f"{src.replace('CALIBRATION_SOURCE_', '').lower()} · yaw {yaw:.1f}° (declared pan {float(rd.get('pan', 0)):g}°) "
              f"· pitch {pitch:.1f}° · roll {roll:.1f}°")
    if len(pitches) >= 2:
        spread = max(pitches) - min(pitches)
        limit = ctx.release.thresholds["calib_tilt_spread_max_deg"]
        c.add("tilt spread", spread <= limit, f"{spread:.1f}° (limit {limit}°)")
    return c.row(spec, calibration_revision=rev)


def _s56(ctx: Context) -> Row:
    spec = STAGE.spec("S5.6")
    if (r := _need_device(ctx, spec)):
        return r
    si = _device(ctx).get("setup_info") or {}
    vals = {k: si.get(k) for k in ("latitude", "longitude", "altitude", "heading")}
    missing = [k for k, v in vals.items() if not isinstance(v, (int, float)) or not math.isfinite(v)]
    if missing:
        return result(spec, "missing: " + ", ".join(missing), False)
    seed = all(math.isclose(float(vals[k]), s, abs_tol=1e-4)
               for k, s in zip(("latitude", "longitude", "altitude", "heading"), SEED_POSE))
    text = f"{vals['latitude']:.5f}, {vals['longitude']:.5f}, alt {vals['altitude']:.1f} m, heading {vals['heading']:g}°"
    return result(spec, text + (" — still the site-seed placeholder" if seed else ""), True)


def _s57(ctx: Context) -> Row:
    spec = STAGE.spec("S5.7")
    if (r := _need_device(ctx, spec)):
        return r
    si = _device(ctx).get("setup_info") or {}
    plan = ctx.release.plan
    cam, spk = si.get("camera") or {}, si.get("speaker") or {}
    c = Checks()
    c.add("camera host", cam.get("host") == plan.camera, f"{cam.get('host') or 'unset'}" +
          ("" if cam.get("host") == plan.camera else f" (want {plan.camera})"))
    ntp = cam.get("ntp_server_host")
    if ntp:
        c.add("camera NTP", ntp == plan.server, ntp + ("" if ntp == plan.server else f" (want {plan.server})"))
    else:
        c.items.append({"label": "camera NTP", "ok": True, "actual": "not set — the driver uses the node's address"})
    c.add("speaker host", spk.get("host") == plan.speaker, f"{spk.get('host') or 'unset'}" +
          ("" if spk.get("host") == plan.speaker else f" (want {plan.speaker})"))
    return c.row(spec)


def _get_config(ctx: Context, owner: str, msg_module: str, msg_name: str):
    """(decoded message | None when no entry, error text)."""
    from ..access.grpc import pb
    try:
        cpb = pb("kela.config.v1alpha1.config_pb2")
        resp = hub(ctx).stub("kela.config.v1alpha1.config_pb2_grpc", "ConfigServiceStub").GetConfig(
            cpb.GetConfigRequest(config_owner=owner), timeout=15)
    except Exception as e:  # noqa: BLE001
        return None, ctx.redact(grpc_error(e))
    if not resp.HasField("entry"):
        return None, ""
    msg = getattr(pb(msg_module), msg_name)()
    resp.entry.payload.Unpack(msg)
    return msg, ""


def _s58(ctx: Context) -> Row:
    spec = STAGE.spec("S5.8")
    c = Checks()
    mp, err = _get_config(ctx, "hub.map", "kela.map.v1alpha1.map_config_pb2", "MapConfig")
    if err:
        c.add("initial position", None, f"hub.map unreadable: {err}")
    elif mp is not None and mp.HasField("initial_position"):
        p = mp.initial_position
        c.add("initial position", True, f"{p.latitude_degrees:.5f}, {p.longitude_degrees:.5f}")
    else:
        c.add("initial position", False, "not set (hub.map) — the map has no site center and DEM has no center")
    pj, err = _get_config(ctx, "hub.projection", "kela.projection.v1alpha1.projection_config_pb2", "ProjectionConfig")
    if err:
        c.add("DEM radius", None, f"hub.projection unreadable: {err}")
    else:
        explicit = pj is not None and pj.HasField("dem_radius_km")
        km = pj.dem_radius_km if explicit else 5.0
        c.add("DEM radius", km > 0, f"{km:g} km" + ("" if explicit else " (default)") +
              (" — 0 downloads the whole QGIS layer" if km == 0 else ""))
    return c.row(spec)


def _s59(ctx: Context) -> Row:
    spec = STAGE.spec("S5.9")
    if (r := _need_device(ctx, spec)):
        return r
    g = _data(ctx)["gotcha"][0]
    want = ctx.release.get("hub.integration_config") or {}
    c = Checks()
    for key, expected in want.items():
        value, where = effective_config(g.get("config"), g.get("schema"), key)
        c.add(key, value == expected if where != "unset" else None,
              f"{json.dumps(value)}" + (" (default)" if where == "default" else "" if where == "set" else " — not in the manifest")
              + ("" if value == expected else f" (want {json.dumps(expected)})"))
    return c.row(spec)


def run(ctx: Context) -> Iterator[Row]:
    try:
        yield from guarded(ctx, STAGE, (_s51, _s52, _s53, _s53b, _s54, _s55, _s510, _s56, _s57, _s58, _s59))
    finally:
        ctx.facts.pop("_s5", None)
        ctx.facts.pop("_s5_apu", None)
