"""Site profile export / apply — bundle integrations + site settings, replay onto a hub.

A *profile* captures everything needed to stand a site's integrations up on a
fresh hub: each integration's manifest, its ``integration_config``, and its
devices, plus the hub's ``SiteConfig`` (entity links etc.). Export from one
context, deploy to another:

    hub-admin profile export --context kela-cuas-06 -o cuas-06.profile.json
    hub-admin profile apply cuas-06.profile.json --context <dest>

On apply, manifests are resolved by *name* against the destination hub (falling
back to the stored manifest ID), so a profile stays portable across hubs whose
manifest IDs differ. Integration ``integration_config`` is replayed verbatim,
devices go through the same schema-filtering transform as ``devices add``, and
the ``SiteConfig`` fully replaces the destination's (requires a hub restart).
"""

from datetime import datetime, timezone

from google.protobuf import struct_pb2
from google.protobuf.json_format import MessageToDict, ParseDict

import grpc
from kela.device.v1alpha1.device_pb2 import ListDevicesRequest
from kela.device.v1alpha1.device_pb2_grpc import DeviceServiceStub
from kela.integration.v1alpha1.integration_pb2 import (
    CreateInternalIntegrationRequest,
    ListIntegrationsRequest,
)
from kela.integration.v1alpha1.integration_pb2_grpc import IntegrationServiceStub
from kela.system.v1alpha1.system_pb2 import (
    GetSiteConfigRequest,
    SetSiteConfigRequest,
)
from kela.system.v1alpha1.system_pb2_grpc import SiteConfigServiceStub

from hub_admin.models import (
    AppliedIntegration,
    ProfileApplyReport,
    ProfileIntegration,
    ProfileSummary,
)
from hub_admin.resources.devices import DeviceResource
from hub_admin.resources.integrations import _slug
from hub_admin.resources.manifests import ManifestResource

PROFILE_VERSION = 1


def _has_field(message, field: str) -> bool:
    """HasField that tolerates protos which don't declare the field at all."""
    try:
        return message.HasField(field)
    except ValueError:
        return False


def _device_to_entry(dev) -> dict:
    """Reverse a device's setup_info Struct back into a device_config entry.

    Mirror image of ``DeviceResource.add_devices``: gRPC ``name`` becomes
    ``device_name`` and the ``latitude``/``longitude`` pair recombines into the
    ``"lat,lng"`` ``location`` string.
    """
    setup = MessageToDict(dev.setup_info) if _has_field(dev, "setup_info") else {}
    entry: dict = {"device_name": dev.name}
    lat = setup.pop("latitude", None)
    lng = setup.pop("longitude", None)
    if lat is not None and lng is not None:
        entry["location"] = f"{lat},{lng}"
    entry.update(setup)
    return entry


