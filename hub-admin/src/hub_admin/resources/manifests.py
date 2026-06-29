"""Manifest queries — list, get schemas, resolve names."""

from __future__ import annotations

from google.protobuf.json_format import MessageToDict

import grpc
from kela.manifest.v1alpha1.manifest_pb2 import (
    GetManifestRequest,
    ListManifestsRequest,
)
from kela.manifest.v1alpha1.manifest_pb2_grpc import ManifestServiceStub

from hub_admin.models import Manifest


class ManifestResource:
    def __init__(self, channel: grpc.Channel):
        self._stub = ManifestServiceStub(channel)

    def list(self) -> list[Manifest]:
        response = self._stub.ListManifests(ListManifestsRequest())
        return [
            Manifest(
                manifest_id=sm.manifest_id,
                name=sm.manifest.name,
                version=sm.manifest.version,
                description=sm.manifest.description,
            )
            for sm in response.manifests
        ]

    def get_schemas(self, manifest_id: str) -> tuple[dict | None, dict | None]:
        """Return (integration_config_schema, device_setup_info_schema)."""
        response = self._stub.GetManifest(GetManifestRequest(manifest_id=manifest_id))
        manifest = response.manifest.manifest
        int_schema = (
            MessageToDict(manifest.integration_config_schema)
            if manifest.HasField("integration_config_schema")
            else None
        )
        dev_schema = (
            MessageToDict(manifest.device_setup_info_schema)
            if manifest.HasField("device_setup_info_schema")
            else None
        )
        return int_schema, dev_schema

    def resolve_name(
        self, manifest_id: str | None, cached: list[Manifest] | None = None
    ) -> str:
        """Best-effort manifest name lookup (cached list first, then RPC)."""
        if not manifest_id:
            return "unknown"
        if cached:
            for m in cached:
                if m.manifest_id == manifest_id:
                    return m.name
        try:
            resp = self._stub.GetManifest(GetManifestRequest(manifest_id=manifest_id))
            return resp.manifest.manifest.name
        except Exception:
            return manifest_id
