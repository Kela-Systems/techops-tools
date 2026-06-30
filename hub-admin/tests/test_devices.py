"""Tests for hub_admin.resources.devices."""

import json
from unittest.mock import MagicMock

from google.protobuf.json_format import MessageToDict

from kela.device.v1alpha1.device_pb2 import (
    Device as DeviceProto,
    ListDevicesResponse,
)

import grpc

from hub_admin.resources.devices import DeviceResource, _apply_merge_patch


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


def _resource_with_mock_stub():
    res = DeviceResource(MagicMock())
    res._stub = MagicMock()
    return res


def test_list_maps_devices_into_models():
    res = _resource_with_mock_stub()
    dev = DeviceProto(id="dev-1", name="Radar 1", integration_id="int-1")
    dev.setup_info.update({"host": "10.0.0.5", "latitude": 31.0})
    res._stub.ListDevices.return_value = ListDevicesResponse(devices=[dev])

    devices = res.list("int-1")

    assert res._stub.ListDevices.call_args.args[0].integration_id == "int-1"
    assert len(devices) == 1
    assert devices[0].id == "dev-1"
    assert devices[0].name == "Radar 1"
    assert devices[0].integration_id == "int-1"
    assert devices[0].setup_info == {"host": "10.0.0.5", "latitude": 31.0}


def test_update_setup_info_builds_merge_patch():
    res = _resource_with_mock_stub()

    res.update_setup_info(
        "int-1", "dev-1", {"host": "192.168.1.50", "stale_key": None}
    )

    req = res._stub.UpdateDeviceSetupInfo.call_args.args[0]
    assert req.integration_id == "int-1"
    assert req.device_id == "dev-1"
    # None survives as a JSON null so the server deletes the key (RFC 7396).
    assert MessageToDict(req.setup_info_patch) == {
        "host": "192.168.1.50",
        "stale_key": None,
    }


def test_apply_merge_patch_overwrite_delete_and_deep_merge():
    base = {"host": "1.1.1.1", "pose": {"pan": 1, "tilt": 2}, "stale": "x"}
    patch = {"host": "2.2.2.2", "pose": {"tilt": 9}, "stale": None}
    assert _apply_merge_patch(base, patch) == {
        "host": "2.2.2.2",
        "pose": {"pan": 1, "tilt": 9},
    }
    # original is not mutated
    assert base["host"] == "1.1.1.1" and base["stale"] == "x"


class _UnimplementedError(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNIMPLEMENTED


def test_update_setup_info_falls_back_to_wholesale_on_unimplemented():
    res = _resource_with_mock_stub()
    res._stub.UpdateDeviceSetupInfo.side_effect = _UnimplementedError()

    dev = DeviceProto(id="dev-1", name="Radar 1", integration_id="int-1")
    dev.setup_info.update({"host": "1.1.1.1", "keep": "yes"})
    res._stub.ListDevices.return_value = ListDevicesResponse(devices=[dev])

    res.update_setup_info("int-1", "dev-1", {"host": "2.2.2.2"})

    # Fell back to UpdateDevice with the full, merged setup_info.
    req = res._stub.UpdateDevice.call_args.args[0]
    assert req.integration_id == "int-1"
    assert req.device_id == "dev-1"
    assert MessageToDict(req.setup_info) == {"host": "2.2.2.2", "keep": "yes"}


def test_update_setup_info_reraises_non_unimplemented():
    class _OtherError(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.INTERNAL

    res = _resource_with_mock_stub()
    res._stub.UpdateDeviceSetupInfo.side_effect = _OtherError()

    try:
        res.update_setup_info("int-1", "dev-1", {"host": "2.2.2.2"})
    except grpc.RpcError:
        pass
    else:
        raise AssertionError("expected the non-UNIMPLEMENTED error to propagate")
    res._stub.UpdateDevice.assert_not_called()


def test_rename_only_sets_name():
    res = _resource_with_mock_stub()

    res.rename("int-1", "dev-1", "New Name")

    req = res._stub.UpdateDevice.call_args.args[0]
    assert req.integration_id == "int-1"
    assert req.device_id == "dev-1"
    assert req.name == "New Name"
    # setup_info left unset so the server leaves it untouched.
    assert not req.HasField("setup_info")
