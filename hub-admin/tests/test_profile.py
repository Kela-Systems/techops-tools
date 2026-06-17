"""Tests for hub_admin.resources.profile (export / apply / summarize)."""

from unittest.mock import MagicMock, patch

from hub_admin.models import Manifest
from hub_admin.resources.profile import ProfileResource


def _integration(id_, name, manifest_id="m-magos", config=None):
    ig = MagicMock()
    ig.id = id_
    ig.name = name
    ig.manifest_id = manifest_id
    ig.integration_config = config if config is not None else {}

    def has_field(field):
        if field == "manifest_id":
            return manifest_id is not None
        if field == "integration_config":
            return config is not None
        return False

    ig.HasField.side_effect = has_field
    return ig


def _device(name, setup_info):
    dev = MagicMock()
    dev.name = name
    dev.HasField.side_effect = lambda f: f == "setup_info"
    dev.setup_info = setup_info
    return dev


def _profile_patches():
    """Patch the gRPC stubs + sub-resources ProfileResource builds in __init__."""
    return (
        patch("hub_admin.resources.profile.IntegrationServiceStub"),
        patch("hub_admin.resources.profile.DeviceServiceStub"),
        patch("hub_admin.resources.profile.SiteConfigServiceStub"),
        patch("hub_admin.resources.profile.ManifestResource"),
        patch("hub_admin.resources.profile.DeviceResource"),
    )


def test_export_captures_config_devices_and_site_config(mock_channel):
    p_int, p_dev, p_site, p_man, p_devres = _profile_patches()
    with (
        p_int as mock_int,
        p_dev as mock_dev,
        p_site as mock_site,
        p_man as mock_man,
        p_devres,
        patch(
            "hub_admin.resources.profile.MessageToDict",
            side_effect=lambda s: dict(s),
        ),
    ):
        mock_man.return_value.list.return_value = [
            Manifest("m-magos", "magos", "1.0", "Magos radars"),
        ]
        mock_int.return_value.ListIntegrations.return_value.integrations = [
            _integration("int-1", "Magos Radars", "m-magos", config={"site": "06"}),
        ]
        mock_dev.return_value.ListDevices.return_value.devices = [
            _device(
                "Radar 1",
                {"IpAddress": "192.168.1.20", "latitude": 32.5, "longitude": 35.7},
            ),
        ]
        mock_site.return_value.GetSiteConfig.return_value.config = {
            "availableLinks": {"a": {"targetAssetIds": ["b"]}}
        }

        res = ProfileResource(mock_channel)
        bundle, summary = res.export("kela-cuas-06")

    assert bundle["source_context"] == "kela-cuas-06"
    ig = bundle["integrations"][0]
    assert ig["manifest_name"] == "magos"
    assert ig["integration_config"] == {"site": "06"}
    dev = ig["devices"]["radar_1"]
    assert dev["device_name"] == "Radar 1"
    assert dev["location"] == "32.5,35.7"
    assert dev["IpAddress"] == "192.168.1.20"
    assert "latitude" not in dev

    assert bundle["site_config"] == {"availableLinks": {"a": {"targetAssetIds": ["b"]}}}
    assert summary.integrations[0].has_config is True
    assert summary.integrations[0].device_count == 1
    assert summary.site_config_keys == ["availableLinks"]


def test_export_can_skip_site_config(mock_channel):
    p_int, p_dev, p_site, p_man, p_devres = _profile_patches()
    with (
        p_int as mock_int,
        p_dev as mock_dev,
        p_site as mock_site,
        p_man as mock_man,
        p_devres,
        patch(
            "hub_admin.resources.profile.MessageToDict",
            side_effect=lambda s: dict(s),
        ),
    ):
        mock_man.return_value.list.return_value = []
        mock_int.return_value.ListIntegrations.return_value.integrations = []
        mock_dev.return_value.ListDevices.return_value.devices = []

        res = ProfileResource(mock_channel)
        bundle, summary = res.export("kela-cuas-06", include_site_config=False)

    assert bundle["site_config"] is None
    mock_site.return_value.GetSiteConfig.assert_not_called()
    assert summary.site_config_keys == []


