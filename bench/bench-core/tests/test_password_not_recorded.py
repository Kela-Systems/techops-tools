"""Verification checks must never carry a password (TEC-349).

`verify_configuration()` returns `{item, expected, actual, ok}` rows that go
into every run record verbatim, are written to the per-run JSON, and are
uploaded to bench-central. Two passwords can reach that row:

* the station's shared password, via `expected`;
* the device's own per-device label password, via `actual` — but only when the
  password change did NOT take, which is exactly the run someone will later go
  back and read.

Since TEC-349 the bench is handed the label password off the sticker, so this
stopped being theoretical. The check reports the outcome instead.
"""
import pytest

from bench_core import TeltonikaClient

SHARED = "Kelasys123!"
LABEL = "zZ?40*kA"          # a real OTD500 label password


def client(current_password):
    """A client with SSH stubbed out — `verify_configuration` needs no network
    once every optional check is out of scope."""
    c = TeltonikaClient(host="192.0.2.1")
    c.ssh_exec = lambda *a, **k: ""
    c.password = current_password
    return c


def verify(current_password):
    return client(current_password).verify_configuration(
        hostname="otd-haifa", zonename="Asia/Jerusalem", new_password=SHARED,
        sim_4g=False, rms=False, tailscale=False)


def password_check(checks):
    return next(c for c in checks if c["item"] == "admin/root password")


def test_the_password_change_is_reported_as_passed():
    check = password_check(verify(SHARED))
    assert check["ok"] is True
    assert check["actual"] == "in use"


def test_a_failed_password_change_is_reported_as_failed():
    check = password_check(verify(LABEL))
    assert check["ok"] is False
    assert "NOT set" in check["actual"]


@pytest.mark.parametrize("current,secret", [
    (SHARED, SHARED),       # the shared password, on the passing path
    (LABEL, SHARED),        # ...and on the failing one
    (LABEL, LABEL),         # the device's label password, still in use
])
def test_no_check_contains_a_password(current, secret):
    # Every row, not just the password one: a leak anywhere in the list ends up
    # in the same record.
    for check in verify(current):
        assert secret not in str(check), check
