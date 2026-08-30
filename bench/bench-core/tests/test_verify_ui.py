"""The Verify button, on every page that has one (TEC-348).

Two halves, because the mode's UI has two failure modes.

The first is the shared behaviour in `bench.js`: what Verify posts (nothing an
operator typed — the whole point is that a QA sweep needs no site name and no
label password), and when it is available (a device is detected and nothing is
running). Driven for real in node against a fake DOM, in the style of
`test_bench_js_focus.py`.

The second is a per-page audit. Five pages each wire the same six things by
hand, and a page that quietly misses one is invisible in Python tests: the
operator just never gets a button, or gets a verify run whose live log never
appears because the page still compares `phase === 'configuring'`. Static
assertions over the HTML catch that; they are crude, and they are the only
thing standing between a copy-paste and a tool without the mode.

Skipped (the node half) when node is absent; bench stations only need Python.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parent.parent.parent          # bench/
BENCH_JS = BENCH / "bench-core" / "src" / "bench_core" / "static" / "bench.js"
BENCH_CSS = BENCH / "bench-core" / "src" / "bench_core" / "static" / "bench.css"

# The five tools that support the mode. Magos radar/APU are deliberately absent
# — a different base and a different verification shape, tracked separately.
PAGES = {
    "tsw": BENCH / "tsw-config-ui" / "static" / "tsw.html",
    "rutm": BENCH / "rutm-config-ui" / "static" / "rutm.html",
    "otd": BENCH / "otd-config-ui" / "static" / "otd.html",
    "speaker": BENCH / "speaker-config-ui" / "static" / "speaker.html",
    "raythink": BENCH / "raythink-config-ui" / "static" / "raythink.html",
}


def page(name: str) -> str:
    return PAGES[name].read_text()


# ── the shared button, driven for real ───────────────────────────────────────

needs_node = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node is not installed")

FAKE_DOM = r"""
const nodes = {};

function makeEl(id) {
  const handlers = {};
  const el = {
    id: id, disabled: false, textContent: '', className: '', style: {},
    addEventListener(type, fn) { (handlers[type] = handlers[type] || []).push(fn); },
    click() { (handlers.click || []).forEach((fn) => fn()); },
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    animate() {}, querySelector: () => null, querySelectorAll: () => [],
    appendChild() {}, setAttribute() {}, focus() {},
  };
  nodes[id] = el;
  return el;
}

global.document = {
  body: makeEl('body'),
  documentElement: makeEl('html'),
  activeElement: null,
  addEventListener() {},
  createElement: () => makeEl('created'),
  getElementById: (id) => nodes[id] || null,
  querySelector: () => null,
};
global.window = { addEventListener() {}, location: { host: 'x' } };

const posted = [];
"""


def drive(script):
    """Load the real bench.js over the fake DOM and run `script`."""
    driver = f"""
        const fs = require('fs');
        {FAKE_DOM}
        eval(fs.readFileSync({json.dumps(str(BENCH_JS))}, 'utf8'));
        postJSON = async (url, body, opts) => {{
          posted.push({{ url, body, buttons: (opts || {{}}).buttons || [] }});
          return {{}};
        }};
        const result = (async () => {{ {script} }})();
        result.then((out) => process.stdout.write(JSON.stringify(out)))
              .catch((e) => {{ console.error(e); process.exit(1); }});
    """
    done = subprocess.run(["node", "-e", driver], capture_output=True,
                          text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


MOUNT = """
    const btn = makeEl('verifyBtn');
    const verify = mountVerifyButton('verifyBtn');