def test_apply_resolves_manifest_by_name_and_creates(mock_channel):
    bundle = {
        "version": 1,
        "source_context": "kela-cuas-06",
        "integrations": [
            {
                "name": "Magos Radars",
                "manifest_id": "m-magos-OLD",
                "manifest_name": "magos",
                "integration_config": {"site": "06"},
                "devices": {"radar_1": {"device_name": "Radar 1"}},
            }
        ],
        "site_config": {"availableLinks": {"a": {"targetAssetIds": ["b"]}}},
    }

    from kela.system.v1alpha1.system_pb2 import GetSiteConfigResponse

    p_int, p_dev, p_site, p_man, p_devres = _profile_patches()
    with (
        p_int as mock_int,
        p_dev,
        p_site as mock_site,
        p_man as mock_man,
        p_devres as mock_devres,
        patch("hub_admin.resources.profile.ParseDict") as mock_parse,
    ):
        # Real SiteConfig proto so SetSiteConfigRequest(config=...) accepts it.
        mock_site.return_value.GetSiteConfig.return_value = GetSiteConfigResponse()
        # Destination hub has the manifest under a *different* ID.
        mock_man.return_value.list.return_value = [
            Manifest("m-magos-NEW", "magos", "1.0", "Magos radars"),
        ]
        mock_man.return_value.get_schemas.return_value = (None, None)
        mock_int.return_value.CreateInternalIntegration.return_value.integration_id = (
            "int-new"
        )
        mock_devres.return_value.allowed_keys_from_schema.return_value = None
        mock_devres.return_value.add_devices.return_value = [("Radar 1", "dev-1")]

        res = ProfileResource(mock_channel)
        report = res.apply(bundle, "kela-cuas-06")

    applied = report.applied[0]
    assert applied.integration_id == "int-new"
    assert applied.manifest_id == "m-magos-NEW"
    assert applied.matched_by_name is True
    assert applied.devices == [("Radar 1", "dev-1")]
    assert applied.error is None

    # Manifest resolved by name => CreateInternalIntegration used the NEW id.
    req = mock_int.return_value.CreateInternalIntegration.call_args.args[0]
    assert req.manifest_id == "m-magos-NEW"

    # Site config replaced + persisted.
    mock_parse.assert_called_once()
    mock_site.return_value.SetSiteConfig.assert_called_once()
    assert report.site_config_applied is True


def test_apply_falls_back_to_stored_id_then_errors(mock_channel):
    bundle = {
        "integrations": [
            {  # unknown name -> falls back to stored id
                "name": "Legacy",
                "manifest_id": "m-stored",
                "manifest_name": "not-on-dest",
                "devices": {},
            },
            {  # no id and unknown name -> error, no create
                "name": "Orphan",
                "manifest_id": None,
                "manifest_name": "also-missing",
                "devices": {},
            },
        ],
    }

    p_int, p_dev, p_site, p_man, p_devres = _profile_patches()
    with (
        p_int as mock_int,
        p_dev,
        p_site,
        p_man as mock_man,
        p_devres as mock_devres,
    ):
        mock_man.return_value.list.return_value = []
        mock_man.return_value.get_schemas.return_value = (None, None)
        mock_int.return_value.CreateInternalIntegration.return_value.integration_id = (
            "int-legacy"
        )
        mock_devres.return_value.allowed_keys_from_schema.return_value = None
        mock_devres.return_value.add_devices.return_value = []

        res = ProfileResource(mock_channel)
        report = res.apply(bundle, "dest", include_site_config=False)

    legacy, orphan = report.applied
    assert legacy.integration_id == "int-legacy"
    assert legacy.matched_by_name is False
    assert legacy.manifest_id == "m-stored"

    assert orphan.integration_id is None
    assert orphan.error is not None
    # Only one create call (the orphan was skipped).
    assert mock_int.return_value.CreateInternalIntegration.call_count == 1


def test_summarize_offline():
    bundle = {
        "source_context": "kela-cuas-06",
        "integrations": [
            {
                "name": "Magos Radars",
                "manifest_name": "magos",
                "integration_config": {"x": 1},
                "devices": {"a": {}, "b": {}},
            }
        ],
        "site_config": {"availableLinks": {}},
    }
    summary = ProfileResource.summarize(bundle)
    assert summary.source_context == "kela-cuas-06"
    assert summary.integrations[0].device_count == 2
    assert summary.integrations[0].has_config is True
    assert summary.site_config_keys == ["availableLinks"]
