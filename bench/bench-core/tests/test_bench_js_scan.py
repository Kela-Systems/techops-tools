"""The browser's scan detection must agree with the server's parser (TEC-349).

`findLabelPayload()` in bench.js decides what to send to `/api/label-scan`, and
`parse_device_label()` decides what to do with it. They are two
implementations of "is this a device label?" in two languages, so they can
drift — and the failure mode is quiet: the page either sends nothing (the
operator scans and nothing happens) or trims a payload the server then
rejects. This pins the contract between them.

Skipped when node is absent; bench stations only need Python.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from bench_core.device_label import parse_device_label

BENCH_JS = (Path(__file__).resolve().parent.parent
            / "src" / "bench_core" / "static" / "bench.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node is not installed")

REAL_OTD = ("SN:6008219573;I:864088065513384;M:2097272B00F7;"
            "U:admin;PW:zZ?40*kA;B:015;")
REAL_RUTM = "SN:6010212527;M:20972732F638;U:admin;PW:mL$7=b6N;B:039;"


def run_js(field_values):
    """Feed each string through bench.js's detector, in node.

    bench.js only declares functions at the top level and touches `document`
    from inside them, so it loads outside a browser untouched — no shims, which
    is the point: the tested code is exactly what ships.
    """
    driver = f"""
        const fs = require('fs');
        eval(fs.readFileSync({json.dumps(str(BENCH_JS))}, 'utf8'));
        const out = JSON.parse(process.argv[1]).map((text) => {{
          const hit = findLabelPayload(text);
          return {{
            text: text,
            could: couldBeLabelScan(text),
            payload: hit ? hit.payload : null,
            remainder: hit ? text.slice(0, hit.cut) : null,
          }};
        }});
        process.stdout.write(JSON.stringify(out));
    """
    done = subprocess.run(["node", "-e", driver, json.dumps(field_values)],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.fixture(scope="module")
def js():
    """One node process for the whole module — startup dominates the runtime."""
    cases = [
        REAL_OTD,
        REAL_RUTM,
        "~" + REAL_OTD,                     # with the scan marker
        "~" + REAL_RUTM,
        REAL_OTD.rstrip(";"),               # no trailing semicolon
        "haifa-port~" + REAL_OTD,           # scanned into a half-typed site name
        REAL_OTD[1:],                       # first character lost to a focus change
        "SN:1;M:AABBCCDDEEFF;PW:ab;cd;B:015;",
        # not labels
        "", "haifa-port", "~", "haifa~port", "Dana K", "EMP-00417",
        "1234567890", "M:AABBCCDDEEFF;", "SN:6008219573",
        # a partial burst, mid-scan
        "~SN:60082", "~SN:6008219573;I:8640",
        "~SN:6008219573;I:864088065513384;M:2097272B00F7;U:admin",
        # a label whose PW field is present but empty — the server explains it
        "~SN:6008219573;M:2097272B00F7;U:admin;PW:;B:015;",
    ]
    return {row["text"]: row for row in run_js(cases)}


# ── real labels are found, and handed over intact ─────────────────────────────

@pytest.mark.parametrize("raw", [REAL_OTD, REAL_RUTM])
def test_a_plain_label_is_detected_whole(js, raw):
    row = js[raw]
    assert row["payload"] == raw
    assert row["remainder"] == ""       # nothing left behind in the field


@pytest.mark.parametrize("raw", [REAL_OTD, REAL_RUTM])
def test_the_scan_marker_is_stripped(js, raw):
    row = js["~" + raw]
    assert row["payload"] == raw
    assert row["remainder"] == ""       # the '~' goes too


def test_a_label_scanned_into_a_half_typed_field_leaves_the_typing(js):
    # The pages focus #siteName the moment a device is detected, so this is the
    # normal case, not an edge one.
    row = js["haifa-port~" + REAL_OTD]
    assert row["payload"] == REAL_OTD
    assert row["remainder"] == "haifa-port"


def test_a_burst_missing_its_first_character_still_yields_the_password(js):
    # Focusing the sink costs the first keystroke. With the marker configured
    # that is the expendable '~'; without it, the serial is lost but the
    # password — the point of the exercise — survives.
    payload = js[REAL_OTD[1:]]["payload"]
    assert payload is not None
    assert parse_device_label(payload).password == "zZ?40*kA"


# ── the two implementations agree ─────────────────────────────────────────────
#
# The direction that matters is one-way: anything the page decides to send must
# be something the server can parse. The reverse is deliberately not symmetric
# — see test_js_holds_back_payloads_with_no_password.

@pytest.mark.parametrize("raw", [
    REAL_OTD, REAL_RUTM, "~" + REAL_OTD, "~" + REAL_RUTM, REAL_OTD.rstrip(";"),
    "haifa-port~" + REAL_OTD, REAL_OTD[1:],
    "SN:1;M:AABBCCDDEEFF;PW:ab;cd;B:015;",
])
def test_everything_the_page_sends_is_parseable_by_the_server(js, raw):
    payload = js[raw]["payload"]
    assert payload is not None, f"the page found no label in {raw!r}"
    assert parse_device_label(payload) is not None, \
        f"the page would post {payload!r}, which the server rejects"


@pytest.mark.parametrize("text", [
    "", "haifa-port", "~", "haifa~port", "Dana K", "EMP-00417",
    "1234567890", "M:AABBCCDDEEFF;", "SN:6008219573",
])
def test_neither_side_sees_a_label_in_ordinary_text(js, text):
    assert js[text]["payload"] is None
    assert parse_device_label(text) is None


@pytest.mark.parametrize("text", ["~SN:6008219573;I:8640"])
def test_js_holds_back_payloads_with_no_password(js, text):
    # A deliberate asymmetry. The server parses this happily — two keys is
    # enough structure — and would then refuse it for carrying no password. The
    # page declines to send it at all, because in practice it is not a PW-less
    # label but a *partial* one: a burst the scanner has not finished. Sending
    # it would arm nothing and blame the operator for it.
    assert parse_device_label(text) is not None
    assert js[text]["payload"] is None
    assert js[text]["could"] is True     # the flush timer still has to arm


@pytest.mark.parametrize("raw,password", [
    (REAL_OTD, "zZ?40*kA"),
    (REAL_RUTM, "mL$7=b6N"),
    ("~" + REAL_OTD, "zZ?40*kA"),
    ("haifa-port~" + REAL_OTD, "zZ?40*kA"),
    (REAL_OTD.rstrip(";"), "zZ?40*kA"),
    ("SN:1;M:AABBCCDDEEFF;PW:ab;cd;B:015;", "ab;cd"),
])
def test_the_detected_payload_yields_the_right_password(js, raw, password):
    assert parse_device_label(js[raw]["payload"]).password == password


# ── nothing normal is mistaken for a scan ─────────────────────────────────────

@pytest.mark.parametrize("text", [
    "haifa-port", "Dana K", "EMP-00417", "1234567890", "~", "haifa~port", "",
])
def test_typing_and_badges_are_not_labels(js, text):
    assert js[text]["payload"] is None


@pytest.mark.parametrize("text", [
    "~SN:60082",                            # a few characters in
    "~SN:6008219573;I:8640",                # two keys, no PW yet
    "~SN:6008219573;I:864088065513384;M:2097272B00F7;U:admin",   # right before PW
])
def test_a_partial_burst_is_not_flushed_early(js, text):
    # `input` fires per character, so every prefix of a scan is seen. Ending
    # the burst is normally the flush timer's job (the CR suffix, or 200ms of
    # quiet), but a scanner that stalls would defeat that — so a prefix must
    # not look like a payload in the first place.
    assert js[text]["payload"] is None
    # ...while still arming the timer, or the burst would never be completed.
    assert js[text]["could"] is True


def test_a_stray_marker_arms_the_timer_but_yields_nothing(js):
    # Someone typing '~' in a site name schedules a flush that then finds no
    # payload. That has to be a no-op, not a cleared field.
    assert js["haifa~port"]["could"] is True
    assert js["haifa~port"]["payload"] is None


def test_an_empty_pw_field_is_still_forwarded(js):
    # The key is there, so this is a finished label, not a partial burst. It
    # goes to the server, which refuses it with an explanation the operator can
    # act on — better than the page silently doing nothing.
    raw = "~SN:6008219573;M:2097272B00F7;U:admin;PW:;B:015;"
    assert js[raw]["payload"] == raw[1:]
    assert parse_device_label(js[raw]["payload"]).password == ""


def test_min_keys_matches_the_parser(js):
    from bench_core.device_label import MIN_KEYS
    assert MIN_KEYS == 2
    # One key is below both implementations' threshold.
    assert js["M:AABBCCDDEEFF;"]["payload"] is None
    assert js["SN:6008219573"]["payload"] is None
