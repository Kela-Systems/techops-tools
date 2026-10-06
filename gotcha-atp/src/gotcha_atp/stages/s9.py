"""S9 — Power-cycle soak (optional: 'Extended run' on Home).

Runs after S0–S8 (the baseline), with the engineer at the unit:

  1. baseline: the server's boot time, the uptimes the devices report
     (radars, APUs: systemStatus; router, TSW202: /proc/uptime) and which
     S1–S8 rows passed
  2. the SSH masters are dropped and the engineer is asked to cut mains to
     the PwF and the Edge box, wait 30 s, restore it, and answer — that answer
     is T0
  3. the laptop polls the tailnet every 5 s and logs in again: the tailnet
     coming back proves modem → router → tailscaled → server self-recovered
  4. the server booted after the instruction (/proc/stat btime) and the
     engineer did not press a power button (S9.1)
  5. every device with an uptime reports one shorter than the time since the
     instruction (S9.2); the camera, speaker and IGS-4215 expose no uptime, so
     for them "came back" is recorded — they share the PwF's power
  6. once the node-controller logs 'system converged' after the boot, S0–S8
     re-run silently until every S1–S8 row that passed before the cut passes
     again, or 15 min after T0 (S9.3). Rows failing before the cut are recorded,
     not judged — the soak asks what the outage broke. The re-check waives
     S3.2's container-age rule: after a reboot every container is minutes old
  7. no zombie pods, and restart counts stop climbing (S9.4)
"""
from __future__ import annotations

import json
import time
from typing import Iterator, Optional

from .. import stages as all_stages
from ..access import socks_http, tailnet
from ..access.exec import AccessError
from ..context import Context
from ..devices import Magos
from ..model import AMBER, PASS, Checks, Row, RowSpec, StageSpec, amber
from .s3 import COMMANDS as S3_COMMANDS, pod_problems

STAGE = StageSpec(
    id="S9", name="Power-cycle soak (optional)", optional=True, depends_on=("S0.3",),
    vantage="laptop (tailnet) — the unit vanishes and must come back on its own",
    rows=(
        RowSpec("S9.0", "Precondition: 'power on after AC loss' was set during assembly", "confirmed",
                "low", "manual",
                prompt="Was 'power on after AC loss' set on the NRU (and 'Power On AC' on the "
                       "Toughbook) during assembly?"),
        RowSpec("S9.1", "Cold start with no hands",
                "SSH back within 5 min of T0; the server booted after the power cut; no power button pressed",
                "critical"),
        RowSpec("S9.2", "Devices really cycled and came back",
                "radar / APU / router / TSW202 uptimes shorter than the time since the cut; camera, "
                "speaker, IGS-4215 reachable again", "high"),
        RowSpec("S9.3", "System re-converges by itself",
                "every S1–S8 row that passed before the cut passes again within 15 min of T0", "critical"),
        RowSpec("S9.4", "Nothing left behind by the outage",
                "0 stuck / zombie pods; restart counts stop climbing", "high"),
    ),
)

SSH_BACK_MAX_S = 300
RECONVERGE_MAX_S = 900
DEVICES_BACK_MAX_S = 600
POLL_S = 5
RECHECK_PAUSE_S = 30
RESTART_SETTLE_S = 60
BOOT_SLACK_S = 5
RECHECK_STAGES = ("S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8")
JUDGED_STAGES = RECHECK_STAGES[1:]

POWER_CUT = RowSpec(
    "S9.cut", "Power cycle", "power restored", "critical", "manual",
    prompt="Cut mains to the PwF and the Edge box (breaker or plug), wait 30 seconds, restore power, "
           "then answer Yes — the clock starts at your answer. No skips the power cycle.")
NO_HANDS = RowSpec(
    "S9.hands", "Power button", "No", "critical", "manual",
    prompt="Did you press any power button (NRU, Toughbook, switches) to bring the unit back?")


# ── pure helpers (covered by tests/test_s9.py) ──────────────────────────────

def baseline_passed(results: dict[str, Row]) -> list[str]:
    """Automated S1–S8 rows that passed in the baseline run."""
    return sorted(i for i, r in results.items()
                  if i.split(".")[0] in JUDGED_STAGES and r.state == PASS and r.cls != "manual")


