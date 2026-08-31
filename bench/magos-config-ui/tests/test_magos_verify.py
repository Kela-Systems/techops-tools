"""The radar's verification rows, and what they refuse to claim (TEC-851).

`FakeRadar` fakes the HTTP layer rather than the client, so the client's REAL
`_get`/`_post` — and therefore its read-only guard — is what runs, and every
request is recorded. Mutation-freedom is then asserted on what was sent, not on
what the flag caught.

The radar's per-unit intent (which channel it is, and so which address it was
given) lives in its configure record, so the "no record" cases matter as much as
the happy path: a radar nobody ever provisioned must not verify green.

`RF channel` is honestly amber rather than green on firmware that doesn't report
the variant the WebSocket set, and the tests pin that. `ok=None` is not a soft
pass: `verification_outcome` leaves it out of the verdict, which is the point —
a row that says "I could not look" must not be counted as either answer.

The device's own clock is checked by nothing here, deliberately, and a test pins
its absence. `FakeRadar` still serves a `Date` header (including a wildly wrong
one, and none at all) so that staying out of the verdict is asserted against a
device that offers the temptation, rather than by omission.
"""
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from bench_core import MutationBlocked

import magos_verify as mod
from magos_configure import MagosClient, MagosError

FACTORY = "192.168.40.50"
ASSIGNED = "192.168.88.51"
NTP = "192.168.88.10"
TZ = "Asia/Jerusalem"
SHARED_PW = "s3cret-station-password"


def settings(**overrides):
    base = {"hosts": [FACTORY], "scheme": "http", "insecure": False,
            "username": "admin", "password": SHARED_PW,
            "ntp": NTP, "timezone": TZ, "netmask": "255.255.255.0",
            "gateway": "192.168.88.1", "dns": "192.168.88.1"}
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


class FakeRadar:
    """A finished radar's dashboard API, at the transport layer.

    `variant=None` and `variants={}` model the firmware that reports no RF
    channel at all. `clock` sets the `Date` header the replies carry (None for
    firmware that sends none) — no row checks it, and one test exists to keep it
    that way.
    """

    def __init__(self, *, ip=ASSIGNED, prefix=24, method="manual",
                 netmask="255.255.255.0", gateway="192.168.88.1",
                 dns=("192.168.88.1",), ntp=NTP, ntp_automatic=False, tz=TZ,
                 serial="AR300-0091", variant="chan1",
                 variants=("chan0", "chan1", "chan2", "chan3"),
                 clock="now", flat_schema=False, password=SHARED_PW):
        self.requests: list[tuple[str, str]] = []
        self.ip, self.prefix, self.method = ip, prefix, method
        self.netmask, self.gateway, self.dns = netmask, gateway, list(dns)
        self.ntp, self.ntp_automatic, self.tz = ntp, ntp_automatic, tz
        self.serial, self.variant, self.variants = serial, variant, list(variants)
        self.clock, self.flat_schema, self.password = clock, flat_schema, password
        self.cookies = {"session": "abc"}
        self.headers: dict = {}
        self.verify = True

    # -- the requests.Session surface the client uses ----------------------
    def get(self, url, **kw):
        return self._answer("GET", url)

    def post(self, url, json=None, **kw):
        return self._answer("POST", url, json)

    def _path(self, url) -> str:
        return "/" + url.split("//", 1)[-1].split("/", 1)[-1]

    def _answer(self, method, url, body=None):
        path = self._path(url)
        self.requests.append((method, path))

        if path.endswith("/login"):
            ok = (body or {}).get("password") == self.password
            return FakeResponse({}, status_code=200 if ok else 401)
        if path.endswith("/dshb/v1/system"):
            return FakeResponse(
                {"ntpAutomatic": self.ntp_automatic, "ntpServer": self.ntp,
                 "timezone": self.tz, "swComponents": [{"version": "3.1.0"}]},
                headers=self._date_header())
        if path.endswith("/dshb/v1/networking"):
            return FakeResponse(self._networking())
        if path.endswith("/dshb/v1/systemStatus"):
            payload = {"serialNumber": self.serial, "model": "AR-300",
                       "macAddress": "aa:bb:cc:dd:ee:01"}
            if self.variant:
                payload["variant"] = self.variant
            return FakeResponse(payload)
        if path.endswith("/radar/v1/listVariants"):
            return FakeResponse({"variantList": [
                {"id": v, "description": f"Channel {v[-1]}"} for v in self.variants]})
        return FakeResponse({}, status_code=404)

    def _date_header(self) -> dict:
        if self.clock is None:
            return {}
        when = (datetime.now(timezone.utc) if self.clock == "now" else self.clock)
        return {"Date": format_datetime(when, usegmt=True)}

    def _networking(self) -> dict:
        if self.flat_schema:
            return {"ip4Method": self.method,
                    "ip4Address": f"{self.ip}/{self.prefix}" if self.ip else "",
                    "ip4Gateway": self.gateway, "ip4DNS": self.dns}
        return {"netInterfaces": {"port1": {
            "ip4Method": self.method, "ip4Address": self.ip,
            "ip4Netmask": self.netmask, "ip4Gateway": self.gateway,
            "ip4DNS": self.dns}}}

    @property
    def writes(self) -> list[tuple[str, str]]:
        """Every recorded request that would change the radar. A POST to /login
        is a session, not a device change, so it is not one."""
        return [(m, p) for m, p in self.requests
                if m == "POST" and not p.endswith("/login")]


