"""S8 — Operator station, over its own tailnet session.

What it checks is what rugged_operator/setup.sh builds:
  * kela-verify (§12) is the station's own machine-checked build gate; it
    only reads, needs root, and exits 0 only when every required check passes
  * the kiosk is Chrome --kiosk as user kela; Chrome trusts the hub through
    kela's NSS database, where kela-install-cert pins the hub's served leaf
    certificate as "Kela Server" (kela-cert-ensure re-pins it when it changes)
  * time: a systemd-timesyncd drop-in with NTP=<station.conf NTP_SERVER>
    (default 192.168.88.10) and ntp.ubuntu.com as fallback
  * isolation: the kela-egress firewall limits user kela — the kiosk, and the
    ATP's own login — to loopback, the LAN and the tailnet

When there is no operator session (no peer, offline, login refused) every S8
row is amber and the run cannot be stamp-eligible.
"""
from __future__ import annotations

import re
import shlex
from typing import Iterator, Optional

from ..context import Context
from ..model import Checks, Row, RowSpec, StageSpec, amber
from ._common import guarded

STAGE = StageSpec(
    id="S8", name="Operator station", depends_on=("S0.3",),
    vantage="operator station (its own tailnet session)",
    rows=(
        RowSpec("S8.1", "kela-verify passes", "sudo kela-verify exit 0; every FAIL line listed", "critical"),
        RowSpec("S8.2", "Kiosk reaches the hub",
                "kiosk running; TAB1 answers 200 with a trusted certificate; Chrome's NSS pin "
                "== the certificate the hub serves now", "critical"),
        RowSpec("S8.3", "Time from the server",
                "NTPSynchronized yes; timesyncd server == station.conf NTP_SERVER (192.168.88.10)",
                "medium"),
        RowSpec("S8.4", "Isolation and remote support",
                "as user kela: ping 8.8.8.8 and HTTPS to the internet blocked; AnyDesk active with "
                "its unattended password", "high"),
    ),
)

KELA_VERIFY = "/usr/local/sbin/kela-verify"
NSS_NICK = "Kela Server"
STATION_CONF = "/etc/kela/station.conf"
OUTSIDE_PING = "8.8.8.8"
OUTSIDE_HTTPS = "https://1.1.1.1/"
ANYDESK_PASSWORD_LINE = "anydesk unattended password set"


# ── pure helpers (covered by tests/test_s8.py) ──────────────────────────────

def parse_kela_verify(text: str) -> dict:
    """{'lines': [(PASS|FAIL|WARN, description)], 'summary': (pass, fail, warn) | None}."""
    lines = [(m.group(1), m.group(2).strip())
             for m in re.finditer(r"^(PASS|FAIL|WARN)\s+(.+)$", text, re.M)]
    m = re.search(r"kela-verify: (\d+) pass, (\d+) fail, (\d+) warn", text)
    return {"lines": lines, "summary": tuple(int(x) for x in m.groups()) if m else None}


