"""The APU's verification rows (TEC-851).

The APU shares its dashboard API — and so the NTP, timezone and network rows —
with the radar, which `test_magos_verify.py` covers in full. This file
pins the two things only an APU has, plus the wiring that makes the shared rows
reach it:

  * **firmware.** A ROW here, not a refusal. A configure run turns a pre-3.1.2
    unit away before writing anything, because the multi-radar assignment does
    not exist on it; a FINISHED unit on the wrong firmware is a QA finding and
    belongs in the table with everything else.
  * **controlled radars.** Read back off the same settings object `set_radars`
    wrote (the GET is confirmed on 3.1.2-rc5) — and amber, not green, on
    firmware with no GET for it.

Same `FakeAPU` shape as the radar's fake: the HTTP layer is faked so the
client's real `_get`/`_post` guard runs and every request is recorded.
"""
import json
from datetime import datetime, timezone
from email.utils import format_datetime

import pytest

from bench_core import MutationBlocked

import magos_verify as mod
from apu_configure import APUClient, REQUIRED_APU_FIRMWARE
from magos_configure import MagosError

FACTORY = "192.168.40.60"
ASSIGNED = "192.168.88.60"
RADARS = [{"radar_id": "radar_0", "ip": "192.168.88.50", "name": "Radar 0"},
          {"radar_id": "radar_1", "ip": "192.168.88.51", "name": "Radar 1"}]
NTP = "192.168.88.10"
TZ = "Asia/Jerusalem"
SHARED_PW = "s3cret-station-password"


def settings(**overrides):
    base = {"hosts": [FACTORY], "scheme": "http", "insecure": False,
            "username": "admin", "password": SHARED_PW,
            "ntp": NTP, "timezone": TZ, "netmask": "255.255.255.0",
            "gateway": "192.168.88.1", "dns": "192.168.88.1", "iface": "port1"}
    base.update(overrides)
    return base


