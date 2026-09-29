"""The dashboard's inline script has to parse.

This exists because it once didn't. An edit to the banner copy ended in the
middle of a string literal and dropped the closing `" +`. Python's `+`-joined
string style makes that a one-character mistake, and the cost is total: a
SyntaxError anywhere in the block means the browser runs NONE of it, so the
page renders its empty shell with no banner, no coverage bars and no node
table. It looks like the data failed to load rather than like a typo.

Nothing caught it. The 204 tests here all exercise the Python side, `lint`
reads the YAML, and `export` only writes a data file — none of them opens the
page. So the failure reached a published dashboard and a person had to notice
it and report it back.

`node --check` is the real guard and also runs in CI, where node is always
present. Locally it skips rather than fails, so a station without node can
still run the suite.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent.parent / "ui"
PAGE = UI / "index.html"
SCRIPT_BLOCK = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


def inline_scripts(html: str) -> list[str]:
    """Only the blocks with a body. A `<script src=...>` has nothing to parse
    here — its contents are a separate file."""
    return [b for b in SCRIPT_BLOCK.findall(html) if b.strip()]


def test_the_page_exists_and_carries_an_inline_script():
    # If this ever fails the page was restructured, and the check below is
    # silently testing nothing — which is how the original bug survived.
    assert PAGE.is_file(), PAGE
    blocks = inline_scripts(PAGE.read_text())
    assert blocks, "no inline script found — has the page been restructured?"
    assert len(blocks[0]) > 5000, "the script block is suspiciously small"


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed; CI always has it")
def test_the_inline_script_parses(tmp_path):
    for i, body in enumerate(inline_scripts(PAGE.read_text())):
        js = tmp_path / f"block{i}.js"
        js.write_text(body)
        done = subprocess.run(["node", "--check", str(js)],
                              capture_output=True, text=True)
        assert done.returncode == 0, (
            f"ui/index.html inline script block {i} does not parse:\n"
            f"{done.stderr}"
        )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_exported_data_file_parses(tmp_path):
    # `export` writes this, so a malformed payload would break the page just as
    # completely as a malformed script — and from a code path no test covers.
    data = UI / "site-data.js"
    if not data.is_file():
        pytest.skip("ui/site-data.js not generated in this checkout")
    done = subprocess.run(["node", "--check", str(data)],
                          capture_output=True, text=True)
    assert done.returncode == 0, f"ui/site-data.js does not parse:\n{done.stderr}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_an_unterminated_string_would_be_caught(tmp_path):
    # A negative control. Without this, a regex that stopped matching the page
    # would make the test above pass by finding nothing to check.
    broken = tmp_path / "broken.js"
    broken.write_text('var html = "a" +\n  "b \n  "c";\n')
    done = subprocess.run(["node", "--check", str(broken)],
                          capture_output=True, text=True)
    assert done.returncode != 0, "node --check accepted an unterminated string"


# ── what the page actually renders ──────────────────────────────────────────
#
# The check above only proves the script PARSES. It shipped a page that parsed
# perfectly and rendered the wrong site: `export sites/*.yaml` globbed in the
# schema template, whose name sorts before every real site, and the page read
# `DATA.sites[0]`. So several published versions showed a fictional 16-device
# site with 0/14 proven, wrapped in prose about a real one. Parsing was never
# the property that mattered — rendering was.
#
# This drives the real page against the real payload in a stub DOM and asserts
# on what came out.

HARNESS = r"""
const fs = require("fs");
global.window = global;
const store = {};
function el(id) {
  return { id,
    set innerHTML(v) { store[id] = v; }, get innerHTML() { return store[id] || ""; },
    set textContent(v) { store[id] = v; }, get textContent() { return store[id] || ""; },
    addEventListener() {}, removeEventListener() {}, setAttribute() {},
    removeAttribute() {}, getAttribute() { return null },
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false } },
    querySelectorAll: () => [], querySelector: () => null, appendChild() {},
    scrollIntoView() {}, focus() {}, style: {}, dataset: {}, closest: () => null };
}
global.document = { getElementById: el, createElement: () => el("new"),
  querySelectorAll: () => [], querySelector: () => null, addEventListener() {},
  documentElement: el("root"), body: el("body") };
