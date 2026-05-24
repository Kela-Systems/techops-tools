"""Tests for hub_admin.config."""

import yaml

from hub_admin.config import HubConfig, load_config


def test_load_config_defaults(tmp_path):
    """Without a config file, defaults should be used."""
    cfg = load_config(config_path=tmp_path / "nonexistent.yaml")
    assert cfg.context == "kela-office-01"
    assert cfg.namespace == "kela"
    assert cfg.port == 8001


def test_load_config_from_yaml(tmp_path):
    path = tmp_path / "hub-admin.yaml"
    path.write_text(
        yaml.dump(
            {
                "defaults": {
                    "context": "my-cluster",
                    "namespace": "custom-ns",
                    "port": 9001,
                },
                "device_configs": ["/path/to/devices.json"],
            }
        )
    )

    cfg = load_config(config_path=path)
    assert cfg.context == "my-cluster"
    assert cfg.namespace == "custom-ns"
    assert cfg.port == 9001
    assert cfg.device_config_paths == ["/path/to/devices.json"]


def test_load_config_cli_override(tmp_path):
    """Explicit context arg should override the YAML file."""
    path = tmp_path / "hub-admin.yaml"
    path.write_text(yaml.dump({"defaults": {"context": "from-yaml"}}))

    cfg = load_config(context="from-cli", config_path=path)
    assert cfg.context == "from-cli"
