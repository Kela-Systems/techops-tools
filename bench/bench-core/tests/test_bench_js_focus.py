"""A scan must not land in the field the operator is typing in (TEC-349).

The pages focus #siteName the moment a device is detected, and a HID scanner
just types — so without intervention the label goes into the site name and the
scanner's trailing Enter submits the form. That was reported from a bench, and
`mountLabelScan` handles it in two layers: it cancels the marker's insertion
and moves focus to an off-screen sink, and if that surface is unavailable it
still lifts the payload back out of whichever field caught it.

These tests drive the real `mountLabelScan` from bench.js against a fake DOM
small enough to be obvious, character by character the way a scanner delivers
one. Nothing here mocks the module under test.

Skipped when node is absent; bench stations only need Python.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

BENCH_JS = (Path(__file__).resolve().parent.parent
            / "src" / "bench_core" / "static" / "bench.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node is not installed")

REAL_OTD = ("SN:6008219573;I:864088065513384;M:2097272B00F7;"
            "U:admin;PW:zZ?40*kA;B:015;")

# Enough of a DOM for mountLabelScan: elements with a value and a caret,
# capture-phase listeners on `document`, and a focus notion. Deliberately not
# jsdom — a stub this size is auditable, and the point is to pin behaviour, not
# to emulate a browser.
FAKE_DOM = r"""
const listeners = {};
let active = null;
const nodes = {};

function makeEl(id, tag) {
  const el = {
    id: id, tagName: tag || 'INPUT', value: '', placeholder: '', className: '',
    _fake: true,
    disabled: false, tabIndex: 0, isConnected: true, selectionStart: 0,
    style: {}, children: [],
    classList: {
      add() {}, remove() {}, toggle() {}, contains: () => false,
    },
    setAttribute() {}, appendChild(c) { el.children.push(c); },
    insertAdjacentElement() {}, prepend() {},
    querySelector: () => null, querySelectorAll: () => [],
    addEventListener() {},
    setSelectionRange(a) { el.selectionStart = a; },
    focus() { active = el; },
    dispatchEvent(ev) { fire(ev.type, { ...ev, target: el }); return true; },
  };
  nodes[id] = el;
  return el;
}

// Looks up by the element's *current* id, since bench.js assigns ids after
// createElement (that is how #benchScanSink gets its name).
function byId(id) {
  return Object.values(nodes).find((el) => el.id === id) || null;
}

function fire(type, ev) {
  (listeners[type] || []).forEach((fn) => fn(ev));
}

global.document = {
  body: makeEl('body', 'BODY'),
  documentElement: makeEl('html', 'HTML'),
  get activeElement() { return active; },
  addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
  createElement: (tag) => makeEl('created-' + tag + '-' + Object.keys(nodes).length, tag),
  getElementById: byId,
  querySelector: () => null,
};
global.window = { addEventListener() {}, location: { host: 'x' } };

// Records every POST instead of making one. Installed *after* bench.js is
// eval'd — it declares its own postJSON, which would otherwise win.
const posted = [];

// Type `text` into whatever has focus, one character at a time, exactly as a
// keyboard wedge does: a cancelable beforeinput, then the insertion, then
// input. `enter` appends the scanner's CR suffix as a keydown.
function typeInto(text, enter) {
  for (const ch of text) {
    const target = active;
    if (!target) continue;
    let cancelled = false;
    fire('beforeinput', {
      type: 'beforeinput', data: ch, target: target,
      preventDefault() { cancelled = true; },
      stopPropagation() {},
    });
    if (cancelled) continue;
    const at = active;   // beforeinput may have moved focus to the sink
    at.value += ch;
    at.selectionStart = at.value.length;
    fire('input', { type: 'input', target: at });
  }
  if (!enter) return;
  let prevented = false, stopped = false;
  fire('keydown', {
    type: 'keydown', key: 'Enter', target: active,
    preventDefault() { prevented = true; },
    stopPropagation() { stopped = true; },
  });
  return { prevented, stopped };
}
"""


def run_scenario(script):
    """Load the real bench.js over the fake DOM and run `script`."""
    driver = f"""
        const fs = require('fs');
        {FAKE_DOM}
        eval(fs.readFileSync({json.dumps(str(BENCH_JS))}, 'utf8'));
        postJSON = async (url, body) => {{ posted.push({{ url, body }}); return {{}}; }};
        const result = (async () => {{ {script} }})();
        result.then((out) => process.stdout.write(JSON.stringify(out)))
              .catch((e) => {{ console.error(e); process.exit(1); }});
    """
    done = subprocess.run(["node", "-e", driver], capture_output=True,
                          text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


PRELUDE = """
    const siteName = document.createElement('input');
    siteName.id = 'siteName';
    const scan = mountLabelScan(null);
    scan.update({ label_scan: { enabled: true, armed: null, problem: null },
                  active_mac: '2097272B00F7' });
    siteName.value = 'Hohit';
    siteName.selectionStart = 5;
    siteName.focus();