def client(host=ASSIGNED, **kwargs) -> MagosClient:
    c = MagosClient(host)
    c.s = FakeRadar(**kwargs)
    return c


def resolver(expected=None, prior=None):
    """`MagosBench.verify_resolver`'s contract."""
    return lambda identity: (expected or {}, prior)


def recorded(ip=ASSIGNED, channel="1", **extra):
    return resolver({"ip": ip, "channel": channel, **extra})


def row(result, item):
    return next(c for c in result["verification"] if c["item"] == item)


def items(result):
    return [c["item"] for c in result["verification"]]


# ── mutation-freedom, asserted on the wire ───────────────────────────────────

def test_a_verify_pass_sends_no_write():
    c = client()
    mod.verify_radar(c, settings=settings(), resolve=recorded(), reached=ASSIGNED)
    assert c.s.writes == []


def test_a_verify_pass_logs_in_and_otherwise_only_reads():
    c = client()
    mod.verify_radar(c, settings=settings(), resolve=recorded(), reached=ASSIGNED)
    methods = {m for m, _ in c.s.requests}
    assert methods == {"GET", "POST"}
    assert [p for m, p in c.s.requests if m == "POST"] == ["/dshb/v1/login"]


def test_the_client_is_read_only_for_the_whole_pass():
    c = client()
    mod.verify_radar(c, settings=settings(), resolve=recorded(), reached=ASSIGNED)
    assert c.read_only is True
    with pytest.raises(MutationBlocked):
        c.set_ntp(NTP)
    with pytest.raises(MutationBlocked):
        c.set_network("192.168.88.99/24", "192.168.88.1", "192.168.88.1")


def test_a_radar_on_different_credentials_stops_the_pass():
    # Nothing can be read, so a green table would be a lie and a red one a guess
    # about which of a dozen things is wrong.
    c = client(password="something-nobody-knows")
    with pytest.raises(MagosError, match="Cannot log in"):
        mod.verify_radar(c, settings=settings(), resolve=recorded(), reached=ASSIGNED)


# ── the address row: effect-based ────────────────────────────────────────────

def test_the_address_we_reached_it_on_is_the_check():
    c = client()
    result = mod.verify_radar(c, settings=settings(), resolve=recorded(),
                              reached=ASSIGNED)
    assert row(result, "reached at") == {"item": "reached at",
                                         "expected": ASSIGNED,
                                         "actual": ASSIGNED, "ok": True}
    assert result["ok"] is True


def test_a_radar_answering_somewhere_else_fails():
    # Swept up at 192.168.88.53 while its record says .51: it either never took
    # the address, or something else has it.
    c = client(host="192.168.88.53", ip="192.168.88.53")
    result = mod.verify_radar(c, settings=settings(), resolve=recorded(),
                              reached="192.168.88.53")
    check = row(result, "reached at")
    assert check["expected"] == ASSIGNED
    assert check["actual"] == "192.168.88.53"
    assert check["ok"] is False
    assert result["ok"] is False


