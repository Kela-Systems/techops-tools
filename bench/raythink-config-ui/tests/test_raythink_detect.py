"""Telling the two Raythink camera generations apart (raythink_client).

Everything above the client is written against one interface, so this is the
only place that decides which camera is on the bench — and it decides it twice,
for a reason worth keeping straight:

  * `detect_generation` probes both logins BEFORE anyone has a session, because
    establishing one is the thing that differs. That is what the bench actually
    runs on.
  * `generation_from_firmware` reads the version convention AFTER login. It is
    the definitive answer, but it arrives too late to open the connection with,
    so it serves as a cross-check.

No hardware: the two probes are faked at `requests.post`.
"""
import pytest

import raythink_client as mod
from raythink_base import GEN_REST, GEN_RPC2, CameraError
from raythink_camera import RaythinkCameraClient
from raythink_rest import RaythinkRestClient

REST_FW = "B1.2.01.01.15, 2026-05-14"
RPC2_FW = "1.000.General 00.0.T, build: 2025-04-09"


class Reply:
    def __init__(self, body):
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


def probes(monkeypatch, *, rest=None, rpc2=None):
    """Fake both endpoints. `None` for either means 'this route does not exist',
    which is how the OTHER generation actually behaves — neither implements the
    other's login."""
    seen = []

    def post(url, **kwargs):
        seen.append(url)
        if "/v1/token" in url:
            if rest is None:
                raise mod.requests.exceptions.HTTPError("404")
            return Reply(rest)
        if rpc2 is None:
            raise mod.requests.exceptions.HTTPError("404")
        return Reply(rpc2)

    monkeypatch.setattr(mod.requests, "post", post)
    return seen


# ── the pre-login probe ──────────────────────────────────────────────────────

def test_a_newer_camera_answers_the_rest_probe(monkeypatch):
    # No credentials are sent: what is being tested is that the route exists and
    # answers in the vendor's envelope, which is the `Code` field.
    probes(monkeypatch, rest={"Code": 401, "Message": "msg: unauthorized"})
    assert mod.detect_generation("192.168.1.123") == GEN_REST


def test_an_older_camera_answers_the_rpc2_challenge(monkeypatch):
    probes(monkeypatch, rpc2={"result": False,
                              "params": {"random": "12345",
                                         "realm": "Login to KK0552PAZ00681"}})
    assert mod.detect_generation("192.168.1.123") == GEN_RPC2


def test_the_rest_probe_is_tried_first(monkeypatch):
    # Not correctness — the two probes are mutually exclusive — but speed: an
    # older camera 404s the REST route immediately, whereas a newer camera handed
    # the RPC2 probe would have to be waited out.
    seen = probes(monkeypatch, rest={"Code": 200})
    mod.detect_generation("192.168.1.123")
    assert len(seen) == 1 and "/v1/token" in seen[0]


def test_something_else_on_port_80_is_not_a_camera(monkeypatch):
    # The bench range holds a router, a speaker and an NTP server. Answering is
    # not enough; the reply has to be one of the two APIs.
    probes(monkeypatch, rest={"error": "not found"}, rpc2={"result": False,
                                                           "params": {}})
    assert mod.detect_generation("192.168.88.99") is None


def test_a_non_json_reply_is_not_a_camera(monkeypatch):
    probes(monkeypatch, rest=None, rpc2=None)
    assert mod.detect_generation("192.168.88.99") is None


def test_an_unreachable_host_is_not_a_camera(monkeypatch):
    def boom(url, **kwargs):
        raise mod.requests.exceptions.ConnectionError("nothing there")

    monkeypatch.setattr(mod.requests, "post", boom)
    assert mod.detect_generation("192.168.88.99") is None


# ── the firmware convention ──────────────────────────────────────────────────

@pytest.mark.parametrize("firmware, expected", [
    (REST_FW, GEN_REST),
    ("B1.2.01.01.12, 2025-12-18", GEN_REST),
    (RPC2_FW, GEN_RPC2),
    ("2.820.0000000.48.R, build: 2023-11-02", GEN_RPC2),
    # Neither convention: a format nobody has seen. Not a guess — None, so the
    # cross-check says so instead of asserting something it cannot know.
    ("7.1.2", None),
    ("unknown", None),
    ("", None),
])
def test_the_version_convention(firmware, expected):
    assert mod.generation_from_firmware(firmware) == expected


def test_a_firmware_that_disagrees_with_the_probe_warns_without_aborting(caplog):
    # The device is still driven over the API that ANSWERED, which is a fact
    # rather than an inference — but somebody needs to know the convention moved.
    with caplog.at_level("WARNING"):
        mod.check_firmware_generation({"firmware": REST_FW}, GEN_RPC2)
    assert "check whether the firmware version convention has changed" in caplog.text


def test_a_firmware_that_agrees_says_nothing(caplog):
    with caplog.at_level("INFO"):
        mod.check_firmware_generation({"firmware": RPC2_FW}, GEN_RPC2)
    assert caplog.text == ""


def test_an_unrecognised_firmware_is_noted_but_not_a_warning(caplog):
    with caplog.at_level("INFO"):
        mod.check_firmware_generation({"firmware": "7.1.2"}, GEN_REST)
    assert "matches neither version convention" in caplog.text
    assert "WARNING" not in caplog.text


# ── the factory ──────────────────────────────────────────────────────────────

def test_the_factory_builds_the_client_the_probe_chose(monkeypatch):
    probes(monkeypatch, rest={"Code": 401})
    client = mod.open_camera("192.168.1.123")
    assert isinstance(client, RaythinkRestClient)
    assert client.generation == GEN_REST

    probes(monkeypatch, rpc2={"result": False, "params": {"random": "1"}})
    client = mod.open_camera("192.168.1.123")
    assert isinstance(client, RaythinkCameraClient)
    assert client.generation == GEN_RPC2


def test_a_known_generation_skips_the_probe(monkeypatch):
    # The UI's detection loop established this a second ago; re-probing would
    # spend a round trip re-learning it.
    seen = probes(monkeypatch, rest={"Code": 401}, rpc2={"result": True})
    client = mod.open_camera("192.168.1.123", generation=GEN_RPC2)
    assert isinstance(client, RaythinkCameraClient)
    assert seen == []


def test_nothing_answering_fails_with_one_clear_error(monkeypatch):
    # Rather than a login error against a protocol the device never spoke.
    probes(monkeypatch)
    with pytest.raises(CameraError) as e:
        mod.open_camera("192.168.88.99")
    assert "answered either Raythink API" in str(e.value)
