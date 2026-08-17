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
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent

from bench_core import load_settings, make_step_runner
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


def main():
    p = argparse.ArgumentParser(description="Provision a single Raythink camera.")
    p.add_argument("--profile", required=True, help="config profile key (e.g. lan, cellular)")
    addressing = p.add_mutually_exclusive_group(required=True)
    addressing.add_argument("--ip", type=int,
                            help="last octet of the static IP (e.g. 30 -> 192.168.88.30)")
    addressing.add_argument("--dhcp", action="store_true",
                            help="leave the camera on DHCP instead of assigning a static IP")
    p.add_argument("--config", default=str(BASE_DIR / "config" / "raythink.config.json"),
                   help="shared settings JSON (default: config/raythink.config.json)")
    args = p.parse_args()

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

    try:
        profile_path = resolve_profile(settings, args.profile)
    except CameraError as e:
        sys.exit(str(e))

    client = RaythinkCameraClient(
        host=settings.get("host", DEFAULT_HOST),
        username=settings.get("username", DEFAULT_USERNAME),
        scheme=settings.get("scheme", DEFAULT_SCHEME),
        verify=not settings.get("insecure", True),
    )
    try:
        result = configure_camera(client, profile_name=args.profile,
                                  profile_path=str(profile_path), octet=octet,
                                  settings=settings)
    except CameraError as e:
        log.error("Provisioning FAILED: %s", e)
        sys.exit(1)          # the finally below closes the client exactly once
    finally:
        client.close()
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