def not_regained(baseline: list[str], recheck: dict[str, Row]) -> list[str]:
    return [i for i in baseline if i not in recheck or recheck[i].state != PASS]


def restarts(pods: dict) -> dict[str, int]:
    """{namespace/pod/container: restartCount}."""
    out = {}
    for p in pods.get("items") or []:
        key = f"{p['metadata'].get('namespace')}/{p['metadata']['name']}"
        for cs in (p.get("status") or {}).get("containerStatuses") or []:
            out[f"{key}/{cs.get('name')}"] = int(cs.get("restartCount") or 0)
    return out


def _fmt(seconds: float) -> str:
    return f"{seconds:.0f} s" if seconds < 120 else f"{seconds / 60:.1f} min"


# ── reads ───────────────────────────────────────────────────────────────────

def _btime(ctx: Context) -> Optional[int]:
    r = ctx.server("awk '/^btime/{print $2}' /proc/stat", 15)
    return int(r.text) if r.ok and r.text.isdigit() else None


def _uptimes(ctx: Context) -> dict[str, Optional[float]]:
    """{label: uptime seconds | None (unreachable)} for every device that reports one."""
    plan = ctx.release.plan
    out: dict[str, Optional[float]] = {}
    with socks_http.client(ctx.session.socks_url) as http:
        targets = [(rid, ip, "magos") for rid, ip in plan.radars.items()] + \
                  [(f"APU .{ip.rsplit('.', 1)[1]}", ip, "apu") for ip in plan.apus]
        for label, ip, family in targets:
            try:
                m = Magos(http, ip, ctx.creds.device(family))
                m.login()
                out[label] = float(m.system_status().get("uptime"))
            except Exception:  # noqa: BLE001 — unreachable is a value here
                out[label] = None
    for label, ip in (("router", plan.router), ("TSW202", plan.switch)):
        try:
            r = ctx.device_ssh(ip, "teltonika").run("cut -d. -f1 /proc/uptime")
            out[label] = float(r.text) if r.ok and r.text.isdigit() else None
        except Exception:  # noqa: BLE001
            out[label] = None
    return out


def _reachable(ctx: Context, ips: list[str]) -> dict[str, bool]:
    script = " ".join(f"ping -n -q -c 2 -W 1 {ip} >/dev/null 2>&1 && echo {ip}=up || echo {ip}=down;"
                      for ip in ips)
    r = ctx.server(script, 30)
    got = dict(line.split("=", 1) for line in r.out.split() if "=" in line)
    return {ip: got.get(ip) == "up" for ip in ips}


def _wait(ctx: Context, seconds: float) -> None:
    ctx.cancel.wait(seconds)


# ── the soak ────────────────────────────────────────────────────────────────

def _unit_back(ctx: Context, t0: float, deadline: float) -> Optional[float]:
    """Seconds after T0 the server session was back, or None."""
    last_log = 0.0
    while time.time() < deadline and not ctx.cancel.is_set():
        st = tailnet.status()
        peers = tailnet.unit(st, ctx.unit.site, ctx.unit.operator_name or None) if "error" not in st else None
        if peers and peers.server and peers.server.online:
            try:
                ctx.session.reconnect()
                return time.time() - t0
            except AccessError:
                pass
        if time.time() - last_log >= 30:
            ctx.log(f"S9: waiting for {ctx.unit.site} — {_fmt(time.time() - t0)} since power was restored")
            last_log = time.time()
        _wait(ctx, POLL_S)
    return None


def _recheck(ctx: Context) -> dict[str, Row]:
    """S0–S8 once more, silently, on a fresh context over the same session."""
    child = Context(unit=ctx.unit, creds=ctx.creds, redact=ctx.redact, release=ctx.release,
                    session=ctx.session, log=lambda msg: None, cancel=ctx.cancel)
    child.facts["pod_stable_min_minutes"] = 0
    try:
        for sid in RECHECK_STAGES:
            stage = all_stages.BY_ID[sid].STAGE
            if any(not child.passed(d) for d in stage.depends_on if "." in d):
                continue
            try:
                for row in all_stages.BY_ID[sid].run(child):
                    spec = stage.spec(row.id)
                    if row.state != AMBER and any(not child.passed(d) for d in spec.depends_on):
                        row = amber(spec, "prerequisite not passed")
                    child.results[row.id] = row
            except Exception as e:  # noqa: BLE001 — one stage must not end the soak
                ctx.log(ctx.redact(f"S9 re-check: {sid} stopped: {type(e).__name__}: {e}"))
    finally:
        child.close_devices()
    return child.results


