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

A tool that writes its OWN `verify_configuration` (the TSW202 does, since the
shared one's parameters describe a router's surface) has to hold the same line,
so it is checked here rather than only in its own suite — one place to look
when a new tool is added.
"""
import sys
from pathlib import Path

import pytest

from bench_core import TeltonikaClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tsw-config-ui"))
from tsw_configure import TswClient   # noqa: E402

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


# ── the TSW202's own verify_configuration, held to the same rule ─────────────

TSW_LABEL = "qN4$8xTr"      # a TSW202-shaped label password


def tsw_verify(current_password):
    c = TswClient(host="192.0.2.2")
    c.ssh_exec = lambda *a, **k: ""
    c.password = current_password
    return c.verify_configuration(new_password=SHARED, zonename="Asia/Jerusalem",
                                  ntp_server="192.168.88.10",
                                  minimum_firmware="TSW2_R_00.01.07.1")


def test_the_tsw_reports_the_password_outcome_too():
    assert password_check(tsw_verify(SHARED))["actual"] == "in use"
    failed = password_check(tsw_verify(TSW_LABEL))
    assert failed["ok"] is False
    assert "NOT set" in failed["actual"]


@pytest.mark.parametrize("current,secret", [
    (SHARED, SHARED),
    (TSW_LABEL, SHARED),
    (TSW_LABEL, TSW_LABEL),
])
def test_no_tsw_check_contains_a_password(current, secret):
    for check in tsw_verify(current):
        assert secret not in str(check), check


def test_both_tools_name_the_password_row_identically():
    # bench-central reads records from every tool; a row renamed in one of them
    # would quietly drop out of any cross-tool query for password failures.
    assert (password_check(tsw_verify(SHARED))["item"]
            == password_check(verify(SHARED))["item"])
