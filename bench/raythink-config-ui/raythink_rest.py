#!/usr/bin/env python3
"""
Raythink thermal-camera device client for the NEWER cameras (REST /v1 API).

This is one of the two generations the bench provisions. A camera of this
generation reports a firmware version like `B1.2.01.01.15, 2026-05-14`; the
older ones report `1.000.General 00.0.T, build: 2025-04-09` and speak the
Dahua-OEM RPC2 protocol (`raythink_camera.py`). `raythink_client` tells them
apart and hands the pipeline whichever client fits, so nothing above this file
knows which it got.

Everything that is not REST-specific — the addressing steps, the reachability
helpers, the verification report — lives in `raythink_base.py`; this file
supplies the REST half through that base's hooks.

Protocol (vendor doc "Thermal EN API" v1.0.0, plus two endpoints captured off a
live camera — see UNDOCUMENTED below):

  * Login is POST /v1/token?username=&password=, where the password is
    AES-128-CBC encrypted under a FIXED key that is also the IV, PKCS7-padded,
    base64'd and then URL-encoded. Not a challenge/response and not a secret —
    the key ships in the vendor's own web bundle — so this is obfuscation, not
    security. It buys nothing over plaintext, but it is what the device accepts.
  * The reply carries a Token; every later call sends it as the X-Token header.
    The token expires when idle, so it is refreshed lazily (see _request).
  * Every reply is the same envelope: {Code, Data, Detail, Message, Translate}.
    Code 200 is success; anything else is an error and Detail/Message say why.
    A transport-level HTTP status is NOT the answer — the device answers 200 OK
    with a non-200 Code — so `_request` checks the envelope, not r.status_code.

UNDOCUMENTED, captured from the camera's own web UI. Neither appears anywhere in
the 597-page vendor PDF, so a reader checking them against it will not find them:

  * PUT /v1/user/user?newpwd=&desc=&oldpwd=  — change the logged-in user's
    password. Both values use the same AES encoding as the login password.
  * GET /v1/system/product/version — the only source of serial / model /
    firmware. The vendor doc has no device-identity endpoint at all.
"""
from __future__ import annotations

import base64
import json
import sys
import time
from datetime import datetime
from typing import Optional
from urllib.parse import quote

try:
    import requests
except ImportError:
    sys.exit("This script needs 'requests'.  Install it with:  pip install requests")

try:
    from cryptography.hazmat.primitives import padding as _padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:  # reported at use time — see encrypt_password
    _padding = Cipher = algorithms = modes = None

from raythink_base import (
    DEFAULT_HOST,
    DEFAULT_SCHEME,
    DEFAULT_USERNAME,
    GEN_REST,
    BaseRaythinkClient,
    CameraError,
    CameraUnreachable,
    MutationBlocked,
    NetworkView,
    log,
    sanitize_profile,
    set_log_serial,
)

# The vendor's fixed AES key, which is also the IV. Both are the same 16 ASCII
# bytes, shipped in the camera's own web bundle — see the module docstring on why
# this is obfuscation rather than a secret.
_AES_KEY = _AES_IV = b"YJWL202101010000"

# How long a token is left unused before `_request` refreshes it. The device
# reports its own idle limit in LoginLimit.NoOperationTimeout (24 minutes on the
# unit we have), and the vendor doc suggests refreshing every 30s; 20s of slack
# under the doc's own figure costs one cheap PUT and removes the whole question.
TOKEN_REFRESH_SEC = 20

# The envelope codes that mean the call WORKED. 200 is the ordinary one; 200000
# is "done, but it needs a reboot before it is live", and the config import
# answers with it. Reading anything but 200 as an error is how a successful
# 125-section import came back as
# "PUT /v1/system/magic/configuration failed: 200000 msg: success, need to
# reboot device" — the message says "success" in it and the tool still failed
# the step. The vendor doc lists neither code; both are off a live camera.
SUCCESS_CODES = (200, 200000)
CODE_REBOOT_REQUIRED = 200000

