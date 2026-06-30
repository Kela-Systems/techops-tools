"""Device creation, listing, and modification."""

from __future__ import annotations

import json
import os
from collections.abc import Callable

from google.protobuf import struct_pb2
from google.protobuf.json_format import MessageToDict

import grpc
from kela.device.v1alpha1.device_pb2 import (
    CreateDeviceRequest,
    ListDevicesRequest,
    UpdateDeviceRequest,
    UpdateDeviceSetupInfoRequest,
)
from kela.device.v1alpha1.device_pb2_grpc import DeviceServiceStub

from hub_admin.models import Device


def _apply_merge_patch(target: dict, patch: dict) -> dict:
    """Apply an RFC 7396 JSON Merge Patch to ``target``, returning a new dict.

    Top-level keys overwrite (nested objects are deep-merged), a ``None`` value
    deletes the key, and keys absent from the patch are preserved. Used to
    emulate ``UpdateDeviceSetupInfo`` client-side on older hubs that only
    implement the wholesale ``UpdateDevice``.
    """
    result = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict):
            result[key] = _apply_merge_patch(result.get(key, {}), value)
        else:
            result[key] = value
    return result


class DeviceResource:
    def __init__(self, channel: grpc.Channel):
        self._stub = DeviceServiceStub(channel)

    def create(self, integration_id: str, name: str, setup_info: dict) -> str:
        req = CreateDeviceRequest(integration_id=integration_id, name=name)
        if setup_info:
            s = struct_pb2.Struct()
            s.update(setup_info)
            req.setup_info.CopyFrom(s)
        response = self._stub.CreateDevice(req)
        return response.device_id

    def list(self, integration_id: str) -> list[Device]:
        """List the devices belonging to an integration."""
        response = self._stub.ListDevices(
            ListDevicesRequest(integration_id=integration_id)
        )
        return [
            Device(
                id=dev.id,
                name=dev.name,
                integration_id=dev.integration_id,
                setup_info=(
                    MessageToDict(dev.setup_info)
                    if dev.HasField("setup_info")
                    else {}
                ),
            )
            for dev in response.devices
        ]

    def update_setup_info(
        self, integration_id: str, device_id: str, patch: dict
    ) -> None:
        """Apply an RFC 7396 merge-patch to a device's setup_info.

        Top-level keys in ``patch`` overwrite the current value (nested objects
        are deep-merged); a key whose value is ``None`` is *deleted* from
        setup_info; keys absent from ``patch`` are preserved as-is. This wraps
        ``UpdateDeviceSetupInfo`` so partial edits never clobber background-written
        keys such as ``intrinsic_calibration`` (unlike the wholesale
        ``UpdateDevice``). ``struct_pb2.Struct`` maps Python ``None`` to
        ``NullValue``, which is exactly the delete signal the patch expects.

        Older hubs that predate ``UpdateDeviceSetupInfo`` (HUB-1520) return
        ``UNIMPLEMENTED``; in that case we transparently fall back to applying
        the patch over the device's current setup_info and sending a wholesale
        ``UpdateDevice``. Because the merge starts from the full stored
        setup_info, keys we don't touch (including ``intrinsic_calibration``)
        are still preserved.
        """
        s = struct_pb2.Struct()
        s.update(patch)
        req = UpdateDeviceSetupInfoRequest(
            integration_id=integration_id, device_id=device_id
        )
        req.setup_info_patch.CopyFrom(s)
        try:
            self._stub.UpdateDeviceSetupInfo(req)
        except grpc.RpcError as e:
            if e.code() != grpc.StatusCode.UNIMPLEMENTED:
                raise
            current = self._current_setup_info(integration_id, device_id)
            merged = _apply_merge_patch(current, patch)
            self._update_full_setup_info(integration_id, device_id, merged)

    def _current_setup_info(self, integration_id: str, device_id: str) -> dict:
        """Fetch a single device's current setup_info via ListDevices."""
        for dev in self.list(integration_id):
            if dev.id == device_id:
                return dev.setup_info
        raise KeyError(
            f"Device {device_id} not found in integration {integration_id}"
        )

    def _update_full_setup_info(
        self, integration_id: str, device_id: str, setup_info: dict
    ) -> None:
        """Wholesale-overwrite setup_info via UpdateDevice (older-hub fallback)."""
        req = UpdateDeviceRequest(
            integration_id=integration_id, device_id=device_id
        )
        s = struct_pb2.Struct()
        s.update(setup_info)
        req.setup_info.CopyFrom(s)
        self._stub.UpdateDevice(req)

    def rename(self, integration_id: str, device_id: str, name: str) -> None:
        """Rename a device, leaving its setup_info untouched.

        Only the ``name`` field is set on ``UpdateDeviceRequest``; setup_info is
        left unset so the server's field-mask semantics leave it unchanged.
        """
        self._stub.UpdateDevice(
            UpdateDeviceRequest(
                integration_id=integration_id, device_id=device_id, name=name
            )
        )

    def add_from_config(
        self,
        integration_id: str,
        section_key: str,
        config_path: str,
        allowed_keys: set[str] | None = None,
        on_drop: Callable[[str, list[str]], None] | None = None,
    ) -> list[tuple[str, str]]:
        """Add devices from a config file section. Returns list of (name, device_id).

        When `allowed_keys` is given (typically derived from the target
        integration's device_setup_info_schema via `allowed_keys_from_schema`),
        any setup_info field not in that set is dropped before creating the
        device — this lets a config exported from one site replay onto another
        whose manifest schema differs (it would otherwise reject unknown fields
        with INVALID_ARGUMENT). Dropped fields are reported via `on_drop`.
        """
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Device config not found at {config_path}")

        with open(config_path) as f:
            device_config = json.load(f)

        if section_key not in device_config:
            raise KeyError(f"Section '{section_key}' not found in config")

        return self.add_devices(
            integration_id,
            device_config[section_key],
            allowed_keys=allowed_keys,
            on_drop=on_drop,
        )

    def add_devices(
        self,
        integration_id: str,
        devices: dict,
        allowed_keys: set[str] | None = None,
        on_drop: Callable[[str, list[str]], None] | None = None,
    ) -> list[tuple[str, str]]:
        """Create devices from an in-memory config section dict.

        Same transform as `add_from_config` (``device_name`` -> gRPC name,
        ``location`` -> ``latitude``/``longitude``, schema-based key filtering)
        but sourced from a dict rather than a file — used by both the file-based
        flow and `ProfileResource.apply`. Returns list of (name, device_id).
        """
        results = []
        for key, entry in devices.items():
            entry = dict(entry)
            name = entry.pop("device_name", key)
            if "location" in entry:
                lat, lng = entry.pop("location").split(",")
                entry["latitude"] = float(lat.strip())
                entry["longitude"] = float(lng.strip())
            if allowed_keys is not None:
                dropped = sorted(k for k in entry if k not in allowed_keys)
                for k in dropped:
                    entry.pop(k)
                if dropped and on_drop:
                    on_drop(name, dropped)
            device_id = self.create(integration_id, name, entry)
            results.append((name, device_id))
        return results

    @staticmethod
    def allowed_keys_from_schema(schema: dict | None) -> set[str] | None:
        """Allowed setup_info keys from a device_setup_info_schema.

        Returns None when the schema is absent or permits additional
        properties (the JSON-schema default) — meaning no filtering should be
        applied. Only when `additionalProperties` is explicitly false do we
        restrict to the declared `properties`.
        """
        if not schema or schema.get("additionalProperties", True):
            return None
        return set((schema.get("properties") or {}).keys())

    @staticmethod
    def find_matching_section(config_path: str, manifest_name: str) -> str | None:
        """Case-insensitive substring match: manifest name <-> config section key."""
        if not os.path.exists(config_path):
            return None
        with open(config_path) as f:
            device_config = json.load(f)
        name_lower = manifest_name.lower()
        for section_key in device_config:
            if section_key.lower() in name_lower or name_lower in section_key.lower():
                return section_key
        return None

    @staticmethod
    def list_config_sections(config_path: str) -> list[str]:
        if not os.path.exists(config_path):
            return []
        with open(config_path) as f:
            return list(json.load(f).keys())
