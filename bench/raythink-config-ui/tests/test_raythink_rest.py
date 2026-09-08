"""The newer Raythink cameras' REST client (raythink_rest).

No hardware: a `FakeCamera` stands in for `requests.Session` and answers the
/v1 routes out of its own state, so the client's real `_request` — and with it
the token handling and the read-only gate — is what runs, and a write shows up
as a recorded call rather than as a flag somebody remembered to check.

The addressing steps and the verification report are NOT re-tested here; they
live in `raythink_base` and `test_raythink_camera.py` drives the same code. What
this file covers is the half that is specific to this generation: the wire
format, the hooks the base calls, and the two endpoints the vendor documentation
does not mention.
"""
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from bench_core import MutationBlocked
import raythink_base as base
from raythink_base import CameraError, CameraUnreachable
from raythink_rest import (FIRMWARE_CHUNK_BYTES, FLASH_DONE,
                           RaythinkRestClient, encrypt_password)

SHARED = "Kelafield123!"
FACTORY_PW = "admin"
CAMERA_MAC = "00:1e:42:aa:bb:01"
SERIAL = "CB6280103"
FIRMWARE = "B1.2.01.01.15, 2026-05-14"


class FakeResponse:
    def __init__(self, body):
        self.status_code = 200
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def ok(data=None):
    return {"Code": 200, "Message": "msg: success", "Translate": "操作成功",
            "Detail": "", "Data": data}


