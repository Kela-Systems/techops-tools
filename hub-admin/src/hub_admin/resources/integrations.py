"""Integration CRUD — list existing, create new."""

from google.protobuf import struct_pb2
from google.protobuf.json_format import MessageToDict

import grpc
from kela.device.v1alpha1.device_pb2 import ListDevicesRequest
from kela.device.v1alpha1.device_pb2_grpc import DeviceServiceStub
from kela.integration.v1alpha1.integration_pb2 import CreateInternalIntegrationRequest
from kela.integration.v1alpha1.integration_pb2_grpc import IntegrationServiceStub

from hub_admin.models import Integration


class IntegrationResource:
    def __init__(self, hub_client, channel: grpc.Channel):
        self._client = hub_client
        self._int_stub = IntegrationServiceStub(channel)
        self._dev_stub = DeviceServiceStub(channel)

    def list(self) -> list[Integration]:
        response = self._client.list_integrations()
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

    def create(self, manifest_id: str, config: dict | None = None) -> str:
        req = CreateInternalIntegrationRequest(manifest_id=manifest_id)
        if config:
            s = struct_pb2.Struct()
            s.update(config)
            req.integration_config.CopyFrom(s)
        response = self._int_stub.CreateInternalIntegration(req)
        return response.integration_id
