"""Device creation — single or bulk from config file."""

import json
import os
from collections.abc import Callable

from google.protobuf import struct_pb2

import grpc
from kela.device.v1alpha1.device_pb2 import CreateDeviceRequest
from kela.device.v1alpha1.device_pb2_grpc import DeviceServiceStub


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
