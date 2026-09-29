"""The Raythink verify-only pass and the detection it needs (TEC-348).

Two halves, and the first is the reason this tool went last:

  * detection. Verify is unreachable unless the tool can FIND a finished camera,
    and this one used to probe only the factory address — a camera the bench
    moved to 192.168.88.31 was invisible to it. `find_camera` sweeps the
    assigned static range too.
  * the pass itself. `FakeCamera` records every RPC2 call, so mutation-freedom
    is asserted on what was sent rather than on what the read-only flag caught.

The camera's per-unit expectations (which address it was given, which profile)
come from its configure record, so the "no record" cases matter as much as the
happy path: a camera nobody ever provisioned must not verify green.
"""
import json

import pytest

from bench_core import MutationBlocked
from raythink_base import GEN_REST, GEN_RPC2
from raythink_camera import (
    DEFAULT_INITIAL_PASSWORD as FACTORY_PW,
    MUTATING_RPC_METHODS,
    CameraError,
    RaythinkCameraClient,
)

SHARED = "test-shared-pw"   # a fake on purpose: proving the pipeline
                            # applies the configured password needs a
                            # value, not THE value

import raythink_configure as mod

FACTORY = "192.168.1.123"
ASSIGNED = "192.168.88.31"
NTP = "192.168.88.10"


def settings(**overrides):
    base = {"host": FACTORY, "scheme": "http", "new_password": SHARED,
            "ntp_server": NTP,
            "static": {"subnet_prefix": "192.168.88", "octet_min": 30,
                       "octet_max": 50, "netmask": "255.255.255.0",
                       "gateway": "192.168.88.1"},
            "profiles": {"lan": "profiles/lan.json"}}
    base.update(overrides)
    return base


# ── detection: the range sweep that makes Verify reachable ───────────────────

def test_the_candidate_list_covers_the_factory_address_and_the_whole_range():
    hosts = mod.camera_hosts(settings())
    assert hosts[0] == FACTORY          # a fresh camera wins over a finished one
    assert "192.168.88.30" in hosts
    assert ASSIGNED in hosts
    assert "192.168.88.50" in hosts
    assert "192.168.88.51" not in hosts


def only(host, generation=GEN_RPC2):
    """A `camera_generation` stand-in: `host` is a camera, nothing else is.
    Detection now reports WHICH generation answered, because establishing that
    is the same probe as establishing that this is a camera at all."""
    return lambda h, scheme=None, timeout=None: generation if h == host else None


def test_a_finished_camera_in_the_assigned_range_is_found(monkeypatch):
    # The whole point: before this, a camera at 192.168.88.31 was undetectable
    # and so unverifiable.
    monkeypatch.setattr(mod, "tcp_port_open",
                        lambda host, port, timeout=0: host == ASSIGNED)
    monkeypatch.setattr(mod, "camera_generation", only(ASSIGNED))
    assert mod.find_camera(mod.camera_hosts(settings())) == (ASSIGNED, GEN_RPC2)


def test_a_fresh_camera_on_the_factory_address_is_still_found(monkeypatch):
    monkeypatch.setattr(mod, "tcp_port_open", lambda host, port, timeout=0: True)
    monkeypatch.setattr(mod, "camera_generation",
                        lambda h, scheme=None, timeout=None: GEN_RPC2)
    assert mod.find_camera(mod.camera_hosts(settings())) == (FACTORY, GEN_RPC2)


def test_a_newer_camera_is_found_and_reported_as_one(monkeypatch):
    # Both generations ship on the factory address and look identical from the
    # outside, so the sweep has to say which one it found — the profile that can
    # be sent to it depends on the answer.
    monkeypatch.setattr(mod, "tcp_port_open", lambda host, port, timeout=0: True)
    monkeypatch.setattr(mod, "camera_generation", only(FACTORY, GEN_REST))
    assert mod.find_camera(mod.camera_hosts(settings())) == (FACTORY, GEN_REST)


def test_something_else_answering_in_the_range_is_not_a_camera(monkeypatch):
    # The bench range holds a router, a speaker and an NTP server. Anything that
    # answers port 80 but neither API must be ignored, or Verify would log in to
    # the wrong device.
    monkeypatch.setattr(mod, "tcp_port_open", lambda host, port, timeout=0: True)
    monkeypatch.setattr(mod, "camera_generation", only(ASSIGNED))
    assert mod.find_camera(mod.camera_hosts(settings())) == (ASSIGNED, GEN_RPC2)