def parse_station_conf(text: str) -> dict[str, str]:
    """KEY='value' lines as written by setup.sh."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Z0-9_]+)=(.*)$", line.strip())
        if m:
            try:
                parts = shlex.split(m.group(2))
            except ValueError:
                parts = [m.group(2)]
            out[m.group(1)] = parts[0] if parts else ""
    return out


def fingerprint(text: str) -> Optional[str]:
    """'sha256 Fingerprint=AB:CD:…' (any case) → 'AB:CD:…'."""
    m = re.search(r"Fingerprint=([0-9A-Fa-f:]+)", text or "")
    return m.group(1).upper() if m else None


def _rc(text: str) -> Optional[int]:
    m = re.search(r"rc=(\d+)", text or "")
    return int(m.group(1)) if m else None


# ── data ────────────────────────────────────────────────────────────────────

def _no_operator(ctx: Context, spec: RowSpec) -> Optional[Row]:
    if ctx.session is None or not ctx.session.has_operator:
        why = ctx.redact(ctx.session.operator_error) if ctx.session else "no session"
        return amber(spec, f"no operator session — {why or 'operator not reachable'}")
    return None


def _station(ctx: Context) -> dict:
    """One round-trip for S8.2–S8.4 (S8.1 runs kela-verify on its own)."""
    if "_s8" in ctx.facts:
        return ctx.facts["_s8"]
    src = f". {STATION_CONF} 2>/dev/null; "
    s = ctx.operator_sections({
        "conf": f"cat {STATION_CONF}",
        "kiosk": "pgrep -u kela -f 'google-chrome-stable.*--kiosk' >/dev/null && echo running || echo stopped",
        "tab1": src + "curl -sS -m 10 -o /dev/null -w '%{http_code}' \"${TAB1:-https://kela.local/}\"; echo \" rc=$?\"",
        "served": src + "echo | timeout 10 openssl s_client -connect \"${KELA_LOCAL_IP:-192.168.88.10}:443\" "
                        "-servername \"${KELA_LOCAL_HOST:-kela.local}\" 2>/dev/null | openssl x509 -noout -fingerprint -sha256",
        "nss": f"certutil -L -d sql:$HOME/.pki/nssdb -n '{NSS_NICK}' -a | openssl x509 -noout -fingerprint -sha256",
        "ntp_sync": "timedatectl show -p NTPSynchronized --value",
        "ntp_server": "timedatectl show-timesync -p ServerAddress --value; timedatectl show-timesync -p ServerName --value",
        "ping_out": f"ping -n -c 2 -W 2 {OUTSIDE_PING} >/dev/null 2>&1; echo rc=$?",
        "https_out": f"curl -s -m 6 -o /dev/null {OUTSIDE_HTTPS}; echo rc=$?",
        "anydesk": "systemctl is-active anydesk",
        "anydesk_pw": "grep -qiE '(pwd_hash|_unattended_access\\.pwd)=[0-9a-f]' /etc/anydesk/system.conf "
                      "&& echo set || echo \"unset rc=$?\"",
    }, timeout=90)
    ctx.facts["_s8"] = s
    return s


# ── rows ────────────────────────────────────────────────────────────────────

def _s81(ctx: Context) -> Row:
    spec = STAGE.spec("S8.1")
    if (r := _no_operator(ctx, spec)):
        return r
    res = ctx.session.as_root("operator", KELA_VERIFY, ctx.creds.ssh_password, timeout=120)
    text = res.out + res.err
    if res.rc == 127 or "command not found" in text or "No such file" in text:
        return Checks().add("kela-verify", False, "not installed — the station was not built with "
                            "rugged_operator/setup.sh").row(spec)
    if "a password is required" in text or "incorrect password" in text or "Sorry, try again" in text:
        return amber(spec, "sudo refused on the operator — check the kela password in config.toml")
    parsed = parse_kela_verify(text)
    ctx.facts["kela_verify"] = parsed
    if not parsed["lines"]:
        return amber(spec, "kela-verify printed no PASS/FAIL lines: " + ctx.redact(text.strip()[-160:]))
    c = Checks()
    fails = [d for kind, d in parsed["lines"] if kind == "FAIL"]
    warns = [d for kind, d in parsed["lines"] if kind == "WARN"]
    p, f, w = parsed["summary"] or (len(parsed["lines"]) - len(fails) - len(warns), len(fails), len(warns))
    c.add("kela-verify", res.rc == 0 and not fails, f"exit {res.rc} — {p} pass, {f} fail, {w} warn")
    for d in fails:
        c.add(d, False, "FAIL")
    if warns:
        c.items.append({"label": "warnings (recorded)", "ok": True, "actual": "; ".join(warns)})
    return c.row(spec, kela_verify=parsed["lines"])


def _s82(ctx: Context) -> Row:
    spec = STAGE.spec("S8.2")
    if (r := _no_operator(ctx, spec)):
        return r
    s = _station(ctx)
    conf = parse_station_conf(s["conf"].out) if s["conf"].ok else {}
    tab1 = conf.get("TAB1") or "https://kela.local/"
    c = Checks()
    c.add("kiosk", s["kiosk"].text == "running", "Chrome --kiosk running as kela" if s["kiosk"].text == "running"
          else "not running (escaped, or the graphical session is not up)")
    m = re.search(r"(\d{3})?\s*rc=(\d+)", s["tab1"].out)
    code, rc = (m.group(1), int(m.group(2))) if m else (None, None)
    if rc == 60:
        c.add(f"TAB1 {tab1}", False, "certificate NOT trusted (curl error 60)")
    elif rc != 0:
        c.add(f"TAB1 {tab1}", False, f"no answer (curl rc {rc})")
    else:
        c.add(f"TAB1 {tab1}", code == "200", f"HTTP {code}, certificate trusted")
    served, pinned = fingerprint(s["served"].out), fingerprint(s["nss"].out)
    if served is None:
        c.add("Chrome trust", None, "the hub served no certificate to read")
    elif pinned is None:
        c.add("Chrome trust", False, f"no '{NSS_NICK}' certificate in kela's NSS database")
    else:
        c.add("Chrome trust", served == pinned, "NSS pin matches the served certificate" if served == pinned
              else f"NSS pin {pinned[:23]}… ≠ served {served[:23]}… (kela-cert-ensure has not re-pinned)")
    return c.row(spec, station_conf={k: conf[k] for k in ("TAB1", "TAB2", "TAB3", "KELA_LOCAL_IP",
                                                          "KELA_LOCAL_HOST", "NTP_SERVER") if k in conf})


def _s83(ctx: Context) -> Row:
    spec = STAGE.spec("S8.3")
    if (r := _no_operator(ctx, spec)):
        return r
    s = _station(ctx)
    conf = parse_station_conf(s["conf"].out) if s["conf"].ok else {}
    want = conf.get("NTP_SERVER") or ctx.release.plan.server
    c = Checks()
    sync = s["ntp_sync"].text
    c.add("NTPSynchronized", sync == "yes", sync or "unreadable")
    names = [x for x in s["ntp_server"].out.split() if x]
    if not s["ntp_server"].ok or not names:
        c.add("time server", None, "timedatectl show-timesync unreadable")
    else:
        c.add("time server", want in names, names[0] + ("" if want in names else f" (want {want} — on the fallback?)"))
    if want != ctx.release.plan.server:
        c.add("station.conf NTP_SERVER", False, f"{want} (want {ctx.release.plan.server})")
    return c.row(spec)


def _s84(ctx: Context) -> Row:
    spec = STAGE.spec("S8.4")
    if (r := _no_operator(ctx, spec)):
        return r
    s = _station(ctx)
    c = Checks()
    for label, key, target in (("ping to the internet", "ping_out", OUTSIDE_PING),
                               ("HTTPS to the internet", "https_out", OUTSIDE_HTTPS)):
        rc = _rc(s[key].out)
        if rc is None:
            c.add(label, None, f"{target}: no result")
        else:
            c.add(label, rc != 0, f"{target} blocked" if rc else f"{target} REACHABLE — the kela egress lock is off")
    active = s["anydesk"].text
    c.add("AnyDesk", active == "active", active or "not installed")
    verify = dict((d, kind) for kind, d in (ctx.facts.get("kela_verify") or {}).get("lines", []))
    if ANYDESK_PASSWORD_LINE in verify:
        c.add("AnyDesk unattended password", verify[ANYDESK_PASSWORD_LINE] == "PASS",
              "set (kela-verify)" if verify[ANYDESK_PASSWORD_LINE] == "PASS" else "not set (kela-verify)")
    elif s["anydesk_pw"].text == "set":
        c.add("AnyDesk unattended password", True, "set")
    else:
        unreadable = "rc=2" in s["anydesk_pw"].text
        c.add("AnyDesk unattended password", None if unreadable else False,
              "not readable without root" if unreadable else "not set")
    ident = (ctx.facts.get("identity") or {}).get("operator") or {}
    anydesk_id = (ident.get("build_info") or {}).get("anydesk_id")
    if anydesk_id:
        c.items.append({"label": "AnyDesk ID (recorded)", "ok": True, "actual": anydesk_id})
    return c.row(spec)


def run(ctx: Context) -> Iterator[Row]:
    try:
        yield from guarded(ctx, STAGE, (_s81, _s82, _s83, _s84))
    finally:
        ctx.facts.pop("_s8", None)
