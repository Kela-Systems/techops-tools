#!/usr/bin/env python3
"""
Shared Raythink camera-client machinery — everything that does not depend on
which API the camera on the bench speaks.

Raythink ships two generations of thermal camera with entirely different control
protocols. The older units speak the Dahua-OEM RPC2 JSON API
(`raythink_camera.py`); the newer ones speak a REST API under /v1 with an
X-Token header (`raythink_rest.py`). They are told apart by
`raythink_client.detect_generation`.

What the two do NOT differ in is the hard part of addressing a camera on a
bench. The address change drops the connection by design — we are talking to the
camera over the very interface we are moving — so finding it again is host-side
work: renew the laptop's own lease, sweep the subnets, read the ARP cache,
distinguish "took an address but isn't serving yet" from "never appeared". That
logic is long, was arrived at from real bench failures, and is identical
whichever protocol answers. Duplicating it per generation is how the two copies
would drift apart, so it lives here once, over four small per-generation hooks:

    read_network()  -> NetworkView            the addressing the device holds
    apply_network() -> None                   write it back (reply may not come)
    read_ntp()      -> {"address", "enable"}
    onvif_check()   -> (ok, detail)

Everything above the client — the pipeline in `raythink_configure.py`, the bench
UI in `raythink_app.py` — talks to the union of this base and the per-generation
methods marked "override" below, and never learns which generation it got.
"""
from __future__ import annotations

import logging
import socket
import sys
import time
from dataclasses import dataclass
from typing import Optional

try:
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    sys.exit("This script needs 'requests'.  Install it with:  pip install requests")

from bench_core import (
    MutationBlocked,
    arp_table,
    canonical_mac,
    find_ip_by_mac,
    format_verification,
    host_iface_for,
    install_log_context,
    renew_host_dhcp,
    set_log_serial,
)

# All device-talking steps log through this named logger so the bench UI's
# StepCollector and rolling file handler pick them up (same pattern as the
# Teltonika client's "teltonika" logger). The context filter (shared serial +
# lowercased level) and set_log_serial come from bench_core. One logger for both
# generations: an operator reading the step log cares what happened, not which
# API it happened over.
log = logging.getLogger("raythink")
install_log_context(log)


# --- Raythink factory defaults ----------------------------------------------
# Both generations ship on the same address with the same credentials.
DEFAULT_HOST = "192.168.1.123"
DEFAULT_USERNAME = "admin"
DEFAULT_SCHEME = "http"
DEFAULT_INITIAL_PASSWORD = "admin"
DEFAULT_NEW_PASSWORD = "Kelafield123!"
DEFAULT_NTP_SERVER = "192.168.88.10"

# Pacing of the hunt for a camera that was just switched to DHCP (set_dhcp).
DHCP_SETTLE_SEC = 10        # before looking: let it drop its old address
DHCP_HOST_RENEW_SEC = 45    # re-renew this PC's own lease while waiting
DHCP_PROGRESS_SEC = 20      # how often to log that we are still looking


# The two camera generations. Named here rather than in `raythink_client`, which
# is where they are chosen between, so that each client class can declare its own
# without importing the module that imports it.
GEN_RPC2 = "rpc2"
GEN_REST = "rest"
GENERATIONS = (GEN_RPC2, GEN_REST)

# Human-readable, for log lines and the UI. "Older"/"newer" rather than version
# numbers on purpose: an operator at the bench knows which box is which by age,
# not by a firmware string they would have to go and read.
GENERATION_LABELS = {
    GEN_RPC2: "older (RPC2)",
    GEN_REST: "newer (REST /v1)",
}


class CameraError(Exception):
    """A hard, run-aborting device error (login, password, network)."""


class CameraUnreachable(CameraError):
    """The request never got an answer — a refused, reset or timed-out
    connection, as opposed to the device replying and saying no.

    Separate from CameraError because the two mean opposite things right after a
    reboot: a device that ANSWERS "wrong password" will still say that however
    long we wait, while one that resets the connection is very likely still
    coming up and will be fine a second later. `relogin` retries only this one.
    """