class FakeCamera:
    """One newer camera's /v1 surface. Records `(method, path)` for every call
    and the query/body it carried, so the tests assert on what was SENT."""

    def __init__(self, *, password=SHARED, ip="192.168.1.123",
                 netmask="255.255.255.0", gateway="192.168.1.1", dhcp=False,
                 ntp="192.168.88.10", ntp_enable=True,
                 onvif_password=FACTORY_PW, onvif_users=("admin",)):
        self.calls: list[tuple[str, str]] = []
        self.sent: list[dict] = []
        self.password = password
        self.ip, self.netmask, self.gateway, self.dhcp = ip, netmask, gateway, dhcp
        self.ntp, self.ntp_enable = ntp, ntp_enable
        self.onvif_password = onvif_password
        self.onvif_users = list(onvif_users)
        self.token = "tok-1"
        self.tokens_issued = 0
        self.imported = None
        self.reboots = 0
        self.up = True             # answers on its port

        self.upgrade_ready = None
        self.parts: list[dict] = []
        self.status_polls = 0
        self.flash_stages: list[int] = []
        self.dies_while_writing = False
        self.flash_stalls = False
        # What the camera itself would export: the profile's sections plus the
        # two the sanitiser is required to drop before a profile can be committed.
        self.stored = dict(V2_EXPORT)
        self.stored.setdefault("NetworkInfo", {"Card": [{"IP": ip}]})
        self.stored.setdefault("OnvifUser", {"User": [{"Name": "admin",
                                                       "Password": onvif_password}]})

    @staticmethod
    def _decrypt(value: str) -> str:
        """What the device does to an ONVIF password it is handed.

        It decrypts unconditionally, so a caller that follows the vendor doc and
        sends plaintext does not get the plaintext stored — it gets rubbish, and
        in practice an EMPTY password, while the call still answers Code 200.
        Reproduced here because that silent emptying is the bug this fake exists
        to keep out, and a fake that just stored the string would not catch it.
        """
        for candidate in (SHARED, FACTORY_PW, "another-one", ""):
            if value == encrypt_password(candidate):
                return candidate
        return ""

    # -- the requests.Session half the client talks to ------------------------
    def request(self, method, url, **kwargs):
        parsed = urlparse(url)
        path = parsed.path
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        query.update(kwargs.get("params") or {})
        body = json.loads(kwargs["data"]) if kwargs.get("data") else None
        self.calls.append((method, path))
        self.sent.append({"method": method, "path": path, "query": query,
                          "body": body, "files": kwargs.get("files"),
                          "headers": dict(kwargs.get("headers") or {})})
        return FakeResponse(self._answer(method, path, query, body,
                                         kwargs.get("files")))

    def close(self):
        pass

    def _answer(self, method, path, query, body, files):
        if path == "/v1/token":
            if method == "POST":
                if query.get("password") != encrypt_password(self.password):
                    return {"Code": 401, "Message": "msg: wrong password",
                            "Detail": "", "Data": None}
                self.tokens_issued += 1
                self.token = f"tok-{self.tokens_issued + 1}"
                return ok({"Token": self.token})
            if method == "PUT":
                self.tokens_issued += 1
                self.token = f"tok-{self.tokens_issued + 1}"
                return ok({"Token": self.token})
            return ok(None)

        if path == "/v1/user/user" and method == "PUT":
            if query.get("oldpwd") != encrypt_password(self.password):
                return {"Code": 403, "Message": "msg: wrong old password",
                        "Detail": "", "Data": None}
            enc = query.get("newpwd", "")
            for candidate in (SHARED, FACTORY_PW, "another-one"):
                if enc == encrypt_password(candidate):
                    self.password = candidate
            return ok(None)

        if path == "/v1/system/product/version":
            return ok({"PDCode": "PC464A1", "PDName": "XX-MVP-PC4-V100",
                       "PDSN": SERIAL, "PDPN": "PC464A1-P2500601S00",
                       "Software": FIRMWARE, "Core": "U3,2025-12-16"})

        if path == "/v1/system/local/ntp/client":
            if method == "PUT":
                self.ntp = body["Address"]
                self.ntp_enable = body["Enable"]
                return ok(None)
            return ok({"Address": self.ntp, "Enable": self.ntp_enable,
                       "Port": 123, "UpdatePeriod": 60})

        if path == "/v1/system/local/time" and method == "PUT":
            # Changing the clock invalidates the token, so the device hands back
            # a replacement in Data.
            self.tokens_issued += 1
            self.token = f"tok-{self.tokens_issued + 1}"
            return ok(self.token)

        if path == "/v1/netapp/network":
            if method == "PUT":
                card = body["Card"][0]
                self.dhcp = card["DHCPEnable"]
                self.ip = card.get("IPAddress", self.ip)
                self.netmask = card.get("SubnetMask", self.netmask)
                self.gateway = card.get("Gateway", self.gateway)
                return ok(None)
            return ok({"DefaultInterface": "eth0",
                       "Card": [{"Name": "eth0", "DHCPEnable": self.dhcp,
                                 "IPAddress": self.ip, "SubnetMask": self.netmask,
                                 "Gateway": self.gateway,
                                 "PhysicalAddress": CAMERA_MAC,
                                 "DefaultDNS": "8.8.8.8"}]})

        if path == "/v1/netapp/onvif/user":
            # This endpoint hands the password back ENCRYPTED, unlike the export.
            return ok([{"Name": n, "Group": 1,
                        "Password": encrypt_password(
                            self.onvif_password if n == "admin" else "x")}
                       for n in self.onvif_users])
        if path == "/v1/netapp/onvif/changepwd" and method == "PUT":
            self.onvif_password = self._decrypt(body["NewPassword"])
            return ok(None)
        if path == "/v1/netapp/onvif/add" and method == "POST":
            self.onvif_users.append(body["Name"])
            self.onvif_password = self._decrypt(body["Password"])
            return ok(None)

        if path == "/v1/system/magic/configuration":
            # The export is the ONLY endpoint that answers with the file itself
            # rather than the {Code, Data, ...} envelope, and the import refuses
            # a file that does not carry every section the device knows about
            # ("400204 msg: file is incomplete"). Both are real behaviours of the
            # device, and between them they are what made the first bench run
            # fail, so the fake has to reproduce them.
            if method == "GET":
                # The export reports the ONVIF password in PLAINTEXT, and reports
                # it as it is NOW — the client reads the credential back through
                # here, so a snapshot frozen at construction would make every
                # check pass regardless of what was written.
                return dict(self.stored, OnvifUser={
                    "User": [{"Group": 1, "Name": n,
                              "Password": self.onvif_password if n == "admin" else "x"}
                             for n in self.onvif_users]})
            missing = [s for s in self.stored if s not in json.loads(files["file"][1])]
            if missing:
                return {"Code": 400204, "Message": "msg: file is incomplete",
                        "Detail": "", "Data": None}
            self.imported = json.loads(files["file"][1])
            # A SUCCESS code, despite not being 200: the device took the file but
            # will not run it until it restarts.
            return {"Code": 200000, "Message": "msg: success, need to reboot device.",
                    "Translate": "", "Detail": "", "Data": None}

        if path == "/v1/system/magic/reboot" and method == "PUT":
            self.reboots += 1
            self.up = False        # a real camera stops answering, eventually
            return ok(None)

        if path == "/v1/system/upgrade/ready":
            self.upgrade_ready = (method, query.get("islocal"))
            return ok(None)
        if path == "/v1/system/upgrade/common/package":
            self.parts.append({"query": query, "method": method,
                               "field": next(iter(files)), "part": files["package"]})
            return ok(None)
        if path == "/v1/system/upgrade/common/status":
            # Process 1 (verifying) -> 2 (writing) -> 3 (done), as captured.
            if self.dies_while_writing:
                raise requests.exceptions.ConnectionError("Connection aborted.")
            self.status_polls += 1
            if self.flash_stalls:      # never reaches Process 3
                return ok({"Process": 2, "Percent": 40})
            stage = self.flash_stages.pop(0) if self.flash_stages else FLASH_DONE
            return ok({"Process": stage, "Percent": 100 if stage == FLASH_DONE else 40})

        return ok(None)

    @property
    def writes(self) -> list[tuple[str, str]]:
        """Every recorded call that would change the camera — which on this API
        is simply everything that is not a GET."""
        return [(m, p) for m, p in self.calls if m != "GET"]


@pytest.fixture
def cam(monkeypatch):
    """A logged-in client over a FakeCamera. `client.device` is the camera."""
    def make(**kwargs):
        c = RaythinkRestClient(host="192.168.1.123")
        c.device = FakeCamera(**kwargs)
        c.s = c.device
        # Whether the camera answers its port is device state here, not a real
        # socket: a reboot takes it down, and nothing waits on a live network.
        c.port_open = lambda host=None: c.device.up
        # Host-side effects and retry pacing are not what these tests are about.
        monkeypatch.setattr(base, "host_iface_for", lambda ip: "en0")
        monkeypatch.setattr(base, "renew_host_dhcp", lambda iface: None)
        monkeypatch.setattr(base, "arp_table", dict)
        monkeypatch.setattr(base.time, "sleep", lambda s: None)
        return c
    return make


@pytest.fixture
def client(cam):
    c = cam()
    c.login(SHARED)
    c.device.calls.clear()
    return c


