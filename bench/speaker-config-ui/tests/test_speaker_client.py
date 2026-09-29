"""SpeakerClient tests against a faked requests.Session — no hardware/network.

The fake session replays canned JSON bodies per (method, url-query) and records
every call, so the tests assert both the wire format (MD5 password, form
fields, multipart upload field) and the client's -500 re-login behaviour.
"""
import json

import pytest

from speaker_client import SpeakerClient, SpeakerError, md5_hex


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


class FakeSession:
    """Answers each request from a scripted queue keyed on a substring of the
    URL; records (method, url, kwargs) for assertions."""

    def __init__(self):
        self.calls = []
        self.scripts = []   # list of (url_substring, FakeResponse) consumed in order

    def script(self, url_part, body, status=200):
        self.scripts.append((url_part, FakeResponse(body, status)))

    def _answer(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for i, (part, resp) in enumerate(self.scripts):
            if part in url:
                del self.scripts[i]
                return resp
        raise AssertionError(f"unexpected {method} {url}")

    def get(self, url, **kw):
        return self._answer("GET", url, **kw)

    def post(self, url, **kw):
        return self._answer("POST", url, **kw)

    def close(self):
        pass


OK = {"result": 0, "reboot": 0, "reason": "OK"}


@pytest.fixture()
def client():
    c = SpeakerClient(host="192.168.1.57")
    c.s = FakeSession()
    return c


def login_body(changepwd=0):
    return {**OK, "pid": 101, "changepwd": changepwd}


def test_login_sends_md5_and_detects_forced_change(client):
    client.s.script("?login", login_body(changepwd=85))
    client.login("123456")
    method, url, kw = client.s.calls[0]
    assert kw["data"]["password"] == md5_hex("123456")
    assert kw["data"]["username"] == "admin"
    assert client.forced_changepwd is True
    assert client.password == "123456"


def test_login_failure_raises(client):
    client.s.script("?login", {"result": -1, "reason": "NG", "pid": 101, "changepwd": 0})
    with pytest.raises(SpeakerError):
        client.login("wrong")


def test_cgi_get_relogins_once_on_session_timeout(client):
    client.s.script("?login", login_body())
    client.login("123456")
    client.s.script("config=overview.get", {"result": -500, "reason": "NG"})
    client.s.script("?login", login_body())
    client.s.script("config=overview.get", {**OK, "data": {"uid": "X"}})
    assert client.cgi_get("overview.get") == {"uid": "X"}


def test_set_password_uses_changepwd_endpoint_when_forced(client):
    client.s.script("?login", login_body(changepwd=85))
    client.login("123456")
    client.s.script("config=changepwd.set", login_body())
    client.s.script("?login", login_body())        # re-login under the new password
    client.set_password("test-shared-pw")
    call = next(c for c in client.s.calls if "changepwd.set" in c[1])
    form = call[2]["data"]
    assert form["password"] == md5_hex("123456")
    assert form["new_password"] == md5_hex("test-shared-pw")
    assert form["new_password_org"] == "test-shared-pw"
    assert client.password == "test-shared-pw"


def test_set_password_uses_security_endpoint_normally(client):
    client.s.script("?login", login_body(changepwd=0))
    client.login("123456")
    client.s.script("config=security.set", login_body())
    client.s.script("?login", login_body())
    client.set_password("test-shared-pw")
    call = next(c for c in client.s.calls if "security.set" in c[1])
    form = call[2]["data"]
    assert form["username"] == "admin"
    assert form["new_username"] == "admin"
    assert form["new_password"] == md5_hex("test-shared-pw")


def test_set_password_skips_when_already_on_target(client):
    client.s.script("?login", login_body())
    client.login("test-shared-pw")
    client.set_password("test-shared-pw")   # no scripted call -> would raise if it posted
    assert len(client.s.calls) == 1


def test_set_ntp_form_fields(client):
    client.s.script("?login", login_body())
    client.login("123456")
    client.s.script("config=datetime.set", login_body())
    client.set_ntp("192.168.88.10", timezone=840, interval=10)
    form = client.s.calls[-1][2]["data"]
    assert form == {"timezone": 840, "timesetmode": 0,
                    "ntpserverstr": "192.168.88.10", "ntpport": "",
                    "ntpinterval": 10, "manualtime": ""}


def test_upload_media_multipart_field_and_ok_check(client, tmp_path):
    media = tmp_path / "chime.mp3"
    media.write_bytes(b"\xff\xfb" * 100)
    client.s.script("?login", login_body())
    client.login("123456")
    # free-space read, then the upload itself
    client.s.script("config=overview.get", {**OK, "data": {"musicfreespace": "3862528"}})
    client.s.script("mediaupload", "upload OK")
    client.upload_media(str(media), idx=0)
    method, url, kw = client.s.calls[-1]
    assert "mediaupload" in url
    assert kw["params"] == {"idx": 0}
    assert "upload0" in kw["files"]


def test_upload_media_rejects_wrong_extension(client, tmp_path):
    bad = tmp_path / "notes.txt"
    bad.write_text("x")
    with pytest.raises(SpeakerError, match="mp3 or .wav"):
        client.upload_media(str(bad))


def test_upload_media_rejects_when_no_space(client, tmp_path):
    media = tmp_path / "big.mp3"
    media.write_bytes(b"0" * 1000)
    client.s.script("?login", login_body())
    client.login("123456")
    client.s.script("config=overview.get", {**OK, "data": {"musicfreespace": "10"}})
    with pytest.raises(SpeakerError, match="free"):
        client.upload_media(str(media))


def test_upload_media_failure_without_ok_body(client, tmp_path):
    media = tmp_path / "chime.wav"
    media.write_bytes(b"RIFF")
    client.s.script("?login", login_body())
    client.login("123456")
    client.s.script("config=overview.get", {**OK, "data": {"musicfreespace": "999999"}})
    client.s.script("mediaupload", "FAILED")
    with pytest.raises(SpeakerError, match="rejected"):
        client.upload_media(str(media))


def test_set_static_ip_follows_device_to_new_address(client, monkeypatch):
    client.s.script("?login", login_body())
    client.login("123456")
    client.s.script("config=network.set", login_body())
    monkeypatch.setattr("speaker_client.host_iface_for", lambda ip: None)
    monkeypatch.setattr("speaker_client.renew_host_dhcp", lambda iface: None)
    monkeypatch.setattr("speaker_client.time.sleep", lambda s: None)
    monkeypatch.setattr(SpeakerClient, "port_open", lambda self, host=None: True)
    check = client.set_static_ip("192.168.88.70", "255.255.255.0", "192.168.88.1",
                                 "192.168.88.1", "", wait=5)
    form = next(c for c in client.s.calls if "network.set" in c[1])[2]["data"]
    assert form["dhcp"] == 0
    assert form["netip"] == "192.168.88.70"
    assert check["ok"] is True
    assert client.host == "192.168.88.70"
    assert client.base == "http://192.168.88.70"


def test_identity_reads_overview(client):
    client.s.script("?login", login_body())
    client.login("123456")
    client.s.script("config=overview.get", {**OK, "data": {
        "uid": "5044736250826B1C", "serialnumber": "TM-CS20-000001-XX",
        "version": "V3.3.39-PR1", "mac": "74:F8:DB:5F:25:6A",
        "netip": "192.168.1.57"}})
    ident = client.get_identity()
    assert ident["serial"] == "TM-CS20-000001-XX"
    assert ident["mac"] == "74:f8:db:5f:25:6a"
    assert ident["firmware"] == "V3.3.39-PR1"