@dataclass
class NetworkView:
    """The addressing a camera currently holds, normalised across generations.

    The two APIs agree on almost nothing here: RPC2 keeps a Network config table
    whose interface section may nest the address under `IPAddress` or flatten it,
    and may spell the DHCP flag `DhcpEnable` or `EnableDhcp`; REST keeps a `Card`
    array of interfaces. None of that shape reaches the addressing steps, which
    only ever need these six values.
    """
    iface: str = "eth0"
    ip: str = ""
    netmask: str = ""
    gateway: str = ""
    dhcp: bool = False
    mac: str = ""


# --- profile sanitising ------------------------------------------------------
#
# Sections a config profile must never carry into a camera. Both generations
# export their whole configuration, so an export taken from a reference camera
# contains things that belong to that camera rather than to the profile.
#
# The address, because the profile is imported in the MIDDLE of the pipeline
# while addressing is deliberately LAST: a profile that sets the reference
# camera's address moves the unit under us and the run loses it. The legacy
# convention was for whoever exported the profile to delete this by hand, which
# works right up until someone forgets.
#
# The ONVIF credential, because a v2 export carries it in PLAINTEXT
# (`OnvifUser.User[].Password`) and these profiles are committed. The tool sets
# the ONVIF password itself (step 4b), so dropping it costs nothing.
PROFILE_DROP_SECTIONS = {
    # v2 (REST) export section names.
    "NetworkInfo": "the tool sets the address itself, as the last step",
    "OnvifUser": "the tool sets the ONVIF credential itself, and a raw export "
                 "carries it in plaintext",
    # Legacy (RPC2) config-table name for the same addressing problem.
    "Network": "the tool sets the address itself, as the last step",
}

_REDACTED = ""


def sanitize_profile(data: dict, *, secrets: tuple[str, ...] = ()) -> tuple[dict, list[str]]:
    """A committable, importable copy of a config profile, plus a note per change.

    Two rules, deliberately narrow — a profile is hundreds of device-normalised
    fields and a sanitiser that rewrites more than it must would silently change
    what the bench ships:

    1. Drop the sections in PROFILE_DROP_SECTIONS outright (see there).
    2. Blank any string anywhere whose value is one of `secrets` — the station's
       shared password. A secret is a secret wherever it appears, so this matches
       on the value rather than the key name.

    Note what rule 2 does NOT do: blank every password-looking field. The
    existing legacy profiles legitimately carry factory placeholders
    (`Email.Password: "none"`, `WLan.eth2.EAP.Password: "admin"`), and blanking
    those would change what a legacy camera gets configured with. Only the
    station password is a leak; factory defaults are public.
    """
    notes: list[str] = []
    wanted = {s for s in secrets if s}

    def scrub(node):
        if isinstance(node, dict):
            return {k: scrub(v) for k, v in node.items()}
        if isinstance(node, list):
            return [scrub(v) for v in node]
        return _REDACTED if isinstance(node, str) and node in wanted else node

    cleaned: dict = {}
    for name, section in data.items():
        why = PROFILE_DROP_SECTIONS.get(name)
        if why:
            notes.append(f"dropped '{name}' — {why}")
            continue
        cleaned[name] = scrub(section)

    if wanted:
        redacted = _count_redacted(data, cleaned)
        if redacted:
            notes.append(f"blanked {redacted} field(s) holding the station password")
    return cleaned, notes


def _count_redacted(before: dict, after: dict) -> int:
    """How many string values `sanitize_profile` blanked, for the note it logs.
    Counted by comparing the two trees rather than tracked during the walk, so
    the walk itself stays a plain expression."""
    def strings(node):
        if isinstance(node, dict):
            for v in node.values():
                yield from strings(v)
        elif isinstance(node, list):
            for v in node:
                yield from strings(v)
        elif isinstance(node, str):
            yield node

    kept = {k: v for k, v in before.items() if k in after}
    return sum(1 for a, b in zip(strings(kept), strings(after)) if a != b)


