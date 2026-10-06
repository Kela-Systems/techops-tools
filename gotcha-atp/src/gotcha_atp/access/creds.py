"""Credentials from the laptop's local config — never from the repo.

    ~/.config/gotcha-atp/config.toml     (override: GOTCHA_ATP_CONFIG)

The kela password is fleet-wide, so Home pre-fills it from this file and the
engineer confirms once per run. Every password is held in memory only and is
registered with the run's `Redactor`, which scrubs it (and the encoded forms
the devices put on the wire) out of every record, log line, event and report.
See config.example.toml for the shape.
"""
from __future__ import annotations

import hashlib
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

CONFIG_PATH = Path(os.environ.get(
    "GOTCHA_ATP_CONFIG", Path.home() / ".config" / "gotcha-atp" / "config.toml"))

MASK = "••••"

# Device families with their own login. A family without a password of its own
# falls back to [devices].password (most of the fleet shares one).
DEVICE_FAMILIES = {
    "magos": "admin",       # AR-300 radars dashboard API
    "apu": "admin",         # MIC-711 APUs dashboard API (falls back to [devices.magos])
    "camera": "admin",      # Raythink REST
    "speaker": "admin",     # Provision-ISR CGI
    "teltonika": "root",    # RUTM08 / OTD500 / TSW202 over SSH
    "planet": "admin",      # IGS-4215 web + SSH CLI
}


@dataclass
class DeviceLogin:
    username: str
    password: str

    def __repr__(self) -> str:
        return f"DeviceLogin({self.username!r}, {MASK if self.password else ''!r})"


@dataclass
class Credentials:
    ssh_user: str = "kela"
    ssh_password: str = ""
    devices: dict[str, DeviceLogin] = field(default_factory=dict)
    engineer: str = ""
    bench_central_url: str = ""
    fleet_url: str = ""
    fleet_token: str = ""
    source: str = ""            # the file they came from, '' when none

    def device(self, family: str) -> DeviceLogin:
        return self.devices.get(family) or DeviceLogin(DEVICE_FAMILIES.get(family, "admin"), "")

    def secrets(self) -> list[str]:
        return [s for s in (self.ssh_password, self.fleet_token,
                            *(d.password for d in self.devices.values())) if s]

    def public(self) -> dict:
        """What the UI may see: which secrets are set, never their values."""
        return {
            "config_path": str(CONFIG_PATH),
            "config_found": bool(self.source),
            "ssh_user": self.ssh_user,
            "ssh_password_set": bool(self.ssh_password),
            "devices": {k: {"username": v.username, "password_set": bool(v.password)}
                        for k, v in self.devices.items()},
            "engineer": self.engineer,
            "bench_central_url": self.bench_central_url,
            "fleet_url": self.fleet_url,
            "fleet_token_set": bool(self.fleet_token),
        }

    def __repr__(self) -> str:
        return f"Credentials(source={self.source!r}, ssh_user={self.ssh_user!r})"


def load(path: Optional[Path] = None) -> Credentials:
    """Read the local config. A missing file is not an error — the engineer
    can still type the password on Home."""
    path = Path(path or CONFIG_PATH)
    if not path.is_file():
        return Credentials(devices=_devices({}))
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    ssh = data.get("ssh") or {}
    return Credentials(
        ssh_user=str(ssh.get("user") or "kela"),
        ssh_password=str(ssh.get("password") or ""),
        devices=_devices(data.get("devices") or {}),
        engineer=str(data.get("engineer") or ""),
        bench_central_url=str((data.get("bench_central") or {}).get("url") or
                              os.environ.get("BENCH_CENTRAL_URL", "")).rstrip("/"),
        fleet_url=str((data.get("fleet") or {}).get("url") or "").rstrip("/"),
        fleet_token=str((data.get("fleet") or {}).get("token") or ""),
        source=str(path),
    )


def _devices(section: dict[str, Any]) -> dict[str, DeviceLogin]:
    shared = str(section.get("password") or "")
    out = {}
    for family, default_user in DEVICE_FAMILIES.items():
        own = section.get(family) or {}
        out[family] = DeviceLogin(str(own.get("username") or default_user),
                                  str(own.get("password") or shared))
    # APUs historically shared the radar login; a unit whose APUs were given
    # their own password sets [devices.apu].
    apu = section.get("apu") or {}
    if not apu.get("password"):
        out["apu"] = DeviceLogin(str(apu.get("username") or out["magos"].username), out["magos"].password)
    return out


# Vendor factory defaults (Magos "password", Raythink "admin", speaker
# "123456", ...) are public, and masking them would mangle every message that
# happens to contain the word.
PUBLIC_DEFAULTS = frozenset({"password", "admin", "root", "123456", "12345678"})


class Redactor:
    """Scrubs secrets out of text and nested structures. Registers each secret
    together with the MD5-hex form the speaker login sends, so a captured
    request cannot leak it either."""

    def __init__(self, secrets: Optional[list[str]] = None) -> None:
        self._secrets: set[str] = set()
        for s in secrets or []:
            self.add(s)

    def add(self, secret: str) -> None:
        if secret and len(secret) >= 3 and secret.lower() not in PUBLIC_DEFAULTS:
            self._secrets.add(secret)
            self._secrets.add(hashlib.md5(secret.encode()).hexdigest())
            self._pattern = None

    _pattern: Optional[re.Pattern] = None

    def __call__(self, text: Any) -> Any:
        if not isinstance(text, str) or not self._secrets:
            return text
        if self._pattern is None:
            self._pattern = re.compile("|".join(
                re.escape(s) for s in sorted(self._secrets, key=len, reverse=True)))
        return self._pattern.sub(MASK, text)

    def scrub(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self(obj)
        if isinstance(obj, dict):
            return {self(k) if isinstance(k, str) else k: self.scrub(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.scrub(v) for v in obj]
        return obj
