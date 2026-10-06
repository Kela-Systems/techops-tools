"""S7 — Time & latency.

Clocks are compared against the server (the unit's NTP source, .10). The
laptop reads each device and the server through the session, so every offset
carries a measurement uncertainty: half the round-trip plus half the device's
clock resolution (most report whole seconds; the Teltonika devices are read at
the tick of their second, see SECOND_EDGE). A sub-check passes when the
offset is inside the limit even at the edge of that uncertainty, fails when it
is outside even at the other edge, and is "not checked" in between — a 1 s
clock read over a 0.4 s link cannot prove a 1.1 s offset either way.

Radars and APUs measure their own offset against .10 (systemStatus
timeSyncOffset, read in S0.6); those are judged directly.

Field findings folded in (gotcha-rev3-dev, 2026-10-05):
  * the camera's GET /v1/system/local/time is wall time plus "Timezone":
    "GMT+03:00"
  * `chronyc clients` "Last" ages a client that left long ago (a stale .47 at
    488 min), so S7.2 judges recency against each client's own poll interval
  * hub_grpc_request_duration_seconds includes health checks, Watch* streams
    and ExecuteTask (PTZ, device-bound, p95 1.65 s) — excluded from S7.3
"""
from __future__ import annotations

import json
import re
import statistics
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Iterator, Optional
from urllib.parse import quote

from ..access import socks_http
from ..context import Context
from ..devices import Raythink
from ..discovery import friendly_error
from ..model import Checks, Row, RowSpec, StageSpec, amber
from ._common import guarded
from .s1 import _targets, parallel_script, parse_blocks, parse_ping
from .s6 import MTX_API

STAGE = StageSpec(
    id="S7", name="Time & latency", depends_on=("S0.3",),
    vantage="server + devices (SOCKS / SSH on the server session) + operator",
    rows=(
        RowSpec("S7.1", "NTP offset matrix",
                "every device within release.yaml ntp_offset_s of the server; radars/APUs "
                "|self-reported offset| < magos_time_sync_offset_s", "high"),
        RowSpec("S7.2", "Devices are syncing from the server",
                "APUs, radars, camera, speaker, router, TSW202, IGS-4215 in chronyc clients, "
                "last request within 2× their poll interval", "medium"),
        RowSpec("S7.3", "Hub API responsiveness",
                "unary gRPC p95 (15 min) < hub_api_p95_ms; kela.local TTFB median < hub_ttfb_ms",
                "medium"),
        RowSpec("S7.4", "LAN round-trip while streaming",
                "S1.1 thresholds while both camera streams flow into MediaMTX", "medium"),
        RowSpec("S7.5", "Video glass-to-glass", "< 1 s", "low", "manual",
                prompt="Wave a hand in front of the camera and watch the C2 tile. "
                       "Is the delay under about one second?"),
    ),
)

API_EXCLUDE = r"grpc\.health.*|.*/Watch.*|.*/ExecuteTask"   # PromQL raw (`…`) string
API_WINDOW = "15m"
TTFB_SAMPLES = 5
MIN_STALE_S = 600
# RutOS `date` has no %N: wait for the device's second to tick over, then print
# it — the reply leaves the device at N.000, so the read is good to the link's
# half round-trip instead of ±0.5 s. Runs ≤ 1 s, changes nothing.
SECOND_EDGE = 's=$(date +%s); while [ "$(date +%s)" = "$s" ]; do :; done; date +%s'
EDGE_SLACK_S = 0.03
CLOCK_READS_MAX = 15      # a 1 s web clock is read back-to-back until the reads span > 1 s (one tick)


# ── pure helpers (covered by tests/test_s7.py) ──────────────────────────────

def judge(offset: float, uncertainty: float, limit: float) -> Optional[bool]:
    """True when |offset| ≤ limit whatever the error, False when it is over the
    limit whatever the error, None when the measurement cannot tell."""
    if abs(offset) + uncertainty <= limit:
        return True
    if abs(offset) - uncertainty > limit:
        return False
    return None


