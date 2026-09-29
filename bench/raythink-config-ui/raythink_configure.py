#!/usr/bin/env python3
"""
Provision a single Raythink thermal camera, of either generation.

Raythink ships two generations with incompatible control APIs — the older ones
speak Dahua-OEM RPC2, the newer ones a REST /v1 API. `raythink_client` tells
them apart and hands back the right client, and everything in this file is
written against the shared client interface, so the pipeline below is one
pipeline rather than two. The only place a generation is named is where the
device genuinely differs: which config profile file to send it.

A fresh camera of either generation ships on the static IP 192.168.1.123 with
admin/admin. Pipeline for one camera (one at a time — the profile and the
addressing are chosen in the UI / on the CLI):

  login(admin/admin) -> set password <shared> -> import config profile
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
  python3 raythink_configure.py --sanitize-profile export.json -o profiles/v2/lan.json
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

from bench_core import (load_settings, make_step_runner, shared_new_password,
                        tcp_port_open)
from raythink_base import (
    DEFAULT_HOST,
    DEFAULT_INITIAL_PASSWORD,
    DEFAULT_NTP_SERVER,
    DEFAULT_SCHEME,
    DEFAULT_USERNAME,
    GENERATION_LABELS,
    GENERATIONS,
    GEN_REST,
    GEN_RPC2,
    CameraError,
    find_plaintext_passwords,
    format_verification,
    fw_at_least,
    log,
    sanitize_profile,
    set_log_serial,
)
from raythink_client import (
    check_firmware_generation,
    detect_generation,
    open_camera,
)
from raythink_firmware import check_model, plan_upgrade

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
# Covers only the reboot: the write itself is awaited inside upgrade_firmware,
# which returns when the device reports it finished. Still generous, because the
# camera boots a freshly written image and may take a lease on the way up.
DEFAULT_FIRMWARE_REBOOT_TIMEOUT = 600

DHCP_DEVICE_NAME = "raythink-dhcp"


def device_name(octet: Optional[int]) -> str:
    """raythink-<octet>, or raythink-dhcp for a camera left on DHCP — there is
    no assigned octet to name that one after."""
    return DHCP_DEVICE_NAME if octet is None else f"raythink-{octet}"


CONFIG_DIR = BASE_DIR / "config"


def resolve_profile(settings: dict, profile_name: str,
                    generation: str = GEN_RPC2) -> Path:
    """Map a profile key (e.g. 'lan') to its JSON file for `generation`.

    The two generations export incompatible files — the older one a map of Dahua
    config tables replayed table by table, the newer one a set of sections
    uploaded whole — so one profile key names two files:

        "lan": {"rpc2": "profiles/lan.json", "rest": "profiles/v2/lan.json"}

    A plain string still means the older generation only, so a bench that has
    not met a new camera yet needs no config change:

        "lan": "profiles/lan.json"

    Relative paths resolve against the config/ folder.
    """
    profiles = settings.get("profiles", {}) or {}
    if profile_name not in profiles:
        raise CameraError(f"Unknown profile '{profile_name}'. "
                          f"Known: {', '.join(profiles) or '(none)'}.")
    entry = profiles[profile_name]
    rel = entry.get(generation) if isinstance(entry, dict) else (
        entry if generation == GEN_RPC2 else "")
    if not rel:
        raise CameraError(
            f"Profile '{profile_name}' has no file for the "
            f"{GENERATION_LABELS.get(generation, generation)} cameras. Export a "
            f"config from a reference camera of that generation, sanitize it "
            f"(--sanitize-profile) and add it as "
            f"\"{profile_name}\": {{\"...\": \"...\", \"{generation}\": \"profiles/…json\"}}.")
    path = (CONFIG_DIR / rel) if not Path(rel).is_absolute() else Path(rel)
    if not path.is_file():
        raise CameraError(f"Profile file for '{profile_name}' "
                          f"({GENERATION_LABELS.get(generation, generation)}) "
                          f"not found: {path}")
    return path


def profile_generations(settings: dict, profile_name: str) -> list[str]:
    """Which generations `profile_name` has a file configured for. Used by the
    UI to say up front that a profile cannot serve the camera on the bench,
    rather than failing three steps into a run."""
    entry = (settings.get("profiles", {}) or {}).get(profile_name)
    if isinstance(entry, dict):
        return [g for g in GENERATIONS if entry.get(g)]
    return [GEN_RPC2] if entry else []


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


def camera_generation(host: str, scheme: str = DEFAULT_SCHEME,
                      timeout: float = DETECT_SIGNATURE_TIMEOUT) -> Optional[str]:
    """The generation of the camera answering at `host`, or None if it is not a
    camera at all.

    Both generations are probed unauthenticated, which is what tells a camera
    apart from whatever else answers port 80 in the bench range (a router's web
    UI, a speaker): neither of those produces an RPC2 login challenge or the
    REST API's response envelope.
    """
    return detect_generation(host, scheme, timeout)


def is_camera(host: str, scheme: str = DEFAULT_SCHEME,
              timeout: float = DETECT_SIGNATURE_TIMEOUT) -> bool:
    """True when `host` is a Raythink camera of either generation."""
    return camera_generation(host, scheme, timeout) is not None


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
                first_guess: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """`(address, generation)` of a camera among `hosts`, or `(None, None)`.

    The generation comes back with the address because establishing it is the
    same probe as establishing that this is a camera at all — returning only the
    address would mean probing the device twice for one answer.

    `first_guess` (where one was last seen) is probed alone first, so the common
    case — the same camera still plugged in — costs one connection instead of a
    sweep, and a camera at the factory address keeps being reported as that
    rather than flapping to whatever else answers.
    """
    def answering(host: str) -> Optional[str]:
        if not tcp_port_open(host, port, timeout=DETECT_SCAN_TIMEOUT):
            return None
        return camera_generation(host, scheme)

    if first_guess:
        gen = answering(first_guess)
        if gen:
            return first_guess, gen

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
        gen = camera_generation(host, scheme)
        if gen:
            return host, gen
    return None, None


# --- pipeline (shared by CLI + web UI) ----------------------------------------

def min_firmware_for(client, settings: dict) -> str:
    """The configured firmware floor, or "" when there is none to check against.

    Empty for the older generation whatever the config says: there is no upgrade
    path for it here, so a floor meant for the newer cameras must not fail an
    older one's verification.
    """
    if client.generation != GEN_REST:
        return ""
    return ((settings.get("firmware", {}) or {}).get("minimum_version") or "").strip()


def firmware_only(client, *, settings: dict, force: bool = False) -> bool:
    """Flash the camera and stop, changing nothing else.

    Exists for a specific chicken-and-egg: the floor has to be set to the
    version string the camera REPORTS, and nothing outside the camera knows what
    that is — the bundle's filename ('B1V0222916') is not the reported scheme
    ('B1.2.02.29.16, 2026-09-03') and nothing inside it pairs the two. So the
    first bundle of a batch gets flashed with this, and the version it prints is
    what goes in the config.

    Deliberately does NOT touch the password, the profile or the addressing:
    a camera used to answer this question should come out of it in the same
    state it went in, apart from its firmware.
    """
    initial_pw = settings.get("initial_password", DEFAULT_INITIAL_PASSWORD)
    new_pw = shared_new_password(settings)
    try:
        client.login(initial_pw)
    except CameraError:
        client.login(new_pw)

    if client.generation != GEN_REST:
        log.error("Firmware upgrades are only supported on the newer (REST) "
                  "cameras; this one is %s.",
                  GENERATION_LABELS.get(client.generation, client.generation))
        return False

    identity = client.get_identity()
    log.info("Camera %s (%s) is on %s.", identity.get("serial", "?"),
             identity.get("model", "?"), identity.get("firmware", "?"))

    dhcp_cfg = settings.get("dhcp", {}) or {}
    failures, _step = make_step_runner(log, CameraError)
    apply_firmware_floor(client, settings, identity, force=force,
                         subnets=dhcp_cfg.get("scan_subnets", DEFAULT_SCAN_SUBNETS),
                         passwords=[new_pw, initial_pw], step=_step)
    if failures:
        for line in failures:
            log.error("%s", line)
        return False

    running = client.get_identity().get("firmware", "")
    log.info("Camera now reports: %s", running or "(nothing)")
    log.info('Set this as the floor:  "firmware": { "minimum_version": "%s" }',
             running)
    return True


def apply_firmware_floor(client, settings: dict, identity: dict, *,
                         subnets: list[str], passwords: list[str], step,
                         force: bool = False) -> str:
    """Bring the camera up to the configured minimum firmware if it is below it,
    and return the note that goes in the run record either way.

    A FLOOR, not a pin (see raythink_firmware.plan_upgrade). Configured entirely
    under `firmware` in the settings, and absent configuration means no firmware
    step at all — so a bench that has not been given an image behaves exactly as
    it did before this existed.

    The whole thing runs through `step`, so a firmware problem fails ITS step and
    is reported with the others rather than aborting the run. That is the right
    trade for a camera that is otherwise provisionable: an operator would rather
    have a fully configured camera on the wrong build, named in the failures,
    than an aborted run leaving it half-done.
    """
    fw = settings.get("firmware", {}) or {}
    minimum = (fw.get("minimum_version") or "").strip()
    zip_path = fw.get("zip_path") or ""
    if zip_path:
        zip_path = str((BASE_DIR / zip_path).resolve())

    note = ""

    def do_firmware() -> None:
        nonlocal note
        current = identity.get("firmware", "")
        if force:
            # --firmware-only --force-firmware: the operator is establishing what
            # a bundle reports, so the floor comparison is exactly what has to be
            # bypassed. The MODEL check is not bypassed — the wrong image bricks.
            if not zip_path:
                raise CameraError("no firmware.zip_path configured, so there is "
                                  "nothing to flash")
            check_model(zip_path, identity.get("model", ""))
            flash, note = True, f"forced from {current or 'unknown'}"
        else:
            flash, note = plan_upgrade(current, minimum, zip_path,
                                       identity.get("model", ""))
        if not flash:
            log.info("Firmware: %s.", note)
            return

        # It restarts on the last part and does NOT reliably come back where it
        # was: on the bench it moved, which is why the camera is searched for by
        # MAC rather than waited for on the address we were talking to. Checked
        # BEFORE the flash starts, not after — without a MAC the camera would be
        # rewritten and then lost, which is far worse than not flashing it.
        mac = identity.get("mac", "")
        if not mac:
            raise CameraError("the camera reported no MAC, so the bench could not "
                              "find it again after the reboot — refusing to flash")

        log.info("Firmware: %s — flashing.", note)
        client.upgrade_firmware(zip_path)

        wait = int(fw.get("reboot_timeout", DEFAULT_FIRMWARE_REBOOT_TIMEOUT))
        if not client.follow_by_mac(mac, subnets, wait=wait,
                                    why="it has to restart on the new firmware"):
            # The image is written by this point (upgrade_firmware waits for the
            # device to say so), so this is a camera that rebooted and did not
            # reappear on any subnet the bench is watching — most often because
            # it landed somewhere this PC has no address on.
            raise CameraError(
                f"the firmware was written, but the camera did not reappear on "
                f"{', '.join(subnets) or '(no subnets configured)'} within {wait}s. "
                "It most likely came back on a subnet this PC is not on — find it "
                "by hand and re-run.")
        client.relogin(passwords, settle=5)

        after = client.get_identity().get("firmware", "")
        note = f"{current or 'unknown'} -> {after or 'unknown'}"
        # Only assertable against a floor. Under --force-firmware there may be
        # none — establishing what the camera reports is the whole point.
        if minimum and not fw_at_least(after, minimum):
            raise CameraError(f"flashed, but the camera reports {after or 'nothing'} "
                              f"rather than {minimum} or newer")
        log.info("Firmware upgraded: %s.", note)

    step("firmware", do_firmware)
    return note or "firmware step failed — see above"


def configure_camera(client, *, profile_name: str,
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
    new_pw = shared_new_password(settings)
    target_ip = "" if dhcp_mode else target_ip_for(settings, octet)
    name = device_name(octet)

    set_log_serial(None)
    log.info("Provisioning a %s camera at %s.",
             GENERATION_LABELS.get(client.generation, client.generation), client.host)

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
    # The probe chose the API before anyone could log in; now that the firmware
    # version is readable, confirm the two agree. Never fatal — see there.
    check_firmware_generation(identity, client.generation)

    # 2. Password change (critical — aborts the run on failure).
    client.modify_password(new_pw, client.password or initial_pw)

    failures, _step = make_step_runner(log, CameraError)

    # 3. Firmware, BEFORE the config import, so the profile lands on the build
    # that will actually run it — a flash afterwards could migrate or discard
    # what we just imported. A floor rather than a pin: see raythink_firmware.
    if client.generation == GEN_REST:
        apply_firmware_floor(client, settings, identity, subnets=scan_subnets,
                             passwords=[new_pw, initial_pw], step=_step)
        # The flash reboots the camera and it can come back elsewhere, so
        # anything read before it is stale.
        identity = client.get_identity()

    # 4. Import the chosen config profile, then re-login (an import can drop the
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
                profile_name=profile_name, imported=imported,
                min_firmware=min_firmware_for(client, settings))
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
            "ip_mode": "dhcp" if dhcp_mode else "static",
            "generation": client.generation}


# --- verify-only pass (TEC-348) ----------------------------------------------

def verify_camera(client, *, settings: dict, resolve=None,
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
    new_pw = shared_new_password(settings)
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
    check_firmware_generation(identity, client.generation)

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
        min_firmware=min_firmware_for(client, settings),
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
            "ip_mode": ip_mode or "static",
            "generation": client.generation}


def sanitize_profile_file(src: Path, dest: Optional[Path], settings: dict) -> int:
    """Turn a raw config export into a committable profile. Returns an exit code.

    A raw export is the reference camera's ENTIRE configuration, which is both
    more and less than a profile should be: it carries that camera's address
    (which would move the next unit mid-run) and, on the newer generation, the
    station's ONVIF password in plaintext — into a file that is meant to be
    committed. `sanitize_profile` removes both; see there for what it
    deliberately leaves alone.

    Anything password-shaped that survives is REPORTED, not removed. A GB28181
    SIP password or an SMTP login may be exactly what the site wants, so the
    call is a human's — but it has to be an informed one, which it is not if
    nothing ever mentions the field exists.
    """
    with open(src, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        print(f"{src}: not a JSON object of config sections.", file=sys.stderr)
        return 1

    secret = shared_new_password(settings)
    cleaned, notes = sanitize_profile(data, secrets=(secret,))
    for note in notes:
        print(f"  {note}")
    if not notes:
        print("  nothing to remove — the export carries no address or station password")

    remaining = find_plaintext_passwords(cleaned)
    if remaining:
        print("\nStill present, for you to review before committing "
              "(these may be legitimate site settings):")
        for path, _value in remaining:
            print(f"  {path}")

    out = json.dumps(cleaned, indent=1, ensure_ascii=False) + "\n"
    if dest is None:
        sys.stdout.write(out)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(out, encoding="utf-8")
        print(f"\nWrote {dest} ({len(cleaned)} section(s)).")
    return 0


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
    p.add_argument("--generation", choices=GENERATIONS, default="",
                   help="skip the API probe and force a generation "
                        f"({', '.join(f'{g} = {GENERATION_LABELS[g]}' for g in GENERATIONS)})")
    p.add_argument("--host", default="",
                   help="the camera's address, overriding the config's host — "
                        "for a camera that has already been moved off the "
                        "factory address (ignored with --verify, which searches)")
    p.add_argument("--firmware-only", action="store_true",
                   help="flash the firmware and stop, changing nothing else — "
                        "for establishing what version a bundle actually reports "
                        "so it can be set as firmware.minimum_version")
    p.add_argument("--force-firmware", action="store_true",
                   help="with --firmware-only, flash even if the camera already "
                        "meets the floor (or none is configured)")
    p.add_argument("--sanitize-profile", metavar="EXPORT.json", default="",
                   help="turn a raw config export into a committable profile "
                        "(drops the reference camera's address and the station "
                        "password) and exit; writes to -o, or stdout")
    p.add_argument("-o", "--output", default="",
                   help="where --sanitize-profile writes (default: stdout)")
    p.add_argument("--config", default=str(BASE_DIR / "config" / "raythink.config.json"),
                   help="shared settings JSON (default: config/raythink.config.json)")
    args = p.parse_args()

    if not args.sanitize_profile and not args.verify and not args.firmware_only:
        if not args.profile:
            p.error("--profile is required when provisioning a camera")
        if args.ip is None and not args.dhcp:
            p.error("one of --ip or --dhcp is required when provisioning a camera")

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_LINE_FORMAT))
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    settings = load_settings(args.config)

    # Touches no device, so it runs before anything goes looking for one.
    if args.sanitize_profile:
        sys.exit(sanitize_profile_file(Path(args.sanitize_profile),
                                       Path(args.output) if args.output else None,
                                       settings))

    octet = None if args.dhcp else args.ip
    if octet is not None:
        static = settings.get("static", {}) or {}
        lo = int(static.get("octet_min", DEFAULT_OCTET_MIN))
        hi = int(static.get("octet_max", DEFAULT_OCTET_MAX))
        if not (lo <= octet <= hi):
            sys.exit(f"--ip {octet} is out of the allowed range {lo}-{hi}.")

    # Which generation comes FIRST, because the profile depends on it: the two
    # generations take incompatible config files, so there is no profile to
    # resolve until we know which camera is on the bench.
    scheme = settings.get("scheme", DEFAULT_SCHEME)
    host = args.host or settings.get("host", DEFAULT_HOST)
    generation = args.generation or None
    if args.verify:
        # A finished camera is not on the factory address any more, so find it
        # the same way the UI does before pointing the client at it.
        found, found_gen = find_camera(camera_hosts(settings),
                                       443 if scheme == "https" else 80, scheme,
                                       first_guess=(target_ip_for(settings, octet)
                                                    if octet is not None else None))
        if not found:
            sys.exit("No camera found on the factory address or the assigned "
                     "static range — is it powered and cabled, and is this PC on "
                     "its subnet?")
        generation = generation or found_gen
        log.info("Camera found at %s — %s.", found,
                 GENERATION_LABELS.get(generation, generation))
        host = found
    elif generation is None:
        generation = detect_generation(host, scheme)
        if generation is None:
            sys.exit(f"Nothing at {host} answered either Raythink API — is the "
                     "camera powered and cabled, and is this PC on its subnet? "
                     "(--generation forces one if the probe is the problem.)")
        log.info("Camera at %s — %s.", host,
                 GENERATION_LABELS.get(generation, generation))

    profile_path = None
    if args.profile:
        try:
            profile_path = resolve_profile(settings, args.profile, generation)
        except CameraError as e:
            sys.exit(str(e))

    client = open_camera(
        host,
        username=settings.get("username", DEFAULT_USERNAME),
        scheme=scheme,
        verify=not settings.get("insecure", True),
        generation=generation,
    )
    try:
        if args.firmware_only:
            sys.exit(0 if firmware_only(client, settings=settings,
                                        force=args.force_firmware) else 1)
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