def test_nothing_answering_is_no_camera(monkeypatch):
    monkeypatch.setattr(mod, "tcp_port_open", lambda host, port, timeout=0: False)
    monkeypatch.setattr(mod, "camera_generation",
                        lambda h, scheme=None, timeout=None: GEN_RPC2)
    assert mod.find_camera(mod.camera_hosts(settings())) == (None, None)


def test_the_last_seen_address_is_probed_alone_first(monkeypatch):
    # The common case is the same camera still plugged in, and it must cost one
    # connection rather than a sweep of the range.
    probed = []
    monkeypatch.setattr(mod, "tcp_port_open",
                        lambda host, port, timeout=0: probed.append(host) or True)
    monkeypatch.setattr(mod, "camera_generation",
                        lambda h, scheme=None, timeout=None: GEN_RPC2)
    assert mod.find_camera(mod.camera_hosts(settings()),
                           first_guess=ASSIGNED) == (ASSIGNED, GEN_RPC2)
    assert probed == [ASSIGNED]


# ── the verify pass ──────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, body):
        self.status_code = 200
        self._body = body

    @property
    def text(self):
        return json.dumps(self._body)

    def json(self):
        return self._body


class FakeCamera:
    """A finished camera's RPC2 surface, faked at the HTTP layer so the client's
    real `_rpc` — and therefore its read-only guard — is what runs.

    Answers reads from its own state and records every method called, so a write
    shows up as a recorded call."""

    def __init__(self, *, ip=ASSIGNED, netmask="255.255.255.0",
                 gateway="192.168.88.1", dhcp=False, ntp=NTP, ntp_enable=True,
                 serial="KK0552PAZ00681"):
        self.calls: list[str] = []
        self.ip = ip
        self.netmask = netmask
        self.gateway = gateway
        self.dhcp = dhcp
        self.ntp = ntp
        self.ntp_enable = ntp_enable
        self.serial = serial

    # -- the requests.Session half the client talks to --------------------
    def post(self, url, data=None, **kwargs):
        body = json.loads(data)
        return FakeResponse(self._answer(body.get("method"),
                                         body.get("params") or {}))

    def close(self):
        pass

    def _answer(self, method, params) -> dict:
        self.calls.append(method)
        if method == "configManager.getConfig":
            return {"result": True,
                    "params": {"table": self._table(params.get("name"))}}
        if method == "magicBox.getSystemInfo":
            return {"result": True, "params": {"serialNumber": self.serial,
                                               "deviceType": "Raythink"}}
        if method == "magicBox.getSoftwareVersion":
            return {"result": True,
                    "params": {"version": {"Version": "1.000.General 00.0.T, "
                                                      "build: 2025-04-09"}}}
        return {"result": True, "params": {}}

    def _table(self, name) -> dict:
        if name == "NTP":
            return {"Enable": self.ntp_enable, "Address": self.ntp, "Port": 123}
        if name == "Network":
            return {"DefaultInterface": "eth0",
                    "eth0": {"PhysicalAddress": "00:1e:42:aa:bb:01",
                             "DhcpEnable": self.dhcp,
                             "IPAddress": {"IPAddress": self.ip,
                                           "SubnetMask": self.netmask,
                                           "DefaultGateway": self.gateway}}}
        return {}

    @property
    def writes(self) -> list[str]:
        """Every recorded call that would change the camera. Taken from the
        client's own deny-list so the two can't drift apart."""
        return [m for m in self.calls if m in MUTATING_RPC_METHODS]


def client(monkeypatch, *, host=ASSIGNED, password=SHARED, onvif_ok=True,
           **kwargs) -> RaythinkCameraClient:
    c = RaythinkCameraClient(host=host)
    c.device = FakeCamera(**kwargs)
    c.s = c.device
    # The login is faked but its OUTCOME is not: `password` is what the camera
    # actually accepts, so a mismatch leaves client.password unset and the
    # password row goes red — the same way it would on hardware.
    def login(pw):
        if pw != password:
            raise CameraError("Login failed: wrong password")
        c.password = pw
    monkeypatch.setattr(c, "login", login)
    monkeypatch.setattr(c, "onvif_get_users",
                        lambda pw: (onvif_ok and pw == password,
                                    "authenticated (users: admin)" if onvif_ok
                                    else "ONVIF rejected the credentials "
                                         "(NotAuthorized)",
                                    ["admin"]))
    return c


def resolver(expected=None, prior=None):
    """`BenchConfigurator.verify_resolver`'s contract."""
    return lambda identity: (expected or {}, prior)


