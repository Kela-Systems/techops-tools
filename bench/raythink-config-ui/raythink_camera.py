#!/usr/bin/env python3
"""
Raythink thermal-camera device client (Dahua-OEM RPC2 JSON API).

A fresh camera boots on a static 192.168.1.123 with admin/admin and speaks the
Dahua "RPC2" JSON protocol (the web UI's own stack — see the device's
js/core/interfaceBase.js). This module is the field client both the pipeline and
the bench UI build on; the per-device pipeline lives in raythink_configure.py.

Protocol, reverse-engineered from the camera's own scripts:

  * Login is a two-step challenge/response against /RPC2_Login:
      1. global.login with an empty password -> the device answers (error
         "login challenge!") with {realm, random, encryption, session}.
      2. global.login again, password =
             MD5( user:random:MD5(user:realm:password) )   (encryption "Default")
         carrying the session + authorityType. Dahua's getAuth() uses UPPERCASE
         hex on most firmwares (a few use lowercase), so login() tries UPPER
         first and falls back to lower before giving up.
  * Every later call is POST /RPC2 with {method, params, id, session}; success
    is a truthy "result". configManager.getConfig({name}) -> params.table;
    configManager.setConfig({name, table, options:[]}) writes it back.
  * The device locks the account after a few bad logins (error 268632081);
    we surface that instead of hammering it further.

There is no single "import config" RPC — the web's Setup > System > Import reads
the exported JSON (a map of config-table name -> table) and replays each table
through configManager.setConfig. import_config() does the same.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import socket
import sys
import time
from datetime import datetime
from typing import Optional

try:
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    sys.exit("This script needs 'requests'.  Install it with:  pip install requests")

from bench_core import host_iface_for, renew_host_dhcp

# All device-talking steps log through this named logger so the bench UI's
# StepCollector and rolling file handler pick them up (same pattern as the
# Teltonika client's "teltonika" logger).
log = logging.getLogger("raythink")
_LOG_CTX = {"sn": "-"}


class _ContextFilter(logging.Filter):
    """Give every record the fields the bench-UI log formatter expects
    (LOG_LINE_FORMAT uses %(levelname_lc)s and %(sn)s)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.levelname_lc = record.levelname.lower()
        if not hasattr(record, "sn"):
            record.sn = _LOG_CTX["sn"]
        return True


log.addFilter(_ContextFilter())


def set_log_serial(serial: Optional[str]) -> None:
    """Tag subsequent log lines with this camera's serial (None resets to '-')."""
    _LOG_CTX["sn"] = serial or "-"


# --- Raythink factory defaults ----------------------------------------------
DEFAULT_HOST = "192.168.1.123"
DEFAULT_USERNAME = "admin"
DEFAULT_SCHEME = "http"
DEFAULT_INITIAL_PASSWORD = "admin"
DEFAULT_NEW_PASSWORD = "Kelafield123!"
DEFAULT_NTP_SERVER = "192.168.88.10"

# Login error codes from the web's interfaceLogin.js.
ERR_CHALLENGE = 268632079    # expected on the first (empty-password) call
ERR_USER_INVALID = 268632070
ERR_PASSWORD_INVALID = 268632071
ERR_LOCKED = 268632081


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CameraError(Exception):
    """A hard, run-aborting device error (login, password, network)."""


