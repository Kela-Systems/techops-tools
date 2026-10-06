"""Read-only device clients — a login and GETs, nothing that writes.

Own code, same wire shapes as the bench tools (bench/magos-config-ui,
raythink-config-ui/raythink_rest.py, speaker-config-ui/speaker_client.py,
planet-config-ui/planet_configure.py, bench-core's Teltonika client). The only
non-GET requests are the logins; there is deliberately no method here that
could change a device.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from typing import Any, Callable, Optional
from urllib.parse import quote

import httpx
import paramiko
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .access.creds import DeviceLogin
from .access.exec import AccessError, DeviceSSH, ssh_failure


class DeviceError(RuntimeError):
    """A device answered but not usefully (login refused, unexpected reply)."""


def _get_json(r: httpx.Response, what: str) -> Any:
    try:
        return r.json()
    except ValueError:
        raise DeviceError(f"{what}: non-JSON reply (HTTP {r.status_code})")


def _http(fn: Callable[[], httpx.Response], what: str) -> httpx.Response:
    try:
        return fn()
    except httpx.HTTPError as e:
        raise DeviceError(f"{what}: {type(e).__name__}: {e}")


# ── Magos AR-300 radar / MIC-711 APU dashboard API ──────────────────────────

class Magos:
    """POST /dshb/v1/login (session cookie), then GETs.

    Field finding (rev3-dev): APUs can refuse the dashboard login while
    accepting the same credentials as HTTP Basic auth — which is how the kela
    driver reaches them. So a refused login falls back to Basic auth, and only
    fails if that is refused too."""

    def __init__(self, http: httpx.Client, ip: str, login: DeviceLogin) -> None:
        self.http, self.ip, self.creds = http, ip, login
        self.base = f"http://{ip}"
        self.auth_mode = ""

    def login(self) -> None:
        r = _http(lambda: self.http.post(f"{self.base}/dshb/v1/login", json={
            "username": self.creds.username, "password": self.creds.password}),
            f"{self.ip} login")
        if r.status_code == 200:
            self.auth_mode = "session"
            return
        self.http.auth = httpx.BasicAuth(self.creds.username, self.creds.password)
        probe = _http(lambda: self.http.get(f"{self.base}/dshb/v1/systemStatus"), f"{self.ip} login")
        if probe.status_code != 200:
            self.http.auth = None
            raise DeviceError(f"{self.ip} login refused (HTTP {r.status_code}; Basic auth HTTP {probe.status_code})")
        self.auth_mode = "basic"

    def basic_header(self) -> dict:
        import base64 as _b64
        tok = _b64.b64encode(f"{self.creds.username}:{self.creds.password}".encode()).decode()
        return {"Authorization": f"Basic {tok}"}

    def get(self, path: str) -> Any:
        r = _http(lambda: self.http.get(self.base + path), f"{self.ip} GET {path}")
        if r.status_code != 200:
            raise DeviceError(f"{self.ip} GET {path}: HTTP {r.status_code}")
        return _get_json(r, f"{self.ip} GET {path}")

    def system_status(self) -> dict:
        data = self.get("/dshb/v1/systemStatus")
        return data if isinstance(data, dict) else {}

    def apu_settings(self) -> dict:
        data = self.get("/apu/v1/settings")
        return data if isinstance(data, dict) else {}

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.http.cookies.items())


def magos_identity(status: dict) -> dict:
    return {
        "serial": str(status.get("productSerial") or "unknown"),
        "model": str(status.get("productModel") or "unknown"),
        "firmware": str(status.get("softwareVersion") or "unknown"),
        "mac": normalize_mac(status.get("macAddr")) or "unknown",
        "hostname": status.get("hostname"),
        "uptime": status.get("uptime"),
        "counters": {k: status.get(k) for k in
                     ("ethernetCarrierDown", "ethernetRXErrors", "ethernetTXErrors")},
    }


# ── Raythink PC464A1 (REST /v1 generation) ──────────────────────────────────

# The vendor's fixed AES key (also the IV), shipped in the camera's own web
# bundle — obfuscation, not a secret. See bench raythink_rest.py.
_RAYTHINK_KEY = b"YJWL202101010000"


def raythink_password(password: str) -> str:
    padder = padding.PKCS7(128).padder()
    data = padder.update(password.encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(_RAYTHINK_KEY), modes.CBC(_RAYTHINK_KEY)).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode("ascii")


class Raythink:
    """POST /v1/token (AES password in the query), then GETs with X-Token.
    Every reply is an envelope {Code, Data, ...}; Code 200 is success."""

    def __init__(self, http: httpx.Client, ip: str, login: DeviceLogin,
                 register_secret: Optional[Callable[[str], None]] = None) -> None:
        self.http, self.ip, self.creds = http, ip, login
        self.base = f"http://{ip}"
        self.token = ""
        self._register = register_secret

    def login(self) -> None:
        enc = raythink_password(self.creds.password)
        if self._register:
            self._register(enc)
            self._register(quote(enc, safe=""))
        qs = f"username={quote(self.creds.username, safe='')}&password={quote(enc, safe='')}"
        r = _http(lambda: self.http.post(f"{self.base}/v1/token?{qs}"), f"{self.ip} login")
        env = _get_json(r, f"{self.ip} login")
        if not isinstance(env, dict) or env.get("Code") != 200:
            raise DeviceError(f"{self.ip} login refused (Code {env.get('Code') if isinstance(env, dict) else '?'})")
        data = env.get("Data")
        self.token = data if isinstance(data, str) else (data or {}).get("Token", "")
        if not self.token:
            raise DeviceError(f"{self.ip} login returned no token")

    def get(self, path: str) -> Any:
        r = _http(lambda: self.http.get(self.base + path, headers={"X-Token": self.token}),
                  f"{self.ip} GET {path}")
        env = _get_json(r, f"{self.ip} GET {path}")
        if not isinstance(env, dict) or env.get("Code") not in (200, 200000):
            raise DeviceError(f"{self.ip} GET {path}: Code {env.get('Code') if isinstance(env, dict) else '?'}")
        return env.get("Data")

    def identity(self) -> dict:
        info = self.get("/v1/system/product/version") or {}
        return {"serial": str(info.get("PDSN") or "unknown"),
                "model": str(info.get("PDName") or "unknown"),
                "firmware": str(info.get("Software") or "unknown"),
                "mac": "unknown"}


# ── Provision-ISR speaker (CGI) ─────────────────────────────────────────────

class Speaker:
    """POST /cgi-bin/CGI?login (MD5-hex password), then ?config=<name>.get."""

    def __init__(self, http: httpx.Client, ip: str, login: DeviceLogin) -> None:
        self.http, self.ip, self.creds = http, ip, login
        self.base = f"http://{ip}/cgi-bin/CGI"

    def login(self) -> None:
        pw = hashlib.md5(self.creds.password.encode()).hexdigest()
        r = _http(lambda: self.http.post(f"{self.base}?login", data={
            "username": self.creds.username, "password": pw}), f"{self.ip} login")
        data = _get_json(r, f"{self.ip} login")
        if not isinstance(data, dict) or data.get("result") != 0:
            raise DeviceError(f"{self.ip} login refused (result {data.get('result') if isinstance(data, dict) else '?'})")

    def get(self, config: str) -> dict:
        r = _http(lambda: self.http.get(f"{self.base}?config={config}",
                                        params={"t": int(time.time() * 1000)}),
                  f"{self.ip} {config}")
        data = _get_json(r, f"{self.ip} {config}")
        if not isinstance(data, dict) or data.get("result") != 0:
            raise DeviceError(f"{self.ip} {config}: result {data.get('result') if isinstance(data, dict) else '?'}")
        return data.get("data") or {}

    def identity(self) -> dict:
        o = self.get("overview.get")
        return {"serial": str(o.get("serialnumber") or o.get("uid") or "unknown"),
                "model": "Provision-ISR speaker",
                "firmware": str(o.get("version") or "unknown"),
                "mac": normalize_mac(o.get("mac")) or "unknown",
                "netip": o.get("netip")}


# ── Planet IGS-4215 (web identity + SSH CLI) ────────────────────────────────

PLANET_CMD_LOGIN = 1
PLANET_CMD_SYSTEM_INFO = 512
PLANET_PROMPT = re.compile(r"[\w.\-()]+ ?[#>] ?$")
PLANET_REJECT = re.compile(r"(Invalid|Incomplete|Unknown command|% ?Error)", re.I)


def parse_info_table(page: str) -> dict[str, str]:
    """Label/value pairs out of the switch's system-info HTML table."""
    cells = [re.sub(r"<[^>]+>|&nbsp;", " ", c) for c in
             re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", page, re.S | re.I)]
    cells = [" ".join(c.split()) for c in cells]
    out = {}
    for label, value in zip(cells, cells[1:]):
        if label and label not in out and len(label) < 40:
            out[label] = value
    return out


class PlanetWeb:
    def __init__(self, http: httpx.Client, ip: str, login: DeviceLogin) -> None:
        self.http, self.ip, self.creds = http, ip, login
        self.base = f"http://{ip}/cgi-bin/dispatcher.cgi"

    def login(self) -> None:
        r = _http(lambda: self.http.post(f"{self.base}?cmd={PLANET_CMD_LOGIN}", data={
            "username": self.creds.username, "password": self.creds.password, "login": "1"}),
            f"{self.ip} login")
        if "hid=" not in r.headers.get("set-cookie", ""):
            raise DeviceError(f"{self.ip} web login refused")

    def identity(self) -> dict:
        r = _http(lambda: self.http.get(f"{self.base}?cmd={PLANET_CMD_SYSTEM_INFO}"),
                  f"{self.ip} system info")
        page = r.text
        f = parse_info_table(page)
        mac = normalize_mac(f.get("MAC Address")) or "unknown"
        return {
            "model": f.get("System Name") or f.get("Model Name") or "unknown",
            "firmware": f.get("Firmware Version") or "unknown",
            "mac": mac,
            # The switch exposes no serial; the MAC is its per-unit handle (bench does the same).
            "serial": mac.replace(":", "") if mac != "unknown" else "unknown",
            "power_inputs": {"PWR1": "PWR1:ON" in page, "PWR2": "PWR2:ON" in page},
        }


class PlanetCli:
    """Interactive CLI over SSH, tunnelled through the unit session. Only `show` commands
    and `terminal length 0` are ever sent."""

    def __init__(self, proxy_command: str, ip: str, login: DeviceLogin,
                 timeout: float = 20) -> None:
        self.proxy_command, self.ip, self.creds, self.timeout = proxy_command, ip, login, timeout
        self._t: Optional[paramiko.Transport] = None
        self._ch = None

    def open(self) -> None:
        if not self.creds.password:
            raise AccessError(f"no password configured for the PoE switch {self.ip}")
        try:
            t = paramiko.Transport(paramiko.ProxyCommand(self.proxy_command))
            t.start_client(timeout=self.timeout)
            t.auth_password(self.creds.username, self.creds.password)
            ch = t.open_session(timeout=self.timeout)
            ch.get_pty(term="vt100", width=200, height=60)
            ch.invoke_shell()
        except paramiko.AuthenticationException:
            raise AccessError(f"PoE switch {self.ip} refused the SSH login "
                              "(or its firmware is below the SSH-fix floor)")
        except Exception as e:  # noqa: BLE001
            raise AccessError(ssh_failure(f"PoE switch {self.ip}", e))
        self._t, self._ch = t, ch
        banner = self._read_until_prompt(5)
        if ch.closed or ch.eof_received:
            raise AccessError(f"PoE switch {self.ip} closed the CLI after its banner "
                              f"({banner.strip()[-60:]!r}) — firmware below the SSH-fix floor?")
        self.cli("terminal length 0")

    def _read_until_prompt(self, timeout: float) -> str:
        out, end = "", time.time() + timeout
        while time.time() < end:
            if self._ch.recv_ready():
                out += self._ch.recv(65535).decode(errors="replace")
                if "--More--" in out[-30:]:
                    self._ch.send(" ")
                    continue
                if PLANET_PROMPT.search(out.rstrip()):
                    break
            else:
                time.sleep(0.05)
        return out

    def cli(self, command: str, timeout: float = 25) -> str:
        if not (command.startswith("show ") or command == "terminal length 0"):
            raise ValueError(f"refusing to send a non-show command to the switch: {command!r}")
        self._ch.send(command + "\n")
        return self._read_until_prompt(timeout)

    def close(self) -> None:
        for c in (self._ch, self._t):
            try:
                if c is not None:
                    c.close()
            except Exception:  # noqa: BLE001
                pass
        self._ch = self._t = None


# ── Teltonika RUTM08 / OTD500 / TSW202 (SSH) ────────────────────────────────

def _find(d: dict, *keys: str) -> Optional[str]:
    low = {str(k).lower(): v for k, v in (d or {}).items()}
    for k in keys:
        v = low.get(k.lower())
        if v not in (None, ""):
            return str(v)
    return None


def teltonika_identity(ssh: DeviceSSH) -> dict:
    s = ssh.sections({"mnf": "ubus call mnfinfo get 2>/dev/null",
                      "board": "ubus call system board 2>/dev/null",
                      "version": "cat /etc/version 2>/dev/null"})
    if all(r.rc == 255 for r in s.values()):
        raise DeviceError(next(iter(s.values())).err.strip() or f"{ssh.host} unreachable")

    def j(name: str) -> dict:
        try:
            v = json.loads(s[name].out)
            return v if isinstance(v, dict) else {}
        except ValueError:
            return {}

    mnf, board = j("mnf"), j("board")
    # RutOS wraps the fields: {"mnfinfo": {"serial": ..., "mac": ...}}
    if isinstance(mnf.get("mnfinfo"), dict):
        mnf = mnf["mnfinfo"]
    return {
        "serial": _find(mnf, "serial", "sn", "serial_number") or "unknown",
        "model": _find(board, "model") or _find(mnf, "name", "mname", "product_code") or "unknown",
        "firmware": s["version"].text or _find(board.get("release") or {}, "version") or "unknown",
        "mac": normalize_mac(_find(mnf, "mac", "maceth", "macaddr")) or "unknown",
        "hostname": board.get("hostname"),
    }


# ── helpers ─────────────────────────────────────────────────────────────────

def normalize_mac(value: Any) -> Optional[str]:
    """Any MAC spelling (aa-bb-.., aabb.ccdd.eeff, AABBCC..) -> 'aa:bb:cc:dd:ee:ff'."""
    if not value:
        return None
    hexes = re.sub(r"[^0-9a-fA-F]", "", str(value))
    if len(hexes) != 12:
        return None
    hexes = hexes.lower()
    return ":".join(hexes[i:i + 2] for i in range(0, 12, 2))
