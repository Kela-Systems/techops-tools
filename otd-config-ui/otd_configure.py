#!/usr/bin/env python3
"""Provision a Teltonika OTD500 (RutOS) — the OTD-specific pipeline + CLI.

The device client (TeltonikaClient and the shared helpers) lives in the
`teltonika_provision` package; this module owns only the OTD500 step sequence
(set password -> firmware -> hostname -> timezone -> 4G-only -> RMS ->
Tailscale -> [optional] eSIM) and the single-device CLI.

CLI (single device):
  python3 otd_configure.py --site haifa-port --label-password 'Xy7Kp2Lm9Qa'
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from getpass import getpass
from typing import Optional

from teltonika_provision import (
    DEFAULT_HOST,
    DEFAULT_NAME_PREFIX,
    DEFAULT_NEW_PASSWORD,
    DEFAULT_SCHEME,
    DEFAULT_TIMEZONE,
    DEFAULT_USERNAME,
    LOG_LINE_FORMAT,
    TeltonikaClient,
    device_name,
    format_verification,
    log,
    register_in_rms,
    set_log_serial,
)


# --- pipeline (shared by CLI + web UI) --------------------------------------

def configure_device(client: TeltonikaClient, *, label_password: str, site_name: str,
                     settings: dict, expected: Optional[dict] = None) -> dict:
    """Run the full provisioning pipeline for ONE device. Logs every step through
    the shared 'teltonika' logger. Returns the discovered identity + verification warnings.
    Raises SystemExit on a hard failure (login, password change, firmware)."""
    name = device_name(site_name, settings.get("name_prefix", DEFAULT_NAME_PREFIX))
    new_password = settings.get("new_password", DEFAULT_NEW_PASSWORD)

    # Let SSH fall back to the shared password for the whole run, so a device
    # left half-changed by an earlier failed run (root already on the new
    # password, admin still on the label one) still provisions cleanly.
    client._ssh_alt_passwords = [new_password]

    try:
        client.login(label_password)
    except SystemExit as e:
        # A device that failed an earlier run AFTER the password step is already
        # on the shared password — heal (the REST mirror of _ssh_alt_passwords)
        # instead of dead-ending every retry on "Login failed".
        if not new_password or new_password == label_password:
            raise
        log.info("Label password rejected (%s) — retrying with the shared "
                 "password (device may be half-provisioned by an earlier run).", e)
        client.login(new_password)
    identity = client.get_identity()
    warnings = client.verify_identity(identity, expected or {})

    # Critical steps abort the run (login + password already ran above).
    client.set_admin_password(new_password)

    # Every later step is run through _step so a failure is recorded (not
    # swallowed) and the run reports it instead of falsely "completing".
    failures: list[str] = []

    def _step(label: str, fn) -> None:
        try:
            fn()
        except SystemExit as e:
            failures.append(f"{label}: {e}")
            log.error("Step '%s' FAILED: %s", label, e)

    # Local config first (offline UCI): set the SIM to 4G *before* we wait for
    # data, and so settings survive a keep-settings firmware reboot.
    _step("hostname", lambda: client.set_hostname(name))
    _step("timezone", lambda: client.set_timezone(settings.get("timezone", DEFAULT_TIMEZONE)))
    if settings.get("sim_4g_only", True):
        _step("sim-4g-only", client.set_sims_4g_only)

    wait_net = settings.get("wait_for_internet", True)
    net_timeout = int(settings.get("internet_timeout", 180))

    fw = settings.get("firmware", {}) or {}
    mode = fw.get("mode", "none")
    if mode == "local":
        # Firmware is critical (it reboots) — let it abort the run on failure.
        client.upgrade_firmware(bin_path=fw.get("bin_path"),
                                keep_settings=fw.get("keep_settings", True),
                                skip_if_version=fw.get("expected_version", ""))
        client.get_identity()  # refresh fw version after reboot
    elif mode == "fota":
        if not client.ensure_online(net_timeout):
            raise SystemExit("FOTA needs internet, but the SIM has no data connection.")
        client.upgrade_firmware(fota=True, keep_settings=fw.get("keep_settings", True),
                                net_wait=net_timeout)
        client.get_identity()
    elif mode == "rms":
        log.info("Firmware upgrade deferred to RMS (pending action on first connect).")

    # RMS has two halves: (1) enable the on-device client (offline UCI) and
    # (2) register the unit in the RMS cloud by serial+MAC so it actually appears
    # in the account. (2) is a host-side API call and needs api_token+company_id.
    rms = settings.get("rms", {}) or {}
    if rms.get("enabled"):
        _step("rms-enable", lambda: client.enable_rms(rms.get("auth_code", "")))
        if rms.get("api_token") and rms.get("company_id"):
            reg_mac = identity.get("mac") if identity.get("mac") not in ("", "unknown") \
                else (expected or {}).get("mac", "")
            _step("rms-register", lambda: register_in_rms(
                rms.get("api_token", ""), rms.get("company_id", ""),
                name=name, serial=identity.get("serial", ""), mac=reg_mac,
                device_password=new_password,
                device_series=rms.get("device_series", "otd"),
                wait=net_timeout))
        else:
            log.info("RMS api_token/company_id not set — device enabled on-device only "
                     "(run rms_register.py to register it in the cloud).")

    # Internet-dependent tail: Tailscale join + eSIM download. Wait once for the
    # modem to get data before attempting either.
    ts = settings.get("tailscale", {}) or {}
    esim = settings.get("esim", {}) or {}
    needs_esim = bool(esim.get("enabled") and expected and expected.get("esim_activation_code"))
    online = None
    if wait_net and (ts.get("enabled") or needs_esim):
        online = client.ensure_online(net_timeout)

    if ts.get("enabled"):
        if online is False:
            failures.append("tailscale: no mobile data — could not join (needs internet)")
            log.error("Tailscale: no mobile data — cannot join.")
        else:
            _step("tailscale", lambda: client.join_tailscale(
                ts.get("_resolved_auth_key") or ts.get("auth_key", ""),
                name, ts.get("login_server", "")))

    if needs_esim:
        if online is False:
            failures.append("esim: no mobile data — skipped (needs internet)")
            log.error("eSIM: no mobile data — skipped.")
        else:
            _step("esim", lambda: client.load_esim(expected["esim_activation_code"]))

    # Final verification: re-read everything off the device and report.
    verification: list[dict] = []
    try:
        verification = client.verify_configuration(
            hostname=name,
            zonename=settings.get("timezone", DEFAULT_TIMEZONE),
            new_password=new_password,
            sim_4g=bool(settings.get("sim_4g_only", True)),
            rms=bool(rms.get("enabled")),
            tailscale=bool(ts.get("enabled")),
            esim=needs_esim,
            expected_firmware=(fw.get("expected_version") or client.fw_target or ""),
            rms_api_token=rms.get("api_token", ""),
            serial=identity.get("serial", ""),
        )
        for line in format_verification(verification).splitlines():
            log.info("%s", line)
    except SystemExit as e:
        log.error("Verification step could not run: %s", e)

    verify_failed = [c["item"] for c in verification if c["ok"] is False]
    ok = not failures and not verify_failed
    if ok:
        log.info("Provisioning complete for %s — all steps verified.", name)
    else:
        problems = failures + [f"verify:{i}" for i in verify_failed]
        log.error("Provisioning of %s finished with problems: %s", name, " | ".join(problems))
    return {"name": name, "identity": identity, "warnings": warnings,
            "failures": failures, "verification": verification, "ok": ok}


def load_settings(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}


def main():
    p = argparse.ArgumentParser(description="Provision a single Teltonika OTD500.")
    p.add_argument("--site", required=True, help="site name -> device becomes otd-<site>")
    p.add_argument("--label-password", help="the device's factory label password "
                   "(else prompted)")
    p.add_argument("--config", default="site.config.json",
                   help="shared settings JSON (default: site.config.json)")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--username", default=DEFAULT_USERNAME)
    p.add_argument("--scheme", default=DEFAULT_SCHEME, choices=["http", "https"])
    p.add_argument("--no-firmware", action="store_true", help="skip the firmware upgrade")
    p.add_argument("--serial", help="expected serial (verification)")
    p.add_argument("--imei", help="expected IMEI (verification)")
    p.add_argument("--mac", help="expected LAN MAC (verification)")
    args = p.parse_args()

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_LINE_FORMAT))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    set_log_serial(None)

    try:
        settings = load_settings(args.config)
    except FileNotFoundError:
        log.warning("Config %s not found; using built-in defaults.", args.config)
        settings = {}
    if args.no_firmware:
        settings["firmware"] = {"mode": "none"}

    label_pw = args.label_password or getpass("Device label password: ")

    client = TeltonikaClient(host=args.host, username=args.username, scheme=args.scheme,
                             verify=not settings.get("insecure", True))
    try:
        result = configure_device(
            client, label_password=label_pw, site_name=args.site, settings=settings,
            expected={"serial": args.serial, "imei": args.imei, "mac": args.mac},
        )
    finally:
        client.close()

    print()
    print(format_verification(result.get("verification", [])))
    print()
    if result["ok"]:
        print("Done — all steps verified OK.")
    else:
        if result["failures"]:
            print("FAILED steps:")
            for f in result["failures"]:
                print(f"  - {f}")
        verify_failed = [c["item"] for c in result.get("verification", []) if c["ok"] is False]
        if verify_failed:
            print("Verification mismatches:")
            for i in verify_failed:
                print(f"  - {i}")
        sys.exit(1)


if __name__ == "__main__":
    main()
