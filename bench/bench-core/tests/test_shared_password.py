"""The shared bench password is resolved, never compiled in.

It used to be `DEFAULT_NEW_PASSWORD = "<the real one>"` in bench_core, as a
fallback the per-tool config could override. The reasoning in the comment was
that the bench network is controlled and "real deployments set their own". That
turned out to be false in the only way that matters: the value also shipped in
every `*.example.json`, every station copied the example, nobody overrode it —
so the fallback became the live password on deployed hardware, in plaintext, in
git, in 29 files.

These tests pin the three properties that stop it coming back: there is no
constant to import, an unset password fails loudly instead of guessing, and a
copied example whose placeholder was never filled in counts as unset.
"""
import pytest

import bench_core
from bench_core import (
    NEW_PASSWORD_ENV,
    MissingSharedPassword,
    shared_new_password,
)

REAL_ENOUGH = "A-Station-Password-1!"


@pytest.fixture(autouse=True)
def _no_ambient_password(monkeypatch):
    """The suite-wide autouse fixture in conftest.py sets the env var for every
    other test, which is right for pipelines under test and wrong here: these
    tests are about what happens when nothing supplies a password."""
    monkeypatch.delenv(NEW_PASSWORD_ENV, raising=False)


def test_there_is_no_password_constant_left_to_import():
    # The whole point. A module attribute holding a password is a password in
    # git however carefully its comment is worded.
    assert not hasattr(bench_core, "DEFAULT_NEW_PASSWORD")


def test_the_station_config_supplies_it():
    assert shared_new_password({"new_password": REAL_ENOUGH}) == REAL_ENOUGH


def test_the_environment_supplies_it_when_the_config_does_not(monkeypatch):
    monkeypatch.setenv(NEW_PASSWORD_ENV, REAL_ENOUGH)
    assert shared_new_password({}) == REAL_ENOUGH
    assert shared_new_password(None) == REAL_ENOUGH


def test_the_config_wins_over_the_environment(monkeypatch):
    # A station's own config is the more specific statement of intent, and it is
    # the file an operator edits.
    monkeypatch.setenv(NEW_PASSWORD_ENV, "from-the-environment")
    assert shared_new_password({"new_password": REAL_ENOUGH}) == REAL_ENOUGH


def test_nothing_configured_refuses_instead_of_guessing():
    with pytest.raises(MissingSharedPassword) as e:
        shared_new_password({})
    # The message has to tell an operator what to do, because this is the first
    # thing they will see on a station whose config was never filled in.
    assert "new_password" in str(e.value)
    assert NEW_PASSWORD_ENV in str(e.value)


@pytest.mark.parametrize("placeholder", [
    "", "   ", "SET-ME", "set-me", "changeme", "CHANGEME", "TODO", "xxx",
    "password", "none",
])
def test_an_unfilled_placeholder_counts_as_unset(placeholder):
    # This is the failure mode the examples are designed around: someone copies
    # config/<tool>.config.example.json and runs the tool before editing it. It
    # must fail the way an empty config fails, not authenticate with "SET-ME"
    # and leave them reading device logs to work out why the login was refused.
    with pytest.raises(MissingSharedPassword):
        shared_new_password({"new_password": placeholder})


def test_surrounding_whitespace_is_not_part_of_the_password():
    # A value pasted into JSON tends to bring a space with it.
    assert shared_new_password({"new_password": f"  {REAL_ENOUGH}\n"}) == REAL_ENOUGH


def test_a_comparison_gets_none_rather_than_an_exception():
    # Callers that only ask "is this device already on the shared password?"
    # must not fail a run when no shared password is configured. The honest
    # answer there is "no", which `None` gives without raising.
    assert shared_new_password({}, required=False) is None
    assert shared_new_password({"new_password": "SET-ME"}, required=False) is None
    assert shared_new_password({"new_password": REAL_ENOUGH},
                               required=False) == REAL_ENOUGH


def test_the_key_can_be_named_for_tools_that_call_it_something_else():
    assert shared_new_password({"admin_password": REAL_ENOUGH},
                               key="admin_password") == REAL_ENOUGH