class ProfileResource:
    def __init__(self, channel: grpc.Channel):
        self._int_stub = IntegrationServiceStub(channel)
        self._dev_stub = DeviceServiceStub(channel)
        self._site_stub = SiteConfigServiceStub(channel)
        self._manifests = ManifestResource(channel)
        self._devices = DeviceResource(channel)

    # ── export ───────────────────────────────────────────────────────────

    def export(
        self, source_context: str, include_site_config: bool = True
    ) -> tuple[dict, ProfileSummary]:
        """Capture every integration (+ config + devices) and the SiteConfig.

        Returns ``(bundle, summary)``. ``bundle`` is a self-contained,
        JSON-serialisable profile; ``summary`` describes it for reporting.
        """
        manifests = self._manifests.list()
        id_to_name = {m.manifest_id: m.name for m in manifests}

        response = self._int_stub.ListIntegrations(ListIntegrationsRequest())
        integrations_out: list[dict] = []
        summary_integrations: list[ProfileIntegration] = []

        for integration in response.integrations:
            manifest_id = (
                integration.manifest_id
                if _has_field(integration, "manifest_id")
                else None
            )
            manifest_name = id_to_name.get(manifest_id) if manifest_id else None

            integration_config = None
            if _has_field(integration, "integration_config"):
                integration_config = MessageToDict(integration.integration_config)

            try:
                devs = self._dev_stub.ListDevices(
                    ListDevicesRequest(integration_id=integration.id)
                ).devices
            except Exception:
                devs = []

            devices: dict = {}
            for i, dev in enumerate(devs, 1):
                entry = _device_to_entry(dev)
                key = _slug(dev.name, fallback=f"device_{i}")
                dk, n = key, 2
                while dk in devices:
                    dk = f"{key}_{n}"
                    n += 1
                devices[dk] = entry

            integrations_out.append(
                {
                    "name": integration.name,
                    "manifest_id": manifest_id,
                    "manifest_name": manifest_name,
                    "integration_config": integration_config,
                    "devices": devices,
                }
            )
            summary_integrations.append(
                ProfileIntegration(
                    name=integration.name,
                    manifest_id=manifest_id,
                    manifest_name=manifest_name,
                    has_config=integration_config is not None,
                    device_count=len(devs),
                )
            )

        site_config = None
        if include_site_config:
            try:
                resp = self._site_stub.GetSiteConfig(GetSiteConfigRequest())
                site_config = MessageToDict(resp.config)
            except grpc.RpcError as e:
                if e.code() != grpc.StatusCode.UNIMPLEMENTED:
                    raise
                site_config = None

        bundle = {
            "version": PROFILE_VERSION,
            "source_context": source_context,
            "exported_at": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "integrations": integrations_out,
            "site_config": site_config,
        }
        summary = ProfileSummary(
            source_context=source_context,
            integrations=summary_integrations,
            site_config_keys=sorted(site_config) if site_config else [],
        )
        return bundle, summary

    # ── apply ────────────────────────────────────────────────────────────

    def apply(
        self,
        bundle: dict,
        target_context: str,
        include_site_config: bool = True,
    ) -> ProfileApplyReport:
        """Deploy a profile bundle onto the connected hub.

        For each integration: resolve its manifest on the destination (by name,
        then by stored ID), create it with the saved ``integration_config``, and
        add its devices (filtering setup_info to the destination manifest's
        schema). Finally, replace the destination ``SiteConfig`` if requested.
        """
        report = ProfileApplyReport(target_context=target_context)

        dest_manifests = self._manifests.list()
        name_to_id: dict[str, str] = {}
        for m in dest_manifests:
            name_to_id.setdefault(m.name, m.manifest_id)

        for spec in bundle.get("integrations", []):
            name = spec.get("name") or "(unnamed)"
            stored_id = spec.get("manifest_id")
            manifest_name = spec.get("manifest_name")
            integration_config = spec.get("integration_config")
            devices = spec.get("devices") or {}

            manifest_id = stored_id
            matched_by_name = False
            if manifest_name and manifest_name in name_to_id:
                manifest_id = name_to_id[manifest_name]
                matched_by_name = True

            if not manifest_id:
                report.applied.append(
                    AppliedIntegration(
                        name=name,
                        manifest_name=manifest_name,
                        integration_id=None,
                        manifest_id=None,
                        matched_by_name=False,
                        error="no manifest on destination (set manifest_name or manifest_id)",
                    )
                )
                continue

            try:
                req = CreateInternalIntegrationRequest(manifest_id=manifest_id)
                if integration_config:
                    s = struct_pb2.Struct()
                    s.update(integration_config)
                    req.integration_config.CopyFrom(s)
                integration_id = self._int_stub.CreateInternalIntegration(
                    req
                ).integration_id
            except grpc.RpcError as e:
                report.applied.append(
                    AppliedIntegration(
                        name=name,
                        manifest_name=manifest_name,
                        integration_id=None,
                        manifest_id=manifest_id,
                        matched_by_name=matched_by_name,
                        error=f"create failed: {e.details() or e.code()}",
                    )
                )
                continue

            try:
                _int_schema, dev_schema = self._manifests.get_schemas(manifest_id)
            except Exception:
                dev_schema = None
            allowed = self._devices.allowed_keys_from_schema(dev_schema)

            dropped: dict[str, list[str]] = {}

            def _on_drop(dev_name: str, drp: list[str], _store=dropped):
                _store[dev_name] = drp

            created = self._devices.add_devices(
                integration_id, devices, allowed_keys=allowed, on_drop=_on_drop
            )

            report.applied.append(
                AppliedIntegration(
                    name=name,
                    manifest_name=manifest_name,
                    integration_id=integration_id,
                    manifest_id=manifest_id,
                    matched_by_name=matched_by_name,
                    devices=created,
                    dropped=dropped,
                )
            )

        site_config = bundle.get("site_config")
        if include_site_config and site_config:
            resp = self._site_stub.GetSiteConfig(GetSiteConfigRequest())
            config = resp.config
            config.Clear()
            ParseDict(site_config, config, ignore_unknown_fields=True)
            self._site_stub.SetSiteConfig(SetSiteConfigRequest(config=config))
            report.site_config_applied = True

        return report

    # ── offline inspection ───────────────────────────────────────────────

    @staticmethod
    def summarize(bundle: dict) -> ProfileSummary:
        """Build a ProfileSummary from a bundle dict without touching a hub."""
        integrations = [
            ProfileIntegration(
                name=spec.get("name") or "(unnamed)",
                manifest_id=spec.get("manifest_id"),
                manifest_name=spec.get("manifest_name"),
                has_config=bool(spec.get("integration_config")),
                device_count=len(spec.get("devices") or {}),
            )
            for spec in bundle.get("integrations", [])
        ]
        site_config = bundle.get("site_config") or {}
        return ProfileSummary(
            source_context=bundle.get("source_context", "unknown"),
            integrations=integrations,
            site_config_keys=sorted(site_config),
        )
