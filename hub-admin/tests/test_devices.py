"""Tests for hub_admin.resources.devices."""

import json
import os
import tempfile

from hub_admin.resources.devices import DeviceResource


def test_find_matching_section_case_insensitive(tmp_path):
    config = {"OnvifCams": {"cam1": {"device_name": "test"}}}
    path = tmp_path / "devices.json"
    path.write_text(json.dumps(config))

    assert DeviceResource.find_matching_section(str(path), "onvifcams") == "OnvifCams"
    assert DeviceResource.find_matching_section(str(path), "ONVIFCAMS") == "OnvifCams"
    assert DeviceResource.find_matching_section(str(path), "magos") is None


def test_list_config_sections(tmp_path):
    config = {"cameras": {}, "radars": {}}
    path = tmp_path / "devices.json"
    path.write_text(json.dumps(config))

    sections = DeviceResource.list_config_sections(str(path))
    assert sections == ["cameras", "radars"]


def test_list_config_sections_missing_file():
    assert DeviceResource.list_config_sections("/nonexistent/path.json") == []


def test_allowed_keys_from_schema():
    # additionalProperties:false -> restrict to declared properties
    schema = {
        "properties": {"host": {}, "latitude": {}, "longitude": {}},
        "additionalProperties": False,
    }
    assert DeviceResource.allowed_keys_from_schema(schema) == {
        "host",
        "latitude",
        "longitude",
    }
    # permissive / absent schema -> no filtering
    assert DeviceResource.allowed_keys_from_schema({"properties": {"a": {}}}) is None
    assert DeviceResource.allowed_keys_from_schema(None) is None


def test_add_from_config_drops_disallowed_keys(tmp_path):
    """Fields not in the target schema are dropped (and reported) before create."""
    config = {
        "meduza": {
            "dev1": {
                "device_name": "meduza-1",
                "location": "31.0,34.0",
                "host": "10.0.0.5",
                "mediation_bda_release_on_timer": False,
            }
        }
    }
    path = tmp_path / "device_config.json"
    path.write_text(json.dumps(config))

    captured: list[tuple[str, dict]] = []
    drops: list[tuple[str, list[str]]] = []

    class _Capturing(DeviceResource):
        def __init__(self):
            pass

        def create(self, integration_id, name, setup_info):
            captured.append((name, setup_info))
            return "dev-1"

    allowed = {"host", "latitude", "longitude"}
    _Capturing().add_from_config(
        "int-1", "meduza", str(path),
        allowed_keys=allowed,
        on_drop=lambda n, d: drops.append((n, d)),
    )

    name, setup = captured[0]
    assert name == "meduza-1"
    assert "mediation_bda_release_on_timer" not in setup
    assert setup["host"] == "10.0.0.5"
    assert setup["latitude"] == 31.0 and setup["longitude"] == 34.0
    assert drops == [("meduza-1", ["mediation_bda_release_on_timer"])]