def bound_offset(samples: list[tuple[float, float, float]], resolution: float) -> Optional[tuple[float, float]]:
    """(offset, ± half-width) of a device clock vs the laptop from reads
    [(sent, received, device value)]. Each read says the device showed
    `value` (truncated to `resolution`) at some instant between sent and
    received, so offset ∈ [value − received, value + resolution − sent];
    intersecting several reads that straddle a tick narrows a 1 s clock to
    about one round-trip. None when the reads contradict each other."""
    if not samples:
        return None
    lo = max(v - b for a, b, v in samples)
    hi = min(v + resolution - a for a, b, v in samples)
    if lo > hi:
        return None
    return (lo + hi) / 2, (hi - lo) / 2


def fmt_offset(offset: float, uncertainty: float) -> str:
    return f"{offset:+.2f} s (±{uncertainty:.2f})"


def camera_epoch(t: dict) -> Optional[float]:
    """Raythink {Year..Second, Timezone: 'GMT+03:00'} → epoch seconds."""
    try:
        m = re.fullmatch(r"(?:GMT|UTC)?\s*([+-])(\d{1,2}):?(\d{2})?", str(t.get("Timezone") or "+00:00").strip())
        sign, hh, mm = (m.group(1), int(m.group(2)), int(m.group(3) or 0)) if m else ("+", 0, 0)
        tz = timezone((1 if sign == "+" else -1) * timedelta(hours=hh, minutes=mm))
        return datetime(int(t["Year"]), int(t["Month"]), int(t["Day"]), int(t["Hour"]),
                        int(t["Minute"]), int(t["Second"]), tzinfo=tz).timestamp()
    except (KeyError, TypeError, ValueError):
        return None


def http_date_epoch(value: Optional[str]) -> Optional[float]:
    try:
        return parsedate_to_datetime(value).timestamp() if value else None
    except (TypeError, ValueError):
        return None


def chrony_last_s(text: str) -> Optional[float]:
    """chronyc 'Last' column: '15', '27m', '4h', '3d', '-' → seconds."""
    m = re.fullmatch(r"(\d+)([smhdy]?)", (text or "").strip())
    if not m:
        return None
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "y": 31536000}[m.group(2)]


def parse_chrony_clients(text: str) -> dict[str, dict]:
    """{ip: {ntp, int_log2, last_s}} from `chronyc -n clients`."""
    out = {}
    for line in text.splitlines():
        cols = line.split()
        if len(cols) >= 6 and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", cols[0]):
            out[cols[0]] = {"ntp": int(cols[1]) if cols[1].isdigit() else 0,
                            "int_log2": int(cols[3]) if cols[3].lstrip("-").isdigit() else None,
                            "last_s": chrony_last_s(cols[5])}
    return out


def client_fresh(entry: dict) -> bool:
    """The client asked recently enough for its own poll interval."""
    last = entry.get("last_s")
    if last is None or not entry.get("ntp"):
        return False
    interval = 2 ** entry["int_log2"] if entry.get("int_log2") is not None else 1024
    return last <= max(2 * interval, MIN_STALE_S)


# ── clock reads ─────────────────────────────────────────────────────────────

def _timed(read: Callable[[], Optional[float]]) -> tuple[Optional[float], float, float]:
    """(device epoch, laptop midpoint, round-trip) around one read."""
    t0 = time.time()
    value = read()
    t1 = time.time()
    return value, (t0 + t1) / 2, t1 - t0


def _round_trip(fn: Callable[[], object]) -> float:
    t0 = time.time()
    fn()
    return time.time() - t0


def _server_offset(ctx: Context) -> tuple[float, float]:
    """(server clock − laptop clock, uncertainty) — the best of three reads."""
    best: Optional[tuple[float, float]] = None
    for _ in range(3):
        value, mid, rtt = _timed(lambda: float(ctx.server("date +%s.%N", 15).text))
        if best is None or rtt / 2 < best[1]:
            best = (value - mid, rtt / 2)
    return best  # type: ignore[return-value]