class FakeResponse:
    def __init__(self, body, status_code=200, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    @property
    def text(self):
        return json.dumps(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


class FakeAPU:
    """A finished APU's dashboard + /apu/v1 API, at the transport layer.

    `radars=None` models firmware that answers no GET on the settings endpoint.
    """

    def __init__(self, *, ip=ASSIGNED, method="manual", netmask="255.255.255.0",
                 gateway="192.168.88.1", dns=("192.168.88.1",), ntp=NTP,
                 ntp_automatic=False, tz=TZ, serial="APU-0007",
                 firmware=REQUIRED_APU_FIRMWARE, radars="assigned",
                 password=SHARED_PW):
        self.requests: list[tuple[str, str]] = []
        self.ip, self.method, self.netmask = ip, method, netmask
        self.gateway, self.dns = gateway, list(dns)
        self.ntp, self.ntp_automatic, self.tz = ntp, ntp_automatic, tz
        self.serial, self.firmware, self.password = serial, firmware, password
        self.radars = ([{"radar_id": r["radar_id"], "name": r["name"],
                         "remote_base_url": f"http://{r['ip']}",
                         "range_gates": None, "mvdr_range_group_size": None,
                         "mvdr_doppler_group_size": None,
                         "detector_threshold": None} for r in RADARS]
                       if radars == "assigned" else radars)
        self.posted: list[tuple[str, dict]] = []
        self.cookies = {"session": "abc"}
        self.headers: dict = {}
        self.verify = True

    def get(self, url, **kw):
        return self._answer("GET", url)

    def post(self, url, json=None, **kw):
        return self._answer("POST", url, json)

    def _answer(self, method, url, body=None):
        path = "/" + url.split("//", 1)[-1].split("/", 1)[-1]
        self.requests.append((method, path))
        if method == "POST":
            self.posted.append((path, body))

        if path.endswith("/login"):
            ok = (body or {}).get("password") == self.password
            return FakeResponse({}, status_code=200 if ok else 401)
        if path.endswith("/dshb/v1/system"):
            # swComponents lists SUB-component versions, not the unit's own
            # firmware — that is /systemStatus's softwareVersion below, which is
            # what `firmware_version` reads (confirmed against a real 3.1.2-rc5).
            return FakeResponse(
                {"ntpAutomatic": self.ntp_automatic, "ntpServer": self.ntp,
                 "timezone": self.tz,
                 "swComponents": [{"name": "chrony", "version": "4.2.0"},
                                  {"name": "phoenix", "version": "1.3.1"}]},
                headers={"Date": format_datetime(datetime.now(timezone.utc),
                                                 usegmt=True)})
        if path.endswith("/dshb/v1/networking"):
            return FakeResponse({"netInterfaces": {"port1": {
                "ip4Method": self.method, "ip4Address": self.ip,
                "ip4Netmask": self.netmask, "ip4Gateway": self.gateway,
                "ip4DNS": self.dns}}})
        if path.endswith("/dshb/v1/systemStatus"):
            payload = {"serialNumber": self.serial, "model": "MSA1588APU",
                       "macAddress": "aa:bb:cc:dd:ee:60"}
            if self.firmware:
                payload["softwareVersion"] = self.firmware
            return FakeResponse(payload)
        if path.endswith("/apu/v1/settings"):
            if self.radars is None:
                return FakeResponse({}, status_code=404)
            return FakeResponse({"radars": self.radars})
        return FakeResponse({}, status_code=404)

    @property
    def writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p in self.requests
                if m == "POST" and not p.endswith("/login")]


def client(host=ASSIGNED, **kwargs) -> APUClient:
    c = APUClient(host)
    c.s = FakeAPU(**kwargs)
    return c


def resolver(expected=None, prior=None):
    return lambda identity: (expected or {}, prior)


def recorded(ip=ASSIGNED, radars=None, **extra):
    return resolver({"ip": ip,
                     "radars": RADARS if radars is None else radars, **extra})


def row(result, item):
    return next(c for c in result["verification"] if c["item"] == item)


# ── mutation-freedom, asserted on the wire ───────────────────────────────────

def test_a_verify_pass_sends_no_write():
    c = client()
    mod.verify_apu(c, settings=settings(), resolve=recorded(), reached=ASSIGNED)
    assert c.s.writes == []


def test_a_verify_pass_logs_in_and_otherwise_only_reads():
    c = client()
    mod.verify_apu(c, settings=settings(), resolve=recorded(), reached=ASSIGNED)
    assert [p for m, p in c.s.requests if m == "POST"] == ["/dshb/v1/login"]


def test_the_client_is_read_only_for_the_whole_pass():
    c = client()
    mod.verify_apu(c, settings=settings(), resolve=recorded(), reached=ASSIGNED)
    assert c.read_only is True
    with pytest.raises(MutationBlocked):
        c.set_radars(RADARS)
    with pytest.raises(MutationBlocked):
        c.set_network("port1", "192.168.88.99", "255.255.255.0",
                      "192.168.88.1", "192.168.88.1")


def test_an_apu_on_different_credentials_stops_the_pass():
    c = client(password="something-nobody-knows")
    with pytest.raises(MagosError, match="Cannot log in"):
        mod.verify_apu(c, settings=settings(), resolve=recorded(), reached=ASSIGNED)


# ── firmware: a row on a verify pass, not a refusal ──────────────────────────

def test_the_required_firmware_passes():
    c = client()
    result = mod.verify_apu(c, settings=settings(), resolve=recorded(),
                            reached=ASSIGNED)
    check = row(result, "firmware")
    assert check["ok"] is True
    assert check["actual"] == REQUIRED_APU_FIRMWARE
    assert result["firmware"] == REQUIRED_APU_FIRMWARE


def test_an_rc_build_of_the_required_firmware_passes():
    c = client(firmware="3.1.2-rc5")
    result = mod.verify_apu(c, settings=settings(), resolve=recorded(),
                            reached=ASSIGNED)
    assert row(result, "firmware")["ok"] is True


def test_a_finished_apu_on_old_firmware_fails_rather_than_refusing_to_check():
    # A configure run turns this unit away before writing anything. A verify
    # pass has nothing to protect, so the finding goes in the table — the point
    # of a QA sweep is to surface it, not to decline to look.
    c = client(firmware="3.0.1")
    result = mod.verify_apu(c, settings=settings(), resolve=recorded(),
                            reached=ASSIGNED)
    check = row(result, "firmware")
    assert check["ok"] is False
    assert check["actual"] == "3.0.1"
    assert result["ok"] is False


def test_unreadable_firmware_fails():
    c = client(firmware=None)
    result = mod.verify_apu(c, settings=settings(), resolve=recorded(),
                            reached=ASSIGNED)
    assert row(result, "firmware")["ok"] is False


# ── the controlled radars ────────────────────────────────────────────────────

def test_the_assigned_radars_are_read_back():
    c = client()
    result = mod.verify_apu(c, settings=settings(), resolve=recorded(),
                            reached=ASSIGNED)
    check = row(result, "controlled radars")
    assert check["ok"] is True
    assert check["expected"] == "192.168.88.50, 192.168.88.51"


def test_an_apu_pointing_at_the_wrong_radars_fails():
    # Two APUs both driving radar_0 is a whole-system fault that nothing else
    # on the bench would catch.
    c = client()
    c.s.radars = [{"radar_id": "radar_0", "name": "Radar 0",
                   "remote_base_url": "http://192.168.88.52"}]
    result = mod.verify_apu(c, settings=settings(), resolve=recorded(),
                            reached=ASSIGNED)
    check = row(result, "controlled radars")
    assert check["ok"] is False
    assert check["actual"] == "192.168.88.52"
    assert result["ok"] is False


def test_an_apu_missing_one_of_its_two_radars_fails():
    c = client()
    c.s.radars = c.s.radars[:1]
    result = mod.verify_apu(c, settings=settings(), resolve=recorded(),
                            reached=ASSIGNED)
    assert row(result, "controlled radars")["ok"] is False


def test_the_radars_are_compared_as_a_set_not_in_order():
    # `set_radars` POSTs an array and the firmware may hand it back in any
    # order. Failing a correct APU on that would be a red row about nothing.
    c = client()
    c.s.radars = list(reversed(c.s.radars))
    result = mod.verify_apu(c, settings=settings(), resolve=recorded(),
                            reached=ASSIGNED)
    assert row(result, "controlled radars")["ok"] is True


def test_firmware_that_does_not_report_the_assignment_is_amber():
    c = client(radars=None)
    result = mod.verify_apu(c, settings=settings(), resolve=recorded(),
                            reached=ASSIGNED)
    check = row(result, "controlled radars")
    assert check["ok"] is None
    assert "does not report" in check["actual"]


def test_no_radars_assigned_by_the_configure_run_is_not_a_failure():
    # A manual-IP run may legitimately assign none, so there is no intent to
    # check. The failing `prior run` row is what covers a unit with no record at
    # all — this is not that case.
    c = client()
    result = mod.verify_apu(c, settings=settings(), resolve=recorded(radars=[]),
                            reached=ASSIGNED)
    check = row(result, "controlled radars")
    assert check["ok"] is None
    assert "no radars were assigned" in check["expected"]


# ── range gates / detector threshold ─────────────────────────────────────────

FILTERS = "range gates / detector threshold"


def test_set_radars_disables_range_gates_and_detector_threshold():
    # null is what the dashboard itself sends when both are switched off.
    c = client()
    c.set_radars(RADARS)
    (path, body), = [(p, b) for p, b in c.s.posted if p.endswith("/apu/v1/settings")]
    assert [(r["radar_id"], r["remote_base_url"]) for r in body["radars"]] == \
        [("radar_0", "http://192.168.88.50"), ("radar_1", "http://192.168.88.51")]
    for radar in body["radars"]:
        assert "range_gates" in radar and radar["range_gates"] is None
        assert "detector_threshold" in radar and radar["detector_threshold"] is None


def test_disabled_range_gates_and_detector_threshold_pass():
    c = client()
    result = mod.verify_apu(c, settings=settings(), resolve=recorded(),
                            reached=ASSIGNED)
    check = row(result, FILTERS)
    assert check["ok"] is True
    assert check["actual"] == "disabled"


@pytest.mark.parametrize("key,label", [("range_gates", "Range Gates"),
                                       ("detector_threshold", "Detector Threshold")])
def test_either_one_left_enabled_fails(key, label):
    c = client()
    c.s.radars[1][key] = 12
    result = mod.verify_apu(c, settings=settings(), resolve=recorded(),
                            reached=ASSIGNED)
    check = row(result, FILTERS)
    assert check["ok"] is False
    assert check["actual"] == f"radar_1 {label}=12"
    assert result["ok"] is False


def test_filters_on_firmware_that_does_not_report_settings_are_amber():
    c = client(radars=None)
    result = mod.verify_apu(c, settings=settings(), resolve=recorded(),
                            reached=ASSIGNED)
    assert row(result, FILTERS)["ok"] is None


def test_filters_not_checked_when_no_radars_were_assigned():
    c = client()
    result = mod.verify_apu(c, settings=settings(), resolve=recorded(radars=[]),
                            reached=ASSIGNED)
    assert row(result, FILTERS)["ok"] is None


# ── the shared rows reach the APU too ────────────────────────────────────────

def test_the_address_we_reached_it_on_is_the_check():
    c = client(host="192.168.88.61", ip="192.168.88.61")
    result = mod.verify_apu(c, settings=settings(), resolve=recorded(),
                            reached="192.168.88.61")
    assert row(result, "reached at")["ok"] is False
    assert row(result, "static IP")["ok"] is False


def test_a_wrong_ntp_server_fails():
    c = client(ntp="pool.ntp.org")
    result = mod.verify_apu(c, settings=settings(), resolve=recorded(),
                            reached=ASSIGNED)
    assert row(result, "NTP server")["ok"] is False


def test_a_wrong_timezone_fails():
    c = client(tz="UTC")
    result = mod.verify_apu(c, settings=settings(), resolve=recorded(),
                            reached=ASSIGNED)
    assert row(result, "timezone")["ok"] is False


def test_the_apu_has_no_rf_channel_row():
    # The channel belongs to the radars the APU controls, not to the APU.
    c = client()
    result = mod.verify_apu(c, settings=settings(), resolve=recorded(),
                            reached=ASSIGNED)
    assert "RF channel" not in [r["item"] for r in result["verification"]]


# ── the prior run, and what is never in a row ────────────────────────────────

def test_a_missing_configure_record_fails_the_pass():
    missing = {"item": "prior run", "expected": "a recorded configure run",
               "actual": "none found", "ok": False}
    c = client()
    result = mod.verify_apu(c, settings=settings(),
                            resolve=resolver({"ip": ASSIGNED, "radars": RADARS},
                                             missing),
                            reached=ASSIGNED)
    assert result["verification"][0]["item"] == "prior run"
    assert result["ok"] is False


def test_no_row_carries_the_password():
    c = client()
    result = mod.verify_apu(c, settings=settings(), resolve=recorded(),
                            reached=ASSIGNED)
    for check in result["verification"]:
        assert SHARED_PW not in str(check), check


def test_a_fully_correct_apu_passes():
    c = client()
    result = mod.verify_apu(c, settings=settings(), resolve=recorded(),
                            reached=ASSIGNED)
    assert result["ok"] is True
    assert result["verified"] is True
    assert result["identity"]["serial"] == "APU-0007"


def test_every_row_uses_the_shared_schema():
    c = client()
    result = mod.verify_apu(c, settings=settings(), resolve=recorded(),
                            reached=ASSIGNED)
    assert result["verification"]
    for check in result["verification"]:
        assert set(check) == {"item", "expected", "actual", "ok"}
        assert check["ok"] in (True, False, None)


# ── the configure run's own re-read uses the same rows ───────────────────────

def test_the_post_configure_recheck_emits_the_same_rows(monkeypatch):
    fake = FakeAPU()

    def preloaded(host, **kw):
        c = APUClient(host)
        c.s = fake
        return c

    monkeypatch.setattr(mod, "APUClient", preloaded)
    rows = mod.recheck_apu_at(ASSIGNED, settings=settings(),
                              firmware=REQUIRED_APU_FIRMWARE,
                              expected={"ip": ASSIGNED, "radars": RADARS})
    verified = mod.verify_apu(client(), settings=settings(), resolve=recorded(),
                              reached=ASSIGNED)
    assert [r["item"] for r in rows] == \
        [r["item"] for r in verified["verification"] if r["item"] != "reached at"]
    assert all(r["ok"] is not False for r in rows)
    assert fake.writes == []


def test_a_recheck_that_cannot_reach_the_unit_is_amber_not_red(monkeypatch):
    def boom(host, **kw):
        raise MagosError("connection refused")

    monkeypatch.setattr(mod, "APUClient", boom)
    rows = mod.recheck_apu_at(ASSIGNED, settings=settings(),
                              expected={"ip": ASSIGNED, "radars": RADARS})
    assert len(rows) == 1
    assert rows[0]["ok"] is None
