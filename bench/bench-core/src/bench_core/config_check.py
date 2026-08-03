#!/usr/bin/env python3
"""Startup config self-check + config fingerprint (TEC-356).

Per-station `*.config.json` files are hand-copied and otherwise invisible —
without this, a stale token or a placeholder value (e.g. `mint_per_device`
left pointing at `tskey-api-xxxxx`) only shows up as a failure mid-run, or
worse, a silent fallback. So at startup each bench base:

* runs `check_config()` — flags placeholder values, expired/expiring JWT
  tokens, and fields the committed example config has but the live one lost —
  and surfaces the warnings in the page banner and `/api/state`;
* computes `config_fingerprint()` over the REDACTED config and stamps it into
  `/api/state` and every run record, so config drift across stations is
  visible centrally once records are centralized.

Checks are generic (no per-tool schema): values inside a section whose
`enabled` flag is False are skipped — a placeholder in a feature that never
runs is not a problem.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

# A token that expires within this window is worth renewing before it strands
# a station mid-batch.
EXPIRY_SOON_DAYS = 14

# Placeholder shapes seen in the example configs and in hand-edited copies.
# Conservative on purpose: a false positive would nag every launch.
_PLACEHOLDER_PATTERNS = (
    (re.compile(r"x{5,}", re.IGNORECASE), "'xxxxx'"),
    (re.compile(r"example\.(com|org|net)", re.IGNORECASE), "'example.com'"),
    (re.compile(r"change[-_]?me", re.IGNORECASE), "'changeme'"),
    (re.compile(r"placeholder", re.IGNORECASE), "'placeholder'"),
    (re.compile(r"^<[^<>]+>$"), "'<angle-brackets>'"),
)


def config_fingerprint(cfg: dict) -> str:
    """Short stable hash of a config dict ('none' when empty). Pass the
    REDACTED config, so the fingerprint can travel in state feeds and central
    run records without carrying secret-derived material."""
    if not cfg:
        return "none"
    canonical = json.dumps(cfg, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _placeholder_kind(value: str) -> Optional[str]:
    for pattern, kind in _PLACEHOLDER_PATTERNS:
        if pattern.search(value):
            return kind
    return None


def _jwt_expiry(value: str) -> Optional[datetime]:
    """The `exp` claim of a JWT-shaped string, or None when the value isn't a
    JWT / carries no usable expiry. No signature check — we only want the
    date the issuer stamped into it (e.g. an RMS Personal Access Token)."""
    if not value.startswith("eyJ") or value.count(".") != 2:
        return None
    payload = value.split(".")[1]
    try:
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(decoded)
    except (ValueError, TypeError):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return None
    try:
        return datetime.fromtimestamp(exp, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _check_values(node, path: tuple, warnings: list[str], now: datetime) -> None:
    """Walk the live config flagging placeholder strings and expiring tokens.
    A dict with `enabled: False` is a feature that never runs — skip it."""
    if isinstance(node, dict):
        if node.get("enabled") is False:
            return
        for key, value in node.items():
            if isinstance(key, str) and key.startswith("_"):
                continue  # comment keys (stripped on load, present in examples)
            _check_values(value, path + (key,), warnings, now)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _check_values(value, path + (str(i),), warnings, now)
    elif isinstance(node, str) and node:
        label = ".".join(path)
        kind = _placeholder_kind(node)
        if kind:
            warnings.append(f"{label} looks like a placeholder ({kind}) — "
                            "fill in the real value.")
            return
        expiry = _jwt_expiry(node)
        if expiry is None:
            return
        if expiry <= now:
            warnings.append(f"{label}: token expired on {expiry:%Y-%m-%d} — renew it.")
        elif expiry - now <= timedelta(days=EXPIRY_SOON_DAYS):
            days = max((expiry - now).days, 0)
            warnings.append(f"{label}: token expires in {days} day(s) "
                            f"({expiry:%Y-%m-%d}) — renew it soon.")


def _check_missing(cfg: dict, example: dict, path: tuple,
                   warnings: list[str]) -> None:
    """Flag keys the example config has that the live one lost — a hand-copied
    config that drifted from the template. Sections disabled in the live
    config are skipped, same as the value checks."""
    if cfg.get("enabled") is False:
        return
    for key, example_value in example.items():
        if isinstance(key, str) and key.startswith("_"):
            continue
        if key not in cfg:
            warnings.append(f"{'.'.join(path + (key,))} is missing "
                            "(the example config has it).")
        elif isinstance(example_value, dict) and isinstance(cfg[key], dict):
            _check_missing(cfg[key], example_value, path + (key,), warnings)


def check_config(cfg: dict, example: Optional[dict] = None, *,
                 now: Optional[datetime] = None) -> list[str]:
    """Validate a loaded station config. Returns human-readable warnings:
    placeholder values, expired/expiring JWT tokens, and (when the tool's
    committed example config is given) required fields that went missing."""
    now = now or datetime.now(timezone.utc)
    warnings: list[str] = []
    _check_values(cfg, (), warnings, now)
    if example:
        _check_missing(cfg, example, (), warnings)
    return warnings
