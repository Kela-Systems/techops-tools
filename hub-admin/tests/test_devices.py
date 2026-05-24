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