def find_plaintext_passwords(data) -> list[tuple[str, str]]:
    """Every non-empty password-ish field in a profile, as (dotted path, value).

    Reported, never acted on: this is what a human reviews before committing a
    profile. `sanitize_profile` only blanks values it KNOWS are the station
    secret; everything else here may be a legitimate setting (a GB28181 SIP
    password, an SMTP login) that the site actually wants.
    """
    found: list[tuple[str, str]] = []

    def walk(node, path: list[str]) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, path + [str(k)])
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, path + [str(i)])
        elif isinstance(node, str) and node:
            key = (path[-1] if path else "").lower()
            if any(w in key for w in ("password", "passwd", "pwd")):
                found.append((".".join(path), node))

    walk(data, [])
    return found


class BaseRaythinkClient:
    """One Raythink camera, protocol-agnostic. Stateless between calls apart from
    the login session; never raises on a transport blip during the IP move (the
    device is leaving its address by then)."""

    # Which generation this subclass speaks. Read by the pipeline for its log
    # lines and the firmware cross-check, and by the run record.
    generation = ""

    # verify=False is intentional (bench): both APIs are plain HTTP by default on
    # a direct local link, so there is no TLS chain to validate.
    def __init__(self, host: str = DEFAULT_HOST, username: str = DEFAULT_USERNAME,
                 scheme: str = DEFAULT_SCHEME, verify: bool = False, timeout: int = 15):
        self.host = host
        self.username = username
        self.scheme = scheme
        self.timeout = timeout
        self.base = f"{scheme}://{host}"
        self.s = requests.Session()
        self.s.verify = verify
        self.password: Optional[str] = None
        # A verify-only run must not be able to change the camera even by
        # accident (TEC-348). How that is enforced is per-generation — the two
        # protocols have very different notions of "a write" — so the subclass
        # gates on this flag; see `set_read_only`.
        self.read_only = False

    # --- read-only gate -----------------------------------------------------
    def set_read_only(self) -> None:
        """Refuse every write from here on. One-way on purpose: nothing in a
        verify run has a reason to turn it back off."""
        self.read_only = True
        log.info("Client is now read-only — any write will be refused.")

    # --- per-generation hooks (override) ------------------------------------
    def login(self, password: str) -> None:  # override
        raise NotImplementedError

    def get_identity(self) -> dict:  # override
        """serial / model / firmware / MAC, best-effort. Tags the log with the
        serial so later step lines carry it."""
        raise NotImplementedError

    def modify_password(self, new_password: str, old_password: str) -> None:  # override
        raise NotImplementedError

    def import_config(self, json_path: str) -> dict:  # override
        raise NotImplementedError

    def set_ntp(self, server: str, port: int = 123, update_period: int = 60) -> None:  # override
        raise NotImplementedError

    def sync_time_to_pc(self) -> None:  # override
        raise NotImplementedError

    def set_onvif_password(self, new_password: str,
                           current_candidates: list[str]) -> None:  # override
        raise NotImplementedError

    def read_network(self) -> NetworkView:  # override
        """The addressing the device currently reports. Raises CameraError if it
        cannot be read."""
        raise NotImplementedError

    def apply_network(self, *, dhcp: bool, ip: str = "", netmask: str = "",
                      gateway: str = "") -> None:  # override
        """Read-modify-write the device's addressing. Read-modify-write rather
        than write, always: both generations keep unrelated settings (DNS, the
        other interfaces) in the same structure, and a blind write would drop
        them.

        With `dhcp` the address arguments are ignored and the device's existing
        static values are left in place on purpose — the camera falls back to
        them if no lease ever arrives, which is a more useful failure than an
        unreachable camera with no address at all.

        May raise CameraError: the reply often never arrives, because the address
        changes mid-request. `_apply_network` treats that as "the move started".
        """
        raise NotImplementedError

    def read_ntp(self) -> dict:  # override
        """`{"address": str, "enable": bool}` as the device reports it."""
        raise NotImplementedError

    def onvif_check(self, password: str) -> tuple[bool, str]:  # override
        """Whether ONVIF accepts `password`, and a human-readable detail for the
        verification row. Never raises — a failure IS the answer."""
        raise NotImplementedError

    # --- reachability / relogin ---------------------------------------------
    def _port(self) -> int:
        return 443 if self.scheme == "https" else 80

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

    def relogin(self, candidates: list[str], settle: int = 0,
                retry_for: int = 20) -> None:
        """Re-establish a session, trying each candidate password in turn. Used
        after a password change or a config import that may reset the session.
        Raises CameraError only if none work.

        A connection that is REFUSED OR RESET is retried for `retry_for`
        seconds, because this is called just after a reboot and a camera that has
        started answering pings can still reset the first request or two while
        its web server finishes coming up — which is not a login failure and
        should not be reported as one. A device that answers and rejects the
        password is not retried: it will keep rejecting it, and on a device that
        locks an account after a few tries, retrying is actively harmful.
        """
        if settle:
            time.sleep(settle)
        deadline = time.monotonic() + max(0, retry_for)
        last = None
        while True:
            unreachable = False
            for pw in [p for p in candidates if p]:
                try:
                    self.login(pw)
                    return
                except CameraUnreachable as e:
                    last, unreachable = e, True
                    break          # nothing is answering; the password is moot
                except CameraError as e:
                    last = e
                    continue
            if not unreachable or time.monotonic() >= deadline:
                raise CameraError(f"Could not re-login after the previous step: {last}")
            time.sleep(2)

    def close(self) -> None:
        self.s.close()

    def _point_at(self, host: str) -> None:
        """Follow the camera to `host`. Both fields always move together, so this
        is the only place either is reassigned after construction."""
        self.host = host
        self.base = f"{self.scheme}://{host}"

    # --- addressing (LAST: drops the connection) ----------------------------
    def _apply_network(self, what: str, **changes) -> None:
        """Write the addressing back. The reply may never arrive — the address
        changes mid-request — so a transport error means 'the move started', not
        a failure."""
        if self.read_only:
            raise MutationBlocked(f"read-only client refused a write: {what}")
        try:
            self.apply_network(**changes)
        except CameraError as e:
            log.info("Connection dropped applying %s (expected): %s", what, e)

    def set_static_ip(self, ip: str, netmask: str, gateway: str,
                      wait: int = 120) -> dict:
        """Move the camera to a static IP. We are talking to it over that very
        interface, so the change drops the connection by design: we write the
        addressing, then confirm the device answers on the NEW address (renewing
        the host's DHCP lease so the laptop can follow). Returns a
        verification-style check; never raises after the write (the device is
        moving whether we can still see it or not)."""
        iface_host = host_iface_for(self.host)  # resolve while still reachable
        log.info("Moving the camera from %s to %s — the connection will drop ...",
                 self.host, ip)
        self._apply_network("the IP", dhcp=False, ip=ip, netmask=netmask,
                            gateway=gateway)

        # Follow the device to its new address.
        self._point_at(ip)
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

    def set_dhcp(self, *, mac: str, subnets: list[str], wait: int = 300) -> dict:
        """Switch the camera to DHCP — the alternative last step to
        set_static_ip, for a site where the camera is meant to take its address
        from the local DHCP server rather than a bench-assigned one.

        Same shape as the static move (write the addressing, expect the
        connection to drop), with one difference that drives everything else: we
        don't know where the camera reappears. So instead of following it to an
        address we chose, we sweep `subnets` for its `mac` until it turns up,
        then repoint the client at whatever it got.

        `wait` has to cover the camera rebooting, requesting a lease AND
        starting its web server, which is a lot longer than a static move where
        only the address changes. The wait is not silent — progress is logged, so
        an operator watching the step log can see it is still looking.

        Returns a verification-style check; never raises after the write."""
        left_behind = self.host
        iface_host = host_iface_for(self.host)  # resolve while still reachable
        log.info("Switching the camera at %s to DHCP — the connection will drop ...",
                 self.host)
        self._apply_network("DHCP", dhcp=True)

        row = {"item": "DHCP lease", "expected": "an address from the DHCP server"}
        if not mac:
            # Without a MAC there is nothing stable to search for: the camera is
            # on DHCP now, but this run can't say where or verify anything on it.
            log.error("No MAC known for this camera — cannot find it again after "
                      "the switch to DHCP.")
            return {**row, "actual": "switched to DHCP, but no MAC was known to "
                                     "find the camera again", "ok": False}

        log.info("Looking for the camera by MAC %s on %s (up to %ds — it has to "
                 "reboot, take a lease and start serving) ...",
                 mac, ", ".join(subnets) or "(no subnets configured)", wait)
        # Let it actually leave first: probed too early it can still be answering
        # on the address it is about to drop, which would look like "found it".
        time.sleep(DHCP_SETTLE_SEC)
        renew_host_dhcp(iface_host)

        deadline = time.time() + wait
        next_renew = time.time() + DHCP_HOST_RENEW_SEC
        next_note = time.time() + DHCP_PROGRESS_SEC
        while time.time() < deadline:
            found = find_ip_by_mac(mac, subnets, port=self._port())
            if found:
                log.info("Camera is answering on %s.", found)
                self._point_at(found)
                return {**row, "actual": f"answering on {found}", "ok": True}
            now = time.time()
            if now >= next_renew:
                # The laptop may need a lease on the camera's new subnet before
                # it can see the camera there at all, so keep asking.
                renew_host_dhcp(iface_host)
                next_renew = now + DHCP_HOST_RENEW_SEC
            if now >= next_note:
                seen_at = arp_table().get(canonical_mac(mac), [])
                log.info("...still looking for the camera (%ds left)%s",
                         int(deadline - now),
                         f"; its MAC is cached at {', '.join(seen_at)} but nothing "
                         f"answers there yet" if seen_at else "")
                next_note = now + DHCP_PROGRESS_SEC
            time.sleep(3)

        # Out of time. Say precisely what the bench could and couldn't see — the
        # two failures need different fixes and look identical from the outside.
        seen_at = [ip for ip in arp_table().get(canonical_mac(mac), [])
                   if ip != left_behind]
        if seen_at:
            log.warning("Camera's MAC %s is cached at %s but it never answered on "
                        "port %d within %ds.", mac, ", ".join(seen_at), self._port(), wait)
            # Point at it anyway: it is where the camera is, so verification gets
            # one more chance (it may have come up in the last few seconds) and
            # the operator gets an address to go and look at instead of a dead
            # end on the address the camera left.
            self._point_at(seen_at[0])
            return {**row, "actual": f"took {', '.join(seen_at)} but never answered "
                                     f"on port {self._port()} within {wait}s",
                    "ok": False}
        log.warning("Never saw MAC %s on %s within %ds — the camera is on DHCP, but "
                    "nothing could be verified on it.", mac, ", ".join(subnets), wait)
        return {**row, "actual": f"no sign of MAC {mac} on {', '.join(subnets)} within "
                                 f"{wait}s — is there a DHCP server on those subnets, "
                                 f"and does this PC hold an address on the one the "
                                 f"camera landed on? (a device is only findable by MAC "
                                 f"on a subnet this PC is itself on)", "ok": False}

    # --- verification -------------------------------------------------------
    def verify_configuration(self, *, new_password: str, ntp_server: str,
                             ip: str = "", netmask: str = "", gateway: str = "",
                             dhcp: bool = False,
                             profile_name: str = "", imported: Optional[dict] = None,
                             check_onvif: bool = True) -> list[dict]:
        """Re-read the settings we changed and confirm they took. Runs AFTER the
        addressing change, so it talks to the device on its new address (we are
        already re-pointed there). Returns {item, expected, actual, ok} rows.

        With `dhcp`, the address checks confirm the camera is on DHCP and holds a
        lease; the mask and gateway are reported but not asserted (the DHCP
        server chose them, so there is nothing of ours to compare against) —
        `ip`/`netmask`/`gateway` are then unused."""
        checks: list[dict] = []

        def add(item, expected, actual, ok):
            checks.append({"item": item, "expected": expected, "actual": actual, "ok": ok})

        # Password: we are authenticated on new_password (we re-logged-in under
        # it). Neither side of this row may carry an actual password — these rows
        # go to bench-central verbatim, and `actual` on a failure would be the
        # password the camera is still on (TEC-349).
        on_target = self.password == new_password
        add("admin password", "the shared password",
            "in use" if on_target else "NOT set — the camera is still on another "
                                       "password",
            on_target)

        # ONVIF user: a separate credential we set explicitly; confirm it answers
        # an authenticated ONVIF call with admin/new_password.
        if check_onvif:
            ok, detail = self.onvif_check(new_password)
            add(f"ONVIF login ({self.username})", "the shared password", detail, ok)

        if profile_name:
            applied = len((imported or {}).get("applied", []))
            skipped = len((imported or {}).get("skipped", []))
            add("config profile", profile_name,
                f"{profile_name}: {applied} table(s) applied" + (f", {skipped} skipped" if skipped else ""),
                applied > 0)

        try:
            ntp = self.read_ntp()
            got = ntp.get("address", "")
            add("NTP server", ntp_server, f"{got} (enable={ntp.get('enable')})",
                got == ntp_server and bool(ntp.get("enable")))
        except CameraError as e:
            add("NTP server", ntp_server, f"read failed: {e}", False)

        try:
            net = self.read_network()
            if dhcp:
                add("DHCP", "enabled, with a lease",
                    f"{net.ip or 'no address'} (dhcp={net.dhcp})",
                    bool(net.dhcp) and bool(net.ip))
                add("subnet mask", "(from DHCP)", net.netmask or "?", None)
                add("gateway", "(from DHCP)", net.gateway or "?", None)
            else:
                # DHCP has to be OFF as well as the address being right: a lease
                # that happens to match the assignment today is not the static
                # assignment, and the next lease need not match.
                add("static IP", ip, f"{net.ip} (dhcp={net.dhcp})",
                    net.ip == ip and not net.dhcp)
                add("subnet mask", netmask, net.netmask or "?", net.netmask == netmask)
                add("gateway", gateway, net.gateway or "?", net.gateway == gateway)
        except CameraError as e:
            add("DHCP" if dhcp else "static IP", "enabled, with a lease" if dhcp else ip,
                f"read failed: {e}", False)

        return checks


__all__ = [
    "DEFAULT_HOST", "DEFAULT_INITIAL_PASSWORD", "DEFAULT_NEW_PASSWORD",
    "DEFAULT_NTP_SERVER", "DEFAULT_SCHEME", "DEFAULT_USERNAME",
    "DHCP_HOST_RENEW_SEC", "DHCP_PROGRESS_SEC", "DHCP_SETTLE_SEC",
    "GENERATIONS", "GENERATION_LABELS", "GEN_REST", "GEN_RPC2",
    "PROFILE_DROP_SECTIONS",
    "BaseRaythinkClient", "CameraError", "CameraUnreachable", "MutationBlocked",
    "NetworkView",
    "arp_table", "canonical_mac", "find_ip_by_mac", "find_plaintext_passwords",
    "format_verification", "host_iface_for", "log", "renew_host_dhcp",
    "sanitize_profile", "set_log_serial",
]