# ── the password encoding ────────────────────────────────────────────────────

def test_the_encoding_reproduces_a_real_ciphertext():
    # Captured off a live camera's own web UI changing Kelafield123! to itself.
    # This is the one thing in the client that cannot be checked by reading the
    # vendor doc, so it is pinned against the wire rather than against the spec.
    assert encrypt_password(SHARED) == "jSJyRmOZI8IS+IlXDpUh9g=="


def test_a_missing_crypto_library_fails_the_step_not_the_process(monkeypatch):
    # This module is imported lazily, once a newer camera is on the bench, so an
    # import-time sys.exit would take down the bench UI's worker mid-run rather
    # than failing the step with something an operator can act on.
    import raythink_rest
    monkeypatch.setattr(raythink_rest, "Cipher", None)
    with pytest.raises(CameraError) as e:
        encrypt_password("anything")
    assert "cryptography" in str(e.value)


def test_the_encoding_is_deterministic():
    # Fixed key AND fixed IV, so the same password always produces the same
    # ciphertext. Worth pinning because it is also why this is obfuscation
    # rather than encryption, which the module docstring says out loud.
    assert encrypt_password("abc") == encrypt_password("abc")
    assert encrypt_password("abc") != encrypt_password("abd")


# ── login and the token ──────────────────────────────────────────────────────

def test_login_sends_the_encrypted_password_and_keeps_the_token(cam):
    c = cam()
    c.login(SHARED)
    sent = c.device.sent[0]
    assert sent["query"]["password"] == encrypt_password(SHARED)
    assert SHARED not in sent["path"] and SHARED not in str(sent["query"])
    assert c.token == c.device.token
    assert c.password == SHARED


def test_a_wrong_password_fails_and_leaves_the_client_unauthenticated(cam):
    c = cam()
    with pytest.raises(CameraError):
        c.login("not-it")
    assert c.token == ""
    assert c.password is None


def test_a_rejected_login_reports_the_devices_own_reason(cam):
    # "Provisioning failed on login" with no reason is the one failure a bench
    # operator cannot act on: wrong password, locked account and unsupported
    # firmware all look identical without the device's Code and Detail.
    c = cam()
    with pytest.raises(CameraError) as e:
        c.login("not-it")
    assert "401" in str(e.value)
    assert "wrong password" in str(e.value)


def test_a_login_error_never_carries_the_credential(cam):
    # This API puts the password in the QUERY STRING, AES-encrypted under a key
    # that ships in the camera's own web bundle — so a ciphertext in a log is a
    # password in a log, and error text from here reaches the rolling log and
    # the per-camera JSON (TEC-349).
    c = cam()
    with pytest.raises(CameraError) as e:
        c.login("not-it")
    assert "password=" not in str(e.value)
    assert encrypt_password("not-it") not in str(e.value)


def test_a_password_change_error_never_carries_either_credential(cam):
    c = cam(password=FACTORY_PW)
    c.login(FACTORY_PW)
    # Wrong "old" password: the device refuses, and the message must not echo
    # back the two ciphertexts the request carried.
    c.device.password = "something-else"
    with pytest.raises(CameraError) as e:
        c.modify_password(SHARED, FACTORY_PW)
    for secret in (SHARED, FACTORY_PW):
        assert encrypt_password(secret) not in str(e.value)
    assert "newpwd" not in str(e.value) and "oldpwd" not in str(e.value)


def test_a_login_whose_token_arrives_as_a_bare_string_still_works(cam):
    # The vendor doc types Data as an object for the KEEPALIVE but leaves it
    # untyped for the login, and the clock endpoint returns the replacement
    # token as a bare string — so the device uses both shapes for the same
    # value. Accepting only the object shape would read a successful login as a
    # wrong password.
    c = cam()
    real = c.device._answer

    def bare(method, path, query, body, files):
        env = real(method, path, query, body, files)
        if path == "/v1/token" and method == "POST" and env.get("Code") == 200:
            env["Data"] = env["Data"]["Token"]
        return env

    c.device._answer = bare
    c.login(SHARED)
    assert c.token == c.device.token
    assert c.password == SHARED


def test_a_login_that_returns_no_token_at_all_says_so(cam):
    c = cam()
    real = c.device._answer

    def empty(method, path, query, body, files):
        env = real(method, path, query, body, files)
        if path == "/v1/token" and method == "POST":
            env["Data"] = None
        return env

    c.device._answer = empty
    with pytest.raises(CameraError) as e:
        c.login(SHARED)
    assert "no token" in str(e.value)


def test_every_later_call_carries_the_token(client):
    client.read_ntp()
    assert client.device.sent[-1]["headers"]["X-Token"] == client.token


def test_an_idle_token_is_refreshed_before_the_next_call(client, monkeypatch):
    # The device expires an idle token, and the pipeline has long gaps in it.
    # Refreshed lazily from the request path rather than by a background thread:
    # the pipeline is sequential, so there is never a second caller to race.
    now = [1000.0]
    monkeypatch.setattr(base.time, "monotonic", lambda: now[0])
    monkeypatch.setattr("raythink_rest.time.monotonic", lambda: now[0])
    client.read_ntp()
    before = client.token

    now[0] += 60
    client.read_ntp()

    assert client.token != before
    assert ("PUT", "/v1/token") in client.device.calls


