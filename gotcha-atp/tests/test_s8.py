"""S8 against output shaped like rugged_operator/setup.sh's station (kela-verify, station.conf)."""
from pathlib import Path

from gotcha_atp import release
from gotcha_atp.access.creds import Credentials, Redactor
from gotcha_atp.access.exec import ExecResult
from gotcha_atp.access.session import Session, Unit
from gotcha_atp.context import Context
from gotcha_atp.stages import s8

ROOT = Path(__file__).resolve().parents[1]
REL = release.load(ROOT / "release.yaml")
FP = "sha256 Fingerprint=AB:CD:EF:01"

VERIFY = """\
--- site addressing (single source of truth: /etc/kela/station.conf) ---
PASS  station.conf present
PASS  hub reachable (kela.local)
--- remote access (a FAIL here means a stranded station) ---
PASS  anydesk service active
FAIL  anydesk unattended password set
--- kiosk runtime ---
WARN  kiosk service active

kela-verify: 3 pass, 1 fail, 1 warn
"""
CONF = """\
# Kela station addressing — written by operator-setup.sh 2026-07-19.1.
SITE_NAME='gotcha-x'
KELA_LOCAL_IP='192.168.88.10'
KELA_LOCAL_HOST='kela.local'
TAB1='https://kela.local/'
NTP_SERVER='192.168.88.10'
"""


class FakeSession:
    has_operator = True
    operator_error = ""

    def __init__(self, sections=None, verify=ExecResult(1, VERIFY)):
        self._sections, self._verify = sections or {}, verify

    def sections(self, target, commands, timeout=60):
        return {k: self._sections.get(k, ExecResult(0, "")) for k in commands}

    def as_root(self, target, command, password, timeout=60):
        return self._verify


def _ctx(session):
    ctx = Context(unit=Unit("gotcha-x"), creds=Credentials(ssh_password="pw"), redact=Redactor(), release=REL)
    ctx.session = session
    return ctx


def _checks(row):
    return {c["label"]: c["ok"] for c in row.detail["checks"]}


def test_parsers():
    p = s8.parse_kela_verify(VERIFY)
    assert p["summary"] == (3, 1, 1) and ("FAIL", "anydesk unattended password set") in p["lines"]
    assert s8.parse_station_conf(CONF)["NTP_SERVER"] == "192.168.88.10"
    assert s8.fingerprint("SHA256 Fingerprint=ab:cd") == "AB:CD" and s8.fingerprint("") is None


def test_s81_lists_failures_and_feeds_s84():
    ctx = _ctx(FakeSession())
    row = s8._s81(ctx)
    ch = _checks(row)
    assert row.state == "fail" and ch["anydesk unattended password set"] is False
    assert ctx.facts["kela_verify"]["summary"] == (3, 1, 1)
    row = s8._s84(_ctx_with(ctx, {"ping_out": ExecResult(0, "rc=1\n"), "https_out": ExecResult(0, "rc=7\n"),
                                  "anydesk": ExecResult(0, "active\n")}))
    ch = _checks(row)
    assert ch["ping to the internet"] and ch["HTTPS to the internet"] and ch["AnyDesk"]
    assert ch["AnyDesk unattended password"] is False      # taken from kela-verify's FAIL line


def _ctx_with(ctx, sections):
    ctx.session = FakeSession(sections)
    ctx.facts.pop("_s8", None)
    return ctx


def test_s81_not_installed_and_sudo_refused():
    r = s8._s81(_ctx(FakeSession(verify=ExecResult(1, "", "sudo: /usr/local/sbin/kela-verify: command not found"))))
    assert r.state == "fail"
    r = s8._s81(_ctx(FakeSession(verify=ExecResult(1, "", "sudo: a password is required"))))
    assert r.state == "amber"


def test_s82_trust_pin_and_tab():
    good = {"conf": ExecResult(0, CONF), "kiosk": ExecResult(0, "running\n"), "tab1": ExecResult(0, "200 rc=0\n"),
            "served": ExecResult(0, FP), "nss": ExecResult(0, FP)}
    assert s8._s82(_ctx(FakeSession(good))).state == "pass"
    stale = {**good, "nss": ExecResult(0, "sha256 Fingerprint=00:11")}
    assert _checks(s8._s82(_ctx(FakeSession(stale))))["Chrome trust"] is False
    untrusted = {**good, "tab1": ExecResult(0, "000 rc=60\n")}
    assert _checks(s8._s82(_ctx(FakeSession(untrusted))))["TAB1 https://kela.local/"] is False


def test_s83_fallback_server_fails():
    base = {"conf": ExecResult(0, CONF), "ntp_sync": ExecResult(0, "yes\n")}
    ok = s8._s83(_ctx(FakeSession({**base, "ntp_server": ExecResult(0, "192.168.88.10\n192.168.88.10\n")})))
    assert ok.state == "pass"
    fb = s8._s83(_ctx(FakeSession({**base, "ntp_server": ExecResult(0, "91.189.91.157\nntp.ubuntu.com\n")})))
    assert _checks(fb)["time server"] is False


def test_s84_egress_open_fails():
    row = s8._s84(_ctx(FakeSession({"ping_out": ExecResult(0, "rc=0\n"), "https_out": ExecResult(0, "rc=0\n"),
                                    "anydesk": ExecResult(0, "active\n"), "anydesk_pw": ExecResult(0, "set\n")})))
    ch = _checks(row)
    assert ch["ping to the internet"] is False and ch["HTTPS to the internet"] is False


def test_no_operator_is_amber():
    s = FakeSession()
    s.has_operator, s.operator_error = False, "no gotcha-x-operator peer on the tailnet"
    for fn in (s8._s81, s8._s82, s8._s83, s8._s84):
        assert fn(_ctx(s)).state == "amber"


def test_as_root_retries_with_password_on_stdin(monkeypatch):
    calls = []

    def fake_exec(self, target, script, timeout=30, input_text=None):
        calls.append((script, input_text))
        if script.startswith("sudo -n"):
            return ExecResult(1, "", "sudo: a password is required\n")
        return ExecResult(0, "ok")

    monkeypatch.setattr(Session, "exec", fake_exec)
    r = Session.as_root(object.__new__(Session), "operator", "/usr/local/sbin/kela-verify", "s3cret")
    assert r.ok
    assert calls[1] == ("sudo -S -p '' /usr/local/sbin/kela-verify", "s3cret\n")
    assert all("s3cret" not in script for script, _ in calls)