"""

DETECTED = "{ verify_supported: true, detected: true, busy: false, phase: 'detected' }"


@needs_node
def test_pressing_verify_posts_to_the_verify_route():
    out = drive(MOUNT + f"""
        verify.update({DETECTED});
        btn.click();
        await new Promise((r) => setTimeout(r, 10));
        return {{ posted: posted }};
    """)
    assert [p["url"] for p in out["posted"]] == ["/api/verify"]


@needs_node
def test_verify_sends_no_password_and_no_site_name():
    # The mode exists so an operator can sweep a finished batch knowing nothing
    # about any individual unit. A page that sent its form fields would make
    # the button lie about that.
    out = drive(MOUNT + f"""
        verify.update({DETECTED});
        btn.click();
        await new Promise((r) => setTimeout(r, 10));
        return {{ body: posted[0].body }};
    """)
    assert out["body"] == {"expected": {}}


@needs_node
def test_a_page_may_offer_per_unit_expectations():
    # The escape hatch: an operator who DOES know the site name can supply it,
    # and it goes in `expected` rather than as a configure input.
    out = drive("""
        const btn = makeEl('verifyBtn');
        const verify = mountVerifyButton('verifyBtn',
          { expected: () => ({ site_name: 'Haifa Port' }) });
    """ + f"""
        verify.update({DETECTED});
        btn.click();
        await new Promise((r) => setTimeout(r, 10));
        return {{ body: posted[0].body }};
    """)
    assert out["body"] == {"expected": {"site_name": "Haifa Port"}}


@needs_node
def test_the_button_is_offered_whenever_a_device_is_detected_and_idle():
    out = drive(MOUNT + f"""
        verify.update({DETECTED});
        return {{ disabled: btn.disabled, hidden: btn.style.display }};
    """)
    assert out["disabled"] is False
    assert out["hidden"] == ""


@needs_node
@pytest.mark.parametrize("state,why", [
    ("{ verify_supported: true, detected: false, busy: false, phase: 'waiting' }",
     "nothing plugged in"),
    ("{ verify_supported: true, detected: true, busy: true, phase: 'configuring' }",
     "a configure run is in flight"),
    ("{ verify_supported: true, detected: true, busy: true, phase: 'verifying' }",
     "a verify run is in flight"),
])
def test_the_button_is_unavailable_when_it_would_be_refused(state, why):
    # The server rejects all three with 409/400. A button that posted anyway
    # would produce an error toast where nothing was wrong.
    out = drive(MOUNT + f"""
        verify.update({state});
        return {{ disabled: btn.disabled }};
    """)
    assert out["disabled"] is True, why


@needs_node
def test_a_tool_without_the_mode_shows_no_button():
    # `verify_supported` is a class attribute, so a tool that has not been
    # converted (or the Magos pair) must not show a button that 404s.
    out = drive(MOUNT + """
        verify.update({ verify_supported: false, detected: true, busy: false });
        return { hidden: btn.style.display };
    """)
    assert out["hidden"] == "none"


@needs_node
def test_the_button_says_what_it_is_doing_while_it_runs():
    out = drive(MOUNT + """
        verify.update({ verify_supported: true, detected: true, busy: true,
                        phase: 'verifying' });
        return { label: btn.textContent };
    """)
    assert out["label"] == "Verifying…"


@needs_node
def test_a_page_with_no_verify_button_is_not_broken_by_the_helper():
    # bench.js is shared with the Magos pages, which have no such button.
    out = drive("""
        const verify = mountVerifyButton('verifyBtn');
        verify.update({ verify_supported: true, detected: true });
        return { ok: true };
    """)
    assert out["ok"] is True


@needs_node
def test_a_verify_run_counts_as_a_run_in_flight():
    # `isRunning` is what gates the spinner, the live log and the disabled
    # controls. If `verifying` were missing from it, a verify pass would look
    # like an idle page for as long as it took.
    out = drive("""
        return {
          verifying: isRunning({ phase: 'verifying' }),
          configuring: isRunning({ phase: 'configuring' }),
          verified_is_finished: isFinished({ phase: 'verified' }),
          detected: isRunning({ phase: 'detected' }),
        };
    """)
    assert out == {"verifying": True, "configuring": True,
                   "verified_is_finished": True, "detected": False}


# ── the per-page audit ───────────────────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(PAGES))
def test_every_page_has_a_verify_button_wired_to_the_shared_helper(name):
    html = page(name)
    assert 'id="verifyBtn"' in html
    assert "mountVerifyButton(" in html
    # Wiring it and never updating it leaves a button that is enabled while a
    # run is in flight.
    assert re.search(r"verifyBtn\.update\(s\)", html)


@pytest.mark.parametrize("name", sorted(PAGES))
def test_every_page_labels_the_two_new_phases(name):
    # An unlabelled phase renders as the raw string ("verifying") in the header.
    html = page(name)
    assert "verifying:" in html and "verified:" in html


@pytest.mark.parametrize("name", sorted(PAGES))
def test_every_page_counts_verify_runs_separately(name):
    # A sweep re-checks units already in the "done" pile, so folding the two
    # together would double-count the session.
    html = page(name)
    assert 'id="cVerified"' in html
    assert 'id="cVerifyFailed"' in html


@pytest.mark.parametrize("name", sorted(PAGES))
def test_every_page_distinguishes_verify_rows_in_its_history(name):
    # A verify row badged DONE would read as "this unit was provisioned", which
    # is the one thing it does not mean.
    html = page(name)
    assert "r.kind === 'verify'" in html


@pytest.mark.parametrize("name", sorted(PAGES))
def test_no_page_decides_a_run_is_live_by_comparing_to_configuring(name):
    # The subtle one: a page that kept `s.phase === 'configuring'` shows no
    # spinner and no live log for a verify run, and leaves its buttons enabled
    # under it. `isRunning` covers both kinds.
    html = page(name)
    assert "=== 'configuring'" not in html
    assert "isRunning(s)" in html


def test_the_new_phases_are_styled():
    # Without these the status banner falls back to unstyled text mid-run.
    css = BENCH_CSS.read_text()
    assert ".s-verifying" in css
    assert ".s-verified" in css