class RaythinkCameraClient:
    """One Raythink camera over the RPC2 JSON API. Stateless between calls apart
    from the login session; never raises on a transport blip during the IP move
    (the device is leaving its address by then)."""

    def __init__(self, host: str = DEFAULT_HOST, username: str = DEFAULT_USERNAME,
                 scheme: str = DEFAULT_SCHEME, verify: bool = False, timeout: int = 15):
        self.host = host
        self.username = username
        self.scheme = scheme
        self.timeout = timeout
        self.base = f"{scheme}://{host}"
        self.s = requests.Session()
        self.s.verify = verify
        self.session = 0          # RPC2 session id (int); 0 until first login
        self._id = 0
        self.password: Optional[str] = None
        self.encryption = "Default"
        self.realm = ""
        self.hash_uppercase = True   # which hex case the device accepted at login

    # --- transport ----------------------------------------------------------
    def _port(self) -> int:
        return 443 if self.scheme == "https" else 80

    def _rpc(self, method: str, params=None, *, url: str = "/RPC2",
             session: Optional[int] = None, raise_on_error: bool = True) -> dict:
        """One RPC2 call. Returns the parsed JSON. Raises CameraError when
        raise_on_error and the device reports result=false."""
        self._id += 1
        body = {"method": method, "params": params, "id": self._id,
                "session": self.session if session is None else session}
        try:
            r = self.s.post(self.base + url, data=json.dumps(body),
                            headers={"Content-Type": "application/json"},
                            timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            raise CameraError(f"{method}: connection failed ({e})")
        try:
            data = r.json()
        except ValueError:
            raise CameraError(f"{method}: non-JSON reply (HTTP {r.status_code}): {r.text[:160]}")
        if raise_on_error and not data.get("result"):
            err = data.get("error", {}) or {}
            raise CameraError(f"{method} failed: {err.get('code')} {err.get('message', '')}".strip())
        return data

    # --- auth ---------------------------------------------------------------
    def _auth_hash(self, password: str, realm: str, random: str, encryption: str,
                   uppercase: bool = True) -> str:
        """Compute the RPC2 login hash. Dahua's getAuth() hashes
        user:realm:password, then user:random:<that>, in hex. The hex *case*
        differs by firmware, so the caller picks UPPER or lower."""
        u = self.username
        if encryption == "Basic":  # raw password (HTTPS only); case is moot
            return password
        h = _sha256 if encryption == "DigestSHA256" else _md5
        inner = h(f"{u}:{realm}:{password}")
        if uppercase:
            inner = inner.upper()
        outer = h(f"{u}:{random}:{inner}")
        return outer.upper() if uppercase else outer

    def _challenge(self) -> dict:
        """Fire the empty-password global.login to get {realm, random,
        encryption, session}. The random is single-use on some firmwares, so we
        re-challenge for every hash attempt."""
        first = self._rpc("global.login",
                          {"userName": self.username, "password": "",
                           "clientType": "Web3.0", "loginType": "Direct"},
                          url="/RPC2_Login", session=0, raise_on_error=False)
        self.session = first.get("session", 0) or 0
        if (first.get("error", {}) or {}).get("code") == ERR_LOCKED:
            raise CameraError("Account is locked (too many failed logins) — wait ~5 min, then retry.")
        p = first.get("params", {}) or {}
        self.encryption = p.get("encryption", "Default")
        self.realm = p.get("realm", "") or self.realm
        if not p.get("random"):
            raise CameraError(f"No login challenge from the device: {first.get('error') or first}")
        return p

    def login(self, password: str) -> None:
        """Two-step RPC2 login. Stores the session + the working password. Dahua
        firmwares disagree on the hash's hex case, so we try UPPER (the common
        case) then lower before concluding the password is wrong. Raises
        CameraError on failure (with a clear message for a locked account)."""
        last_code = None
        last_raw = None
        for uppercase in (True, False):
            p = self._challenge()
            auth = self._auth_hash(password, self.realm, p["random"], self.encryption, uppercase)
            second = self._rpc("global.login",
                              {"userName": self.username, "password": auth,
                               "clientType": "Web3.0", "loginType": "Direct",
                               "authorityType": self.encryption},
                              url="/RPC2_Login", raise_on_error=False)
            if second.get("result"):
                self.session = second.get("session", self.session) or self.session
                self.password = password
                self.hash_uppercase = uppercase
                log.info("Logged in as '%s' (encryption=%s, hex=%s).",
                         self.username, self.encryption, "UPPER" if uppercase else "lower")
                return
            code = (second.get("error", {}) or {}).get("code")
            last_code, last_raw = code, (second.get("error") or second)
            if code == ERR_LOCKED:
                raise CameraError("Account is locked (too many failed logins) — wait ~5 min, then retry.")
            if code == ERR_USER_INVALID:
                raise CameraError(f"Login failed: user '{self.username}' not valid.")
            if self.encryption == "Basic":
                break  # hex case is irrelevant for Basic auth; no point retrying

        if last_code == ERR_PASSWORD_INVALID:
            raise CameraError("Login failed: wrong password (device rejected the hash for both hex cases).")
        raise CameraError(f"Login failed: {last_raw}")

    def close(self) -> None:
        if self.password is not None:
            try:
                self._rpc("global.logout", raise_on_error=False)
            except CameraError:
                pass
        self.s.close()

    # --- identity -----------------------------------------------------------
    def _serial_from_realm(self) -> str:
        # realm looks like "Login to KK0552PAZ00681".
        parts = (self.realm or "").split()
        return parts[-1] if parts else ""

    def get_identity(self) -> dict:
        """serial / model / firmware / MAC, best-effort. Tags the log with the
        serial so later step lines carry it."""
        info = self._rpc("magicBox.getSystemInfo", raise_on_error=False).get("params", {}) or {}
        sw = self._rpc("magicBox.getSoftwareVersion", raise_on_error=False).get("params", {}) or {}

        serial = (info.get("serialNumber") or self._serial_from_realm() or "unknown")
        model = (info.get("deviceType") or "Raythink")
        ver = sw.get("version")
        if isinstance(ver, dict):
            firmware = ver.get("Version") or ver.get("Build") or "unknown"
        else:
            firmware = str(ver) if ver else "unknown"

        mac = "unknown"
        try:
            net = self._rpc("configManager.getConfig", {"name": "Network"},
                            raise_on_error=False).get("params", {}).get("table", {}) or {}
            iface = net.get("DefaultInterface") or "eth0"
            eth = net.get(iface, {}) if isinstance(net.get(iface), dict) else {}
            mac = eth.get("PhysicalAddress") or net.get("PhysicalAddress") or "unknown"
        except CameraError:
            pass

        identity = {"serial": serial, "model": model, "firmware": firmware,
                    "mac": mac, "raw": {"systemInfo": info, "software": sw}}
        set_log_serial(serial if serial != "unknown" else None)
        log.info("Identity: model=%s serial=%s fw=%s MAC=%s",
                 model, serial, firmware, mac)
        return identity

    # --- password -----------------------------------------------------------
    def _stored_hash(self, password: str, uppercase: bool) -> str:
        """The form Dahua stores a credential in: MD5(user:realm:password) hex
        (case per firmware), or Base64(user:password) for Basic auth. This is
        what userManager.modifyPassword/addUser expect in pwd/pwdOld — NOT the
        plaintext, and NOT the random-salted login hash."""
        u = self.username
        if self.encryption == "Basic":
            return base64.b64encode(f"{u}:{password}".encode("utf-8")).decode("ascii")
        h = _sha256 if self.encryption == "DigestSHA256" else _md5
        digest = h(f"{u}:{self.realm}:{password}")
        return digest.upper() if uppercase else digest

    def _password_forms(self, new_pw: str, old_pw: str) -> list[tuple[str, str, str]]:
        """(pwd, pwdOld, label) candidates for modifyPassword, best guess first:
        the realm hash in the hex case login used, the other case, then plaintext
        (some firmwares accept it). Both fields always use the same form."""
        out: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str]] = set()
        for up in (self.hash_uppercase, not self.hash_uppercase):
            pair = (self._stored_hash(new_pw, up), self._stored_hash(old_pw, up))
            if pair not in seen:
                seen.add(pair)
                out.append((*pair, f"md5 {'UPPER' if up else 'lower'}"))
        if (new_pw, old_pw) not in seen:
            out.append((new_pw, old_pw, "plaintext"))
        return out

    def modify_password(self, new_password: str, old_password: str) -> None:
        """Change the admin password and re-login under it. Idempotent: if we are
        already authenticated on new_password, do nothing. The device wants the
        passwords hashed (error 611 on plaintext), so we try the hashed form
        first and fall back; only the correct form succeeds, so retrying is
        safe."""
        if self.password == new_password:
            log.info("Password already set to the target; skipping.")
            return
        log.info("Changing the admin password ...")
        last_err: dict = {}
        for pwd, pwd_old, label in self._password_forms(new_password, old_password):
            resp = self._rpc("userManager.modifyPassword",
                             {"name": self.username, "pwd": pwd, "pwdOld": pwd_old},
                             raise_on_error=False)
            if resp.get("result"):
                log.info("Password change accepted (form=%s).", label)
                break
            last_err = (resp.get("error", {}) or {})
            log.info("Password change rejected for form=%s (%s %s); trying next.",
                     label, last_err.get("code"), last_err.get("message", ""))
        else:
            raise CameraError(
                f"userManager.modifyPassword failed: "
                f"{last_err.get('code')} {last_err.get('message', '')}".strip())
        # The session usually survives, but re-login to be certain everything
        # downstream runs under the new password.
        self.password = new_password
        self.relogin([new_password])
        log.info("Password changed.")

    # --- reachability / relogin --------------------------------------------
    def port_open(self, host: Optional[str] = None) -> bool:
        try:
            with socket.create_connection((host or self.host, self._port()), timeout=3):
                return True
        except OSError:
            return False

    def wait_reachable(self, timeout: int = 180, host: Optional[str] = None) -> bool:
        """Block until host:port answers (used after an import that may reboot)."""
        target = host or self.host
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.port_open(target):
                return True
            time.sleep(3)
        return False

    def relogin(self, candidates: list[str], settle: int = 0) -> None:
        """Re-establish a session, trying each candidate password in turn. Used
        after a password change or a config import that may reset the session.
        Raises CameraError only if none work."""
        if settle:
            time.sleep(settle)
        last = None
        for pw in [p for p in candidates if p]:
            try:
                self.login(pw)
                return
            except CameraError as e:
                last = e
                continue
        raise CameraError(f"Could not re-login after the previous step: {last}")

    # --- config import ------------------------------------------------------
    @staticmethod
    def _normalize_tables(data) -> dict:
        """Coerce an exported config file into {table_name: table_value}.

        Accepts the common shapes: a plain map of tables, a {"params":{"table":…}}
        or {"config":…}/{"tables":…} wrapper, or a list of {"name","table"} rows."""
        if isinstance(data, dict):
            if "params" in data and isinstance(data["params"], dict) and "table" in data["params"]:
                return RaythinkCameraClient._normalize_tables(data["params"]["table"])
            for key in ("config", "tables", "Config"):
                if key in data and isinstance(data[key], (dict, list)):
                    return RaythinkCameraClient._normalize_tables(data[key])
            if "name" in data and "table" in data:  # single {name, table}
                return {data["name"]: data["table"]}
            return dict(data)
        if isinstance(data, list):
            out = {}
            for row in data:
                if isinstance(row, dict) and "name" in row and "table" in row:
                    out[row["name"]] = row["table"]
            return out
        raise CameraError("Unrecognised config file format (expected a JSON object of tables).")

    def import_config(self, json_path: str) -> dict:
        """Apply an exported config profile (the same JSON the web's Setup >
        System > Import accepts) by replaying each config table through
        setConfig. Per-table failures are warnings, not fatal — some tables are
        read-only or device-specific, exactly as the web import tolerates."""
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        tables = self._normalize_tables(data)
        if not tables:
            raise CameraError(f"Config file {json_path} has no config tables to import.")
        log.info("Importing %d config table(s) from %s ...", len(tables), json_path)
        applied: list[str] = []
        skipped: list[str] = []
        for name, table in tables.items():
            resp = self._rpc("configManager.setConfig",
                             {"name": name, "table": table, "options": []},
                             raise_on_error=False)
            if resp.get("result"):
                applied.append(name)
            else:
                err = (resp.get("error", {}) or {}).get("message", "rejected")
                skipped.append(name)
                log.warning("import: table '%s' not applied (%s).", name, err)
        log.info("Config import: %d applied, %d skipped.", len(applied), len(skipped))
        return {"applied": applied, "skipped": skipped}

    # --- NTP ----------------------------------------------------------------
    def set_ntp(self, server: str, port: int = 123, update_period: int = 60) -> None:
        """Read-modify-write the NTP table so we keep the device's other fields
        (timezone, etc.) and only flip Enable + the server address."""
        log.info("Setting NTP server to %s ...", server)
        table = self._rpc("configManager.getConfig", {"name": "NTP"}).get(
            "params", {}).get("table", {})
        if not isinstance(table, dict):
            table = {}
        table["Enable"] = True
        table["Address"] = server
        table["Port"] = int(port)
        table["UpdatePeriod"] = int(update_period)
        self._rpc("configManager.setConfig", {"name": "NTP", "table": table, "options": []})
        log.info("NTP set.")

    def sync_time_to_pc(self) -> None:
        """Set the camera clock to this PC's current local time — the web UI's
        Setup > System > General > Date & Time 'Sync to PC' button. Dahua RPC is
        global.setCurrentTime {"time": "YYYY-MM-DD HH:MM:SS"} (local wall clock,
        like the browser sends). Tries the plain form, then with a tolerance, to
        cover firmware variants."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log.info("Syncing the camera clock to PC time (%s) ...", now)
        resp = self._rpc("global.setCurrentTime", {"time": now}, raise_on_error=False)
        if not resp.get("result"):
            resp = self._rpc("global.setCurrentTime", {"time": now, "tolerance": 5},
                             raise_on_error=False)
        if not resp.get("result"):
            err = resp.get("error", {}) or {}
            raise CameraError(f"setCurrentTime failed: "
                              f"{err.get('code')} {err.get('message', '')}".strip())
        log.info("Camera clock set to %s.", now)

    # --- static IP (LAST: drops the connection) -----------------------------
    def set_static_ip(self, ip: str, netmask: str, gateway: str, wait: int = 120) -> dict:
        """Move the camera to a static IP. We are talking to it over that very
        interface, so the change drops the connection by design: we read-modify-
        write the Network table, fire setConfig, then confirm the device answers
        on the NEW address (renewing the host's DHCP lease so the laptop can
        follow). Returns a verification-style check; never raises after the write
        (the device is moving whether we can still see it or not)."""
        table = self._rpc("configManager.getConfig", {"name": "Network"}).get(
            "params", {}).get("table", {})
        if not isinstance(table, dict):
            table = {}
        iface = table.get("DefaultInterface") or "eth0"
        eth = table.get(iface)
        if not isinstance(eth, dict):
            eth = {}
            table[iface] = eth

        # Two known schemas: a nested IPAddress object, or flat fields. Support
        # both by writing whichever the device already uses.
        eth["DhcpEnable"] = False
        if "EnableDhcp" in eth:
            eth["EnableDhcp"] = False
        if isinstance(eth.get("IPAddress"), dict):
            addr = eth["IPAddress"]
            # nested keys also vary: IPAddress vs Address
            if "Address" in addr and "IPAddress" not in addr:
                addr["Address"] = ip
            else:
                addr["IPAddress"] = ip
            addr["SubnetMask"] = netmask
            addr["DefaultGateway"] = gateway
        else:
            eth["IPAddress"] = ip
            eth["SubnetMask"] = netmask
            eth["DefaultGateway"] = gateway

        iface_host = host_iface_for(self.host)  # resolve while still reachable
        log.info("Moving the camera from %s to %s — the connection will drop ...",
                 self.host, ip)
        # The reply may never arrive (the IP changes mid-request); treat a
        # transport error as "the move started".
        try:
            self._rpc("configManager.setConfig",
                      {"name": "Network", "table": table, "options": []},
                      raise_on_error=False)
        except CameraError as e:
            log.info("Connection dropped applying the IP (expected): %s", e)

        # Follow the device to its new address.
        self.host = ip
        self.base = f"{self.scheme}://{ip}"
        self.password = self.password  # unchanged
        deadline = time.time() + wait
        time.sleep(5)
        renew_host_dhcp(iface_host)
        renewed_again = False
        while time.time() < deadline:
            if self.port_open(ip):
                log.info("Camera is answering on %s.", ip)
                return {"item": "static IP", "expected": ip,
                        "actual": f"answering on {ip}", "ok": True}
            if not renewed_again and time.time() > deadline - wait / 2:
                renew_host_dhcp(iface_host)
                renewed_again = True
            time.sleep(3)
        log.warning("Camera did not answer on %s within %ds — it may still be fine; "
                    "check the laptop has an address in that subnet.", ip, wait)
        return {"item": "static IP", "expected": ip,
                "actual": f"no answer on {ip} after {wait}s (laptop subnet?)", "ok": False}

    # --- verification -------------------------------------------------------
    def verify_configuration(self, *, new_password: str, ntp_server: str,
                             ip: str, netmask: str, gateway: str,
                             profile_name: str = "", imported: Optional[dict] = None
                             ) -> list[dict]:
        """Re-read the settings we changed and confirm they took. Runs AFTER the
        IP move, so it talks to the device on its new address (we are already
        re-pointed there). Returns {item, expected, actual, ok} rows."""
        checks: list[dict] = []

        def add(item, expected, actual, ok):
            checks.append({"item": item, "expected": expected, "actual": actual, "ok": ok})

        # Password: we are authenticated on new_password (we re-logged-in under it).
        add("admin password", new_password,
            "in use" if self.password == new_password else (self.password or "unknown"),
            self.password == new_password)

        if profile_name:
            applied = len((imported or {}).get("applied", []))
            skipped = len((imported or {}).get("skipped", []))
            add("config profile", profile_name,
                f"{profile_name}: {applied} table(s) applied" + (f", {skipped} skipped" if skipped else ""),
                applied > 0)

        try:
            ntp = self._rpc("configManager.getConfig", {"name": "NTP"}).get(
                "params", {}).get("table", {}) or {}
            got = ntp.get("Address", "")
            add("NTP server", ntp_server, f"{got} (enable={ntp.get('Enable')})",
                got == ntp_server and bool(ntp.get("Enable")))
        except CameraError as e:
            add("NTP server", ntp_server, f"read failed: {e}", False)

        try:
            net = self._rpc("configManager.getConfig", {"name": "Network"}).get(
                "params", {}).get("table", {}) or {}
            iface = net.get("DefaultInterface") or "eth0"
            eth = net.get(iface, {}) if isinstance(net.get(iface), dict) else {}
            addr = eth.get("IPAddress")
            if isinstance(addr, dict):
                got_ip = addr.get("IPAddress") or addr.get("Address") or ""
                got_mask = addr.get("SubnetMask", "")
                got_gw = addr.get("DefaultGateway", "")
            else:
                got_ip = addr or ""
                got_mask = eth.get("SubnetMask", "")
                got_gw = eth.get("DefaultGateway", "")
            dhcp = eth.get("DhcpEnable", eth.get("EnableDhcp"))
            add("static IP", ip, f"{got_ip} (dhcp={dhcp})", got_ip == ip)
            add("subnet mask", netmask, got_mask or "?", got_mask == netmask)
            add("gateway", gateway, got_gw or "?", got_gw == gateway)
        except CameraError as e:
            add("static IP", ip, f"read failed: {e}", False)

        return checks


def format_verification(checks: list[dict]) -> str:
    mark = {True: "PASS", False: "FAIL", None: "skip"}
    width = max((len(c["item"]) for c in checks), default=0)
    lines = ["── Verification ──"]
    for c in checks:
        lines.append(f"  [{mark[c['ok']]}] {c['item']:<{width}}  {c['actual']}")
    return "\n".join(lines)
