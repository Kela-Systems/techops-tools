#!/usr/bin/env python3
"""
Provision a single Raythink thermal camera over its RPC2 JSON API.

A fresh camera ships on the static IP 192.168.1.123 with admin/admin. Pipeline
for one camera (one at a time — the profile and the addressing are chosen in
the UI / on the CLI):

  login(admin/admin) -> set password "Kelafield123!" -> import config profile
    (LAN or Cellular) -> set NTP 192.168.88.10 + sync clock to PC time -> set
    the ONVIF user password (separate credential, via ONVIF SetUser) -> address
    the camera (LAST — drops the connection) -> verify

The addressing step runs last for the same reason as the RUTM08 LAN move: the
moment it applies, the camera leaves 192.168.1.123. It comes in two flavours,
picked by `octet`:

  * a static 192.168.88.XX (octet=XX), confirmed by reaching the camera on that
    exact address;
  * DHCP (octet=None), for a site whose own DHCP server addresses the cameras —
    confirmed by finding the camera again by MAC on the bench subnets, since
    nothing on the bench chose where it would land.

Because a full config import can reset the session (or reboot the camera), we
re-login after importing and re-assert the target password before continuing.

CLI (single camera):
  python3 raythink_configure.py --profile lan --ip 30
  python3 raythink_configure.py --profile lan --dhcp
  python3 raythink_configure.py --verify --ip 30   # check, change nothing
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent

import requests

from bench_core import load_settings, make_step_runner, tcp_port_open
from raythink_camera import (
    DEFAULT_HOST,
    DEFAULT_INITIAL_PASSWORD,
    DEFAULT_NEW_PASSWORD,
    DEFAULT_NTP_SERVER,
    DEFAULT_SCHEME,
    DEFAULT_USERNAME,
    CameraError,
    RaythinkCameraClient,
    format_verification,
    log,
    set_log_serial,
)

LOG_LINE_FORMAT = "[%(levelname_lc)s] [%(sn)s] %(message)s"

DEFAULT_GATEWAY = "192.168.88.1"
DEFAULT_NETMASK = "255.255.255.0"
DEFAULT_SUBNET_PREFIX = "192.168.88"
DEFAULT_OCTET_MIN = 30
DEFAULT_OCTET_MAX = 50

# Where to look for a camera that was just put on DHCP: the bench subnet a
# lease most likely comes from, then the factory one (a camera that got no
# lease falls back to its previous static address).
DEFAULT_SCAN_SUBNETS = ["192.168.88.0/24", "192.168.1.0/24", "192.168.2.0/24"]
# Generous on purpose: unlike a static move, this has to cover the camera
# rebooting, taking a lease AND starting its web server. Giving up early gets a
# camera that is actually fine reported as unverifiable.
DEFAULT_LEASE_TIMEOUT = 300

DHCP_DEVICE_NAME = "raythink-dhcp"


def device_name(octet: Optional[int]) -> str:
    """raythink-<octet>, or raythink-dhcp for a camera left on DHCP — there is
    no assigned octet to name that one after."""
    return DHCP_DEVICE_NAME if octet is None else f"raythink-{octet}"


CONFIG_DIR = BASE_DIR / "config"


def resolve_profile(settings: dict, profile_name: str) -> Path:
    """Map a profile key (e.g. 'lan') to its JSON file. Relative paths in the
    config (e.g. 'profiles/lan.json') resolve against the config/ folder."""
    profiles = settings.get("profiles", {}) or {}
    rel = profiles.get(profile_name)
    if not rel:
        raise CameraError(f"Unknown profile '{profile_name}'. Known: {', '.join(profiles) or '(none)'}.")
    path = (CONFIG_DIR / rel) if not Path(rel).is_absolute() else Path(rel)
    if not path.is_file():
        raise CameraError(f"Profile file for '{profile_name}' not found: {path}")
    return path


def target_ip_for(settings: dict, octet: int) -> str:
    prefix = (settings.get("static", {}) or {}).get("subnet_prefix", DEFAULT_SUBNET_PREFIX)
    return f"{prefix}.{octet}"


# --- detection ----------------------------------------------------------------
#
# A fresh camera is at the factory address and nothing else needs looking for.
# A FINISHED one is somewhere in the assigned static range, which is why verify
# (TEC-348) needs more than the single factory probe this tool used to do: a
# camera the bench already moved to 192.168.88.31 is invisible to a tool that
# only ever looks at 192.168.1.123.
#
# The range is small and known (octet_min..octet_max, ~20 addresses), so it is
# swept directly rather than by MAC — unlike the DHCP path, which has no idea
# which address to expect and must search by MAC instead.

# Short enough that a full sweep of the range fits inside one poll interval.
DETECT_SCAN_TIMEOUT = 0.3
DETECT_SIGNATURE_TIMEOUT = 2.0


def is_camera(host: str, scheme: str = DEFAULT_SCHEME,
              timeout: float = DETECT_SIGNATURE_TIMEOUT) -> bool:
    """True when `host` answers the camera's own RPC2 login challenge.

    The unauthenticated first half of `login()`: a Dahua-family device replies
    to an empty-password `global.login` with a `random` nonce to hash against.
    Anything else answering port 80 in the bench range (a router's web UI, a
    speaker) does not, so this tells a camera apart from its neighbours without
    needing a password.
    """
    try:
        r = requests.post(f"{scheme}://{host}/RPC2_Login",
                          data=json.dumps({"method": "global.login",
                                           "params": {"userName": DEFAULT_USERNAME,
                                                      "password": "",
                                                      "clientType": "Web3.0",
                                                      "loginType": "Direct"},
                                           "id": 1, "session": 0}),
                          headers={"Content-Type": "application/json"},
                          timeout=timeout, verify=False)
        return bool((r.json().get("params") or {}).get("random"))
    except (requests.exceptions.RequestException, ValueError, AttributeError):
        return False


def camera_hosts(settings: dict) -> list[str]:
    """Every address a camera this bench has touched could be answering on: the
    factory address first (a fresh unit), then the assigned static range (a
    finished one)."""
    static = settings.get("static", {}) or {}
    prefix = static.get("subnet_prefix", DEFAULT_SUBNET_PREFIX)
    lo = int(static.get("octet_min", DEFAULT_OCTET_MIN))
    hi = int(static.get("octet_max", DEFAULT_OCTET_MAX))
    return ([settings.get("host", DEFAULT_HOST)]
            + [f"{prefix}.{o}" for o in range(lo, hi + 1)])


def find_camera(hosts: list[str], port: int = 80, scheme: str = DEFAULT_SCHEME,
                first_guess: Optional[str] = None) -> Optional[str]:
    """The address of a camera among `hosts`, or None.

    `first_guess` (where one was last seen) is probed alone first, so the common
    case — the same camera still plugged in — costs one connection instead of a
    sweep, and a camera at the factory address keeps being reported as that
    rather than flapping to whatever else answers.
    """
    def answering(host: str) -> bool:
        return (tcp_port_open(host, port, timeout=DETECT_SCAN_TIMEOUT)
                and is_camera(host, scheme))

    if first_guess and answering(first_guess):
        return first_guess

    open_hosts: list[str] = []
    candidates = [h for h in hosts if h != first_guess]
    with ThreadPoolExecutor(max_workers=min(32, len(candidates) or 1)) as pool:
        futures = {pool.submit(tcp_port_open, h, port, DETECT_SCAN_TIMEOUT): h
                   for h in candidates}
        for fut in as_completed(futures):
            if fut.result():
                open_hosts.append(futures[fut])

    # Only now the (slower) signature check, and in the caller's order so the
    # factory address always wins over a finished unit in the same sweep.
    for host in [h for h in candidates if h in open_hosts]:
        if is_camera(host, scheme):
            return host
    return None


# --- pipeline (shared by CLI + web UI) ----------------------------------------

def configure_camera(client: RaythinkCameraClient, *, profile_name: str,
                     profile_path: str, octet: Optional[int], settings: dict,
                     mac: str = "") -> dict:
    """Run the full provisioning pipeline for ONE camera. Logs every step through
    the 'raythink' logger. Returns identity + per-step failures + verification.
    Raises CameraError on a hard failure (login, password change).

    `octet` picks the addressing: an int gives the camera that static
    192.168.88.<octet>, None leaves it on DHCP. `mac` is a fallback used only to
    find the camera again after a DHCP switch, for the rare unit that doesn't
    report its own MAC (the bench reads it over ARP while the camera is still on
    its factory address)."""
    dhcp_mode = octet is None
    static = settings.get("static", {}) or {}
    gateway = static.get("gateway", DEFAULT_GATEWAY)
    netmask = static.get("netmask", DEFAULT_NETMASK)
    dhcp_cfg = settings.get("dhcp", {}) or {}
    scan_subnets = dhcp_cfg.get("scan_subnets", DEFAULT_SCAN_SUBNETS)
    lease_timeout = int(dhcp_cfg.get("lease_timeout", DEFAULT_LEASE_TIMEOUT))
    ntp_server = settings.get("ntp_server", DEFAULT_NTP_SERVER)
    initial_pw = settings.get("initial_password", DEFAULT_INITIAL_PASSWORD)
    new_pw = settings.get("new_password", DEFAULT_NEW_PASSWORD)
    target_ip = "" if dhcp_mode else target_ip_for(settings, octet)
    name = device_name(octet)

    set_log_serial(None)

    # 1. Login. Fresh camera: admin/admin. Re-run of a half-configured camera:
    # it is already on the target password — fall back to it.
    try:
        client.login(initial_pw)
    except CameraError as e:
        if new_pw and new_pw != initial_pw:
            log.info("Factory password rejected (%s) — trying the target password "
                     "(camera may already be partly configured).", e)
            client.login(new_pw)
        else:
            raise

    identity = client.get_identity()

    # 2. Password change (critical — aborts the run on failure).
    client.modify_password(new_pw, client.password or initial_pw)

    failures, _step = make_step_runner(log, CameraError)

    # 3. Import the chosen config profile, then re-login (an import can drop the
    # session or reboot the camera).
    imported = {"applied": [], "skipped": []}

    def do_import() -> None:
        nonlocal imported
        imported = client.import_config(profile_path)
        if not client.wait_reachable(int(settings.get("import_reboot_timeout", 180))):
            raise CameraError("camera unreachable after import (did it reboot and not return?)")
        client.relogin([new_pw, initial_pw], settle=3)

    _step("import-config", do_import)

    # 4. Date & Time: point NTP at the bench server AND sync the clock to the
    # PC now (the web UI's "Sync to PC time" button), so the camera has the right
    # time immediately even before its first NTP poll.
    _step("ntp", lambda: client.set_ntp(ntp_server))
    _step("sync-time", client.sync_time_to_pc)

    # 4b. ONVIF user — a SEPARATE credential from the web/system account, so the
    # password change above does not touch it. Set it over ONVIF SetUser; the
    # factory ONVIF password is the same default as the web login ('admin').
    _step("onvif-user",
          lambda: client.set_onvif_password(new_pw, [initial_pw, "admin", new_pw]))

    # 5. Addressing — LAST. After this the camera has left 192.168.1.123, either
    # for target_ip or for wherever DHCP put it. Neither call raises after the
    # write; both return a verification row and repoint the client.
    verification: list[dict] = []
    step_label = "dhcp" if dhcp_mode else "static-ip"
    try:
        if dhcp_mode:
            # Prefer the MAC the camera reports about itself; fall back to the
            # one the bench read over ARP.
            device_mac = next((m for m in (identity.get("mac"), mac)
                               if m and m != "unknown"), "")
            verification.append(client.set_dhcp(mac=device_mac, subnets=scan_subnets,
                                                wait=lease_timeout))
        else:
            verification.append(client.set_static_ip(
                target_ip, netmask, gateway,
                wait=int(settings.get("ip_move_timeout", 120))))
    except CameraError as e:
        failures.append(f"{step_label}: {e}")
        log.error("Step '%s' FAILED: %s", step_label, e)

    # 6. Verify wherever the camera ended up (re-login there first, if it
    # answers). client.host is that address — the step above followed it.
    if client.port_open():
        try:
            client.relogin([new_pw], settle=2)
        except CameraError as e:
            log.warning("Could not re-login on %s for verification: %s", client.host, e)
        try:
            verification += client.verify_configuration(
                new_password=new_pw, ntp_server=ntp_server, ip=target_ip,
                netmask=netmask, gateway=gateway, dhcp=dhcp_mode,
                profile_name=profile_name, imported=imported)
            for line in format_verification(verification).splitlines():
                log.info("%s", line)
        except CameraError as e:
            log.error("Verification could not run: %s", e)
    else:
        log.warning("Camera is not answering on %s — skipping read-back verification.",
                    client.host)

    verify_failed = [c["item"] for c in verification if c["ok"] is False]
    ok = not failures and not verify_failed
    where = f"DHCP: {client.host}" if dhcp_mode else target_ip
    if ok:
        log.info("Provisioning complete for %s (%s) — all steps verified.", name, where)
    else:
        problems = failures + [f"verify:{i}" for i in verify_failed]
        log.error("Provisioning of %s finished with problems: %s", name, " | ".join(problems))
    # ip is the address the bench ASSIGNED, so a DHCP run carries none — the
    # lease is the DHCP server's to change, and it is in the verification rows.
    return {"name": name, "hostname": name, "identity": identity, "warnings": [],
            "failures": failures, "verification": verification, "ok": ok,
            "profile": profile_name, "ip": target_ip,
            "ip_mode": "dhcp" if dhcp_mode else "static"}


# --- verify-only pass (TEC-348) ----------------------------------------------

def verify_camera(client: RaythinkCameraClient, *, settings: dict, resolve=None,
                  profile_name: str = "", octet: Optional[int] = None,
                  ip_mode: str = "") -> dict:
    """Check ONE finished camera against its intended state, changing nothing.

    Two of this camera's expectations are per-unit — which address the bench
    assigned it and which profile it was given — so `resolve` recovers them from
    the unit's configure record, and `profile_name`/`octet` let an operator state
    them instead. The shared password, the ONVIF password and the NTP server are
    station-wide and come from the config.

    The address row is effect-based: we are talking to this camera on the address
    the record says it was given. A camera that fell back to the factory address
    (or to a stale one) answers the sweep somewhere else, and the row goes red
    with the address it was actually found on — the ambiguity the old
    "no answer on X — may still be fine" warning left open.

    The profile row cannot be verified from the device and says so. A profile is
    a full config export of several hundred device-normalised fields; the
    configure run itself tolerates tables the camera rejects, so a field-by-field
    diff here would fail cameras that are fine. It reports which profile the
    record names and leaves `ok` unset rather than passing by construction.
    """
    ntp_server = settings.get("ntp_server", DEFAULT_NTP_SERVER)
    initial_pw = settings.get("initial_password", DEFAULT_INITIAL_PASSWORD)
    new_pw = settings.get("new_password", DEFAULT_NEW_PASSWORD)
    static = settings.get("static", {}) or {}
    netmask = static.get("netmask", DEFAULT_NETMASK)
    gateway = static.get("gateway", DEFAULT_GATEWAY)

    set_log_serial(None)

    # The login IS the password check. Falling back to the factory password says
    # WHY it failed rather than just that it did — and a camera that still
    # accepts admin/admin is the worst thing a sweep can find.
    still_factory = False
    try:
        client.login(new_pw)
    except CameraError as e:
        log.info("Target password rejected (%s) — trying the factory password.", e)
        try:
            client.login(initial_pw)
            still_factory = True
        except CameraError:
            raise CameraError(
                "Cannot log in with either the shared or the factory password — "
                "this camera is on neither, so nothing can be checked.")

    # Everything past the login is a read, enforced rather than intended.
    client.set_read_only()

    identity = client.get_identity()

    expected, prior_row = resolve(identity) if resolve else ({}, None)
    profile = profile_name or expected.get("profile") or ""
    # What the bench assigned. An empty `ip` under ip_mode "dhcp" is not a gap —
    # it means the lease was the site's to choose, so there is no address of ours
    # to check against. An empty one under any other mode IS a gap, and gets a
    # failing row rather than being quietly skipped.
    if octet is not None:
        ip_mode, target_ip = "static", target_ip_for(settings, octet)
    elif ip_mode == "dhcp":
        target_ip = ""
    else:
        ip_mode = expected.get("ip_mode") or ("static" if expected.get("ip") else "")
        target_ip = expected.get("ip") or ""
    dhcp_mode = ip_mode == "dhcp"
    reached = client.host

    verification: list[dict] = []
    if prior_row is not None:
        verification.append(prior_row)

    if dhcp_mode:
        verification.append({
            "item": "reached at", "expected": "an address from the DHCP server",
            "actual": f"answering on {reached}", "ok": True})
    elif target_ip:
        verification.append({
            "item": "reached at", "expected": target_ip, "actual": reached,
            "ok": reached == target_ip})
    else:
        # No recorded address and none supplied. Reading the camera's own
        # address and calling it expected would pass by construction — the
        # tautology TEC-348 exists to remove — so report what is missing.
        verification.append({
            "item": "reached at",
            "expected": "the address from this camera's configure run",
            "actual": f"{reached} — no recorded address to compare it with",
            "ok": False})

    verification.append({
        "item": "config profile",
        "expected": profile or "the profile from this camera's configure run",
        "actual": (f"{profile} (per the configure record — a profile is a full "
                   "config export and cannot be re-checked field by field)"
                   if profile else "no recorded profile to compare against"),
        "ok": None if profile else False})

    if still_factory:
        verification.append({
            "item": "admin password", "expected": "the shared password",
            "actual": "NOT set — the camera still answers to the factory "
                      "password", "ok": False})
    verification += [c for c in client.verify_configuration(
        new_password=new_pw, ntp_server=ntp_server,
        ip=target_ip or reached, netmask=netmask, gateway=gateway,
        dhcp=dhcp_mode, profile_name="",   # the profile row is built above
    ) if not (still_factory and c["item"] == "admin password")]

    for line in format_verification(verification).splitlines():
        log.info("%s", line)

    failed = [c["item"] for c in verification if c["ok"] is False]
    ok = not failed
    name = device_name(None if dhcp_mode else octet) if (dhcp_mode or octet is not None) \
        else (expected.get("hostname") or f"the camera at {reached}")
    if ok:
        log.info("%s (%s) PASSED verification — nothing was changed.", name, reached)
    else:
        log.error("%s (%s) FAILED verification: %s", name, reached, ", ".join(failed))
    return {"name": name, "hostname": name, "identity": identity, "warnings": [],
            "failures": [], "verification": verification, "ok": ok,
            "profile": profile, "ip": target_ip,
            "ip_mode": ip_mode or "static"}


def main():
    p = argparse.ArgumentParser(description="Provision a single Raythink camera.")
    p.add_argument("--profile", default="", help="config profile key (e.g. lan, cellular)")
    addressing = p.add_mutually_exclusive_group()
    addressing.add_argument("--ip", type=int,
                            help="last octet of the static IP (e.g. 30 -> 192.168.88.30)")
    addressing.add_argument("--dhcp", action="store_true",
                            help="leave the camera on DHCP instead of assigning a static IP")
    p.add_argument("--verify", action="store_true",
                   help="check a finished camera against its intended state and "
                        "change nothing (TEC-348). --ip/--dhcp and --profile then "
                        "state what to expect instead of what to apply, and are "
                        "optional (--ip is needed to check the address, since a "
                        "CLI run has no configure record to read it from). Exits "
                        "0 only on a full PASS.")
    p.add_argument("--config", default=str(BASE_DIR / "config" / "raythink.config.json"),
                   help="shared settings JSON (default: config/raythink.config.json)")
    args = p.parse_args()

    if not args.verify:
        if not args.profile:
            p.error("--profile is required when provisioning a camera")
        if args.ip is None and not args.dhcp:
            p.error("one of --ip or --dhcp is required when provisioning a camera")

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_LINE_FORMAT))
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    settings = load_settings(args.config)
    octet = None if args.dhcp else args.ip
    if octet is not None:
        static = settings.get("static", {}) or {}
        lo = int(static.get("octet_min", DEFAULT_OCTET_MIN))
        hi = int(static.get("octet_max", DEFAULT_OCTET_MAX))
        if not (lo <= octet <= hi):
            sys.exit(f"--ip {octet} is out of the allowed range {lo}-{hi}.")

    profile_path = None
    if args.profile:
        try:
            profile_path = resolve_profile(settings, args.profile)
        except CameraError as e:
            sys.exit(str(e))

    # A finished camera is not on the factory address any more, so find it the
    # same way the UI does before pointing the client at it.
    host = settings.get("host", DEFAULT_HOST)
    if args.verify:
        scheme = settings.get("scheme", DEFAULT_SCHEME)
        found = find_camera(camera_hosts(settings),
                            443 if scheme == "https" else 80, scheme,
                            first_guess=(target_ip_for(settings, octet)
                                         if octet is not None else None))
        if not found:
            sys.exit("No camera found on the factory address or the assigned "
                     "static range — is it powered and cabled, and is this PC on "
                     "its subnet?")
        log.info("Camera found at %s.", found)
        host = found

    client = RaythinkCameraClient(
        host=host,
        username=settings.get("username", DEFAULT_USERNAME),
        scheme=settings.get("scheme", DEFAULT_SCHEME),
        verify=not settings.get("insecure", True),
    )
    try:
        if args.verify:
            result = verify_camera(client, settings=settings,
                                   profile_name=args.profile, octet=octet,
                                   ip_mode="dhcp" if args.dhcp else "")
        else:
            result = configure_camera(client, profile_name=args.profile,
                                      profile_path=str(profile_path), octet=octet,
                                      settings=settings)
    except CameraError as e:
        log.error("%s FAILED: %s", "Verification" if args.verify else "Provisioning", e)
        sys.exit(1)          # the finally below closes the client exactly once
    finally:
        client.close()
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