def test_a_radar_that_never_left_the_factory_address_fails():
    c = client(host=FACTORY, ip=FACTORY)
    result = mod.verify_radar(c, settings=settings(), resolve=recorded(),
                              reached=FACTORY)
    assert row(result, "reached at")["ok"] is False


def test_no_recorded_address_fails_rather_than_comparing_it_to_itself():
    c = client()
    result = mod.verify_radar(c, settings=settings(), resolve=resolver(),
                              reached=ASSIGNED)
    check = row(result, "reached at")
    assert check["ok"] is False
    assert "no recorded address" in check["actual"]


# ── the network read-back ────────────────────────────────────────────────────

def test_a_radar_holding_a_lease_that_matches_still_fails():
    # It answers on the right address, but the address is DHCP: it moves the
    # next time the lease turns over. A row that only probed reachability would
    # have passed this.
    c = client(method="dhcp")
    result = mod.verify_radar(c, settings=settings(), resolve=recorded(),
                              reached=ASSIGNED)
    assert row(result, "reached at")["ok"] is True
    assert row(result, "static IP")["ok"] is False
    assert "dhcp" in row(result, "static IP")["actual"]


def test_the_legacy_flat_networking_schema_reads_back_the_same():
    # Firmware that stores one CIDR address instead of a per-interface block —
    # `set_network` handles both, so the read-back has to as well, netmask
    # recovered from the prefix.
    c = client(flat_schema=True, prefix=24)
    result = mod.verify_radar(c, settings=settings(), resolve=recorded(),
                              reached=ASSIGNED)
    assert row(result, "static IP")["ok"] is True
    assert row(result, "netmask")["ok"] is True
    assert result["ok"] is True


def test_a_wrong_netmask_fails():
    c = client(netmask="255.255.0.0")
    result = mod.verify_radar(c, settings=settings(), resolve=recorded(),
                              reached=ASSIGNED)
    assert row(result, "netmask")["ok"] is False


def test_a_wrong_gateway_fails():
    c = client(gateway="192.168.88.254")
    result = mod.verify_radar(c, settings=settings(), resolve=recorded(),
                              reached=ASSIGNED)
    assert row(result, "gateway")["ok"] is False


def test_a_wrong_primary_dns_fails():
    c = client(dns=("8.8.8.8",))
    result = mod.verify_radar(c, settings=settings(), resolve=recorded(),
                              reached=ASSIGNED)
    assert row(result, "DNS")["ok"] is False


def test_no_dns_at_all_fails():
    c = client(dns=())
    result = mod.verify_radar(c, settings=settings(), resolve=recorded(),
                              reached=ASSIGNED)
    assert row(result, "DNS")["ok"] is False


# ── time: the read-back and the effect ───────────────────────────────────────

def test_a_wrong_ntp_server_fails():
    c = client(ntp="pool.ntp.org")
    result = mod.verify_radar(c, settings=settings(), resolve=recorded(),
                              reached=ASSIGNED)
    assert row(result, "NTP server")["ok"] is False


def test_automatic_ntp_fails_even_with_the_right_server_stored():
    # `ntpAutomatic` back on means the unit picks its own server, with ours left
    # in a field it no longer reads.
    c = client(ntp_automatic=True)
    result = mod.verify_radar(c, settings=settings(), resolve=recorded(),
                              reached=ASSIGNED)
    assert row(result, "NTP server")["ok"] is False
    assert "ntpAutomatic=on" in row(result, "NTP server")["actual"]


def test_a_wrong_timezone_fails():
    c = client(tz="UTC")
    result = mod.verify_radar(c, settings=settings(), resolve=recorded(),
                              reached=ASSIGNED)
    assert row(result, "timezone")["ok"] is False


@pytest.mark.parametrize("clock", [
    "now",
    datetime.now(timezone.utc) - timedelta(days=400),   # never set / dead RTC
    datetime.now(timezone.utc) + timedelta(seconds=90),  # the first APU verified
    None,                                                # no Date header at all
], ids=["right", "epoch", "drifting", "absent"])
def test_the_device_clock_is_not_checked_whatever_it_reads(clock):
    """Nothing on the bench sets these clocks and the NTP server is only
    reachable from assembly, so the time a unit reports is not evidence of
    anything: a dead RTC and a good unit awaiting its first sync read the same.
    The NTP *configuration* is checked instead, and the clock is left alone."""
    result = mod.verify_radar(client(clock=clock), settings=settings(),
                              resolve=recorded(), reached=ASSIGNED)
    assert not [c for c in result["verification"] if "clock" in c["item"]]
    assert result["ok"] is True
    assert row(result, "NTP server")["ok"] is True