# Marker keys used to catch a profile handed to the wrong generation. A legacy
# export is Dahua config tables (VideoInOptions, Encode, ...); a v2 export is
# these. The two share no section names at all, so one hit is conclusive.
V2_MARKER_SECTIONS = ("NetworkInfo", "NtpInfo", "WebInfo", "OnvifUser", "LocalSettings")
LEGACY_MARKER_SECTIONS = ("VideoInOptions", "Encode", "VideoWidget", "UserGlobal")


def encrypt_password(password: str) -> str:
    """The wire form of a password: AES-128-CBC (key = IV = _AES_KEY), PKCS7,
    base64. Used for the login password and for both fields of the password
    change. The caller URL-encodes it — these all travel as query parameters.

    A missing `cryptography` is reported HERE rather than at import. This module
    is imported lazily, by `open_camera`, only once a newer camera is on the
    bench — so an import-time `sys.exit` would take down the bench UI's worker
    mid-run instead of failing the step with something an operator can read.
    """
    if Cipher is None:
        raise CameraError(
            "The newer Raythink cameras need the 'cryptography' package to "
            "encrypt the login password, and it is not installed in this "
            "environment. Reinstall the bench dependencies "
            "(pip install -e ./bench-core) — it is a declared dependency.")
    padder = _padding.PKCS7(128).padder()
    padded = padder.update(password.encode("utf-8")) + padder.finalize()
    enc = Cipher(algorithms.AES(_AES_KEY), modes.CBC(_AES_IV)).encryptor()
    return base64.b64encode(enc.update(padded) + enc.finalize()).decode("ascii")


