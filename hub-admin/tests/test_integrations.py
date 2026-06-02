"""Tests for hub_admin.resources.integrations (export / reverse transform)."""

import json
from unittest.mock import MagicMock, patch

from hub_admin.resources.devices import DeviceResource
from hub_admin.resources.integrations import IntegrationResource, _slug


def _device(name, setup_info):
    dev = MagicMock()
    dev.name = name
    dev.HasField.return_value = True
    dev.setup_info = setup_info
    return dev


def _integration(id_, name, manifest_id="manifest-1"):
    ig = MagicMock()
    ig.id = id_
    ig.name = name
    ig.manifest_id = manifest_id
    ig.HasField.return_value = manifest_id is not None
    return ig


def test_slug():
    assert _slug("ONVIF Cameras", "x") == "onvif_cameras"
    assert _slug("מצלמה צפונית", "fallback") == "fallback"  # non-ascii -> fallback
    assert _slug("  Radar-1000!! ", "x") == "radar_1000"


def test_export_reverses_setup_info(mock_channel):
    with (
        patch("hub_admin.resources.integrations.IntegrationServiceStub") as mock_int,
        patch("hub_admin.resources.integrations.DeviceServiceStub") as mock_dev,
        patch(
            "hub_admin.resources.integrations.MessageToDict",
            side_effect=lambda s: dict(s),
        ),
    ):
        mock_int.return_value.ListIntegrations.return_value.integrations = [
            _integration("int-onvif", "ONVIF Cameras"),
        ]
        mock_dev.return_value.ListDevices.return_value.devices = [
            _device(
                "Camera North",
                {
                    "host": "192.168.1.10",
                    "latitude": 32.5,
                    "longitude": 35.7,
                    "username": "admin",
                    "device_port": 80,
                },
            ),
        ]
        res = IntegrationResource(mock_channel)
        config, summary = res.export()

    assert "onvif_cameras" in config
    cam = config["onvif_cameras"]["camera_north"]
    assert cam["device_name"] == "Camera North"
    assert cam["location"] == "32.5,35.7"
    assert cam["host"] == "192.168.1.10"
    assert "latitude" not in cam and "longitude" not in cam

    assert summary[0].section == "onvif_cameras"
    assert summary[0].device_count == 1
    assert summary[0].manifest_id == "manifest-1"


def test_export_handles_no_devices(mock_channel):
    with (
        patch("hub_admin.resources.integrations.IntegrationServiceStub") as mock_int,
        patch("hub_admin.resources.integrations.DeviceServiceStub") as mock_dev,
        patch(
            "hub_admin.resources.integrations.MessageToDict",
            side_effect=lambda s: dict(s),
        ),
    ):
        mock_int.return_value.ListIntegrations.return_value.integrations = [
            _integration("int-empty", "Empty Integration"),
        ]
        mock_dev.return_value.ListDevices.return_value.devices = []
        res = IntegrationResource(mock_channel)
        config, summary = res.export()

    assert config == {}
    assert summary[0].section is None
    assert summary[0].device_count == 0


def test_export_round_trips_add_from_config(mock_channel, tmp_path):
    """export() should reconstruct the entries that `devices add` consumed."""
    original = {
        "onvifcams": {
            "onvifcam_1": {
                "device_name": "Camera North",
                "host": "192.168.1.10",
                "location": "32.5,35.7",
                "altitude": 304,
                "username": "admin",
            },
        }
    }
    cfg_path = tmp_path / "device_config.json"
    cfg_path.write_text(json.dumps(original))

    # 1) Feed the config through `devices add`, capturing what would be created.
    created: list[tuple[str, dict]] = []

    class _CapturingDevice(DeviceResource):
        def __init__(self):  # skip gRPC stub setup
            pass

        def create(self, integration_id, name, setup_info):
            created.append((name, setup_info))
            return f"dev-{len(created)}"

    _CapturingDevice().add_from_config("int-onvif", "onvifcams", str(cfg_path))
    assert created[0][0] == "Camera North"
    assert created[0][1]["latitude"] == 32.5 and created[0][1]["longitude"] == 35.7

    # 2) Now export from a hub that returns exactly those created devices.
    with (
        patch("hub_admin.resources.integrations.IntegrationServiceStub") as mock_int,
        patch("hub_admin.resources.integrations.DeviceServiceStub") as mock_dev,
        patch(
            "hub_admin.resources.integrations.MessageToDict",
            side_effect=lambda s: dict(s),
        ),
    ):
        mock_int.return_value.ListIntegrations.return_value.integrations = [
            _integration("int-onvif", "onvifcams"),
        ]
        mock_dev.return_value.ListDevices.return_value.devices = [
            _device(name, setup) for name, setup in created
        ]
        res = IntegrationResource(mock_channel)
        config, _ = res.export()

    # The recovered entry must equal the original entry (key names may differ).
    recovered = next(iter(config["onvifcams"].values()))
    assert recovered == original["onvifcams"]["onvifcam_1"]
