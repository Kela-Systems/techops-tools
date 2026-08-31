"""What a radar's QA label says about the channel (TEC-352).

A configure run picks the channel, so its record carries one and the label
prints the big `CH n` hero. A verify run assigns nothing — so the channel on
one of those labels may only ever be a channel the radar itself confirmed, via
`confirmed_channel`. Everything else falls back to the address face.

The temptation these tests exist to block is deriving the channel from the IP,
since channel n does map to `.5n`. It cannot be done: the channel is an RF
setting pushed into the firmware by `set_channel`, and a manual-IP run (or old
firmware) leaves it untouched, so a radar answering at `.51` need not be
transmitting on chan1. Guessing would put a wrong frequency on a sticker that
outlives the bench.
"""
import pytest

from bench_core.qa_label import label_content, render_zpl

import app as radar_mod

radar = radar_mod.configurator


def result(ip="192.168.88.51", serial="SN-001", verification=None, ok=True):
    return {
        "ok": ok, "skipped": False, "ip": ip,
        "identity": {"serial": serial, "mac": "aa:bb:cc:dd:ee:ff",
                     "model": "AR-300"},
        "raw": {}, "steps": [], "log": "", "error": None,
        "verified": ok, "verify_detail": None,
        "verification": verification if verification is not None else [],
    }


def rf_row(actual, ok):
    return {"item": "RF channel", "expected": "chan1", "actual": actual, "ok": ok}


# ── the configure side ───────────────────────────────────────────────────────

def test_a_configure_run_prints_a_channel_the_radar_confirmed():
    entry = radar.build_entry(radar.resolve_target("1", None), "192.168.40.1",
                              result(verification=[rf_row("chan1", True)]), 90)
    assert entry["device"] == {**entry["device"], "channel": "1", "rf_channel": "1"}
    content = label_content(entry)
    assert content.face == "channel"
    assert content.hero == "1"


def test_a_radar_without_an_rf_channel_records_the_pick_but_prints_the_address():
    """Only the AR-300 line has a channel. `set_channel` skips the step without
    complaint on a radar that has none, so the run succeeds and the pick is
    still recorded — a later verify pass checks against it. What must NOT happen
    is a frequency appearing on the label of a radar that has no frequency."""
    entry = radar.build_entry(
        radar.resolve_target("1", None), "192.168.40.1",
        result(verification=[rf_row("this radar does not report the channel "
                                    "it is on", None)]), 90)
    assert entry["device"]["channel"] == "1"       # intent, kept for verify
    assert "rf_channel" not in entry["device"]     # nothing confirmed it
    content = label_content(entry)
    assert content.face == "shared-ip"
    assert "CH" not in render_zpl(entry)


def test_a_typed_address_prints_the_address_not_the_channel():
    # `resolve_target` stores the literal "other" here, and a manual-IP run
    # deliberately never touches the RF channel.
    entry = radar.build_entry(radar.resolve_target(None, "192.168.88.77"),
                              "192.168.40.1", result(ip="192.168.88.77"), 90)
    assert entry["device"]["channel"] == "other"
    assert "rf_channel" not in entry["device"]
    content = label_content(entry)
    assert content.face == "shared-ip"
    assert content.hero == "192.168.88.77"
    assert "other" not in render_zpl(entry).lower()


# ── the verify side: only a confirmed channel is printed ──────────────────────

def test_a_verify_run_prints_a_channel_the_radar_confirmed():
    entry = radar.build_verify_entry(
        "192.168.88.51", result(verification=[rf_row("chan1", True)]), 20)
    assert entry["device"]["rf_channel"] == "1"
    assert "channel" not in entry["device"]   # a verify pass assigns nothing
    content = label_content(entry)
    assert content.face == "channel"
    assert content.hero == "1"


@pytest.mark.parametrize("actual,ok", [
    ("this radar does not report the channel it is on", None),  # old firmware
    ("chan3", False),                                           # wrong channel
    ("something-else", None),                                  # unlisted variant
])
def test_a_verify_run_prints_the_address_when_the_channel_was_not_confirmed(actual, ok):
    entry = radar.build_verify_entry(
        "192.168.88.51", result(verification=[rf_row(actual, ok)]), 20)
    assert "rf_channel" not in entry["device"]
    content = label_content(entry)
    assert content.face == "shared-ip"
    assert content.hero == "192.168.88.51"


def test_a_verify_label_never_derives_the_channel_from_the_address():
    """The radar is at .51 and reported nothing. `CH 1` must not appear."""
    entry = radar.build_verify_entry(
        "192.168.88.51", result(verification=[rf_row("unreadable", None)]), 20)
    zpl = render_zpl(entry)
    assert "CH" not in zpl
    assert "192.168.88.51" in zpl


def test_a_verify_run_with_no_channel_row_at_all_is_fine():
    entry = radar.build_verify_entry("192.168.88.51", result(), 20)
    assert "rf_channel" not in entry["device"]
    assert label_content(entry).face == "shared-ip"
