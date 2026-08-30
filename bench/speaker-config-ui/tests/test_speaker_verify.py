"""The speaker verify-only pass (speaker_configure.verify_speaker, TEC-348).

A speaker has no per-unit expectations — the static IP, netmask, gateway, NTP
server, media file and shared password are all station-wide — so the config IS
the expectation and there is no history lookup here. What the tests are for is
the other two things:

  * nothing is written. `FakeSpeaker` records every call, so the assertion is on
    what was SENT, not on what the read-only flag happened to catch.
  * the two effect-based rows behave. "reached at" is the address the scan
    actually found the speaker on (the counterpart of the configure run's
    `set_static_ip` row), and the login itself is the password check — a speaker
    that still answers to 123456 must go red no matter what any page says.
"""
import json

import pytest

from bench_core import MutationBlocked
from speaker_client import (
    DEFAULT_INITIAL_PASSWORD as FACTORY,
    DEFAULT_NEW_PASSWORD as SHARED,
    SpeakerClient,
    SpeakerError,
    md5_hex,
)

import speaker_configure as mod

TARGET = "192.168.88.70"
NETMASK = "255.255.255.0"
GATEWAY = "192.168.88.1"
NTP = "192.168.88.10"
MEDIA = "alarm.mp3"


class FakeResponse:
    def __init__(self, body, status=200):
        self.status_code = status
        self._body = body

    @property
    def text(self):
        return self._body if isinstance(self._body, str) else json.dumps(self._body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not JSON")
        return self._body


class FakeSpeaker:
    """A finished speaker over its CGI API. Answers reads from its own state and
    records every request, so a write shows up as a recorded POST."""

    def __init__(self, *, password=SHARED, ip=TARGET, netmask=NETMASK,
                 gateway=GATEWAY, ntp=NTP, timesetmode=0, dhcp=0,
                 media=MEDIA, serial="TMCS20-0001"):
        self.calls: list[tuple[str, str]] = []
        self.password = password
        self.ip = ip
        self.netmask = netmask
        self.gateway = gateway
        self.ntp = ntp
        self.timesetmode = timesetmode
        self.dhcp = dhcp
        self.media = media
        self.serial = serial

    # -- transport ----------------------------------------------------------
    def get(self, url, **kw):
        self.calls.append(("GET", url))
        return FakeResponse({"result": 0, "reason": "OK",
                             "data": self._read(url)})

    def post(self, url, **kw):
        self.calls.append(("POST", url))
        if "?login" in url:
            supplied = (kw.get("data") or {}).get("password")
            if supplied != md5_hex(self.password):
                return FakeResponse({"result": -1, "reason": "NG", "pid": 0,
                                     "changepwd": 0})
            return FakeResponse({"result": 0, "reason": "OK", "pid": 101,
                                 "changepwd": 0})
        return FakeResponse({"result": 0, "reason": "OK", "reboot": 0})

    def close(self):
        pass

    def _read(self, url) -> dict:
        if "overview.get" in url:
            return {"serialnumber": self.serial, "mac": "AA:BB:CC:00:11:22",
                    "version": "V3.3.39-PR1", "netip": self.ip,
                    "musicfreespace": "4000000"}
        if "datetime.get" in url:
            return {"ntpserverstr": self.ntp, "timesetmode": self.timesetmode,
                    "timezone": 840}
        if "network.get" in url:
            return {"netip": self.ip, "netmask": self.netmask,
                    "gateway": self.gateway, "dhcp": self.dhcp}
        if "musicfile.get" in url:
            return {"musicfile": [{"name": self.media, "description": self.media}]}
        return {}

    # -- assertions ---------------------------------------------------------
    @property
    def writes(self) -> list[str]:
        """Every request that could change the speaker: a `?config=*.set` POST
        or a media upload. A `?login` POST changes nothing."""
        return [url for method, url in self.calls
                if method == "POST" and "?login" not in url]


def client(**kwargs) -> SpeakerClient:
    c = SpeakerClient(host=kwargs.pop("host", TARGET))
    c.s = FakeSpeaker(**kwargs)
    c.device = c.s          # test handle
    return c


def settings(**overrides):
    base = {"static": {"ip": TARGET, "netmask": NETMASK, "gateway": GATEWAY},
            "ntp": {"server": NTP}, "initial_password": FACTORY,
            "new_password": SHARED, "media_slot": 0}
    base.update(overrides)
    return base


def row(result, item):
    return next(c for c in result["verification"] if c["item"] == item)


def items(result):
    return [c["item"] for c in result["verification"]]


# ── mutation-freedom ─────────────────────────────────────────────────────────

def test_a_verify_pass_writes_nothing(tmp_path):
    c = client()
    media = tmp_path / MEDIA
    media.write_bytes(b"x")
    mod.verify_speaker(c, settings=settings(), media_path=str(media))
    assert c.device.writes == []


def test_the_client_is_read_only_for_the_whole_pass():
    c = client()
    mod.verify_speaker(c, settings=settings())
    assert c.read_only is True
    with pytest.raises(MutationBlocked):
        c.set_ntp("10.0.0.1")
    with pytest.raises(MutationBlocked):
        c.set_static_ip("10.0.0.2", NETMASK, GATEWAY)


def test_a_read_only_client_refuses_a_media_upload(tmp_path):
    media = tmp_path / MEDIA
    media.write_bytes(b"x")
    c = client()
    c.set_read_only()
    with pytest.raises(MutationBlocked):
        c.upload_media(str(media), 0)
    assert c.device.writes == []


def test_a_refused_write_never_reaches_the_speaker():
    # The guard has to refuse BEFORE the request, or "read-only" only means
    # "we noticed afterwards".
    c = client()
    c.set_read_only()
    with pytest.raises(MutationBlocked):
        c.set_ntp("10.0.0.1")
    assert c.device.calls == []


# ── the effect-based rows ────────────────────────────────────────────────────

def test_the_reached_address_is_the_static_ip_row():
    # We are talking to it on the target address — the same evidence the
    # configure run waits for after the move, asked the honest way round.
    result = mod.verify_speaker(client(), settings=settings())
    check = row(result, "reached at")
    assert check == {"item": "reached at", "expected": TARGET,
                     "actual": TARGET, "ok": True}
    assert result["ok"] is True


def test_a_speaker_still_on_dhcp_fails_the_reached_row():
    # Found by the subnet sweep on its DHCP address: the static IP never took,
    # which is exactly the case the sweep exists to catch.
    c = client(host="192.168.1.57", ip="192.168.1.57", dhcp=1)
    result = mod.verify_speaker(c, settings=settings())
    check = row(result, "reached at")
    assert check["actual"] == "192.168.1.57"
    assert check["ok"] is False
    assert result["ok"] is False


def test_a_dev_port_forward_is_compared_on_the_address_not_the_port():
    c = client(host=f"{TARGET}:8080")
    assert row(mod.verify_speaker(c, settings=settings()), "reached at")["ok"] is True


def test_a_speaker_still_on_the_factory_password_fails_loudly():
    c = client(password=FACTORY)
    result = mod.verify_speaker(c, settings=settings())
    check = row(result, "admin password")
    assert check["ok"] is False
    assert "factory" in check["actual"]
    assert result["ok"] is False
    # One row, not two: the fallback replaces the read-back row rather than
    # sitting beside it.
    assert items(result).count("admin password") == 1


def test_a_speaker_on_neither_password_stops_the_pass():
    # Nothing can be read, so there is nothing to report — a green sweep here
    # would be a lie and a red one would be a guess.
    c = client(password="something-nobody-knows")
    with pytest.raises(SpeakerError):
        mod.verify_speaker(c, settings=settings())


def test_the_password_row_carries_no_password():
    # These rows are shipped to bench-central verbatim (TEC-349).
    for pw in (SHARED, FACTORY):
        result = mod.verify_speaker(client(password=pw), settings=settings())
        for check in result["verification"]:
            assert SHARED not in str(check), check
            assert FACTORY not in str(check), check


# ── the read-back rows ───────────────────────────────────────────────────────

def test_a_speaker_left_on_the_wrong_ntp_server_fails():
    result = mod.verify_speaker(client(ntp="pool.ntp.org"), settings=settings())
    assert row(result, "NTP server")["ok"] is False
    assert result["ok"] is False


def test_ntp_set_to_manual_time_fails_even_with_the_right_server():
    result = mod.verify_speaker(client(timesetmode=1), settings=settings())
    assert row(result, "NTP server")["ok"] is False


def test_the_wrong_gateway_fails():
    result = mod.verify_speaker(client(gateway="192.168.88.254"),
                                settings=settings())
    assert row(result, "gateway")["ok"] is False


def test_the_media_row_is_asked_when_a_file_is_configured(tmp_path):
    media = tmp_path / MEDIA
    media.write_bytes(b"x")
    result = mod.verify_speaker(client(), settings=settings(),
                                media_path=str(media))
    assert row(result, "media file")["ok"] is True


def test_an_empty_media_slot_fails(tmp_path):
    media = tmp_path / MEDIA
    media.write_bytes(b"x")
    result = mod.verify_speaker(client(media=""), settings=settings(),
                                media_path=str(media))
    assert row(result, "media file")["ok"] is False


def test_no_configured_media_leaves_the_row_out():
    # The file not being on THIS bench PC says nothing about the speaker, so
    # there is nothing to report either way.
    assert "media file" not in items(mod.verify_speaker(client(),
                                                        settings=settings()))


def test_the_record_names_the_speaker_the_same_way_a_configure_run_does():
    result = mod.verify_speaker(client(), settings=settings())
    assert result["name"] == result["hostname"] == "speaker-70"
    assert result["ip"] == TARGET
    assert result["identity"]["serial"] == "TMCS20-0001"
