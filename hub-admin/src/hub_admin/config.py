"""Configuration loading — YAML file + CLI overrides."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path.home() / ".hub-admin.yaml"


@dataclass
class HubConfig:
    context: str = "kela-office-01"
    namespace: str = "kela"
    port: int = 8001
    device_config_paths: list[str] = field(default_factory=list)


def load_config(
    context: str | None = None,
    config_path: Path | None = None,
) -> HubConfig:
    """Load config from YAML file, override with explicit arguments."""
    cfg = HubConfig()

    path = config_path or DEFAULT_CONFIG_PATH
    if path.exists():
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        defaults = data.get("defaults", {})
        cfg.context = defaults.get("context", cfg.context)
        cfg.namespace = defaults.get("namespace", cfg.namespace)
        cfg.port = defaults.get("port", cfg.port)
        cfg.device_config_paths = data.get("device_configs", [])

    if context:
        cfg.context = context

    return cfg
