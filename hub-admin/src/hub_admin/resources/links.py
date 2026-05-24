"""Entity link management — assets, links, available-link configuration."""

import grpc
from kela.entity.v1alpha1.entity_pb2 import ListAssetsRequest, ListLinkedEntitiesRequest
from kela.entity.v1alpha1.entity_pb2_grpc import EntityServiceStub
from kela.system.v1alpha1.system_pb2 import (
    GetSiteConfigRequest,
    SetSiteConfigRequest,
)
from kela.system.v1alpha1.system_pb2_grpc import SiteConfigServiceStub

from hub_admin.models import Asset, EntityLink


class LinkResource:
    def __init__(self, channel: grpc.Channel):
        self._entity_stub = EntityServiceStub(channel)
        self._site_stub = SiteConfigServiceStub(channel)

    def list_assets(self) -> list[Asset]:
        resp = self._entity_stub.ListAssets(ListAssetsRequest())
        return [Asset.from_proto(a) for a in resp.assets]

    def list_links(self, assets: list[Asset] | None = None) -> list[EntityLink]:
        resp = self._entity_stub.ListLinkedEntities(ListLinkedEntitiesRequest())
        id_to_name = {a.id: a.name for a in (assets or [])}
        return [
            EntityLink(
                source_id=link.entity_id,
                target_id=link.target_entity_id,
                source_name=id_to_name.get(link.entity_id, link.entity_id[:12]),
                target_name=id_to_name.get(
                    link.target_entity_id, link.target_entity_id[:12]
                ),
                link_type=(
                    f"sensor_control({link.link_type.sensor_control})"
                    if link.link_type.sensor_control
                    else ""
                ),
            )
            for link in resp.linked_entities
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