def recorded(ip=ASSIGNED, profile="lan", ip_mode="static", **extra):
    return resolver({"ip": ip, "profile": profile, "ip_mode": ip_mode,
                     "hostname": "raythink-31", **extra})


def row(result, item):
    return next(c for c in result["verification"] if c["item"] == item)


def items(result):
    return [c["item"] for c in result["verification"]]


def test_a_verify_pass_sends_no_write(monkeypatch):
    c = client(monkeypatch)
    mod.verify_camera(c, settings=settings(), resolve=recorded())
    assert c.device.writes == []


def test_the_client_is_read_only_for_the_whole_pass(monkeypatch):
    c = client(monkeypatch)
    mod.verify_camera(c, settings=settings(), resolve=recorded())
    assert c.read_only is True
    with pytest.raises(MutationBlocked):
        c.set_ntp("10.0.0.1")
    with pytest.raises(MutationBlocked):
        c.sync_time_to_pc()


def test_a_read_only_client_still_answers_reads(monkeypatch):
    # A guard that blocked getConfig alongside setConfig would make the mode
    # useless, and the two method names differ by three letters.
    c = client(monkeypatch)
    c.set_read_only()
    assert c._rpc("configManager.getConfig", {"name": "NTP"})["result"] is True


def test_the_onvif_password_cannot_be_changed_on_a_read_only_client(monkeypatch):
    # ONVIF is a separate protocol, so the RPC2 deny-list does not cover it.
    c = client(monkeypatch)
    c.set_read_only()
    with pytest.raises(MutationBlocked):
        c.set_onvif_password(SHARED, ["admin"])


# ── the address row: effect-based ────────────────────────────────────────────

def test_the_address_we_reached_it_on_is_the_check(monkeypatch):
    c = client(monkeypatch)
    result = mod.verify_camera(c, settings=settings(), resolve=recorded())
    assert row(result, "reached at") == {"item": "reached at",
                                        "expected": ASSIGNED,
                                        "actual": ASSIGNED, "ok": True}
    assert result["ok"] is True


def test_a_camera_answering_somewhere_else_fails(monkeypatch):
    # Found by the sweep at 192.168.88.44 while its record says .31: it either
    # never took the address or something else has it. Either way the old
    # "no answer on X — may still be fine" wording is not good enough.
    c = client(monkeypatch, host="192.168.88.44", ip="192.168.88.44")
    result = mod.verify_camera(c, settings=settings(), resolve=recorded())
    check = row(result, "reached at")
    assert check["expected"] == ASSIGNED
    assert check["actual"] == "192.168.88.44"
    assert check["ok"] is False
    assert result["ok"] is False


def test_a_camera_that_fell_back_to_the_factory_address_fails(monkeypatch):
    c = client(monkeypatch, host=FACTORY, ip=FACTORY)
    result = mod.verify_camera(c, settings=settings(), resolve=recorded())
    assert row(result, "reached at")["ok"] is False


def test_no_recorded_address_fails_rather_than_comparing_it_to_itself(monkeypatch):
    c = client(monkeypatch)
    result = mod.verify_camera(c, settings=settings(), resolve=resolver())
    check = row(result, "reached at")
    assert check["ok"] is False
    assert "no recorded address" in check["actual"]


def test_an_operator_supplied_octet_beats_the_record(monkeypatch):
    c = client(monkeypatch)
    result = mod.verify_camera(c, settings=settings(), resolve=recorded(ip=FACTORY),
                               octet=31)
    assert row(result, "reached at")["ok"] is True
    assert result["ip"] == ASSIGNED
    assert result["name"] == "raythink-31"


def test_a_dhcp_camera_is_judged_on_holding_a_lease_not_on_an_address(monkeypatch):
    # The lease was the site's DHCP server to choose, so there is no address of
    # ours to compare — but answering at all, and having an address, is real.
    c = client(monkeypatch, host="192.168.88.140", ip="192.168.88.140", dhcp=True)
    result = mod.verify_camera(c, settings=settings(),
                               resolve=recorded(ip="", ip_mode="dhcp"))
    assert row(result, "reached at")["ok"] is True
    assert row(result, "DHCP")["ok"] is True
    assert result["ok"] is True
    assert result["ip"] == ""          # nothing of ours was assigned
    assert result["name"] == "raythink-dhcp"


def test_a_dhcp_camera_with_no_lease_fails(monkeypatch):
    c = client(monkeypatch, host="192.168.88.140", ip="", dhcp=True)
    result = mod.verify_camera(c, settings=settings(),
                               resolve=recorded(ip="", ip_mode="dhcp"))
    assert row(result, "DHCP")["ok"] is False


