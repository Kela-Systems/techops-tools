#!/usr/bin/env python3
"""
Provision a single Raythink thermal camera over its RPC2 JSON API.

A fresh camera ships on the static IP 192.168.1.123 with admin/admin. Pipeline
for one camera (one at a time — the profile and the final IP octet are chosen in
the UI / on the CLI):

  login(admin/admin) -> set password "Kelafield123!" -> import config profile
    (LAN or Cellular) -> set NTP 192.168.88.10 + sync clock to PC time -> set
    static IP 192.168.88.XX (LAST — drops the connection; confirmed by reaching
    the camera on the new address) -> verify

The IP move runs last for the same reason as the RUTM08 LAN move: the moment it
applies, the camera leaves 192.168.1.123. Because a full config import can reset
the session (or reboot the camera), we re-login after importing and re-assert the
target password before continuing.

CLI (single camera):
  python3 raythink_configure.py --profile lan --ip 30
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

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


def device_name(octet: int) -> str:
    return f"raythink-{octet}"


def resolve_profile(settings: dict, profile_name: str) -> Path:
    """Map a profile key (e.g. 'lan') to its JSON file under the tool folder."""
    profiles = settings.get("profiles", {}) or {}
    rel = profiles.get(profile_name)
    if not rel:
        raise CameraError(f"Unknown profile '{profile_name}'. Known: {', '.join(profiles) or '(none)'}.")
    path = (BASE_DIR / rel) if not Path(rel).is_absolute() else Path(rel)
    if not path.is_file():
        raise CameraError(f"Profile file for '{profile_name}' not found: {path}")
    return path


def target_ip_for(settings: dict, octet: int) -> str:
    prefix = (settings.get("static", {}) or {}).get("subnet_prefix", DEFAULT_SUBNET_PREFIX)
    return f"{prefix}.{octet}"


# --- pipeline (shared by CLI + web UI) ----------------------------------------

def configure_camera(client: RaythinkCameraClient, *, profile_name: str,
                     profile_path: str, octet: int, settings: dict) -> dict:
    """Run the full provisioning pipeline for ONE camera. Logs every step through
    the 'raythink' logger. Returns identity + per-step failures + verification.
    Raises CameraError on a hard failure (login, password change)."""
    static = settings.get("static", {}) or {}
    gateway = static.get("gateway", DEFAULT_GATEWAY)
    netmask = static.get("netmask", DEFAULT_NETMASK)
    ntp_server = settings.get("ntp_server", DEFAULT_NTP_SERVER)
    initial_pw = settings.get("initial_password", DEFAULT_INITIAL_PASSWORD)
    new_pw = settings.get("new_password", DEFAULT_NEW_PASSWORD)
    target_ip = target_ip_for(settings, octet)
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

    failures: list[str] = []

    def _step(label: str, fn) -> None:
        try:
            fn()
        except CameraError as e:
            failures.append(f"{label}: {e}")
            log.error("Step '%s' FAILED: %s", label, e)

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

    # 5. Static IP — LAST. After this the camera answers on target_ip, not
    # 192.168.1.123. set_static_ip never raises after the write; it returns a
    # verification row.
    verification: list[dict] = []
    try:
        verification.append(client.set_static_ip(
            target_ip, netmask, gateway, wait=int(settings.get("ip_move_timeout", 120))))
    except CameraError as e:
        failures.append(f"static-ip: {e}")
        log.error("Step 'static-ip' FAILED: %s", e)

    # 6. Verify on the new address (re-login there first, if it answers).
    if client.port_open():
        try:
            client.relogin([new_pw], settle=2)
        except CameraError as e:
            log.warning("Could not re-login on the new IP for verification: %s", e)
        try:
            verification += client.verify_configuration(
                new_password=new_pw, ntp_server=ntp_server, ip=target_ip,
                netmask=netmask, gateway=gateway, profile_name=profile_name,
                imported=imported)
            for line in format_verification(verification).splitlines():
                log.info("%s", line)
        except CameraError as e:
            log.error("Verification could not run: %s", e)
    else:
        log.warning("Camera is not answering on %s — skipping read-back verification.", target_ip)

    verify_failed = [c["item"] for c in verification if c["ok"] is False]
    ok = not failures and not verify_failed
    if ok:
        log.info("Provisioning complete for %s (%s) — all steps verified.", name, target_ip)
    else:
        problems = failures + [f"verify:{i}" for i in verify_failed]
        log.error("Provisioning of %s finished with problems: %s", name, " | ".join(problems))
    return {"name": name, "hostname": name, "identity": identity, "warnings": [],
            "failures": failures, "verification": verification, "ok": ok,
            "profile": profile_name, "ip": target_ip}


def load_settings(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}


def main():
    p = argparse.ArgumentParser(description="Provision a single Raythink camera.")
    p.add_argument("--profile", required=True, help="config profile key (e.g. lan, cellular)")
    p.add_argument("--ip", required=True, type=int,
                   help="last octet of the static IP (e.g. 30 -> 192.168.88.30)")
    p.add_argument("--config", default=str(BASE_DIR / "raythink.config.json"),
                   help="shared settings JSON (default: raythink.config.json)")
    args = p.parse_args()

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_LINE_FORMAT))
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    settings = load_settings(args.config)
    static = settings.get("static", {}) or {}
    lo = int(static.get("octet_min", DEFAULT_OCTET_MIN))
    hi = int(static.get("octet_max", DEFAULT_OCTET_MAX))
    if not (lo <= args.ip <= hi):
        sys.exit(f"--ip {args.ip} is out of the allowed range {lo}-{hi}.")

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
                                  profile_path=str(profile_path), octet=args.ip,
                                  settings=settings)
    except CameraError as e:
        log.error("Provisioning FAILED: %s", e)
        client.close()
        sys.exit(1)
    finally:
        client.close()
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
