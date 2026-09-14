#!/usr/bin/env python3
"""
Provision-ISR IP speaker device client (session-cookie CGI API).

A fresh speaker boots on DHCP with admin/123456 and serves a jQuery web UI on
HTTP port 80. Everything the UI does goes through one CGI endpoint (verified
live against a TM-CS20 unit, firmware V3.3.39-PR1):

  * Login:   POST /cgi-bin/CGI?login   form: username, password=MD5-hex(pw)
             -> {"result":0, "reason":"OK", "pid":101, "changepwd":0} plus a
             `session` cookie used by every later call. changepwd==85 means the
             device forces a first-login password change (changepwd.set).
  * Reads:   GET  /cgi-bin/CGI?config=<name>.get   (overview, datetime,
             network, musicfile, ...) -> {"result":0, "reason":"OK", "data":{}}
  * Writes:  POST /cgi-bin/CGI?config=<name>.set   with form fields.
  * Upload:  multipart POST /cgi-bin/mediaupload?idx=<0-9>, field "upload<idx>"
             (.mp3/.wav); success == "OK" somewhere in the response body.
  * result -500 on any call means the session cookie expired (re-login);
    -600 from security.set means the old username/password check failed.

Passwords cross the wire as plain MD5 hex — that is the device's design (its
own web UI does the same via jquery.md5.js), acceptable on a direct bench link.

The per-device pipeline lives in speaker_configure.py; the bench web UI in
speaker_app.py.
"""
from __future__ import annotations

import hashlib
import logging
import os
import socket
import sys
import time
from typing import Optional

try:
    import requests
except ImportError:
    sys.exit("This script needs 'requests'.  Install it with:  pip install requests")

from bench_core import (
    MutationBlocked,
    find_ip_by_mac,
    format_verification,
    host_iface_for,
    install_log_context,
    renew_host_dhcp,
    set_log_serial,
)

# All device-talking steps log through this named logger so the bench UI's
# StepCollector and rolling file handler pick them up (same pattern as the
# "teltonika"/"raythink" loggers).
log = logging.getLogger("speaker")
install_log_context(log)


# --- Provision-ISR speaker factory defaults ----------------------------------
DEFAULT_USERNAME = "admin"
DEFAULT_SCHEME = "http"
DEFAULT_INITIAL_PASSWORD = "123456"
DEFAULT_NTP_SERVER = "192.168.88.10"
DEFAULT_STATIC_IP = "192.168.88.70"
DEFAULT_GATEWAY = "192.168.88.1"
DEFAULT_NETMASK = "255.255.255.0"

# The unauthenticated landing page's <title> — the detection signature that
# tells a speaker apart from every other device answering HTTP on the subnet.
DETECT_SIGNATURE = "<title>IP Speaker</title>"

# result codes (from the device's own JS)
RESULT_OK = 0
RESULT_SESSION_TIMEOUT = -500
RESULT_BAD_CREDENTIALS = -600
# login -> changepwd values
CHANGEPWD_FORCED = 85


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


class SpeakerError(Exception):
    """A hard, run-aborting device error (login, password, network)."""