def test_a_static_camera_left_on_dhcp_fails(monkeypatch):
    c = client(monkeypatch, dhcp=True)
    result = mod.verify_camera(c, settings=settings(), resolve=recorded())
    # It answers on the right address, but the address is a lease, not the
    # static assignment the record says it got.
    assert row(result, "reached at")["ok"] is True
    assert row(result, "static IP")["ok"] is False


# ── the profile row: honest about what it cannot check ───────────────────────

def test_the_profile_row_names_the_profile_without_claiming_to_check_it(monkeypatch):
    c = client(monkeypatch)
    check = row(mod.verify_camera(c, settings=settings(), resolve=recorded()),
                "config profile")
    assert check["ok"] is None         # not a pass, and not a failure
    assert "lan" in check["actual"]
    assert "cannot be re-checked" in check["actual"]


def test_no_recorded_profile_is_a_failure_not_a_shrug(monkeypatch):
    c = client(monkeypatch)
    result = mod.verify_camera(c, settings=settings(),
                               resolve=recorded(profile=""))
    assert row(result, "config profile")["ok"] is False
    assert result["ok"] is False


def test_an_operator_supplied_profile_beats_the_record(monkeypatch):
    c = client(monkeypatch)
    result = mod.verify_camera(c, settings=settings(), resolve=recorded(profile=""),
                               profile_name="cellular")
    assert "cellular" in row(result, "config profile")["actual"]
    assert result["profile"] == "cellular"


# ── the remaining rows ───────────────────────────────────────────────────────

def test_a_camera_still_on_the_factory_password_fails_loudly(monkeypatch):
    c = client(monkeypatch, password=FACTORY_PW)
    result = mod.verify_camera(c, settings=settings(), resolve=recorded())
    check = row(result, "admin password")
    assert check["ok"] is False
    assert "factory" in check["actual"]
    assert result["ok"] is False
    # One row, not two: the fallback replaces the read-back row rather than
    # sitting beside it.
    assert items(result).count("admin password") == 1


def test_a_camera_on_neither_password_stops_the_pass(monkeypatch):
    # Nothing can be read, so a green sweep would be a lie and a red one a
    # guess about which of a dozen things is wrong.
    c = client(monkeypatch, password="something-nobody-knows")
    with pytest.raises(CameraError):
        mod.verify_camera(c, settings=settings(), resolve=recorded())


def test_a_camera_whose_onvif_password_was_never_set_fails(monkeypatch):
    # The failure this tool was built to catch: a normally-provisioned camera
    # kept ONVIF admin/admin while its web password changed.
    c = client(monkeypatch, onvif_ok=False)
    result = mod.verify_camera(c, settings=settings(), resolve=recorded())
    assert row(result, "ONVIF login (admin)")["ok"] is False
    assert result["ok"] is False


def test_a_camera_on_the_wrong_ntp_server_fails(monkeypatch):
    c = client(monkeypatch, ntp="pool.ntp.org")
    result = mod.verify_camera(c, settings=settings(), resolve=recorded())
    assert row(result, "NTP server")["ok"] is False


def test_ntp_disabled_fails_even_with_the_right_server(monkeypatch):
    c = client(monkeypatch, ntp_enable=False)
    result = mod.verify_camera(c, settings=settings(), resolve=recorded())
    assert row(result, "NTP server")["ok"] is False


def test_a_missing_configure_record_fails_the_pass(monkeypatch):
    missing = {"item": "prior run", "expected": "a recorded configure run",
               "actual": "none found", "ok": False}
    c = client(monkeypatch)
    result = mod.verify_camera(c, settings=settings(),
                               resolve=resolver({"ip": ASSIGNED,
                                                 "profile": "lan"}, missing))
    assert result["verification"][0]["item"] == "prior run"
    assert result["ok"] is False


def test_no_row_carries_the_password(monkeypatch):
    # These rows are shipped to bench-central verbatim (TEC-349).
    c = client(monkeypatch)
    result = mod.verify_camera(c, settings=settings(), resolve=recorded())
    for check in result["verification"]:
        assert SHARED not in str(check), check


def test_a_fully_correct_camera_passes(monkeypatch):
    c = client(monkeypatch)
    result = mod.verify_camera(c, settings=settings(), resolve=recorded())
    assert result["ok"] is True
    assert result["identity"]["serial"] == "KK0552PAZ00681"