# ── the RF channel: a known gap, and honest about it ─────────────────────────

def test_the_rf_channel_is_checked_when_the_radar_reports_it():
    c = client(variant="chan1")
    result = mod.verify_radar(c, settings=settings(), resolve=recorded(channel="1"),
                              reached=ASSIGNED)
    assert row(result, "RF channel") == {"item": "RF channel", "expected": "chan1",
                                         "actual": "chan1", "ok": True}


def test_a_radar_on_the_wrong_rf_channel_fails():
    # Two neighbouring radars on one frequency is the fault this row exists for.
    c = client(variant="chan3")
    result = mod.verify_radar(c, settings=settings(), resolve=recorded(channel="1"),
                              reached=ASSIGNED)
    check = row(result, "RF channel")
    assert check["ok"] is False
    assert check["actual"] == "chan3"
    assert result["ok"] is False


def test_a_firmware_that_does_not_report_the_channel_is_amber():
    # `set_channel` writes over a WebSocket and there is no documented read, so
    # nothing here may be guessed into a pass.
    c = client(variant=None)
    result = mod.verify_radar(c, settings=settings(), resolve=recorded(channel="1"),
                              reached=ASSIGNED)
    check = row(result, "RF channel")
    assert check["ok"] is None
    assert "does not report" in check["actual"]


def test_a_variant_the_radar_does_not_list_is_not_trusted_as_a_mismatch():
    # A field called `variant` on some future firmware need not mean the RF
    # channel. Reading one and failing the unit on it would be a red row about
    # nothing.
    c = client(variant="something-else")
    result = mod.verify_radar(c, settings=settings(), resolve=recorded(channel="1"),
                              reached=ASSIGNED)
    assert row(result, "RF channel")["ok"] is None


def test_a_manual_ip_run_has_no_channel_to_check():
    c = client()
    result = mod.verify_radar(c, settings=settings(),
                              resolve=recorded(channel="other"), reached=ASSIGNED)
    check = row(result, "RF channel")
    assert check["ok"] is None
    assert "no channel was assigned" in check["expected"]


# ── the channel a verify run is allowed to record (TEC-352) ──────────────────
#
# A verify pass assigns nothing, so the only channel its record may carry is one
# the radar stated. These cover the four ways that can go, because the QA label
# prints whatever lands in the record.

def test_a_confirmed_channel_is_recorded_for_the_label():
    c = client(variant="chan1")
    result = mod.verify_radar(c, settings=settings(), resolve=recorded(channel="1"),
                              reached=ASSIGNED)
    assert mod.confirmed_channel(result["verification"]) == "1"


def test_an_unread_channel_is_not_recorded():
    # Amber, not green: the radar never said, so there is nothing to print. The
    # label falls back to the address rather than reprinting the configure run's
    # intent as though it had been re-read.
    c = client(variant=None)
    result = mod.verify_radar(c, settings=settings(), resolve=recorded(channel="1"),
                              reached=ASSIGNED)
    assert mod.confirmed_channel(result["verification"]) is None


def test_a_channel_that_came_back_wrong_is_not_recorded():
    # The one case where printing the expected channel would be actively
    # dangerous — the radar is on chan3 and the sticker would claim chan1.
    c = client(variant="chan3")
    result = mod.verify_radar(c, settings=settings(), resolve=recorded(channel="1"),
                              reached=ASSIGNED)
    assert mod.confirmed_channel(result["verification"]) is None


def test_a_manual_ip_run_records_no_channel():
    c = client()
    result = mod.verify_radar(c, settings=settings(),
                              resolve=recorded(channel="other"), reached=ASSIGNED)
    assert mod.confirmed_channel(result["verification"]) is None