"""

SETTLE = "await new Promise((r) => setTimeout(r, 400));"


def test_a_scan_leaves_the_typed_site_name_untouched():
    """The reported bug: the payload was appended to the site name."""
    out = run_scenario(PRELUDE + f"""
        const keys = typeInto('~{REAL_OTD}', true);
        {SETTLE}
        return {{
          siteName: siteName.value,
          sink: byId('benchScanSink').value,
          posted: posted,
          keys: keys,
          refocused: document.activeElement.id,
        }};
    """)

    assert out["siteName"] == "Hohit"
    assert out["sink"] == ""
    assert [p["url"] for p in out["posted"]] == ["/api/label-scan"]
    assert out["posted"][0]["body"]["raw"] == REAL_OTD


def test_the_scanners_enter_cannot_submit_the_form():
    out = run_scenario(PRELUDE + f"""
        const keys = typeInto('~{REAL_OTD}', true);
        {SETTLE}
        return {{ keys: keys }};
    """)
    # Both are required: preventDefault stops a form's implicit submission,
    # stopPropagation stops the field's own keydown handler.
    assert out["keys"]["prevented"] is True
    assert out["keys"]["stopped"] is True


def test_focus_comes_back_to_the_field_the_operator_was_in():
    out = run_scenario(PRELUDE + f"""
        typeInto('~{REAL_OTD}', true);
        {SETTLE}
        return {{ focused: document.activeElement.id }};
    """)
    assert out["focused"] == "siteName"


def test_a_stray_marker_is_handed_back_rather_than_swallowed():
    """A '~' that never becomes a label must not cost the operator keystrokes.

    Contrived — no site name contains a tilde — but the alternative is that any
    unrecognised burst silently eats input, and that is not a failure an
    operator could diagnose.
    """
    out = run_scenario(PRELUDE + f"""
        typeInto('~hello', false);
        {SETTLE}
        return {{
          siteName: siteName.value,
          sink: byId('benchScanSink').value,
          focused: document.activeElement.id,
          posted: posted.length,
        }};
    """)
    assert out["siteName"] == "Hohit~hello"
    assert out["sink"] == ""
    assert out["focused"] == "siteName"
    assert out["posted"] == 0


def test_a_scan_with_no_marker_is_still_lifted_out_of_an_empty_field():
    """The fallback layer: a scanner with no prefix configured sends no '~' at
    all, so the burst does land in #siteName and has to be pulled back out."""
    out = run_scenario(PRELUDE + f"""
        siteName.value = '';
        siteName.selectionStart = 0;
        typeInto('{REAL_OTD}', true);
        {SETTLE}
        return {{
          siteName: siteName.value,
          posted: posted.map((p) => p.body.raw),
        }};
    """)
    assert out["posted"] == [REAL_OTD]
    assert out["siteName"] == ""


def test_without_the_marker_a_scan_abutting_typed_text_loses_its_serial():
    """The cost of having no marker, pinned rather than left to be discovered.

    The shape fallback will only anchor a key that follows a non-alphanumeric
    character, so that ordinary words ending in something like "ROOM:" are not
    mistaken for a label. A burst appended straight onto a typed site name
    gives `HohitSN:` — the `SN` is unanchorable, and detection settles on the
    next key boundary instead, `;I:`. The scan still works and is still
    cross-checked, because the MAC and password are past that point; the serial
    is simply lost from the record.

    That is the argument for the prefix rather than a bug to fix here: `~` is
    always anchorable, and the charset self-test refuses to pass without it.
    """
    out = run_scenario(PRELUDE + f"""
        typeInto('{REAL_OTD}', true);
        {SETTLE}
        return {{
          siteName: siteName.value,
          posted: posted.map((p) => p.body.raw),
        }};
    """)
    raw = out["posted"][0]
    assert raw.startswith("I:")
    assert "SN:6008219573" not in raw
    assert "M:2097272B00F7" in raw and "PW:" in raw   # cross-check and login
    assert out["siteName"] == "HohitSN:6008219573;"    # the unanchorable head
    assert "PW:" not in out["siteName"]                # but never the password


def test_nothing_is_captured_when_the_tool_has_not_opted_in():
    """The other four bench tools share bench.js and must be unaffected."""
    out = run_scenario("""
        const siteName = document.createElement('input');
        siteName.id = 'siteName';
        const scan = mountLabelScan(null);
        scan.update({ label_scan: { enabled: false } });
        siteName.focus();
    """ + f"""
        const keys = typeInto('~{REAL_OTD}', true);
        {SETTLE}
        return {{ siteName: siteName.value, posted: posted.length, keys: keys }};
    """)
    assert out["posted"] == 0
    assert out["siteName"] == "~" + REAL_OTD
    assert out["keys"]["prevented"] is False