def _clock_rows(ctx: Context) -> list[tuple[str, Optional[float], float, str]]:
    """[(label, device epoch − server epoch, uncertainty, note)] for every
    device whose clock the laptop reads; None offset = unreadable (note says why)."""
    plan = ctx.release.plan
    srv_off, srv_u = _server_offset(ctx)
    out: list[tuple[str, Optional[float], float, str]] = []
    unreachable = {p["ip"]: p["error"] for p in ctx.facts.get("inventory") or [] if p.get("error")}

    def skip(label: str, ip: str) -> bool:
        if ip in unreachable:
            out.append((label, None, 0.0, f"not reachable in S0.6 ({unreachable[ip]})"))
            return True
        return False

    def add(label: str, read: Callable[[], Optional[float]], resolution: float, how: str,
            ip: str = "") -> None:
        if ip and skip(label, ip):
            return
        samples: list[tuple[float, float, float]] = []
        while not samples or (resolution and len(samples) < CLOCK_READS_MAX
                              and samples[-1][1] - samples[0][0] < resolution + 0.1):
            try:
                t0 = time.time()
                value = read()
                t1 = time.time()
            except Exception as e:  # noqa: BLE001 — one unreadable clock is a sub-check
                out.append((label, None, 0.0, ctx.redact(friendly_error(e))))
                return
            if value is None:
                out.append((label, None, 0.0, f"no clock in the {how} reply"))
                return
            samples.append((t0, t1, value))
        bound = bound_offset(samples, resolution)
        if bound is None:
            out.append((label, None, 0.0, f"{how}: reads contradict each other (clock stepped?)"))
            return
        out.append((label, bound[0] - srv_off, bound[1] + srv_u, how))

    with socks_http.client(ctx.session.socks_url) as http:
        cam_label = f"camera .{plan.camera.rsplit('.', 1)[1]}"
        cam = None
        if not skip(cam_label, plan.camera):
            try:
                cam = Raythink(http, plan.camera, ctx.creds.device("camera"), ctx.redact.add)
                cam.login()
            except Exception as e:  # noqa: BLE001
                out.append((cam_label, None, 0.0, ctx.redact(friendly_error(e))))
                cam = None
        if cam is not None:
            add(cam_label, lambda: camera_epoch(cam.get("/v1/system/local/time")), 1.0,
                "/v1/system/local/time")

        def date_header(ip: str) -> Callable[[], Optional[float]]:
            return lambda: http_date_epoch(http.get(f"http://{ip}/", timeout=10).headers.get("date"))
        add(f"speaker .{plan.speaker.rsplit('.', 1)[1]}", date_header(plan.speaker), 1.0,
            "HTTP Date header", plan.speaker)
        add(f"IGS-4215 .{plan.poe_switch.rsplit('.', 1)[1]}", date_header(plan.poe_switch), 1.0,
            "HTTP Date header", plan.poe_switch)

    for ip, name in ((plan.router, "router"), (plan.switch, "TSW202")):
        label = f"{name} .{ip.rsplit('.', 1)[1]}"
        if skip(label, ip):
            continue
        try:
            ssh = ctx.device_ssh(ip, "teltonika")
            ssh.connect()
        except Exception as e:  # noqa: BLE001
            out.append((label, None, 0.0, ctx.redact(friendly_error(e))))
            continue

        rtt = min(_round_trip(lambda ssh=ssh: ssh.run("true")) for _ in range(3))
        r = ssh.run(SECOND_EDGE, timeout=5)
        t1 = time.time()
        edge = r.text.split()[-1] if r.ok and r.text else ""
        if not edge.isdigit():
            out.append((label, None, 0.0, "no clock in the date reply"))
            continue
        out.append((label, float(edge) - (t1 - rtt / 2 + srv_off), rtt / 2 + srv_u + EDGE_SLACK_S,
                    "date over SSH (second edge)"))

    if ctx.session.has_operator:
        add("operator station", lambda: float(ctx.operator("date +%s.%N", 15).text), 0.0, "date")
    return out