@pytest.mark.parametrize("actual,expected", [
    ("chan0", "0"),          # channel 0 is real, and falsy-looking
    ("chan3", "3"),
    ("CHAN2", "2"),
    ("2", "2"),              # already bare
    ("north-face", None),    # a renamed channel key has no digit to print
    ("chan", None),
    ("", None),
    (None, None),
])
def test_only_a_variant_with_a_digit_yields_a_channel(actual, expected):
    rows = [{"item": "RF channel", "expected": "x", "actual": actual, "ok": True}]
    assert mod.confirmed_channel(rows) == expected


def test_rows_without_an_rf_channel_row_are_fine():
    # The APU has no such row at all.
    assert mod.confirmed_channel([{"item": "static IP", "actual": "x", "ok": True}]) is None
    assert mod.confirmed_channel([]) is None


# ── the prior run, and what is never in a row ────────────────────────────────

def test_a_missing_configure_record_fails_the_pass():
    # What stops a box nobody provisioned from earning a QA label (TEC-352).
    missing = {"item": "prior run", "expected": "a recorded configure run",
               "actual": "none found", "ok": False}
    c = client()
    result = mod.verify_radar(c, settings=settings(),
                              resolve=resolver({"ip": ASSIGNED, "channel": "1"},
                                               missing),
                              reached=ASSIGNED)
    assert result["verification"][0]["item"] == "prior run"
    assert result["ok"] is False


def test_no_row_carries_the_password():
    # These rows are shipped to bench-central verbatim (TEC-349).
    c = client()
    result = mod.verify_radar(c, settings=settings(), resolve=recorded(),
                              reached=ASSIGNED)
    for check in result["verification"]:
        assert SHARED_PW not in str(check), check


def test_a_fully_correct_radar_passes():
    c = client()
    result = mod.verify_radar(c, settings=settings(), resolve=recorded(),
                              reached=ASSIGNED)
    assert result["ok"] is True
    assert result["verified"] is True
    assert result["verify_detail"] is None
    assert result["identity"]["serial"] == "AR300-0091"


def test_the_failure_detail_names_the_rows_that_failed():
    c = client(tz="UTC", gateway="192.168.88.254")
    result = mod.verify_radar(c, settings=settings(), resolve=recorded(),
                              reached=ASSIGNED)
    assert result["verified"] is False
    assert "timezone" in result["verify_detail"]
    assert "gateway" in result["verify_detail"]


def test_every_row_uses_the_shared_schema():
    # The rows go straight into the run record and the shared table; a row
    # missing a key renders as an empty cell rather than an error.
    c = client()
    result = mod.verify_radar(c, settings=settings(), resolve=recorded(),
                              reached=ASSIGNED)
    assert result["verification"]
    for check in result["verification"]:
        assert set(check) == {"item", "expected", "actual", "ok"}
        assert check["ok"] in (True, False, None)


# ── the configure run's own re-read uses the same rows ───────────────────────

def test_the_post_configure_recheck_emits_the_same_rows(monkeypatch):
    # The parity the other five tools' tests assert: configure and verify have
    # to describe a finished unit the same way, or the sweep checks something
    # else than the run that produced it.
    fake = FakeRadar()
    monkeypatch.setattr(mod, "MagosClient", lambda host, **kw: _preloaded(host, fake))
    rows = mod.recheck_radar_at(ASSIGNED, settings=settings(),
                               expected={"ip": ASSIGNED, "channel": "1"})
    verified = mod.verify_radar(client(), settings=settings(),
                                resolve=recorded(), reached=ASSIGNED)
    assert [r["item"] for r in rows] == \
        [r["item"] for r in verified["verification"] if r["item"] != "reached at"]
    assert all(r["ok"] is not False for r in rows)
    assert fake.writes == []


def test_a_recheck_that_cannot_reach_the_unit_is_amber_not_red(monkeypatch):
    # By the time this runs the radar has already been provisioned. A re-read
    # that could not happen says nothing about whether the writes landed, and
    # failing the run on it would send a good unit back.
    def boom(host, **kw):
        raise MagosError("connection refused")

    monkeypatch.setattr(mod, "MagosClient", boom)
    rows = mod.recheck_radar_at(ASSIGNED, settings=settings(),
                                expected={"ip": ASSIGNED, "channel": "1"})
    assert len(rows) == 1
    assert rows[0]["ok"] is None
    assert "could not re-read" in rows[0]["actual"]


def _preloaded(host, fake) -> MagosClient:
    c = MagosClient(host)
    c.s = fake
    return c
