"""The managed-Mac fallback: reaching a device this interpreter may not.

The failure this exists for reports a plugged-in, pingable, curl-able device as
absent — so the rules about WHEN the fallback engages matter as much as the
relay itself. Two of them are load-bearing and pinned below: an unplugged
device must not be mistaken for a policy block, and a policy block must not be
declared until something the policy permits has confirmed the device is there.
"""
import errno
import socket

import pytest

from bench_core import lan_relay


@pytest.fixture(autouse=True)
def fresh():
    lan_relay.reset()
    yield
    lan_relay.reset()


def refuse(err):
    def fake(address, timeout=None):
        raise OSError(err, "refused")
    return fake


def test_a_station_that_can_connect_stays_direct(monkeypatch):
    monkeypatch.setattr(socket, "create_connection",
                        lambda address, timeout=None: _Closeable())
    assert lan_relay.can_connect("192.168.0.100", 80) is True
    assert lan_relay._mode == "direct"
    assert lan_relay.endpoint("192.168.0.100", 80) == ("192.168.0.100", 80)


def test_an_unplugged_device_is_absent_not_blocked(monkeypatch):
    """A timeout says nothing about policy. Latching "direct" on it would be
    harmless, but latching anything on it spends the one probe that could have
    diagnosed a blocked station later in the shift."""
    monkeypatch.setattr(socket, "create_connection", refuse(errno.ETIMEDOUT))
    monkeypatch.setattr(lan_relay, "_nc_can_connect",
                        lambda *a: pytest.fail("nc must not be asked"))
    assert lan_relay.can_connect("192.168.0.100", 80) is False
    assert lan_relay._mode is None


def test_a_refused_connect_is_not_a_policy_block(monkeypatch):
    """Something answered — there is a host, it just is not listening."""
    monkeypatch.setattr(socket, "create_connection", refuse(errno.ECONNREFUSED))
    assert lan_relay.can_connect("192.168.0.100", 80) is False
    assert lan_relay._mode is None


@pytest.mark.parametrize("err", [errno.EHOSTUNREACH, errno.ENETUNREACH,
                                 errno.EPERM, errno.EACCES])
def test_a_blocked_connect_falls_back_when_the_device_really_is_there(monkeypatch, err):
    monkeypatch.setattr(socket, "create_connection", refuse(err))
    monkeypatch.setattr(lan_relay, "_nc_can_connect", lambda *a: True)
    assert lan_relay.can_connect("192.168.0.100", 80) is True
    assert lan_relay._mode == "relay"


def test_an_empty_cable_also_says_no_route_and_must_not_start_a_relay(monkeypatch):
    """The trap this guards: an unplugged NIC reports EHOSTUNREACH exactly like
    a policy block. Only the permitted binary reaching the device tells them
    apart — and a relay declared here would answer every later probe from its
    own loopback listener, reporting a switch on an empty cable."""
    monkeypatch.setattr(socket, "create_connection", refuse(errno.EHOSTUNREACH))
    monkeypatch.setattr(lan_relay, "_nc_can_connect", lambda *a: False)
    assert lan_relay.can_connect("192.168.0.100", 80) is False
    assert lan_relay._mode is None


def test_no_relay_binary_means_no_fallback(monkeypatch):
    """Windows and most Linux stations have no /usr/bin/nc, and need none."""
    monkeypatch.setattr(socket, "create_connection", refuse(errno.EHOSTUNREACH))
    monkeypatch.setattr(lan_relay.os.path, "exists", lambda path: False)
    assert lan_relay.can_connect("192.168.0.100", 80) is False
    assert lan_relay._mode is None


def test_each_device_port_gets_one_relay_and_keeps_it(monkeypatch):
    """A run opens the web UI repeatedly and holds an SSH session across it;
    a second listener per call would leak a thread and a port each time."""
    started = []
    monkeypatch.setattr(lan_relay, "_mode", "relay")
    monkeypatch.setattr(lan_relay, "_start_relay",
                        lambda host, port: started.append((host, port))
                        or ("127.0.0.1", 40000 + len(started)))
    first = lan_relay.endpoint("192.168.0.100", 80)
    assert lan_relay.endpoint("192.168.0.100", 80) == first
    assert lan_relay.endpoint("192.168.0.100", 22) != first
    assert started == [("192.168.0.100", 80), ("192.168.0.100", 22)]


def test_the_web_base_points_at_whatever_endpoint_says(monkeypatch):
    monkeypatch.setattr(lan_relay, "_mode", "direct")
    assert lan_relay.url_for("192.168.0.100") == "http://192.168.0.100"
    monkeypatch.setattr(lan_relay, "_mode", "relay")
    monkeypatch.setattr(lan_relay, "_start_relay", lambda h, p: ("127.0.0.1", 51000))
    assert lan_relay.url_for("192.168.0.100") == "http://127.0.0.1:51000"


class _Closeable:
    def __enter__(self): return self
    def __exit__(self, *exc): return False
