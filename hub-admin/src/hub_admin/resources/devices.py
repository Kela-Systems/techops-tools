"""Device creation — single or bulk from config file."""

import json
import os

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
    ) -> list[tuple[str, str]]:
        """Add devices from a config file section. Returns list of (name, device_id)."""
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Device config not found at {config_path}")

        with open(config_path) as f:
            device_config = json.load(f)

        if section_key not in device_config:
            raise KeyError(f"Section '{section_key}' not found in config")

        results = []
        devices = device_config[section_key]
        for key, entry in devices.items():
            entry = dict(entry)
            name = entry.pop("device_name", key)
            if "location" in entry:
                lat, lng = entry.pop("location").split(",")
                entry["latitude"] = float(lat.strip())
                entry["longitude"] = float(lng.strip())
            device_id = self.create(integration_id, name, entry)
            results.append((name, device_id))
        return results

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
