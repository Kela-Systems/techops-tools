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