def _skip(reason: str) -> Iterator[Row]:
    for sid in ("S9.1", "S9.2", "S9.3", "S9.4"):
        yield amber(STAGE.spec(sid), reason)


def run(ctx: Context) -> Iterator[Row]:
    if ctx.ask is None:
        yield from _skip("the soak needs the engineer at the unit — run with manual steps")
        return
    plan = ctx.release.plan
    reachable_before = {p["ip"] for p in ctx.facts.get("inventory") or [] if not p.get("error")}
    baseline = baseline_passed(ctx.results)
    boot_before = _btime(ctx)
    up_before = _uptimes(ctx)
    ctx.log(f"S9: baseline — {len(baseline)} S1–S8 rows passed; server booted at {boot_before}")

    ctx.close_devices()
    ctx.session.disconnect()
    t_instr = time.time()
    got = ctx.ask([POWER_CUT]).get(POWER_CUT.id) or {}
    t0 = time.time()
    if got.get("answer") != "yes":
        try:
            ctx.session.reconnect()
        except AccessError as e:
            ctx.log(ctx.redact(f"S9: could not reconnect after the skipped power cycle: {e}"))
        yield from _skip("power cycle not performed (the engineer answered No)" if got else "run cancelled")
        return
    timeline = {"instruction_shown": round(t_instr), "power_restored_T0": round(t0)}

    # ── S9.1 ─────────────────────────────────────────────────────────────────
    back = _unit_back(ctx, t0, t0 + RECONVERGE_MAX_S)
    if back is None:
        yield Checks().add("SSH back", False, f"no SSH to the server within {_fmt(RECONVERGE_MAX_S)} of T0") \
            .row(STAGE.spec("S9.1"), timeline=timeline)
        for sid in ("S9.2", "S9.3", "S9.4"):
            yield amber(STAGE.spec(sid), "the unit did not come back")
        return
    timeline["ssh_back_s"] = round(back)
    boot_after = _btime(ctx)
    hands = (ctx.ask([NO_HANDS]).get(NO_HANDS.id) or {}).get("answer")
    c = Checks()
    c.add("SSH back", back <= SSH_BACK_MAX_S, f"{_fmt(back)} after T0 (limit {_fmt(SSH_BACK_MAX_S)})")
    if boot_after is None:
        c.add("cold boot", None, "boot time unreadable")
    else:
        cold = boot_after > t_instr - BOOT_SLACK_S and boot_after != boot_before
        c.add("cold boot", cold, f"server booted {_fmt(boot_after - t_instr)} after the instruction" if cold
              else "the server did NOT reboot — the power was not cut, or it rode through on a UPS")
        timeline["server_boot_s"] = round(boot_after - t0)
    c.add("no hands", hands == "no" if hands in ("yes", "no") else None,
          {"no": "no power button pressed", "yes": "a power button WAS pressed"}.get(hands, "not answered"))
    yield c.row(STAGE.spec("S9.1"), timeline=timeline)

    # ── S9.2 ─────────────────────────────────────────────────────────────────
    others = {"camera": plan.camera, "speaker": plan.speaker, "IGS-4215": plan.poe_switch}
    deadline = t0 + DEVICES_BACK_MAX_S
    while True:
        since = time.time() - t_instr
        up_after = _uptimes(ctx)
        reach = _reachable(ctx, list(others.values()))
        pending = [k for k, v in up_after.items() if v is None and up_before.get(k) is not None] + \
                  [k for k, ip in others.items() if not reach[ip] and ip in reachable_before]
        if not pending or time.time() >= deadline or ctx.cancel.is_set():
            break
        ctx.log(f"S9: waiting for {', '.join(pending)} to come back")
        _wait(ctx, 15)
    c = Checks()
    absent = []      # already unreachable before the cut: S0.6 / S1.1 report them, the soak does not
    for label, before in up_before.items():
        after = up_after.get(label)
        if before is None and after is None:
            absent.append(label)
        elif after is None:
            c.add(label, False, "did not come back")
        else:
            c.add(label, after < since, f"up {_fmt(after)} — rebooted with the cut" if after < since
                  else f"up {_fmt(after)} — did NOT reboot (on another supply?)")
    for label, ip in others.items():
        if ip not in reachable_before and not reach[ip]:
            absent.append(label)
        else:
            c.add(label, reach[ip], "came back (no uptime to read)" if reach[ip] else "did not come back")
    if absent:
        c.items.append({"label": "unreachable before the cut too (not judged)", "ok": True,
                        "actual": ", ".join(absent)})
    yield c.row(STAGE.spec("S9.2"), uptimes_before=up_before, uptimes_after=up_after)

    # ── S9.3 ─────────────────────────────────────────────────────────────────
    deadline = t0 + RECONVERGE_MAX_S
    converged_at = None
    while time.time() < deadline and not ctx.cancel.is_set():
        if "system converged" in ctx.server(S3_COMMANDS["converged"], 20).out:
            converged_at = time.time() - t0
            break
        _wait(ctx, 15)
    timeline["converged_s"] = round(converged_at) if converged_at is not None else None
    missing, attempts, recheck = list(baseline), 0, {}
    while converged_at is not None and missing and time.time() < deadline and not ctx.cancel.is_set():
        attempts += 1
        ctx.log(f"S9: re-check {attempts} of S1–S8 ({_fmt(time.time() - t0)} after T0)")
        recheck = _recheck(ctx)
        missing = not_regained(baseline, recheck)
        if missing and time.time() + RECHECK_PAUSE_S < deadline:
            _wait(ctx, RECHECK_PAUSE_S)
    timeline["all_green_s"] = round(time.time() - t0) if converged_at is not None and not missing else None
    before_fail = sorted(i for i, r in ctx.results.items()
                         if i.split(".")[0] in JUDGED_STAGES and r.state != PASS and r.cls != "manual")
    c = Checks()
    if converged_at is None:
        c.add("node-controller", False, f"no 'system converged' within {_fmt(RECONVERGE_MAX_S)} of T0")
    else:
        c.add("node-controller", True, f"'system converged' {_fmt(converged_at)} after T0")
        c.add("S1–S8 back", not missing,
              f"all {len(baseline)} rows that passed before the cut pass again "
              f"({_fmt(timeline['all_green_s'])} after T0, {attempts} re-check{'s' if attempts != 1 else ''})"
              if not missing else
              f"{len(missing)} of {len(baseline)} not back after {_fmt(RECONVERGE_MAX_S)}: "
              + "; ".join(f"{i} {recheck[i].actual[:60] if i in recheck else 'not run'}" for i in missing[:6]))
    if before_fail:
        c.items.append({"label": "failing before the cut (not judged)", "ok": True,
                        "actual": ", ".join(before_fail)})
    yield c.row(STAGE.spec("S9.3"), timeline=timeline, not_regained=missing)

    # ── S9.4 ─────────────────────────────────────────────────────────────────
    yield _s94(ctx)


def _pods(ctx: Context) -> Optional[dict]:
    r = ctx.server(S3_COMMANDS["pods"], 60)
    try:
        return json.loads(r.out) if r.ok else None
    except ValueError:
        return None


def _s94(ctx: Context) -> Row:
    spec = STAGE.spec("S9.4")
    first = _pods(ctx)
    _wait(ctx, RESTART_SETTLE_S)
    second = _pods(ctx)
    if first is None or second is None:
        return amber(spec, "pod list unreadable")
    c = Checks()
    stuck = pod_problems(second, None)["stuck"]
    c.add("stuck / zombie pods", not stuck, "none" if not stuck else ", ".join(stuck[:6]))
    a, b = restarts(first), restarts(second)
    climbing = sorted(k for k in b if b[k] > a.get(k, b[k]))
    c.add("restarts settled", not climbing,
          f"no container restarted in {RESTART_SETTLE_S} s" if not climbing
          else f"{len(climbing)} still restarting: " + ", ".join(climbing[:5]))
    return c.row(spec)