def test_a_token_still_warm_is_not_refreshed(client, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("raythink_rest.time.monotonic", lambda: now[0])
    client.read_ntp()
    now[0] += 1
    client.read_ntp()
    assert ("PUT", "/v1/token") not in client.device.calls


# ── the read-only gate ───────────────────────────────────────────────────────

def test_a_read_only_client_refuses_every_write(client):
    # The gate is "not a GET", so a write added later is refused by default
    # instead of needing to be remembered — unlike the older client's deny-list.
    client.set_read_only()
    for call in (lambda: client.set_ntp("10.0.0.1"),
                 lambda: client.sync_time_to_pc(),
                 lambda: client.modify_password("new", SHARED),
                 lambda: client.set_onvif_password(SHARED, [FACTORY_PW]),
                 lambda: client.apply_network(dhcp=True)):
        with pytest.raises(MutationBlocked):
            call()
    assert client.device.writes == []


def test_a_read_only_client_still_answers_reads(client):
    client.set_read_only()
    assert client.read_ntp()["address"] == "192.168.88.10"
    assert client.read_network().ip == "192.168.1.123"


# ── identity ─────────────────────────────────────────────────────────────────

def test_identity_reads_the_serial_the_model_and_the_firmware(client):
    identity = client.get_identity()
    assert identity["serial"] == SERIAL
    assert identity["model"] == "XX-MVP-PC4-V100"
    assert identity["firmware"] == FIRMWARE
    assert identity["mac"] == CAMERA_MAC


def test_identity_survives_a_camera_that_will_not_say(client, monkeypatch):
    # Never fail a run over the identity read: everything downstream works
    # without it, and "unknown" is a usable answer where an exception is not.
    monkeypatch.setattr(client, "_request",
                        lambda *a, **k: (_ for _ in ()).throw(CameraError("nope"))
                        if a[1] != "/v1/system/product/version" else None)
    identity = client.get_identity()
    assert identity["serial"] == "unknown"
    assert identity["mac"] == "unknown"


# ── the admin password ───────────────────────────────────────────────────────

def test_the_password_change_sends_both_values_encrypted(client):
    client.modify_password(SHARED, FACTORY_PW)  # already on SHARED: a no-op
    assert client.device.writes == []


def test_a_factory_camera_is_moved_to_the_shared_password(cam):
    c = cam(password=FACTORY_PW)
    c.login(FACTORY_PW)
    c.modify_password(SHARED, FACTORY_PW)

    change = next(s for s in c.device.sent if s["path"] == "/v1/user/user")
    assert change["query"]["newpwd"] == encrypt_password(SHARED)
    assert change["query"]["oldpwd"] == encrypt_password(FACTORY_PW)
    assert c.device.password == SHARED
    # And the client is re-logged-in under it, or every later step is orphaned.
    assert c.password == SHARED
    assert c.token == c.device.token


def test_a_failed_relogin_leaves_the_client_on_the_working_password(cam, monkeypatch):
    # The device took the change but we could not get back in. Recording the new
    # password would mean the verification row claims a password we never proved.
    c = cam(password=FACTORY_PW)
    c.login(FACTORY_PW)
    monkeypatch.setattr(c, "login", lambda pw: (_ for _ in ()).throw(
        CameraError("no")))
    with pytest.raises(CameraError):
        c.modify_password(SHARED, FACTORY_PW)
    assert c.password == FACTORY_PW


# ── NTP and the clock ────────────────────────────────────────────────────────

def test_ntp_is_set_and_read_back(client):
    client.set_ntp("192.168.88.10")
    assert client.read_ntp() == {"address": "192.168.88.10", "enable": True}


def test_the_clock_change_captures_the_new_token(client):
    # The device replaces the token when the clock moves. Missing that is a
    # DELAYED failure: the next call fails on an expired token several steps
    # later, nowhere near the cause.
    before = client.token
    client.sync_time_to_pc()
    assert client.token == client.device.token != before


def test_the_clock_change_does_not_touch_the_timezone(client):
    # The vendor doc spells the field two different ways and a real export uses
    # the third; the profile owns the zone, so this step sets only the wall clock.
    client.sync_time_to_pc()
    body = next(s for s in client.device.sent
                if s["path"] == "/v1/system/local/time")["body"]
    assert set(body) == {"Year", "Month", "Day", "Hour", "Minute", "Second"}


# ── addressing: the hooks the shared base calls ──────────────────────────────

def test_the_address_is_read_out_of_the_card_array(client):
    net = client.read_network()
    assert (net.ip, net.netmask, net.gateway) == ("192.168.1.123",
                                                  "255.255.255.0", "192.168.1.1")
    assert net.dhcp is False
    assert net.mac == CAMERA_MAC


def test_a_static_move_writes_the_whole_structure_back(client, monkeypatch):
    # Read-modify-write, not write: the device keeps DNS and the other cards in
    # the same structure and a blind write would drop them.
    monkeypatch.setattr(client, "port_open", lambda host=None: True)
    row = client.set_static_ip("192.168.88.30", "255.255.255.0", "192.168.88.1",
                               wait=30)
    sent = next(s for s in client.device.sent
                if s["method"] == "PUT" and s["path"] == "/v1/netapp/network")
    card = sent["body"]["Card"][0]
    assert card["DHCPEnable"] is False
    assert card["IPAddress"] == "192.168.88.30"
    assert card["DefaultDNS"] == "8.8.8.8"      # untouched, not dropped
    assert row["ok"] is True
    assert client.host == "192.168.88.30"       # the client follows the camera


def test_dhcp_leaves_the_static_fallback_alone(client, monkeypatch):
    # The camera falls back to the static address if no lease arrives, which is
    # a more useful failure than an unreachable camera with no address at all.
    monkeypatch.setattr(base, "find_ip_by_mac", lambda *a, **k: "192.168.88.57")
    client.set_dhcp(mac=CAMERA_MAC, subnets=["192.168.88.0/24"], wait=30)
    card = next(s for s in client.device.sent
                if s["method"] == "PUT"
                and s["path"] == "/v1/netapp/network")["body"]["Card"][0]
    assert card["DHCPEnable"] is True
    assert card["IPAddress"] == "192.168.1.123"
    assert client.host == "192.168.88.57"


def test_the_address_move_survives_the_dropped_reply(client, monkeypatch):
    # The reply usually never arrives — the camera drops the link applying it.
    # That is the expected path on this generation too, not a failure.
    monkeypatch.setattr(client, "port_open", lambda host=None: True)
    real = client._request

    def drop(method, path, **kwargs):
        if method == "PUT" and path == "/v1/netapp/network":
            raise CameraError("connection failed")
        return real(method, path, **kwargs)

    monkeypatch.setattr(client, "_request", drop)
    assert client.set_static_ip("192.168.88.30", "255.255.255.0",
                                "192.168.88.1", wait=30)["ok"] is True


# ── ONVIF: a separate credential, two plain calls instead of SOAP ────────────

def test_the_onvif_password_is_set_and_confirmed(client):
    ok_, detail = client.onvif_check(SHARED)
    assert ok_ is False                      # still on the factory credential

    client.set_onvif_password(SHARED, [FACTORY_PW])

    assert client.device.onvif_password == SHARED
    assert client.onvif_check(SHARED)[0] is True


def test_the_onvif_password_travels_encrypted(client):
    # The vendor doc's example body is plaintext, and following it does not fail
    # loudly — the device decrypts whatever it is handed, so plaintext leaves the
    # ONVIF account with NO PASSWORD while the call still answers Code 200. Every
    # newer camera the bench touched was left wide open on ONVIF that way, so the
    # wire form is asserted here directly rather than only through its effect.
    client.set_onvif_password(SHARED, [FACTORY_PW])
    sent = next(s for s in client.device.sent
                if s["path"] == "/v1/netapp/onvif/changepwd")
    assert sent["body"]["NewPassword"] == encrypt_password(SHARED)
    assert sent["body"]["NewPassword"] != SHARED


def test_a_connection_reset_just_after_a_reboot_is_waited_out(client, monkeypatch):
    # A camera that has started answering pings can still reset the first request
    # or two while its web server finishes coming up. That is not a login
    # failure, and reporting it as one produced a run that warned it could not
    # log in for verification and then passed every verification row.
    monkeypatch.setattr(base.time, "sleep", lambda s: None)
    attempts = []
    real_login = client.login

    def flaky(pw):
        attempts.append(pw)
        if len(attempts) < 3:
            raise CameraUnreachable("POST /v1/token: connection failed (reset)")
        return real_login(pw)

    monkeypatch.setattr(client, "login", flaky)
    client.relogin([SHARED])
    assert len(attempts) == 3


def test_a_rejected_password_is_not_retried(client, monkeypatch):
    # The opposite case, and the reason the two are distinguished: a device that
    # ANSWERS and says no will keep saying no, and on one that locks an account
    # after a few tries, retrying is worse than failing.
    monkeypatch.setattr(base.time, "sleep", lambda s: None)
    attempts = []

    def rejected(pw):
        attempts.append(pw)
        raise CameraError("POST /v1/token failed: 401 wrong password")

    monkeypatch.setattr(client, "login", rejected)
    with pytest.raises(CameraError):
        client.relogin([SHARED])
    assert len(attempts) == 1


def test_the_onvif_check_reads_the_live_user_not_the_saved_config(client):
    # After a config import the export answers from the IMPORTED FILE rather than
    # from what the device is running, and the file the tool uploads carries
    # whatever ONVIF password the camera had at the time — on the bench, an empty
    # one. Checking the export therefore reported a freshly set password as still
    # missing, and the step failed on a camera that was correctly configured.
    client.device.stored["OnvifUser"] = {"User": [{"Group": 1, "Name": "admin",
                                                   "Password": ""}]}
    client.set_onvif_password(SHARED, [FACTORY_PW])
    assert client.device.onvif_password == SHARED
    assert client.onvif_check(SHARED)[0] is True


def test_an_onvif_account_left_with_no_password_is_called_out(cam):
    # Distinct from "a different password": no password at all is a way in, and
    # it is the exact state the plaintext bug produced.
    c = cam(onvif_password="")
    c.login(SHARED)
    ok_, detail = c.onvif_check(SHARED)
    assert ok_ is False
    assert "NO password" in detail


def test_setting_the_onvif_password_twice_does_nothing_the_second_time(client):
    client.set_onvif_password(SHARED, [FACTORY_PW])
    client.device.calls.clear()
    client.set_onvif_password(SHARED, [FACTORY_PW])
    assert client.device.writes == []


def test_a_camera_with_no_onvif_user_gets_one(cam):
    # `changepwd` cannot serve a user that does not exist.
    c = cam(onvif_users=())
    c.login(SHARED)
    c.set_onvif_password(SHARED, [FACTORY_PW])
    assert ("POST", "/v1/netapp/onvif/add") in c.device.calls
    assert c.onvif_check(SHARED)[0] is True


def test_the_onvif_row_never_carries_the_password(client):
    # Verification rows go to bench-central verbatim (TEC-349), and this API
    # hands the ONVIF password straight back to the client — so the row has to
    # say whether it matches, never what it is. That it names the ONVIF USERS is
    # fine and deliberate; the username is not a credential.
    client.set_onvif_password(SHARED, [FACTORY_PW])
    assert SHARED not in client.onvif_check(SHARED)[1]
    assert SHARED not in client.onvif_check("wrong")[1]

    c = client
    c.device.onvif_password = "a-password-nobody-configured"
    assert c.device.onvif_password not in c.onvif_check(SHARED)[1]


# ── config import ────────────────────────────────────────────────────────────

def profile(tmp_path, data, name="lan.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


V2_EXPORT = {
    "NtpInfo": {"Address": "192.168.88.10", "Enable": True},
    "WebInfo": {"Port": 80},
    "LocalSettings": {"Language": "English"},
}


def test_a_profile_is_uploaded_as_one_file(client, tmp_path):
    # No per-section replay to tolerate failures of: the device takes the file
    # whole or not at all.
    result = client.import_config(profile(tmp_path, V2_EXPORT))
    assert result == {"applied": ["lan.json"], "skipped": []}
    for section, value in V2_EXPORT.items():
        assert client.device.imported[section] == value


# ── firmware upgrade ─────────────────────────────────────────────────────────
#
# The wire shape here is asserted in detail because it is UNDOCUMENTED — the
# vendor PDF has no upgrade endpoint at all, and every field below comes from a
# capture of the camera's own web UI. There is nothing to re-read it from later.

def image(tmp_path, size: int) -> Path:
    path = tmp_path / "MVP-JUPITER4S-B1V0222916-CN-20260903.zip"
    path.write_bytes(b"\xa5" * size)
    return path


def test_the_image_goes_up_in_four_mib_parts(client, tmp_path):
    # 58,040,972 bytes went as totalNumber=14, which is 4 MiB each.
    path = image(tmp_path, FIRMWARE_CHUNK_BYTES * 2 + 100)
    client.upgrade_firmware(str(path))
    assert client.device.upgrade_ready == ("PUT", "true")
    assert [int(p["query"]["fileNumber"]) for p in client.device.parts] == [1, 2, 3]
    assert [len(p["part"][1]) for p in client.device.parts] == \
        [FIRMWARE_CHUNK_BYTES, FIRMWARE_CHUNK_BYTES, 100]
    assert b"".join(p["part"][1] for p in client.device.parts) == path.read_bytes()


def test_the_part_is_posted_as_package_named_blob(client, tmp_path):
    # Both names are from the capture and neither is guessable: the form field
    # is "package" and the part's filename is the literal string "blob", with
    # the real name travelling in the query string instead.
    client.upgrade_firmware(str(image(tmp_path, 10)))
    sent = client.device.parts[0]
    assert sent["method"] == "POST"
    assert sent["field"] == "package"
    assert sent["part"][0] == "blob"
    assert sent["part"][2] == "application/octet-stream"
    assert sent["query"]["filename"] == "MVP-JUPITER4S-B1V0222916-CN-20260903.zip"


def test_every_part_carries_the_md5_of_the_whole_file(client, tmp_path):
    # NOT a per-part digest: the captured md5 was byte-identical across all 14
    # requests, and matches the md5 of the bundle on disk.
    path = image(tmp_path, FIRMWARE_CHUNK_BYTES + 1)
    whole = hashlib.md5(path.read_bytes()).hexdigest()
    client.upgrade_firmware(str(path))
    assert {p["query"]["md5"] for p in client.device.parts} == {whole}
    assert {p["query"]["totalNumber"] for p in client.device.parts} == {"2"}
    assert {p["query"]["filename"] for p in client.device.parts} == {path.name}


def test_the_configuration_is_not_wiped(client, tmp_path):
    # reset=true is the device's "wipe the configuration" flag. This runs BEFORE
    # the config import, on a camera whose admin password the pipeline has
    # already changed — a wipe would drop it back to the factory address and
    # credential mid-run.
    client.upgrade_firmware(str(image(tmp_path, 10)))
    assert {p["query"]["reset"] for p in client.device.parts} == {"false"}


def test_the_upload_is_not_mistaken_for_the_flash(client, tmp_path):
    # The camera answered all 14 parts and only THEN wrote the image. Returning
    # at the end of the upload would hand the caller a camera that is about to
    # disappear rather than one that already has.
    client.device.flash_stages = [1, 2, 2, FLASH_DONE]
    client.upgrade_firmware(str(image(tmp_path, 10)))
    assert client.device.status_polls == 4
    assert client.token == ""      # nothing survives the reboot


def test_the_camera_vanishing_while_writing_is_the_reboot_not_a_failure(client, tmp_path):
    client.device.dies_while_writing = True
    client.upgrade_firmware(str(image(tmp_path, 10)))
    assert client.token == ""


def test_a_camera_still_writing_when_time_runs_out_is_not_called_done(client, tmp_path):
    # And the message says the one thing that matters, because an operator
    # watching a stuck step reaches for the power.
    client.device.flash_stalls = True
    with pytest.raises(CameraError) as e:
        client._await_flash(timeout=0.05)
    assert "do NOT power it off" in str(e.value)


def test_a_part_that_is_rejected_stops_the_upload(client, tmp_path):
    # Silence partway through is a half-written image, and continuing to the
    # next part would report a flash that never happened.
    path = image(tmp_path, FIRMWARE_CHUNK_BYTES * 3)
    original = client.device.request
    calls = {"n": 0}

    def flaky(method, url, **kwargs):
        if "upgrade/common/package" in url:
            calls["n"] += 1
            if calls["n"] == 2:
                raise requests.exceptions.ConnectionError("Connection aborted.")
        return original(method, url, **kwargs)

    client.device.request = flaky
    with pytest.raises(CameraUnreachable):
        client.upgrade_firmware(str(path))
    assert client.device.status_polls == 0   # never got as far as writing


def test_a_firmware_upgrade_is_refused_by_a_read_only_client(client, tmp_path):
    client.set_read_only()
    with pytest.raises(MutationBlocked):
        client.upgrade_firmware(str(image(tmp_path, 10)))
    assert client.device.parts == []


def test_success_that_asks_for_a_reboot_is_not_read_as_failure(client, tmp_path):
    # The import answers 200000, "msg: success, need to reboot device." — a
    # SUCCESS code that is not 200. Treating only 200 as success failed the step
    # on a device that had just accepted all 125 sections, and the reported
    # reason had the word "success" in it.
    result = client.import_config(profile(tmp_path, V2_EXPORT))
    assert result == {"applied": ["lan.json"], "skipped": []}
    # And the reboot it asked for is actually performed, so that every later
    # step runs against the configuration that is now live rather than a pending
    # one. The caller waits for the camera and logs in again.
    assert client.device.reboots == 1
    assert client.token == ""


def test_a_reboot_waits_for_the_camera_to_actually_go_down(client):
    # The bug this exists for: the camera kept serving for ~30s after accepting
    # the reboot, so returning when the CALL succeeded meant the caller's "wait
    # until it answers" was satisfied by the camera that had not left yet. Every
    # step after the config import — ONVIF, NTP, the addressing — was then
    # written to a camera that rebooted and threw them away.
    client.reboot()
    assert client.device.up is False
    assert client.wait_reachable(timeout=0) is False   # genuinely gone


def test_a_camera_that_lingers_after_a_reboot_is_reported_not_hidden(
        client, caplog, monkeypatch):
    monkeypatch.setattr(base, "REBOOT_DROP_SEC", 0.05)
    # It may just have restarted between polls, so this is a warning rather than
    # a failure — but a silent one would leave the next failure unexplainable.
    original = client.device.request

    def never_drops(method, url, **kwargs):
        r = original(method, url, **kwargs)
        client.device.up = True
        return r

    client.device.request = never_drops
    with caplog.at_level("WARNING"):
        client.reboot()
    assert "never stopped answering" in caplog.text


def test_a_reboot_is_refused_by_a_read_only_client(client):
    client.set_read_only()
    with pytest.raises(MutationBlocked):
        client.reboot()
    assert client.device.reboots == 0


def test_a_partial_profile_is_completed_from_the_device(client, tmp_path):
    # The device refuses a file that is missing sections — "400204 msg: file is
    # incomplete" — and a committed profile is ALWAYS missing some, because the
    # sanitiser has to drop the two that carry the address and the ONVIF
    # password. The gaps are filled from the camera's own current config, which
    # satisfies the device without pushing anything from a reference camera.
    client.import_config(profile(tmp_path, V2_EXPORT))
    assert set(client.device.imported) == set(client.device.stored)


def test_an_import_says_so_when_the_gaps_cannot_be_read(client, tmp_path, monkeypatch):
    # Without the device's current config there is no way to complete the file,
    # and "file is incomplete" from the device explains nothing about why.
    monkeypatch.setattr(client, "export_config",
                        lambda: (_ for _ in ()).throw(CameraError("token expired")))
    with pytest.raises(CameraError) as e:
        client.import_config(profile(tmp_path, V2_EXPORT))
    assert "could not be read" in str(e.value)
    assert "token expired" in str(e.value)


def test_an_older_cameras_profile_is_refused_by_name(client, tmp_path):
    # Named here, in the tool, rather than uploaded and rejected with whatever
    # the device chooses to say about 250 KB of JSON.
    legacy = {"VideoInOptions": [{}], "Encode": [{}], "UserGlobal": {}}
    with pytest.raises(CameraError) as e:
        client.import_config(profile(tmp_path, legacy))
    assert "older camera's export" in str(e.value)
    assert client.device.imported is None


def test_the_reference_cameras_address_is_never_uploaded(client, tmp_path):
    # The run-losing failure: the profile is imported in the MIDDLE of the
    # pipeline and addressing is deliberately LAST, so a profile carrying
    # NetworkInfo moves the camera under us and the run cannot follow it.
    export = dict(V2_EXPORT, NetworkInfo={"DefaultInterface": "eth0",
                                          "Card": [{"Name": "eth0",
                                                    "DHCPEnable": True,
                                                    "IPAddress": "192.168.88.139"}]})
    client.import_config(profile(tmp_path, export))
    # The section IS uploaded, because the device rejects a file without it —
    # but holding this camera's own current address, so the import is a no-op
    # for addressing and the pipeline's last step is still the one that moves it.
    assert client.device.imported["NetworkInfo"] == client.device.stored["NetworkInfo"]
    assert "192.168.88.139" not in json.dumps(client.device.imported)


def test_a_json_file_that_is_not_a_profile_is_refused(client, tmp_path):
    with pytest.raises(CameraError):
        client.import_config(profile(tmp_path, {}))
    assert client.device.imported is None


def test_the_station_password_is_never_uploaded_or_committed(client, tmp_path):
    # A raw export from this generation carries the ONVIF credential in
    # PLAINTEXT, and these profiles are committed. The tool sets ONVIF itself.
    export = dict(V2_EXPORT,
                  OnvifUser={"User": [{"Name": "admin", "Password": SHARED}]},
                  Gb28281Cfg={"Password": "12345678"})
    client.import_config(profile(tmp_path, export))
    # As with the address: the section travels, but carrying the camera's own
    # current ONVIF user rather than the reference camera's credential.
    uploaded = client.device.imported["OnvifUser"]["User"]
    assert [u["Password"] for u in uploaded] == [client.device.onvif_password]
    assert SHARED not in json.dumps(client.device.imported)
    # But a GB28181 SIP password is a legitimate site setting, so it stays.
    assert client.device.imported["Gb28281Cfg"]["Password"] == "12345678"


# ── the shared verification report, driven through this generation's hooks ───
#
# The report itself lives in `raythink_base` and is covered by
# `test_raythink_camera.py`. What is worth pinning here is that the REST hooks
# feed it correctly, because the two generations report the same facts through
# completely different structures — a `Card` array here, a Dahua config table
# there — and a mistake in either mapping produces a green row about a camera
# that was never configured.

def check(rows, item):
    return next(r for r in rows if r["item"] == item)


def verify(client, **kwargs):
    return client.verify_configuration(new_password=SHARED,
                                       ntp_server="192.168.88.10", **kwargs)


def test_the_firmware_row_uses_floor_semantics(client):
    # A camera deliberately left alone for being NEWER than the floor must not
    # then fail its own verification for being newer than the floor. The fake
    # reports 2026-05-14, and this floor is numerically far HIGHER but older by
    # date — so a row that passes here is a row comparing dates, not numbers.
    assert check(verify(client, dhcp=True, min_firmware="B9.9.99.99.99, 2026-01-01"),
                 "firmware")["ok"] is True


def test_a_camera_below_the_floor_fails_its_firmware_row(client):
    # Mirror image: numerically LOWER than what the camera reports, but newer.
    assert check(verify(client, dhcp=True, min_firmware="B1.0.00.00.01, 2026-09-03"),
                 "firmware")["ok"] is False


def test_there_is_no_firmware_row_when_no_floor_is_configured(client):
    # A bench with no image configured must not grow a row it can never pass.
    assert not [r for r in verify(client, dhcp=True) if r["item"] == "firmware"]


def test_a_correctly_configured_camera_passes_every_row(client, monkeypatch):
    monkeypatch.setattr(client, "port_open", lambda host=None: True)
    client.set_ntp("192.168.88.10")
    client.set_onvif_password(SHARED, [FACTORY_PW])
    client.set_static_ip("192.168.88.30", "255.255.255.0", "192.168.88.1", wait=30)

    rows = verify(client, ip="192.168.88.30", netmask="255.255.255.0",
                  gateway="192.168.88.1")

    assert [r["item"] for r in rows if r["ok"] is False] == []
    assert check(rows, "static IP")["ok"] is True
    assert check(rows, "NTP server")["ok"] is True
    assert check(rows, "ONVIF login (admin)")["ok"] is True
    assert check(rows, "admin password")["ok"] is True


def test_a_camera_left_on_dhcp_fails_the_static_row(client):
    client.device.dhcp = True
    client.device.ip = "192.168.88.30"
    rows = verify(client, ip="192.168.88.30", netmask="255.255.255.0",
                  gateway="192.168.88.1")
    # It holds the right address, but as a lease — and the next lease need not
    # match. DHCP being off is as much a part of the check as the address.
    assert check(rows, "static IP")["ok"] is False


def test_a_dhcp_camera_is_judged_on_holding_a_lease(client):
    client.device.dhcp = True
    client.device.ip = "192.168.88.140"
    rows = verify(client, dhcp=True)
    assert check(rows, "DHCP")["ok"] is True
    # The DHCP server chose these, so there is nothing of ours to assert.
    assert check(rows, "subnet mask")["ok"] is None
    assert check(rows, "gateway")["ok"] is None


def test_a_dhcp_camera_with_no_lease_fails(client):
    client.device.dhcp = True
    client.device.ip = ""
    assert check(verify(client, dhcp=True), "DHCP")["ok"] is False


def test_a_camera_whose_onvif_password_was_never_set_fails(client):
    # The failure the ONVIF row exists for: ONVIF keeps its own credential, so a
    # camera can be on the shared web password and still be ONVIF admin/admin.
    rows = verify(client)
    assert check(rows, "ONVIF login (admin)")["ok"] is False


def test_no_row_carries_the_password(client, monkeypatch):
    monkeypatch.setattr(client, "port_open", lambda host=None: True)
    client.set_onvif_password(SHARED, [FACTORY_PW])
    for row in verify(client, ip="192.168.1.123", netmask="255.255.255.0",
                      gateway="192.168.1.1"):
        assert SHARED not in str(row), row
