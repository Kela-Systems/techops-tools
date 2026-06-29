"""Integration CRUD — list existing, create new, export to device_config."""

from __future__ import annotations

import re

from google.protobuf import struct_pb2
from google.protobuf.json_format import MessageToDict

import grpc
from kela.device.v1alpha1.device_pb2 import ListDevicesRequest
from kela.device.v1alpha1.device_pb2_grpc import DeviceServiceStub
from kela.integration.v1alpha1.integration_pb2 import (
    CreateInternalIntegrationRequest,
    ListIntegrationsRequest,
)
from kela.integration.v1alpha1.integration_pb2_grpc import IntegrationServiceStub

from hub_admin.models import ExportedIntegration, Integration


def _slug(value: str, fallback: str) -> str:
    """Turn an arbitrary name into a config-friendly key."""
    slug = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")
    return slug or fallback


class IntegrationResource:
    def __init__(self, channel: grpc.Channel):
        self._int_stub = IntegrationServiceStub(channel)
        self._dev_stub = DeviceServiceStub(channel)

    def list(self) -> list[Integration]:
        response = self._int_stub.ListIntegrations(ListIntegrationsRequest())
        integrations = []
        for integration in response.integrations:
            try:
                devs = self._dev_stub.ListDevices(
                    ListDevicesRequest(integration_id=integration.id)
                )
                device_count = len(devs.devices)
            except Exception:
                device_count = 0

            dev_schema = None
            if integration.HasField("device_setup_info_schema"):
                dev_schema = MessageToDict(integration.device_setup_info_schema)

            integrations.append(
                Integration(
                    id=integration.id,
                    name=integration.name,
                    manifest_id=(
                        integration.manifest_id
                        if integration.HasField("manifest_id")
                        else None
                    ),
                    device_count=device_count,
                    device_setup_info_schema=dev_schema,
                )
            )
        return integrations

    def export(self) -> tuple[dict, list[ExportedIntegration]]:
        """Download every integration + its devices into device_config.json shape.

        This is the inverse of ``DeviceResource.add_from_config``: each device's
        ``setup_info`` Struct is flattened back into a config entry, the device's
        gRPC ``name`` becomes ``device_name``, and the ``latitude``/``longitude``
        pair is recombined into the ``"lat,lng"`` ``location`` string. The result
        can be fed straight back into ``hub-admin devices add -c <file> -s <section>``.

        Returns ``(config, summary)`` where ``config`` is the device_config dict
        (only device sections, so it stays a drop-in device_config.json) and
        ``summary`` describes each integration for human-facing reporting.
        """
        response = self._int_stub.ListIntegrations(ListIntegrationsRequest())
        config: dict = {}
        summary: list[ExportedIntegration] = []
        used_sections: set[str] = set()

        for integration in response.integrations:
            try:
                devs = self._dev_stub.ListDevices(
                    ListDevicesRequest(integration_id=integration.id)
                ).devices
            except Exception:
                devs = []

            section = _slug(integration.name, fallback=f"integration_{integration.id[:8]}")
            base, n = section, 2
            while section in used_sections:
                section = f"{base}_{n}"
                n += 1
            used_sections.add(section)

            entries: dict = {}
            for i, dev in enumerate(devs, 1):
                setup = (
                    MessageToDict(dev.setup_info) if dev.HasField("setup_info") else {}
                )
                entry: dict = {"device_name": dev.name}
                lat = setup.pop("latitude", None)
                lng = setup.pop("longitude", None)
                if lat is not None and lng is not None:
                    entry["location"] = f"{lat},{lng}"
                entry.update(setup)

                key = _slug(dev.name, fallback=f"device_{i}")
                dk, m = key, 2
                while dk in entries:
                    dk = f"{key}_{m}"
                    m += 1
                entries[dk] = entry

            if entries:
                config[section] = entries

            summary.append(
                ExportedIntegration(
                    id=integration.id,
                    name=integration.name,
                    manifest_id=(
                        integration.manifest_id
                        if integration.HasField("manifest_id")
                        else None
                    ),
                    section=section if entries else None,
                    device_count=len(devs),
                )
            )

        return config, summary

    def create(self, manifest_id: str, config: dict | None = None) -> str:
        req = CreateInternalIntegrationRequest(manifest_id=manifest_id)
        if config:
            s = struct_pb2.Struct()
            s.update(config)
            req.integration_config.CopyFrom(s)
        response = self._int_stub.CreateInternalIntegration(req)
        return response.integration_id
