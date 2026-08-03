"""BenchConfigurator wiring of the config self-check (TEC-356): fingerprint +
warnings computed at startup, refreshed on /api/reload, stamped into runs."""
import json

import pytest

from bench_core.bench_ui import BenchConfigurator

PLACEHOLDER_CFG = {"host": "192.168.1.1",
                   "tailscale": {"enabled": True, "mint_per_device": True,
                                 "api_key": "tskey-api-xxxxx",
                                 "tailnet": "example.com"}}
GOOD_CFG = {"host": "192.168.1.1",
            "tailscale": {"enabled": True, "mint_per_device": True,
                          "api_key": "tskey-api-kFGiyz3CNTRL",
                          "tailnet": "kela.ts.net"}}


class Tool(BenchConfigurator):
    config_filename = "config/tool.config.json"
    log_filename = "tool.log"
    logger_name = "test-config-check"

    def initial_state(self) -> dict:
        return {"phase": "waiting", "busy": False, "message": "",
                "last_result": None, "history": []}


@pytest.fixture(autouse=True)
def no_central(monkeypatch):
    monkeypatch.delenv("BENCH_CENTRAL_URL", raising=False)


def make_tool(tmp_path, cfg=None, example=None):
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    if cfg is not None:
        (tmp_path / "config/tool.config.json").write_text(
            json.dumps(cfg), encoding="utf-8")
    if example is not None:
        (tmp_path / "config/tool.config.example.json").write_text(
            json.dumps(example), encoding="utf-8")
    return Tool(tmp_path)


def test_missing_config_file_warns_and_hashes_none(tmp_path):
    tool = make_tool(tmp_path)
    assert tool.config_hash == "none"
    assert tool.config_warnings == [
        "config/tool.config.json not found — copy the example config "
        "and fill in this station's values."]


def test_placeholders_and_missing_fields_surface_in_state(tmp_path):
    example = dict(PLACEHOLDER_CFG, name_prefix="rut-")
    tool = make_tool(tmp_path, cfg=PLACEHOLDER_CFG, example=example)
    assert len(tool.config_hash) == 12
    joined = "\n".join(tool.config_warnings)
    assert "tailscale.api_key" in joined       # placeholder key
    assert "tailscale.tailnet" in joined       # placeholder tailnet
    assert "name_prefix is missing" in joined  # dropped vs the example
    state = tool.public_state()
    assert state["config_hash"] == tool.config_hash
    assert state["config_warnings"] == tool.config_warnings


def test_clean_config_has_no_warnings(tmp_path):
    tool = make_tool(tmp_path, cfg=GOOD_CFG, example=GOOD_CFG)
    assert tool.config_warnings == []


def test_reload_recomputes_hash_and_warnings(tmp_path):
    tool = make_tool(tmp_path, cfg=PLACEHOLDER_CFG, example=PLACEHOLDER_CFG)
    assert tool.config_warnings
    bad_hash = tool.config_hash
    (tmp_path / "config/tool.config.json").write_text(
        json.dumps(GOOD_CFG), encoding="utf-8")
    tool.reload()
    assert tool.config_warnings == []
    assert tool.config_hash != bad_hash


def test_run_stamp_carries_config_hash(tmp_path):
    tool = make_tool(tmp_path, cfg=GOOD_CFG)
    assert tool.run_stamp()["config_hash"] == tool.config_hash


def test_redacted_hash_ignores_secret_rotation(tmp_path):
    # The fingerprint is computed over the REDACTED config: rotating a secret
    # (same shape, different value) must not read as config drift.
    rotated = json.loads(json.dumps(GOOD_CFG))
    rotated["tailscale"]["api_key"] = "tskey-api-kFGiyz3-other"
    a = make_tool(tmp_path / "a", cfg=GOOD_CFG)
    b = make_tool(tmp_path / "b", cfg=rotated)
    assert a.config_hash == b.config_hash
