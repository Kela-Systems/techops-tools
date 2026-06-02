"""Entity link management — assets, links, available-link configuration.

Assets and asset-links moved from the (removed) `kela.entity.v1alpha1`
package to `kela.asset.v1alpha1` (`AssetService`). The site-config write
path (`SiteConfigService` / `SiteConfig.available_links`) is unchanged.
"""

import grpc
from kela.asset.v1alpha1.asset_pb2 import ListAssetsRequest
from kela.asset.v1alpha1.asset_service_pb2_grpc import AssetServiceStub
from kela.asset.v1alpha1.link_pb2 import ListLinkedAssetsRequest
from kela.system.v1alpha1.system_pb2 import (
    GetSiteConfigRequest,
    SetSiteConfigRequest,
)
from kela.system.v1alpha1.system_pb2_grpc import SiteConfigServiceStub

from hub_admin.models import Asset, EntityLink


class LinkResource:
    def __init__(self, channel: grpc.Channel):
        self._asset_stub = AssetServiceStub(channel)
        self._site_stub = SiteConfigServiceStub(channel)

    def list_assets(self) -> list[Asset]:
        resp = self._asset_stub.ListAssets(ListAssetsRequest())
        return [Asset.from_proto(a) for a in resp.assets]

    def list_links(self, assets: list[Asset] | None = None) -> list[EntityLink]:
        resp = self._asset_stub.ListLinkedAssets(ListLinkedAssetsRequest())
        id_to_name = {a.id: a.name for a in (assets or [])}
        return [
            EntityLink(
                source_id=link.asset_id,
                target_id=link.target_asset_id,
                source_name=id_to_name.get(link.asset_id, link.asset_id[:12]),
                target_name=id_to_name.get(
                    link.target_asset_id, link.target_asset_id[:12]
                ),
            )
            for link in resp.linked_assets
        ]

    def add_available_link(self, source_id: str, target_id: str) -> bool:
        """Add link pair to SiteConfig.available_links. Returns True if new."""
        resp = self._site_stub.GetSiteConfig(GetSiteConfigRequest())
        config = resp.config

        if source_id in config.available_links:
            if target_id in config.available_links[source_id].target_asset_ids:
                return False
            config.available_links[source_id].target_asset_ids.append(target_id)
        else:
            config.available_links[source_id].target_asset_ids.append(target_id)

        self._site_stub.SetSiteConfig(SetSiteConfigRequest(config=config))
        return True