class RaythinkRestClient(BaseRaythinkClient):
    """One newer Raythink camera over the REST /v1 API."""

    generation = GEN_REST

    def __init__(self, host: str = DEFAULT_HOST, username: str = DEFAULT_USERNAME,
                 scheme: str = DEFAULT_SCHEME, verify: bool = False, timeout: int = 15):
        super().__init__(host=host, username=username, scheme=scheme,
                         verify=verify, timeout=timeout)
        self.token: str = ""
        self._token_used = 0.0   # monotonic time of the last call that used it
        self.last_code: Optional[int] = None   # envelope Code of the last reply

    # --- transport ----------------------------------------------------------
    def _request(self, method: str, path: str, *, params: Optional[dict] = None,
                 body: Optional[dict] = None, files=None, keepalive: bool = True,
                 raise_on_error: bool = True):
        """One REST call. Returns the envelope's `Data`. Raises CameraError when
        raise_on_error and the device reports a non-200 `Code`.

        The read-only gate lives here and is simply "anything that is not a GET"
        (TEC-348). That is a much better gate than the RPC2 client's deny-list of
        method names: a write added later is refused by default instead of
        needing to be remembered, and no read can ever be blocked by mistake.
        """
        if self.read_only and method != "GET":
            raise MutationBlocked(f"read-only client refused a write: {method} {path}")
        if keepalive:
            self._keep_token_alive()
        return self._raw(method, path, params=params, body=body, files=files,
                         raise_on_error=raise_on_error)

    @staticmethod
    def _safe(path: str) -> str:
        """`path` with its query string dropped, for error messages and logs.

        This API puts credentials in the QUERY STRING — the login password, and
        both passwords of a change. They are AES-encrypted, but under a fixed key
        that ships in the camera's own web bundle, so a ciphertext in a log is a
        password in a log. Error text from here reaches the rolling log, the
        per-camera JSON and potentially a verification row (TEC-349), so the
        query never travels with it.
        """
        return path.split("?", 1)[0]

    def _raw(self, method: str, path: str, *, params: Optional[dict] = None,
             body: Optional[dict] = None, files=None, raise_on_error: bool = True):
        """The call itself, with no read-only gate and no token refresh — used by
        the two operations that cannot recurse through them (the login and the
        refresh itself)."""
        headers = {"X-Token": self.token} if self.token else {}
        kwargs = {"timeout": self.timeout, "headers": headers, "params": params}
        if files is not None:
            kwargs["files"] = files
        elif body is not None:
            headers["Content-Type"] = "application/json"
            kwargs["data"] = json.dumps(body)
        where = f"{method} {self._safe(path)}"
        try:
            r = self.s.request(method, self.base + path, **kwargs)
        except requests.exceptions.RequestException as e:
            raise CameraUnreachable(f"{where}: connection failed ({e})")
        try:
            env = r.json()
        except ValueError:
            raise CameraError(f"{where}: non-JSON reply (HTTP {r.status_code}): "
                              f"{r.text[:160]}")
        self._token_used = time.monotonic()
        code = env.get("Code")
        self.last_code = code
        if raise_on_error and code not in SUCCESS_CODES:
            detail = env.get("Detail") or env.get("Message") or ""
            raise CameraError(f"{where} failed: {code} {detail}".strip())
        return env.get("Data") if code in SUCCESS_CODES else None

    def _keep_token_alive(self) -> None:
        """Refresh the token if it has been idle long enough to be worth it.

        Lazily, from inside the request path, rather than from a background
        thread: the pipeline is strictly sequential, so there is never a second
        caller to race with, and a thread would have to be started, stopped and
        reasoned about across the reboot in the middle of a config import. The
        one genuinely long gap — waiting for the camera to come back after an
        import — is already followed by a relogin, which replaces the token
        outright.
        """
        if not self.token or (time.monotonic() - self._token_used) < TOKEN_REFRESH_SEC:
            return
        try:
            data = self._raw("PUT", "/v1/token", body={}, raise_on_error=False)
        except CameraError:
            return  # the next real call will fail with a better message
        self.token = self._token_from(data) or self.token

    # --- auth ---------------------------------------------------------------
    def login(self, password: str) -> None:
        """POST /v1/token. Stores the token + the working password.

        Both credentials go in the query string, and the encrypted password is
        base64 (so it can contain '+' and '=') — hence the explicit `quote` with
        an empty safe list rather than letting requests build the query, which
        would leave '+' to be read as a space.

        Errors are deliberately NOT swallowed here. A rejected login raises out
        of `_raw` carrying the device's own Code and Detail, because "login
        failed" with no reason is the one failure a bench operator cannot act
        on — wrong password, locked account and unsupported firmware all look
        identical from here otherwise. `relogin` still catches it to try the
        next candidate password, so nothing downstream changes.
        """
        self.token = ""
        qs = (f"username={quote(self.username, safe='')}"
              f"&password={quote(encrypt_password(password), safe='')}")
        data = self._raw("POST", f"/v1/token?{qs}")
        token = self._token_from(data)
        if not token:
            raise CameraError(
                f"Login as '{self.username}' was accepted but the device "
                f"returned no token (Data was {type(data).__name__}).")
        self.token = token
        self.password = password
        log.info("Logged in as '%s' (REST /v1, token acquired).", self.username)

    @staticmethod
    def _token_from(data) -> str:
        """The token out of an envelope's `Data`, whichever shape it arrives in.

        The vendor doc documents a `{Token, NoOperationTimeout}` object for the
        keepalive but leaves `Data` untyped for the login itself, and the clock
        endpoint returns the replacement token as a BARE STRING. So the device
        demonstrably uses both shapes for the same value, and which one the login
        uses is not written down anywhere. Accepting either costs one isinstance
        and removes a failure that would look like a wrong password.
        """
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            return data.get("Token") or ""
        return ""

    def close(self) -> None:
        if self.token:
            try:
                self._raw("DELETE", "/v1/token", raise_on_error=False)
            except CameraError:
                pass
            self.token = ""
        super().close()

    # --- identity -----------------------------------------------------------
    def get_identity(self) -> dict:
        """serial / model / firmware / MAC from GET /v1/system/product/version.

        UNDOCUMENTED (see the module docstring). Unlike the RPC2 client there is
        no fallback needed: PDSN is the serial outright, rather than something to
        be scraped out of a login realm.
        """
        info = self._request("GET", "/v1/system/product/version",
                             raise_on_error=False) or {}
        if not isinstance(info, dict):
            info = {}
        serial = info.get("PDSN") or "unknown"
        model = info.get("PDName") or "Raythink"
        firmware = info.get("Software") or "unknown"

        mac = "unknown"
        try:
            mac = self.read_network().mac or "unknown"
        except Exception:  # noqa: BLE001 — MAC is best-effort; never fail identity over it
            pass

        identity = {"serial": serial, "model": model, "firmware": firmware,
                    "mac": mac, "raw": {"version": info}}
        set_log_serial(serial if serial != "unknown" else None)
        log.info("Identity: model=%s serial=%s fw=%s MAC=%s",
                 model, serial, firmware, mac)
        return identity

    # --- password -----------------------------------------------------------
    def modify_password(self, new_password: str, old_password: str) -> None:
        """Change the admin password and re-login under it. Idempotent: if we are
        already authenticated on new_password, do nothing.

        UNDOCUMENTED (see the module docstring). Both passwords use the same AES
        encoding as the login, which is why this needs none of the RPC2 client's
        three-way guessing at which hash form the firmware wants — there is one
        encoding and the device either accepts it or the credential is wrong.
        The endpoint takes no username: it acts on whoever the token belongs to.
        """
        if self.password == new_password:
            log.info("Password already set to the target; skipping.")
            return
        log.info("Changing the admin password ...")
        qs = (f"newpwd={quote(encrypt_password(new_password), safe='')}"
              f"&desc=&oldpwd={quote(encrypt_password(old_password), safe='')}")
        self._request("PUT", f"/v1/user/user?{qs}")
        # relogin()->login() sets self.password only on success, so a failed
        # relogin correctly leaves the client on the still-working old password
        # instead of a wrong one.
        self.relogin([new_password])
        log.info("Password changed.")

    # --- config import ------------------------------------------------------
    def import_config(self, json_path: str) -> dict:
        """Apply an exported config profile by uploading it whole.

        Unlike the RPC2 generation there is no per-table replay to tolerate
        failures of: the device takes the file in one multipart PUT and either
        accepts it or does not. So the profile is parsed and checked HERE, where
        a bad one can be named precisely, rather than being posted and rejected
        with whatever the device chooses to say about 250 KB of JSON.
        """
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not data:
            raise CameraError(f"Config file {json_path} is not a JSON object of "
                              "config sections.")
        if not any(k in data for k in V2_MARKER_SECTIONS):
            wrong = [k for k in LEGACY_MARKER_SECTIONS if k in data]
            raise CameraError(
                f"Config file {json_path} is not an export from a camera of this "
                "generation"
                + (f" — it looks like an older camera's export (has {', '.join(wrong)})"
                   if wrong else "")
                + ". Export a profile from a reference camera of the newer "
                  "generation and point the profile's 'rest' entry at it.")

        data, notes = sanitize_profile(data, secrets=(self.password or "",))
        for note in notes:
            log.info("import: %s.", note)

        data = self._complete_from_device(data)

        payload = json.dumps(data).encode("utf-8")
        log.info("Importing %d config section(s) from %s (%d KB) ...",
                 len(data), json_path, len(payload) // 1024)
        self._request("PUT", "/v1/system/magic/configuration",
                      files={"file": ("config.json", payload, "application/json")})
        if self.last_code == CODE_REBOOT_REQUIRED:
            # The device took the file but will not run it until it restarts, and
            # says so in the reply. Doing it here rather than leaving it to the
            # operator keeps every later step — ONVIF, the address — working
            # against the configuration that is actually live. The caller already
            # waits for the camera to come back and logs in again.
            log.info("The device took the config but needs a restart to run it; "
                     "rebooting now.")
            self.reboot()
        name = json_path.rsplit("/", 1)[-1]
        log.info("Config import accepted (%d section(s)).", len(data))
        # Same shape as the RPC2 client's return so the shared "config profile"
        # verification row needs no idea which generation produced it. This
        # device applies the file as a unit, so it is one entry, not a tally.
        return {"applied": [name], "skipped": []}

    def reboot(self) -> None:
        """Restart the camera, tolerating the restart eating the reply.

        The device may well drop the connection as it goes down instead of
        answering, and that is a reboot doing its job, not a failure — so a
        transport error here is swallowed. What must not be swallowed is a
        refusal, which is why the call goes through `_request` and its read-only
        gate first. The token does not survive, so it is dropped.
        """
        if self.read_only:
            raise MutationBlocked("read-only client refused a reboot")
        try:
            self._request("PUT", "/v1/system/magic/reboot", raise_on_error=False)
        except CameraError:
            pass
        self.token = ""

    def export_config(self) -> dict:
        """The camera's own current configuration, as the export endpoint gives it.

        This is the one endpoint that does NOT answer with the
        {Code, Data, Detail, ...} envelope every other call uses: it returns the
        configuration file itself, a bare map of section name to section. So it
        cannot go through `_request`, which would read the absent `Code` as a
        failure — which is exactly what it did, reporting `failed: None`. An
        ERROR still arrives as an envelope, hence the shape check below.
        """
        path = "/v1/system/magic/configuration"
        where = f"GET {path}"
        headers = {"X-Token": self.token} if self.token else {}
        try:
            r = self.s.request("GET", self.base + path, params={"default": "false"},
                               headers=headers, timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            raise CameraUnreachable(f"{where}: connection failed ({e})")
        try:
            data = r.json()
        except ValueError:
            raise CameraError(f"{where}: non-JSON reply (HTTP {r.status_code})")
        self._token_used = time.monotonic()
        if not isinstance(data, dict) or not data:
            raise CameraError(f"{where}: expected a map of config sections, got "
                              f"{type(data).__name__}")
        if "Code" in data and "Data" in data:   # an error envelope, not the file
            raise CameraError(f"{where} failed: {data.get('Code')} "
                              f"{data.get('Detail') or data.get('Message') or ''}".strip())
        return data

    def _complete_from_device(self, data: dict) -> dict:
        """`data` with every section the camera has but the profile lacks filled
        in from the camera's OWN current configuration.

        The device rejects a partial file outright — `400204 msg: file is
        incomplete` — and a committed profile is necessarily partial, because the
        sanitiser drops NetworkInfo and OnvifUser (a raw export carries the
        address and the ONVIF password in plaintext, so neither may be committed;
        see PROFILE_DROP_SECTIONS).

        Filling the gaps from the device itself satisfies the device without
        giving up either property that made us drop them: the camera gets its own
        current address back, so the import does not move it and addressing stays
        the last step, and it gets its own current ONVIF user back, so no
        credential from a reference camera is pushed onto this one. The real
        ONVIF password is set afterwards by its own step.
        """
        try:
            live = self.export_config()
        except CameraError as e:
            raise CameraError(
                f"The profile is missing section(s) the device requires, and its "
                f"current configuration could not be read to supply them: {e}")
        missing = [s for s in live if s not in data]
        if not missing:
            return data
        log.info("Filling %d section(s) the profile does not carry from the "
                 "camera's own current config: %s.",
                 len(missing), ", ".join(sorted(missing)))
        return {**data, **{s: live[s] for s in missing}}

    # --- NTP ----------------------------------------------------------------
    def set_ntp(self, server: str, port: int = 123, update_period: int = 60) -> None:
        log.info("Setting NTP server to %s ...", server)
        self._request("PUT", "/v1/system/local/ntp/client",
                      body={"Address": server, "Enable": True, "Port": int(port),
                            "UpdatePeriod": int(update_period)})
        log.info("NTP set.")

    def read_ntp(self) -> dict:
        data = self._request("GET", "/v1/system/local/ntp/client",
                             params={"default": "false"}) or {}
        if not isinstance(data, dict):
            data = {}
        return {"address": data.get("Address", ""), "enable": bool(data.get("Enable"))}

    def sync_time_to_pc(self) -> None:
        """Set the camera clock to this PC's current local time — the web UI's
        'Sync to PC time' button.

        The timezone is deliberately not sent. The vendor doc spells that field
        `TimeZone` in its field table but `Timezone` in its own example, and a
        real export uses `Timezone`; sending the wrong spelling would either be
        ignored or reset the zone the profile just set. The wall clock is what
        this step is for, and the zone is the profile's business.

        The reply carries a REPLACEMENT token — changing the clock invalidates
        the one we hold, since a token encodes an expiry — so it is captured
        here. Missing that is a delayed failure: the next call would fail on an
        expired token several steps later, nowhere near the cause.
        """
        now = datetime.now()
        log.info("Syncing the camera clock to PC time (%s) ...",
                 now.strftime("%Y-%m-%d %H:%M:%S"))
        data = self._request("PUT", "/v1/system/local/time",
                             body={"Year": now.year, "Month": now.month, "Day": now.day,
                                   "Hour": now.hour, "Minute": now.minute,
                                   "Second": now.second})
        self.token = self._token_from(data) or self.token
        log.info("Camera clock set to %s.", now.strftime("%Y-%m-%d %H:%M:%S"))

    # --- addressing hooks ---------------------------------------------------
    def _network(self) -> tuple[dict, dict]:
        """The device's TCP/IP config and the default interface's card inside it
        — both mutable, ready to hand back. The card is created if the device
        didn't report one."""
        data = self._request("GET", "/v1/netapp/network", params={"default": "false"})
        if not isinstance(data, dict):
            data = {}
        iface = data.get("DefaultInterface") or "eth0"
        cards = data.get("Card")
        if not isinstance(cards, list):
            cards = []
            data["Card"] = cards
        card = next((c for c in cards
                     if isinstance(c, dict) and c.get("Name") == iface), None)
        if card is None:
            # Fall back to the first card the device does report: a unit with one
            # interface whose Name disagrees with DefaultInterface is still
            # unambiguous, and inventing an empty card would wipe its addressing.
            card = next((c for c in cards if isinstance(c, dict)), None)
        if card is None:
            card = {"Name": iface}
            cards.append(card)
        data.setdefault("DefaultInterface", iface)
        return data, card

    def read_network(self) -> NetworkView:
        data, card = self._network()
        return NetworkView(
            iface=card.get("Name") or data.get("DefaultInterface") or "eth0",
            ip=card.get("IPAddress", "") or "",
            netmask=card.get("SubnetMask", "") or "",
            gateway=card.get("Gateway", "") or "",
            dhcp=bool(card.get("DHCPEnable")),
            mac=card.get("PhysicalAddress", "") or "",
        )

    def apply_network(self, *, dhcp: bool, ip: str = "", netmask: str = "",
                      gateway: str = "") -> None:
        data, card = self._network()
        card["DHCPEnable"] = dhcp
        if not dhcp:
            card["IPAddress"] = ip
            card["SubnetMask"] = netmask
            card["Gateway"] = gateway
        self._request("PUT", "/v1/netapp/network", body=data)

    # --- ONVIF (a SEPARATE credential store from the system user) -----------
    # ONVIF keeps its own credential, distinct from the system/web account, on
    # this generation exactly as on the older one — so the password change above
    # does NOT touch it and it has to be set explicitly.
    #
    # What is different is the effort: these cameras expose ONVIF user management
    # as two plain JSON calls, which replaces the whole hand-rolled SOAP /
    # WS-Security digest block the RPC2 client needs.
    def _onvif_users(self) -> list[dict]:
        """The ONVIF users, with their passwords in PLAINTEXT.

        Read from the config export rather than from GET /v1/netapp/onvif/user,
        which is the obvious endpoint and the wrong one: it returns each password
        AES-encrypted, in the same form the wire uses, while the export returns
        it as it is. Comparing against the encrypted form would work too, but
        only by re-deriving an encoding the vendor documents nowhere; the export
        is what the bench measured against a live camera, so it is what this
        trusts. The export is the larger read, and this runs a handful of times
        per camera, so the cost does not signify.
        """
        export = self.export_config()
        users = (export.get("OnvifUser") or {}).get("User") or []
        return [u for u in users if isinstance(u, dict)]

    def _onvif_users_live(self) -> list[dict]:
        """The ONVIF users as the device holds them RIGHT NOW, passwords in the
        AES form (confirmed against camera CB6280156: this endpoint answered
        `AES(password)` for the same account the export reported in plaintext).

        Distinct from `_onvif_users`, which reads the export, because the two
        disagree after a config import: the export reports the SAVED
        configuration, and once a file has been imported that is the imported
        file rather than what the device is running. So a password set after an
        import reads back stale from the export and current from here.
        """
        data = self._request("GET", "/v1/netapp/onvif/user", raise_on_error=False)
        return [u for u in (data or []) if isinstance(u, dict)]

    def onvif_check(self, password: str) -> tuple[bool, str]:
        """Whether the ONVIF admin user is on `password`.

        Read back rather than authenticated against: this API hands the ONVIF
        credential straight back to an authenticated caller, so there is no
        separate ONVIF login to make. The password itself must never reach the
        returned detail — verification rows go to bench-central verbatim
        (TEC-349) — so the row says whether it matches, never what it is.

        Asked of the LIVE user list rather than the export, because after a
        config import the export answers from the imported file and would report
        a password set afterwards as still missing.
        """
        try:
            users = self._onvif_users_live()
        except CameraError as e:
            return False, f"ONVIF user list could not be read ({e})"
        if not users:
            return False, "the device reports no ONVIF users"
        me = next((u for u in users if u.get("Name") == self.username), None)
        if me is None:
            names = ", ".join(str(u.get("Name")) for u in users) or "?"
            return False, f"no ONVIF user '{self.username}' (found: {names})"
        held = me.get("Password")
        if held == encrypt_password(password):
            return True, f"set (ONVIF users: {', '.join(str(u.get('Name')) for u in users)})"
        if not held or held == encrypt_password(""):
            # Worth saying outright rather than as "a different password": an
            # ONVIF account with no password at all is a way in, not a
            # misconfiguration, and it is what sending an unencrypted password
            # to changepwd used to leave behind.
            return False, "ONVIF has NO password set"
        return False, "ONVIF is on a different password"

    def set_onvif_password(self, new_password: str, current_candidates: list[str]) -> None:
        """Set the ONVIF admin user's password. Idempotent: if it is already the
        target, do nothing.

        `current_candidates` is unused on this generation and kept only because
        it is part of the shared client interface: the RPC2 path has to
        authenticate to ONVIF before it can change anything, so it must guess
        which password ONVIF is currently on. Here the change is authorised by
        the X-Token we already hold, so the current ONVIF password is irrelevant.

        The user is created if the device has no ONVIF admin at all, which is the
        one case `changepwd` cannot serve.
        """
        if self.read_only:
            raise MutationBlocked("read-only client refused an ONVIF password change")
        ok, _detail = self.onvif_check(new_password)
        if ok:
            log.info("ONVIF password already set to the target; skipping.")
            return
        users = self._onvif_users_live()
        exists = any(u.get("Name") == self.username for u in users)
        log.info("Setting the ONVIF admin password ...")
        # The password goes on the wire ENCRYPTED, exactly as the login and the
        # admin-password change send theirs. The vendor doc says otherwise — its
        # example body is a plaintext "admin123" — and following it is silently
        # destructive: the device decrypts whatever arrives, so a plaintext
        # password decrypts to nothing and the account is left with NO PASSWORD
        # while the call still answers Code 200. Measured on a live camera
        # (CB6280156): sending the encrypted form sets the password, sending the
        # plaintext form empties it.
        if exists:
            self._request("PUT", "/v1/netapp/onvif/changepwd",
                          body={"Name": self.username,
                                "NewPassword": encrypt_password(new_password)})
        else:
            log.info("No ONVIF user '%s' on the device — creating it.", self.username)
            self._request("POST", "/v1/netapp/onvif/add",
                          body={"Group": 1, "Name": self.username,
                                "Password": encrypt_password(new_password)})
        ok, detail = self.onvif_check(new_password)
        if not ok:
            raise CameraError(f"ONVIF password did not take: {detail}")
        log.info("ONVIF password set (%s).", detail)


__all__ = ["RaythinkRestClient", "TOKEN_REFRESH_SEC", "V2_MARKER_SECTIONS",
           "LEGACY_MARKER_SECTIONS", "encrypt_password"]