global.location = { hash: "", search: "" };
eval(fs.readFileSync(process.argv[2], "utf8"));           // site-data.js
eval(fs.readFileSync(process.argv[3], "utf8"));           // archive-data.js
const html = fs.readFileSync(process.argv[4], "utf8");    // index.html
const re = /<script[^>]*>([\s\S]*?)<\/script>/g;
let m; const blocks = [];
while ((m = re.exec(html))) if (m[1].trim()) blocks.push(m[1]);
eval(blocks[0]);
console.log(JSON.stringify(store));
"""


def render_page(tmp_path):
    """Boot ui/index.html against ui/site-data.js and return {elementId: html}."""
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    data = UI / "site-data.js"
    if not data.is_file():
        pytest.skip("ui/site-data.js not generated in this checkout")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS)
    done = subprocess.run(
        ["node", str(harness), str(data), str(UI / "archive-data.js"), str(PAGE)],
        capture_output=True, text=True)
    assert done.returncode == 0, f"the page threw while rendering:\n{done.stderr}"
    import json
    return json.loads(done.stdout)


def text_of(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def test_the_page_renders_a_real_site_not_a_template(tmp_path):
    store = render_page(tmp_path)
    name = text_of(store.get("site-name", ""))
    assert name, "no site name rendered"
    assert not name.startswith("_"), (
        f"the dashboard is rendering the template site {name!r}. A template is "
        f"invented data; publishing it as a site is worse than publishing "
        f"nothing."
    )


def test_the_numbers_on_the_page_are_the_sites_own_numbers(tmp_path):
    # The bug that got through was a MISMATCH: real prose, template numbers.
    # Tie the rendered text back to the payload it claims to describe.
    import json
    store = render_page(tmp_path)
    payload = (UI / "site-data.js").read_text()
    body = payload[payload.index("{"):payload.rstrip().rstrip(";").rindex("}") + 1]
    site = json.loads(body)["sites"][0]
    cov = site["coverage"]

    lede = text_of(re.search(r'<p class="lede">(.*?)</p>', store["banner"], re.S).group(1))
    assert f"{len(site['nodes'])} devices" in lede, lede
    assert f"{cov['model']['proven']} of {cov['model']['total']}" in lede, lede
    assert text_of(store["site-name"]) == site["name"]


def test_the_banner_opens_short_and_keeps_the_detail(tmp_path):
    # The banner was eight always-visible paragraphs and unreadable. It has to
    # stay scannable, and it must not have achieved that by deleting the
    # detail — so both halves are asserted.
    store = render_page(tmp_path)
    banner = store["banner"]
    visible = text_of(re.sub(r'<div class="bbody">.*?</div>', "", banner, flags=re.S))
    hidden = " ".join(text_of(m) for m in
                      re.findall(r'<div class="bbody">(.*?)</div>', banner, re.S))
    assert len(visible) < 900, f"the collapsed banner is too long ({len(visible)} chars)"
    assert len(hidden) > 2000, "the detail was deleted rather than collapsed"
    assert banner.count("<details>") >= 5


# ── the stylesheet's own vocabulary ─────────────────────────────────────────
#
# A misspelled custom property fails silently and completely: the declaration
# is dropped, the element keeps whatever it inherited, and the page still
# parses, renders and passes every test above. That is how the site switcher
# shipped with `background: var(--ink); color: var(--card)` — `--card` is not
# a token this page defines, so the label inherited dark ink onto a near-black
# ground and the selected site name arrived looking redacted.
#
# Nothing here opens a browser, so this is the only cheap guard: every token
# used has to be one the page defines.

TOKEN_USE = re.compile(r"var\((--[a-z0-9-]+)")
TOKEN_DEF = re.compile(r"^\s*(--[a-z0-9-]+)\s*:", re.M)


def test_every_css_variable_the_page_uses_is_one_it_defines():
    html = PAGE.read_text()
    used = set(TOKEN_USE.findall(html))
    defined = set(TOKEN_DEF.findall(html))
    missing = sorted(used - defined)
    assert not missing, (
        f"these custom properties are used but never defined, so every "
        f"declaration reading them is silently dropped: {missing}"
    )


def test_the_guard_would_catch_an_invented_token():
    # A negative control: without this, a regex that stopped matching the
    # page would make the check above pass by finding nothing.
    html = 'a { color: var(--nope); } :root { --real: #fff; }'
    used = set(TOKEN_USE.findall(html))
    defined = set(TOKEN_DEF.findall(html))
    assert sorted(used - defined) == ["--nope"]