# ── rows ────────────────────────────────────────────────────────────────────

def _s71(ctx: Context) -> Row:
    spec = STAGE.spec("S7.1")
    th = ctx.release.thresholds
    c = Checks()
    plan = ctx.release.plan
    statuses = ctx.facts.get("magos_status") or {}
    magos = [(rid, ip) for rid, ip in plan.radars.items()] + \
            [(f"APU .{ip.rsplit('.', 1)[1]}", ip) for ip in plan.apus]
    for label, ip in magos:
        st = statuses.get(ip)
        if not isinstance(st, dict):
            c.add(label, None, "no systemStatus from S0.6")
            continue
        off, target = st.get("timeSyncOffset"), str(st.get("timeSyncTarget") or "").strip("<>")
        if not isinstance(off, (int, float)):
            c.add(label, False, f"{st.get('timeSyncDetail') or 'not synced'} (no offset)")
            continue
        ok = bool(st.get("timeSyncOk")) and abs(off) < th["magos_time_sync_offset_s"] and plan.server in target
        c.add(label, ok, f"{off * 1000:+.1f} ms to {target or '—'}" + ("" if st.get("timeSyncOk") else " (not synced)"))
    matrix = {}
    for label, offset, u, note in _clock_rows(ctx):
        if offset is None:
            c.add(label, None, note)
            continue
        matrix[label] = {"offset_s": round(offset, 3), "uncertainty_s": round(u, 3), "via": note}
        c.add(label, judge(offset, u, th["ntp_offset_s"]), fmt_offset(offset, u))
    return c.row(spec, matrix=matrix)


def _s72(ctx: Context) -> Row:
    spec = STAGE.spec("S7.2")
    r = ctx.server("sudo -n chronyc -n clients", 20)
    if not r.ok:
        return amber(spec, ctx.redact(r.text[:160] or "chronyc clients failed"))
    clients = parse_chrony_clients(r.out)
    plan = ctx.release.plan
    want = {ip: "APU" for ip in plan.apus}
    want.update({ip: rid for rid, ip in plan.radars.items()})
    want.update({plan.camera: "camera", plan.speaker: "speaker", plan.router: "router",
                 plan.switch: "TSW202", plan.poe_switch: "IGS-4215"})
    c = Checks()
    for ip, name in want.items():
        e = clients.get(ip)
        if e is None:
            c.add(f"{name} .{ip.rsplit('.', 1)[1]}", False, "never asked the server for time")
            continue
        fresh = client_fresh(e)
        last = e["last_s"]
        c.add(f"{name} .{ip.rsplit('.', 1)[1]}", fresh,
              f"last request {_age(last)} ago" + ("" if fresh else " — stopped syncing"))
    others = sorted(set(clients) - set(want))
    if others:
        c.items.append({"label": "other clients (recorded)", "ok": True,
                        "actual": ", ".join(f"{ip} ({_age(clients[ip]['last_s'])} ago)" for ip in others)})
    return c.row(spec, clients=clients)


def _age(s: Optional[float]) -> str:
    if s is None:
        return "?"
    return f"{s:.0f} s" if s < 120 else f"{s / 60:.0f} min" if s < 7200 else f"{s / 3600:.1f} h"


