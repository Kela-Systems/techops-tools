"""Tests for hub_admin.resources.links (migrated to kela.asset)."""

from unittest.mock import MagicMock, patch

from hub_admin.resources.links import LinkResource


def test_add_available_link_new(mock_channel):
    """A genuinely new link should be persisted and return True."""
    from kela.system.v1alpha1.system_pb2 import GetSiteConfigResponse

    with (
        patch("hub_admin.resources.links.AssetServiceStub"),
        patch("hub_admin.resources.links.SiteConfigServiceStub") as mock_site,
    ):
        mock_site.return_value.GetSiteConfig.return_value = GetSiteConfigResponse()

        res = LinkResource(mock_channel)
        result = res.add_available_link("source-uuid", "target-uuid")

    assert result is True
    mock_site.return_value.SetSiteConfig.assert_called_once()


def test_add_available_link_duplicate(mock_channel):
    """A duplicate link should not trigger SetSiteConfig."""
    from kela.system.v1alpha1.system_pb2 import GetSiteConfigResponse

    resp = GetSiteConfigResponse()
    resp.config.available_links["source-uuid"].target_asset_ids.append("target-uuid")

    with (
        patch("hub_admin.resources.links.AssetServiceStub"),
        patch("hub_admin.resources.links.SiteConfigServiceStub") as mock_site,
    ):
        mock_site.return_value.GetSiteConfig.return_value = resp

        res = LinkResource(mock_channel)
        result = res.add_available_link("source-uuid", "target-uuid")

    assert result is False
    mock_site.return_value.SetSiteConfig.assert_not_called()


def test_list_assets(mock_channel):
    """list_assets should convert proto objects to Asset dataclasses."""
    mock_asset = MagicMock()
    mock_asset.id = "asset-1"
    mock_asset.name = "Camera 1"
    mock_asset.asset_type = 1
    mock_asset.sensors = []

    with (
        patch("hub_admin.resources.links.AssetServiceStub") as mock_asset_svc,
        patch("hub_admin.resources.links.SiteConfigServiceStub"),
    ):
        mock_asset_svc.return_value.ListAssets.return_value.assets = [mock_asset]

        res = LinkResource(mock_channel)
        assets = res.list_assets()

    assert len(assets) == 1
    assert assets[0].id == "asset-1"
    assert assets[0].name == "Camera 1"
    assert assets[0].asset_type == "camera"


def test_list_links_resolves_names(mock_channel):
    """list_links should map asset IDs to asset names when available."""
    from hub_admin.models import Asset

    mock_link = MagicMock()
    mock_link.asset_id = "aaa"
    mock_link.target_asset_id = "bbb"

    assets = [
        Asset(id="aaa", name="Radar", asset_type="radar"),
        Asset(id="bbb", name="Camera", asset_type="camera"),
    ]

    with (
        patch("hub_admin.resources.links.AssetServiceStub") as mock_asset_svc,
        patch("hub_admin.resources.links.SiteConfigServiceStub"),
    ):
        mock_asset_svc.return_value.ListLinkedAssets.return_value.linked_assets = [
            mock_link
        ]

        res = LinkResource(mock_channel)
        links = res.list_links(assets)

    assert len(links) == 1
    assert links[0].source_name == "Radar"
    assert links[0].target_name == "Camera"
