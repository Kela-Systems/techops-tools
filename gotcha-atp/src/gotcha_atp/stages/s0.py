"""S0 — Pre-flight & discovery.

laptop → server over the tailnet (first choice), with a separate session to
the operator for its own checks; through the operator to 192.168.88.10 only
when the direct server login fails (see access/session.py). S0.2 needs the
laptop, the server and the operator online; a unit without an operator peer
still runs every stage, but S0.2 fails and the run can never be stamped.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterator, Optional

import httpx

from ..access import tailnet
from ..access.exec import AccessError
from ..access.session import SERVER_VIA_OPERATOR, Session, Unit
from ..context import Context
from ..discovery import discover
from ..fleet import Fleet
from ..model import Checks, Row, RowSpec, StageSpec, amber, result

STAGE = StageSpec(
    id="S0", name="Pre-flight & discovery",
    vantage="laptop → server and operator over the tailnet",
    rows=(
        RowSpec("S0.1", "release.yaml loads and validates", "valid", "critical", "read-back"),
        RowSpec("S0.2", "Laptop, <site> and <site>-operator are on the tailnet",
                "laptop, server and operator online", "critical"),
        # The server session is what every later stage runs on; the operator
        # session is judged in S0.4, so a missing operator never blocks S1/S2.
        RowSpec("S0.3", "SSH session to the server established",
                "server session up (direct over the tailnet, or through the operator); "
                "tailnet RTT recorded (informational)", "critical"),
        RowSpec("S0.4", "Unit identity is consistent",
                "operator session up; operator hostname == <site>-operator; server hostname == site == build-info "
                "site on both; role == gotcha; server setup_version == "
                "release.yaml.server.init_version; static_ip == 192.168.88.10; model recorded",
                "critical", "read-back", depends_on=("S0.3",)),
        RowSpec("S0.5", "Node adopted in Fleet and checking in",
                "adopted; check-in < 5 min; applied == desired", "high"),
        RowSpec("S0.6", "Device discovery",
                "serial + model + firmware for every part; recorded in the run", "high",
                depends_on=("S0.3",)),
    ),
)

FLEET_CHECKIN_MAX_S = 300


# ── helpers shared with the UI's Home screen ────────────────────────────────

def parse_build_info(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def unit_for(peers: tailnet.UnitPeers) -> Unit:
    return Unit(site=peers.site,
                operator_addr=peers.operator.ip if peers.operator else "",
                server_addr=peers.server.ip if peers.server else "",
                operator_name=peers.operator.name if peers.nonstandard_operator else "",
                operator_peer=peers.operator is not None)


def read_identity(session: Session) -> dict:
    """build-info + hostnames on both hosts — what Home shows for confirmation."""
    out: dict = {"operator": None, "server": None}
    lan = session.unit.server_lan
    if session.has_operator:
        s = session.sections("operator", {
            "hostname": "hostname",
            "build": "cat /etc/kela/build-info",
            "route": f"ip -4 -o route get {lan}",
        }, timeout=20)
        m = re.search(r"\bsrc (\d+\.\d+\.\d+\.\d+)", s["route"].out)
        out["operator"] = {"hostname": s["hostname"].text,
                           "build_info": parse_build_info(s["build"].out),
                           "build_info_error": "" if s["build"].ok else s["build"].text,
                           "lan_ip": m.group(1) if m else ""}
    s = session.sections("server", {
        "hostname": "hostname",
        "build": "cat /etc/kela/build-info",
        "machine_id": "cat /etc/machine-id",
        "serial": "tr -d '\\0' < /sys/firmware/devicetree/base/serial-number 2>/dev/null",
    }, timeout=20)
    out["server"] = {"hostname": s["hostname"].text,
                     "build_info": parse_build_info(s["build"].out),
                     "build_info_error": "" if s["build"].ok else s["build"].text,
                     "machine_id": s["machine_id"].text,
                     "board_serial": s["serial"].text}
    return out


def _strip_prefix(ip: str) -> str:
    return ip.split("/", 1)[0].strip()


# ── rows ────────────────────────────────────────────────────────────────────

def _s01(ctx: Context) -> Row:
    spec = STAGE.spec("S0.1")
    if ctx.release is None:
        return result(spec, ctx.release_error or "not loaded", False)
    r = ctx.release
    return result(spec, f"{r.name} · commit {r.commit} · sha256 {r.sha256[:12]}", True,
                  detail=r.info())


def _s02(ctx: Context) -> Row:
    spec = STAGE.spec("S0.2")
    st = tailnet.status()
    if "error" in st:
        return result(spec, st["error"], False)
    peers = tailnet.unit(st, ctx.unit.site, ctx.unit.operator_name or None)
    ctx.facts["tailnet_peers"] = peers.to_dict()
    if not ctx.unit.operator_addr and peers.operator:
        ctx.unit.operator_addr = peers.operator.ip
    if not ctx.unit.server_addr and peers.server:
        ctx.unit.server_addr = peers.server.ip
    ctx.unit.operator_peer = peers.operator is not None

    def state(p) -> str:
        return ("online " + p.ip) if p and p.online else ("offline" if p else "not a peer")

    c = Checks()
    c.add("laptop", tailnet.self_online(st), "online" if tailnet.self_online(st) else "offline")
    c.add(f"server {ctx.unit.server_host}", bool(peers.server and peers.server.online),
          state(peers.server))
    c.add(f"operator {ctx.unit.operator_host}", bool(peers.operator and peers.operator.online),
          state(peers.operator))
    return c.row(spec)


def _s03(ctx: Context) -> Row:
    spec = STAGE.spec("S0.3")
    s = ctx.session
    try:
        if s is None or not s.opened:
            s = Session(ctx.unit, user=ctx.creds.ssh_user, password=ctx.creds.ssh_password)
            ctx.session = s
            s.open()
    except AccessError as e:
        return result(spec, ctx.redact(str(e)), False)
    info = s.info
    rtt = f", RTT {info['rtt_ms']} ms" if info.get("rtt_ms") is not None else ""
    c = Checks()
    if info["server_path"] == SERVER_VIA_OPERATOR:
        c.add("server", True, f"{info['server']} through the operator{rtt} "
              f"(direct tailnet login failed: {ctx.redact(info['server_note'])})")
    else:
        c.add("server", True, f"{info['server']} over the tailnet{rtt}")
    c.items.append({"label": "auth", "ok": True, "actual": info["auth"]})
    return c.row(spec, **info)


def _s04(ctx: Context) -> Row:
    spec = STAGE.spec("S0.4")
    try:
        ident = read_identity(ctx.session)
    except AccessError as e:
        return amber(spec, ctx.redact(str(e)))
    ctx.facts["identity"] = ident
    site = ctx.unit.site
    c = Checks()
    op = ident["operator"]
    sess = ctx.session
    if op is None:
        # No peer at all: S0.2 already fails on it, so here it is "not evaluated".
        # A peer that refused the login is a failure of its own.
        refused = sess.unit.operator_peer
        c.add("operator session", False if refused else None,
              ctx.redact(sess.operator_error) + " (operator checks skipped)")
    else:
        c.add("operator session", True, sess.info.get("operator") or "up")
        ctx.facts["operator_lan_ip"] = op["lan_ip"]
        want_host = ctx.unit.standard_operator_host
        c.add("operator hostname", op["hostname"] == want_host,
              (op["hostname"] or "—") + ("" if op["hostname"] == want_host else f" (want {want_host})"))
        if op["build_info"]:
            c.add("operator build-info site", op["build_info"].get("site") == site,
                  op["build_info"].get("site") or "no site= line")
        else:
            c.add("operator build-info", False, "/etc/kela/build-info not found (written by operator setup)")
    sv = ident["server"]
    bi = sv["build_info"]
    c.add("server hostname", sv["hostname"] == site, sv["hostname"] or "—")
    if not bi:
        # One missing file is one failure, not four: site, role, setup_version
        # and static_ip all live in it.
        c.add("server build-info", False,
              "/etc/kela/build-info not found — init_gotcha_server.sh was not run on this server "
              "(site, role, setup_version, static_ip cannot be checked)")
        return c.row(spec, identity=ident)
    c.add("server build-info site", bi.get("site") == site, bi.get("site") or "no site= line")
    c.add("role", bi.get("role") == "gotcha", bi.get("role") or "missing")
    want = ctx.release.pin("server.init_version")
    got = bi.get("setup_version", "")
    c.add("setup_version", (got == want) if want else None, f"{got or 'missing'} (want {want or 'unpinned'})")
    static = _strip_prefix(bi.get("static_ip", ""))
    c.add("static_ip", static == ctx.release.plan.server,
          static or f"not set (want {ctx.release.plan.server})")
    c.items.append({"label": "model (recorded)", "ok": True, "actual": bi.get("model") or "—"})
    return c.row(spec, identity=ident)


def _s05(ctx: Context) -> Row:
    spec = STAGE.spec("S0.5")
    fleet = Fleet(ctx.creds.fleet_url, ctx.creds.fleet_token)
    if not fleet.enabled:
        return amber(spec, "Fleet admin API not configured ([fleet] url/token in config.toml)")
    sv = (ctx.facts.get("identity") or {}).get("server") or {}
    try:
        nodes = fleet.nodes()
    except (httpx.HTTPError, ValueError) as e:
        return amber(spec, ctx.redact(f"Fleet admin API unreachable: {type(e).__name__}: {e}"))
    mid = sv.get("machine_id")
    node = next((n for n in nodes if mid and n.get("machineId") == mid), None) or \
        next((n for n in nodes if n.get("hostname") == ctx.unit.site), None)
    if node is None:
        return result(spec, f"no Fleet node with machine-id {mid or '?'} or hostname {ctx.unit.site}", False)
    keep = ("id", "siteId", "status", "hostname", "machineId", "model", "role", "serialNumber",
            "lastSeenAt", "agentVersion", "appliedApplicationDigest",
            "appliedSystemConfigurationVersion", "desiredSystemConfigurationVersion")
    ctx.facts["fleet_node"] = {k: node.get(k) for k in keep}
    c = Checks()
    c.add("status", node.get("status") == "adopted", node.get("status") or "—")
    age = _age_s(node.get("lastSeenAt"))
    c.add("check-in", (age < FLEET_CHECKIN_MAX_S) if age is not None else None,
          f"{age:.0f} s ago" if age is not None else "never")
    ok, text = applied_vs_desired(ctx, fleet, node)
    c.add("applied == desired", ok, text)
    return c.row(spec, node=ctx.facts["fleet_node"])


def applied_vs_desired(ctx: Context, fleet: Fleet, node: dict) -> tuple[Optional[bool], str]:
    """Applied vs the version Fleet serves this node: its pin, or — with no pin —
    the newest version staged for its (model, role), re-read at every check-in.
    The resolved target is kept in facts["fleet_desired"] for S3.1 / S4.3."""
    applied = node.get("appliedSystemConfigurationVersion")
    desired = node.get("desiredSystemConfigurationVersion")
    if desired is not None:
        ctx.facts["fleet_desired"] = {"version": desired, "pinned": True}
        return applied == desired, f"{applied} / {desired} (pinned)"
    try:
        latest = fleet.staged_configuration(node.get("model") or "", node.get("role") or "", None)
    except (httpx.HTTPError, ValueError) as e:
        latest, why = None, ctx.redact(f"{type(e).__name__}: {e}")[:120]
    else:
        why = f"nothing staged for {node.get('model')} / {node.get('role')}"
    if latest is None:
        return None, f"{applied} / not pinned — latest staged unknown ({why})"
    ctx.facts["fleet_desired"] = {"version": latest["version"], "pinned": False}
    return applied == latest["version"], f"{applied} / {latest['version']} (not pinned — follows the latest staged)"


def _age_s(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - t).total_seconds()


def _s06(ctx: Context) -> Row:
    spec = STAGE.spec("S0.6")
    parts = discover(ctx, progress=lambda p: ctx.log(
        f"discovered {p.role} {p.ip}: " + (f"{p.model} {p.serial} {p.firmware}" if not p.error else p.error)))
    c = Checks()
    for p in parts:
        if p.error:
            c.add(p.label, False, p.error)
        elif p.missing:
            c.add(p.label, False, f"answered, but reported no {', '.join(p.missing)}")
        else:
            c.add(p.label, True, f"{p.model} · SN {p.serial} · fw {p.firmware}")
    return c.row(spec, inventory=[p.to_dict() for p in parts])


def run(ctx: Context) -> Iterator[Row]:
    yield _s01(ctx)
    if ctx.release is None:
        for spec in STAGE.rows[1:]:
            yield amber(spec, "prerequisite S0.1 failed")
        return
    yield _s02(ctx)
    yield _s03(ctx)
    yield _s04(ctx) if ctx.passed("S0.3") else amber(STAGE.spec("S0.4"), "prerequisite S0.3 failed")
    yield _s05(ctx)
    yield _s06(ctx) if ctx.passed("S0.3") else amber(STAGE.spec("S0.6"), "prerequisite S0.3 failed")