def _s73(ctx: Context) -> Row:
    spec = STAGE.spec("S7.3")
    th = ctx.release.thresholds
    c = Checks()
    query = ("histogram_quantile(0.95, sum by (le) (rate(hub_grpc_request_duration_seconds_bucket"
             f"{{method!~`{API_EXCLUDE}`}}[{API_WINDOW}])))")
    prom = ctx.server("ip=$(sudo -n k3s kubectl -n monitoring get svc prometheus-server "
                      "-o jsonpath='{.spec.clusterIP}') && "
                      f"curl -s -m 10 \"http://$ip/api/v1/query?query={quote(query, safe='')}\"", 30)
    p95 = None
    try:
        res = json.loads(prom.out)["data"]["result"]
        p95 = float(res[0]["value"][1]) if res else None
    except (ValueError, KeyError, IndexError, TypeError):
        c.add("gRPC p95", None, "Prometheus unreadable: " + ctx.redact(prom.text[:120] or prom.err.strip()[:120]))
    else:
        if p95 is None or p95 != p95:  # NaN: no calls in the window
            c.add("gRPC p95", None, f"no unary hub calls in the last {API_WINDOW}")
        else:
            ms = p95 * 1000
            c.add("gRPC p95", ms < th["hub_api_p95_ms"], f"{ms:.1f} ms over {API_WINDOW}")
    via_op = ctx.session.has_operator
    curl = ("curl -sk -m 10 -o /dev/null -w '%{time_starttransfer}\\n' "
            + ("https://kela.local/" if via_op else
               f"--resolve kela.local:443:{ctx.release.plan.server} https://kela.local/"))
    script = f"for i in $(seq {TTFB_SAMPLES}); do {curl}; done"
    r = ctx.operator(script, 60) if via_op else ctx.server(script, 60)
    samples = [float(x) * 1000 for x in r.out.split() if re.fullmatch(r"[\d.]+", x) and float(x) > 0]
    where = "from the operator" if via_op else "from the server (no operator session)"
    if not samples:
        c.add("TTFB", False, f"https://kela.local/ did not answer {where}")
    else:
        med = statistics.median(samples)
        c.add("TTFB", med < th["hub_ttfb_ms"], f"median {med:.0f} ms of {len(samples)} {where}")
    return c.row(spec, grpc_p95_ms=None if p95 is None or p95 != p95 else round(p95 * 1000, 2),
                 ttfb_ms=[round(s, 1) for s in samples])


def _mtx_bytes(ctx: Context) -> Optional[dict[str, int]]:
    r = ctx.server(f"curl -s -m 8 {MTX_API}", 20)
    try:
        items = json.loads(r.out).get("items") or []
    except ValueError:
        return None
    return {it["name"]: int(it.get("bytesReceived") or it.get("inboundBytes") or 0) for it in items
            if it.get("ready") and (it.get("metadata") or {}).get("type", "Native") == "Native"}


def _s74(ctx: Context) -> Row:
    spec = STAGE.spec("S7.4")
    th = ctx.release.thresholds
    before = _mtx_bytes(ctx)
    targets = _targets(ctx)
    n = int(th["ping_count"])
    r = ctx.server(parallel_script(list(targets), f"ping -n -q -c {n} -i 0.2 -W 1 \"$ip\"", "PING"),
                   timeout=n * 0.2 + 30)
    after = _mtx_bytes(ctx)
    c = Checks()
    if before is None or after is None:
        c.add("streams flowing", None, "MediaMTX API unreadable")
    else:
        grew = sorted(p for p in after if after[p] > before.get(p, after[p]))
        c.add("streams flowing", True if len(grew) >= 2 else None,
              f"{len(grew)} of {len(after)} camera paths received data during the ping"
              + ("" if len(grew) >= 2 else " — not under streaming load (see S6.7a)"))
    blocks = parse_blocks(r.out, "PING")
    matrix = {}
    for ip, name in targets.items():
        p = parse_ping(blocks.get(ip, ""))
        label = f"{name} .{ip.rsplit('.', 1)[1]}"
        if p is None:
            c.add(label, False, "no ping result")
            continue
        matrix[ip] = {"name": name, **p}
        ok = (p["loss_pct"] <= th["ping_loss_pct"] and p["avg"] is not None
              and p["avg"] < th["ping_avg_ms"] and p["mdev"] < th["ping_jitter_ms"])
        c.add(label, ok, f"{p['loss_pct']:g}% loss" + (f", avg {p['avg']:.2f} ms, jitter {p['mdev']:.2f} ms"
                                                     if p["avg"] is not None else ""))
    return c.row(spec, matrix=matrix)


def run(ctx: Context) -> Iterator[Row]:
    yield from guarded(ctx, STAGE, (_s71, _s72, _s73, _s74))