class SpeakerClient:
    """One Provision-ISR speaker over its CGI API. Holds the login session
    cookie; re-logs-in transparently when the device reports it expired."""

    def __init__(self, host: str, username: str = DEFAULT_USERNAME,
                 scheme: str = DEFAULT_SCHEME, timeout: int = 15):
        # `host` may carry a port ("127.0.0.1:8080" on a dev port-forward).
        self.host = host
        self.username = username
        self.scheme = scheme
        self.timeout = timeout
        self.base = f"{scheme}://{host}"
        self.s = requests.Session()
        self.password: Optional[str] = None
        self.forced_changepwd = False   # login said changepwd==85
        # A verify-only run must not be able to change the speaker even by
        # accident (TEC-348). Every write on this device is a `?config=*.set`
        # POST or the media upload, so one gate on cgi_post plus one on the
        # upload covers the whole surface — reads are GETs and login is a POST
        # to ?login, which is not a config write.
        self.read_only = False

    def set_read_only(self) -> None:
        """Refuse every write from here on. One-way on purpose: nothing in a
        verify run has a reason to turn it back off."""
        self.read_only = True
        log.info("Client is now read-only — any write will be refused.")

    # --- transport ------------------------------------------------------------
    def _cgi_url(self, query: str) -> str:
        return f"{self.base}/cgi-bin/CGI?{query}"

    def _check(self, r: "requests.Response", what: str, *, relogin: bool = True) -> dict:
        try:
            data = r.json()
        except ValueError:
            raise SpeakerError(f"{what}: non-JSON reply (HTTP {r.status_code}): {r.text[:160]}")
        if data.get("result") == RESULT_SESSION_TIMEOUT and relogin and self.password:
            log.info("Session expired mid-run — logging in again.")
            self.login(self.password)
            raise _Retry()
        return data

    def cgi_get(self, config_name: str, what: str = "") -> dict:
        """GET ?config=<name>.get — returns the `data` dict. Raises SpeakerError
        on a device-reported failure; re-logs-in once on a -500 session drop."""
        what = what or config_name
        for attempt in (1, 2):
            try:
                r = self.s.get(self._cgi_url(f"config={config_name}"),
                               params={"t": int(time.time() * 1000)}, timeout=self.timeout)
            except requests.exceptions.RequestException as e:
                raise SpeakerError(f"{what}: connection failed ({e})")
            try:
                data = self._check(r, what, relogin=(attempt == 1))
            except _Retry:
                continue
            if data.get("result") != RESULT_OK:
                raise SpeakerError(f"{what} failed: result={data.get('result')} "
                                   f"reason={data.get('reason', '')}")
            return data.get("data", {}) or {}
        raise SpeakerError(f"{what}: still rejected after re-login.")

    def cgi_post(self, query: str, form: dict, what: str = "") -> dict:
        """POST ?<query> with form fields — returns the full JSON reply. Raises
        SpeakerError unless result==0; re-logs-in once on a -500 session drop."""
        what = what or query
        # Every cgi_post this client makes is a `?config=*.set` write. Refusing
        # the whole method rather than matching `.set` means a POST added later
        # has to be thought about rather than silently allowed through.
        if self.read_only:
            raise MutationBlocked(f"read-only client refused a write: {query}")
        for attempt in (1, 2):
            try:
                r = self.s.post(self._cgi_url(query), data=form, timeout=self.timeout)
            except requests.exceptions.RequestException as e:
                raise SpeakerError(f"{what}: connection failed ({e})")
            try:
                data = self._check(r, what, relogin=(attempt == 1))
            except _Retry:
                continue
            if data.get("result") != RESULT_OK:
                raise SpeakerError(f"{what} failed: result={data.get('result')} "
                                   f"reason={data.get('reason', '')}")
            return data
        raise SpeakerError(f"{what}: still rejected after re-login.")

    # --- auth -------------------------------------------------------------------
    def login(self, password: str) -> dict:
        """Login and store the session cookie + working password. Remembers
        whether the device demands a first-login password change (changepwd==85).
        Raises SpeakerError on failure."""
        try:
            r = self.s.post(self._cgi_url("login"),
                            data={"username": self.username,
                                  "password": md5_hex(password)},
                            timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            raise SpeakerError(f"login: connection failed ({e})")
        try:
            data = r.json()
        except ValueError:
            raise SpeakerError(f"login: non-JSON reply (HTTP {r.status_code}): {r.text[:160]}")
        if data.get("result") != RESULT_OK:
            raise SpeakerError(f"Login failed for '{self.username}' "
                               f"(result={data.get('result')}).")
        self.password = password
        self.forced_changepwd = (data.get("changepwd") == CHANGEPWD_FORCED)
        log.info("Logged in as '%s'%s.", self.username,
                 " (device forces a password change)" if self.forced_changepwd else "")
        return data

    def relogin(self, candidates: list[str], settle: int = 0) -> None:
        """Re-establish a session, trying each candidate password in turn.
        Raises SpeakerError only if none work."""
        if settle:
            time.sleep(settle)
        last = None
        for pw in [p for p in candidates if p]:
            try:
                self.login(pw)
                return
            except SpeakerError as e:
                last = e
        raise SpeakerError(f"Could not re-login after the previous step: {last}")

    def close(self) -> None:
        self.s.close()

    # --- identity ---------------------------------------------------------------
    def get_overview(self) -> dict:
        """The overview.get payload: uid, serialnumber, version, mac, netip,
        netmask, gateway, musicfreespace, ... Tags the log with the serial."""
        data = self.cgi_get("overview.get", "overview")
        serial = data.get("serialnumber") or data.get("uid") or ""
        set_log_serial(serial or None)
        return data

    def get_identity(self) -> dict:
        data = self.get_overview()
        identity = {
            "serial": data.get("serialnumber") or data.get("uid") or "unknown",
            "mac": (data.get("mac") or "unknown").lower(),
            "model": "Provision-ISR speaker",
            "firmware": data.get("version") or "unknown",
            "ip": data.get("netip") or self.host,
            "raw": {"overview": data},
        }
        log.info("Identity: serial=%s MAC=%s fw=%s ip=%s",
                 identity["serial"], identity["mac"], identity["firmware"], identity["ip"])
        return identity

    # --- password -----------------------------------------------------------------
    def set_password(self, new_password: str) -> None:
        """Change the admin password to `new_password` and re-login under it.
        Idempotent: if we already authenticated with it, do nothing. Uses the
        forced first-login endpoint (changepwd.set) when the device demands it,
        else the normal security.set. Raises SpeakerError on failure."""
        if self.password == new_password:
            log.info("Password already set to the target; skipping.")
            return
        old = self.password or ""
        log.info("Changing the admin password ...")
        if self.forced_changepwd:
            self.cgi_post("config=changepwd.set",
                          {"password": md5_hex(old),
                           "new_password": md5_hex(new_password),
                           "new_password_org": new_password},
                          "changepwd.set")
        else:
            data = None
            try:
                data = self.cgi_post("config=security.set",
                                     {"username": self.username,
                                      "password": md5_hex(old),
                                      "new_username": self.username,
                                      "new_password": md5_hex(new_password),
                                      "new_password_org": new_password},
                                     "security.set")
            except SpeakerError as e:
                if str(RESULT_BAD_CREDENTIALS) in str(e):
                    raise SpeakerError("security.set rejected the current "
                                       "username/password (result -600).")
                raise
            if data and data.get("reboot") == 1:
                log.info("Device says it wants a reboot after the password change.")
        self.forced_changepwd = False
        # Prove the new password works (and refresh the session under it).
        self.relogin([new_password], settle=1)
        log.info("Password changed.")

    # --- NTP ------------------------------------------------------------------------
    def get_datetime(self) -> dict:
        return self.cgi_get("datetime.get", "datetime")

    def set_ntp(self, server: str, *, timezone: int = 720, interval: int = 10,
                port: str = "") -> None:
        """Point the device at `server` for NTP (timesetmode 0). `timezone` is
        the device's encoding: 720 + UTC-offset-in-minutes (840 = GMT+2)."""
        log.info("Setting NTP server to %s (timezone=%s, interval=%s) ...",
                 server, timezone, interval)
        self.cgi_post("config=datetime.set",
                      {"timezone": timezone,
                       "timesetmode": 0,
                       "ntpserverstr": server,
                       "ntpport": port,
                       "ntpinterval": interval,
                       "manualtime": ""},
                      "datetime.set")
        log.info("NTP set.")

    # --- media files ------------------------------------------------------------------
    def get_music_files(self) -> list[dict]:
        """The user-file slots: [{"name": ..., "description": <filename>}, ...]."""
        data = self.cgi_get("musicfile.get", "musicfile")
        return data.get("musicfile", []) or []

    def upload_media(self, path: str, idx: int = 0, *, upload_timeout: int = 60) -> None:
        """Upload a .mp3/.wav to user-file slot `idx` (0-9). The device's free
        space is tiny (~4 MB), so we check it first. Success is the literal
        "OK" the CGI prints. Raises SpeakerError on failure."""
        if self.read_only:
            raise MutationBlocked("read-only client refused a media upload")
        if not os.path.isfile(path):
            raise SpeakerError(f"media file not found: {path}")
        name = os.path.basename(path)
        ext = os.path.splitext(name)[1].lower()
        if ext not in (".mp3", ".wav"):
            raise SpeakerError(f"media file must be .mp3 or .wav (got '{name}').")
        size = os.path.getsize(path)

        free = self._music_free_space()
        if free is not None and size > free:
            raise SpeakerError(f"media file is {size} bytes but the device only has "
                               f"{free} bytes free — delete old files first.")

        log.info("Uploading media file '%s' (%d bytes) to slot %d ...", name, size, idx)
        mime = "audio/mpeg" if ext == ".mp3" else "audio/wav"
        try:
            with open(path, "rb") as f:
                r = self.s.post(f"{self.base}/cgi-bin/mediaupload",
                                params={"idx": idx},
                                files={f"upload{idx}": (name, f, mime)},
                                timeout=upload_timeout)
        except requests.exceptions.RequestException as e:
            raise SpeakerError(f"media upload: connection failed ({e})")
        if "OK" not in (r.text or ""):
            raise SpeakerError(f"media upload rejected (HTTP {r.status_code}): "
                               f"{(r.text or '')[:160]}")
        log.info("Media file uploaded.")

    def _music_free_space(self) -> Optional[int]:
        """Free bytes for user music files, or None when unreadable."""
        try:
            raw = self.get_overview().get("musicfreespace", "")
            return int(str(raw).strip())
        except (SpeakerError, ValueError):
            return None

    # --- network (static IP — LAST: drops the connection) -----------------------------
    def get_network(self) -> dict:
        return self.cgi_get("network.get", "network")

    def set_static_ip(self, ip: str, netmask: str, gateway: str,
                      dns1: str = "", dns2: str = "", wait: int = 120) -> dict:
        """Move the speaker from its DHCP address to a static IP. The change
        drops the connection by design; we then confirm the device answers on
        the NEW address (renewing the host's DHCP lease so the bench PC can
        follow a subnet change). Returns a verification-style check; never
        raises after the write (the device is moving whether we can still see
        it or not)."""
        iface_host = host_iface_for(self.host.split(":")[0])  # resolve while reachable
        log.info("Moving the speaker from %s to static %s — the connection may drop ...",
                 self.host, ip)
        try:
            self.cgi_post("config=network.set",
                          {"dhcp": 0, "netip": ip, "netmask": netmask,
                           "gateway": gateway, "dns1": dns1, "dns2": dns2},
                          "network.set")
        except SpeakerError as e:
            # The reply may never arrive (the IP changes mid-request) — treat a
            # transport error as "the move started". A device-reported failure
            # (a JSON error) is real and fails the check.
            if "connection failed" not in str(e):
                log.error("network.set rejected: %s", e)
                return {"item": "static IP", "expected": ip,
                        "actual": f"device rejected the change: {e}", "ok": False}
            log.info("Connection dropped applying the IP (expected): %s", e)

        # Follow the device to its new address (plain port 80 — the bench case;
        # a dev port-forward host can't be followed, which shows up as ok=False).
        self.host = ip
        self.base = f"{self.scheme}://{ip}"
        deadline = time.time() + wait
        time.sleep(3)
        renew_host_dhcp(iface_host)
        renewed_again = False
        while time.time() < deadline:
            if self.port_open():
                log.info("Speaker is answering on %s.", ip)
                return {"item": "static IP", "expected": ip,
                        "actual": f"answering on {ip}", "ok": True}
            if not renewed_again and time.time() > deadline - wait / 2:
                renew_host_dhcp(iface_host)
                renewed_again = True
            time.sleep(3)
        log.warning("Speaker did not answer on %s within %ds — it may still be fine; "
                    "check the bench PC has an address in that subnet.", ip, wait)
        return {"item": "static IP", "expected": ip,
                "actual": f"no answer on {ip} after {wait}s (bench PC subnet?)", "ok": False}

    def set_dhcp(self, *, mac: str, subnets: list[str], wait: int = 300) -> dict:
        """Leave the speaker on DHCP — the alternative last step to
        `set_static_ip`, for a site whose own DHCP server owns the address
        (TEC-848).

        A speaker ARRIVES on DHCP, so this is not always a change: a unit still
        on its factory lease is only being told to stay there. It is written
        anyway, because "arrived on DHCP" and "configured for DHCP" are not the
        same claim to make in a run record — a unit re-run after an earlier
        static provision is on `dhcp: 0` and really does have to be moved back.

        Nothing here picks the address, so the speaker is found again by MAC:
        the same trick the camera tool uses, and the reason a run whose MAC
        could not be read is refused before it starts. Returns a
        verification-style row; never raises after the write.
        """
        row = {"item": "DHCP", "expected": "a lease from the site's DHCP server"}
        was = self.host
        log.info("Leaving the speaker on DHCP — the address may change from %s, "
                 "so it will be found again by its MAC.", was)
        try:
            self.cgi_post("config=network.set", {"dhcp": 1}, "network.set")
        except SpeakerError as e:
            if "connection failed" not in str(e):
                log.error("network.set rejected: %s", e)
                return {**row, "actual": f"device rejected the change: {e}",
                        "ok": False}
            log.info("Connection dropped applying DHCP (expected): %s", e)

        if not mac:
            # Refused up front by the UI; reachable from the CLI, where saying
            # so beats silently reporting the old address as the new one.
            return {**row, "actual": "the speaker's MAC is unknown, so it could "
                                     "not be found again after the change",
                    "ok": False}

        deadline = time.time() + wait
        time.sleep(3)
        while time.time() < deadline:
            found = find_ip_by_mac(mac, subnets, port=self._port())
            if found:
                log.info("Speaker is answering on %s.", found)
                self.host = found if self._port() == 80 else f"{found}:{self._port()}"
                self.base = f"{self.scheme}://{self.host}"
                return {**row, "actual": f"answering on {found}", "ok": True}
            time.sleep(5)
        log.warning("Speaker was not found by MAC %s on %s within %ds.",
                    mac, ", ".join(subnets), wait)
        return {**row,
                "actual": f"not found by MAC on {', '.join(subnets)} after "
                          f"{wait}s — is this PC on the subnet it leased from?",
                "ok": False}

    # --- reachability -------------------------------------------------------------------
    def _port(self) -> int:
        if ":" in self.host:
            return int(self.host.rsplit(":", 1)[1])
        return 443 if self.scheme == "https" else 80

    def port_open(self, host: Optional[str] = None) -> bool:
        target = (host or self.host).split(":")[0]
        try:
            with socket.create_connection((target, self._port()), timeout=3):
                return True
        except OSError:
            return False

    # --- verification ---------------------------------------------------------------------
    def verify_configuration(self, *, new_password: str, ntp_server: str,
                             ip: str, netmask: str, gateway: str,
                             media_name: str = "", media_slot: int = 0,
                             dhcp: bool = False) -> list[dict]:
        """Re-read the settings we changed and confirm they took. Runs AFTER the
        address step (we are already re-pointed at wherever the speaker went).
        Returns {item, expected, actual, ok} rows.

        `dhcp` swaps the three address rows for one (TEC-848). Under DHCP the
        bench chose no address, so checking one would be checking the site's
        DHCP server rather than this speaker — the honest question is only
        whether the speaker is set to ask, and the lease it happens to hold is
        reported alongside for the record.
        """
        checks: list[dict] = []

        def add(item, expected, actual, ok):
            checks.append({"item": item, "expected": expected, "actual": actual, "ok": ok})

        # Password: we are authenticated on new_password (re-logged-in under it).
        # Neither side of this row may carry an actual password — these rows go
        # to bench-central verbatim, and `actual` on a failure would be the
        # password the speaker is still on (TEC-349).
        on_target = self.password == new_password
        add("admin password", "the shared password",
            "in use" if on_target else "NOT set — the speaker is still on another "
                                       "password",
            on_target)

        try:
            dt = self.get_datetime()
            got = dt.get("ntpserverstr", "")
            add("NTP server", ntp_server,
                f"{got} (mode={dt.get('timesetmode')})",
                got == ntp_server and str(dt.get("timesetmode")) == "0")
        except SpeakerError as e:
            add("NTP server", ntp_server, f"read failed: {e}", False)

        if media_name:
            try:
                files = self.get_music_files()
                slot = files[media_slot] if media_slot < len(files) else {}
                got = slot.get("name") or slot.get("description") or ""
                add("media file", media_name, got or "(slot empty)",
                    bool(got) and os.path.splitext(media_name)[0] in got)
            except SpeakerError as e:
                add("media file", media_name, f"read failed: {e}", False)

        try:
            net = self.get_network()
            got_ip = net.get("netip", "")
            on_dhcp = net.get("dhcp")
            if dhcp:
                add("DHCP", "enabled",
                    f"dhcp={on_dhcp} (currently leased {got_ip or 'nothing'})",
                    str(on_dhcp) == "1")
            else:
                add("static IP", ip, f"{got_ip} (dhcp={on_dhcp})",
                    got_ip == ip and str(on_dhcp) == "0")
                add("netmask", netmask, net.get("netmask", "?"),
                    net.get("netmask") == netmask)
                add("gateway", gateway, net.get("gateway", "?"),
                    net.get("gateway") == gateway)
        except SpeakerError as e:
            add("DHCP" if dhcp else "static IP", "enabled" if dhcp else ip,
                f"read failed: {e}", False)

        return checks


class _Retry(Exception):
    """Internal: the call was retried after a transparent re-login."""


# format_verification is re-exported from bench_core (identical report).
