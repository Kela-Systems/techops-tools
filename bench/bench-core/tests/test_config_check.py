"""Tests for the startup config self-check + fingerprint
(bench_core.config_check, TEC-356)."""
import base64
import json
from datetime import datetime, timedelta, timezone

from bench_core.config_check import check_config, config_fingerprint

NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)


def jwt_with_exp(exp_dt):
    """An unsigned JWT-shaped token carrying only an `exp` claim."""
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(exp_dt.timestamp())}).encode()).rstrip(b"=")
    return "eyJhbGciOiJIUzI1NiJ9." + payload.decode() + ".sig"


# ── placeholder detection ─────────────────────────────────────────────────────

def test_placeholder_values_flagged_with_path():
    cfg = {"tailscale": {"enabled": True, "mint_per_device": True,
                         "api_key": "tskey-api-xxxxx", "tailnet": "example.com"}}
    warnings = check_config(cfg, now=NOW)
    assert len(warnings) == 2
    assert any(w.startswith("tailscale.api_key ") for w in warnings)
    assert any(w.startswith("tailscale.tailnet ") for w in warnings)


def test_placeholder_variants():
    cfg = {"a": "CHANGEME", "b": "change-me", "c": "<paste token here>",
           "d": "my-placeholder-value"}
    warnings = check_config(cfg, now=NOW)
    assert len(warnings) == 4


def test_placeholders_in_lists_flagged():
    cfg = {"tags": ["tag:rutm", "tag:xxxxx"]}
    warnings = check_config(cfg, now=NOW)
    assert warnings == [
        "tags.1 looks like a placeholder ('xxxxx') — fill in the real value."]


def test_disabled_sections_are_skipped():
    cfg = {"tailscale": {"enabled": False, "api_key": "tskey-api-xxxxx"},
           "esim": {"enabled": False, "code": "example.com"}}
    assert check_config(cfg, now=NOW) == []


def test_real_looking_config_passes():
    cfg = {"host": "192.168.1.1", "username": "admin", "insecure": True,
           "new_password": "test-shared-pw", "timezone": "Asia/Jerusalem",
           "name_prefix": "rut-", "lan_ip": "192.168.88.1",
           "firmware": {"mode": "fota",
                        "bin_path": "./firmware/RUTM08_latest-stable.bin",
                        "keep_settings": True, "expected_version": ""},
           "tailscale": {"enabled": True, "auth_key": "tskey-auth-kFGiyz3CNTRL-abc",
                         "mint_per_device": False, "tags": ["tag:rutm"]}}
    assert check_config(cfg, now=NOW) == []


def test_comment_keys_ignored():
    cfg = {"_comment": "fill in xxxxx below", "host": "192.168.1.1"}
    assert check_config(cfg, now=NOW) == []


# ── token expiry ──────────────────────────────────────────────────────────────

def test_expired_jwt_flagged():
    cfg = {"rms": {"enabled": True,
                   "api_token": jwt_with_exp(NOW - timedelta(days=3))}}
    warnings = check_config(cfg, now=NOW)
    assert len(warnings) == 1
    assert warnings[0].startswith("rms.api_token: token expired on 2026-07-31")


def test_jwt_expiring_soon_flagged():
    cfg = {"rms": {"api_token": jwt_with_exp(NOW + timedelta(days=5))}}
    warnings = check_config(cfg, now=NOW)
    assert len(warnings) == 1
    assert "expires in 5 day(s)" in warnings[0]


def test_jwt_with_far_expiry_passes():
    cfg = {"rms": {"api_token": jwt_with_exp(NOW + timedelta(days=90))}}
    assert check_config(cfg, now=NOW) == []


def test_non_jwt_strings_are_not_expiry_checked():
    # Opaque tokens (no `exp` readable) and ordinary strings must not warn.
    cfg = {"auth_key": "tskey-auth-kFGiyz3CNTRL-abc", "note": "eyJust a string"}
    assert check_config(cfg, now=NOW) == []


# ── missing fields vs the example template ────────────────────────────────────

def test_missing_fields_vs_example_flagged():
    example = {"_comment": "template", "host": "192.168.1.1",
               "rms": {"enabled": True, "api_token": "", "company_id": ""}}
    cfg = {"host": "192.168.1.1", "rms": {"enabled": True, "api_token": ""}}
    warnings = check_config(cfg, example, now=NOW)
    assert warnings == ["rms.company_id is missing (the example config has it)."]


def test_missing_check_skips_disabled_sections():
    example = {"rms": {"enabled": True, "api_token": ""}}
    cfg = {"rms": {"enabled": False}}
    assert check_config(cfg, example, now=NOW) == []


def test_no_example_means_no_missing_check():
    assert check_config({}, None, now=NOW) == []


# ── config fingerprint ────────────────────────────────────────────────────────

def test_fingerprint_stable_across_key_order():
    a = {"host": "192.168.1.1", "rms": {"enabled": True, "company_id": "7"}}
    b = {"rms": {"company_id": "7", "enabled": True}, "host": "192.168.1.1"}
    assert config_fingerprint(a) == config_fingerprint(b)
    assert len(config_fingerprint(a)) == 12


def test_fingerprint_changes_when_a_value_changes():
    a = {"host": "192.168.1.1"}
    b = {"host": "192.168.2.1"}
    assert config_fingerprint(a) != config_fingerprint(b)


def test_fingerprint_of_empty_config_is_none():
    assert config_fingerprint({}) == "none"
