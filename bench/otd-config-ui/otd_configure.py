#!/usr/bin/env python3
"""Provision a Teltonika OTD500 (RutOS) — the OTD-specific pipeline + CLI.

The device client (TeltonikaClient and the shared helpers) lives in the
`bench_core` package; this module owns only the OTD500 step sequence
(set password -> hostname -> timezone -> SIM switch -> 4G-only -> firmware ->
quota-sync -> RMS -> Tailscale -> [optional] eSIM) and the single-device CLI.

CLI (single device):
  python3 otd_configure.py --site haifa-port --label-password 'Xy7Kp2Lm9Qa'
  python3 otd_configure.py --verify --site haifa-port   # check, change nothing
"""
from __future__ import annotations

import argparse
import logging
import sys
from getpass import getpass
from typing import Optional

from bench_core import (
    DEFAULT_HOST,
    DEFAULT_NAME_PREFIX,
    DEFAULT_NEW_PASSWORD,
    DEFAULT_SCHEME,
    DEFAULT_TIMEZONE,
    DEFAULT_USERNAME,
    LOG_LINE_FORMAT,
    TeltonikaClient,
    assert_device_model,
    device_name,
    format_verification,
    load_settings,
    log,
    assign_rms_pack,
    make_step_runner,
    register_in_rms,
    set_log_serial,
    validate_sim_switch_config,
)


# --- pipeline (shared by CLI + web UI) --------------------------------------

def configure_device(client: TeltonikaClient, *, label_password: str, site_name: str,
                     settings: dict, expected: Optional[dict] = None) -> dict:
    """Run the full provisioning pipeline for ONE device. Logs every step through
    the shared 'teltonika' logger. Returns the discovered identity + verification warnings.
    Raises SystemExit on a hard failure (login, password change, firmware)."""
    name = device_name(site_name, settings.get("name_prefix", DEFAULT_NAME_PREFIX))
    new_password = settings.get("new_password", DEFAULT_NEW_PASSWORD)

    # Fail fast on a bad sim_switch block BEFORE touching the device: the
    # sim-switch step commits UCI before the quota-sync step parses the
    # operator table, so a typo caught only there would leave the device
    # half-configured.
    sim_switch = settings.get("sim_switch", {}) or {}
    if sim_switch.get("enabled"):
        validate_sim_switch_config(sim_switch)

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
    # Guard against a wrong-tab mix-up: OTD500 and RUTM08 share the factory IP,
    # so detection alone can't tell them apart. Abort before we name/register a
    # RUTM as an OTD.
    assert_device_model(identity, "OTD", "OTD500 configurator")
    warnings = client.verify_identity(identity, expected or {})

    # Critical steps abort the run (login + password already ran above).
    client.set_admin_password(new_password)

    # Every later step is run through _step so a failure is recorded (not
    # swallowed) and the run reports it instead of falsely "completing".
    failures, _step = make_step_runner(log)

    # Local config first (offline UCI): set the SIM to 4G *before* we wait for
    # data, and so settings survive a keep-settings firmware reboot.
    _step("hostname", lambda: client.set_hostname(name))
    _step("timezone", lambda: client.set_timezone(settings.get("timezone", DEFAULT_TIMEZONE)))
    # SIM failover rules (TEC-359): pure UCI, needs no SIM inserted, and UCI
    # survives a keep-settings firmware flash — so it runs here, before the
    # 4G-only switch (the one step that bounces the modem). The quota-sync half
    # of TEC-359 deploys FILES and must wait until after the firmware step —
    # see below.
    if sim_switch.get("enabled"):
        _step("sim-switch", lambda: client.configure_sim_switch(sim_switch))
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

    # The quota-sync files live OUTSIDE /etc/config (/usr/local/bin +
    # /etc/init.d + the rc.d symlink). install_quota_sync adds them to
    # /etc/sysupgrade.conf so a LATER upgrade keeps them, but that can't help a
    # device being flashed right now — it has nothing to preserve yet. So unlike
    # the sim-switch step above, this must run AFTER the firmware step.
    if sim_switch.get("enabled"):
        _step("quota-sync", lambda: client.install_quota_sync(sim_switch))

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
            # License: assign the Management pack (the RMS UI's "Set pack"
            # action) so the device doesn't run on 30-day credits.
            if rms.get("pack"):
                _step("rms-set-pack", lambda: assign_rms_pack(
                    rms.get("api_token", ""), rms.get("company_id", ""),
                    serial=identity.get("serial", ""), pack=str(rms.get("pack")),
                    wait=net_timeout))
        else:
            log.info("RMS api_token/company_id not set — device enabled on-device only "
                     "(set rms.api_token + rms.company_id in the config to also register "
                     "it in the RMS cloud).")

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
            sim_switch=sim_switch,
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


# --- verify-only pass (TEC-348) ---------------------------------------------

def verify_device(client: TeltonikaClient, *, settings: dict, resolve=None,
                  site_name: str = "") -> dict:
    """Check ONE finished OTD500 against its intended state, changing nothing.

    The widest row set of the five tools — password, hostname, timezone, SIM
    4G-only, the SIM-switch/quota-sync block, RMS, Tailscale, eSIM and firmware
    — because the OTD500 baseline touches all of it. Every row is the same check
    the configure pipeline runs, asked without writing anything first.

    The hostname is per-unit (built from a site name typed during the original
    run), so `resolve` recovers it from the unit's configure record and
    `site_name` lets an operator state it instead. An OTD500 keeps its factory
    address, so unlike the router and the switch there is no LAN-move row here.
    """
    new_password = settings.get("new_password", DEFAULT_NEW_PASSWORD)
    sim_switch = settings.get("sim_switch", {}) or {}
    rms = settings.get("rms", {}) or {}
    ts = settings.get("tailscale", {}) or {}
    esim = settings.get("esim", {}) or {}
    fw = settings.get("firmware", {}) or {}

    client.login(new_password)
    # Everything past the login is a read, enforced rather than intended.
    client.set_read_only()

    identity = client.get_identity()
    assert_device_model(identity, "OTD", "OTD500 configurator")

    expected, prior_run_row = resolve(identity) if resolve else ({}, None)
    prefix = settings.get("name_prefix", DEFAULT_NAME_PREFIX)
    name = (device_name(site_name, prefix) if site_name
            else expected.get("hostname") or "")

    verification: list[dict] = []
    if prior_run_row is not None:
        verification.append(prior_run_row)
    if not name:
        # Nothing to compare against. Reading the device's hostname and calling
        # it expected would pass by construction — the tautology TEC-348 exists
        # to remove — so report what is missing instead.
        actual = client.ssh_exec("uci get system.system.hostname 2>/dev/null",
                                 check=False).strip()
        verification.append({
            "item": "hostname",
            "expected": "the name from this unit's configure run",
            "actual": f"{actual or '(unset)'} — no recorded name to compare it with",
            "ok": False})

    # An eSIM row only makes sense if this station loads eSIM profiles at all.
    # Whether THIS unit got one is a per-unit fact and lives in its record.
    esim_expected = bool(esim.get("enabled")) and (
        bool(expected.get("esim_activation_code")) if expected else True)

    verification += [c for c in client.verify_configuration(
        hostname=name,
        zonename=settings.get("timezone", DEFAULT_TIMEZONE),
        new_password=new_password,
        sim_4g=bool(settings.get("sim_4g_only", True)),
        sim_switch=sim_switch,
        rms=bool(rms.get("enabled")),
        tailscale=bool(ts.get("enabled")),
        esim=esim_expected,
        expected_firmware=(fw.get("expected_version") or ""),
        rms_api_token=rms.get("api_token", ""),
        serial=identity.get("serial", ""),
    ) if not (c["item"] == "hostname" and not name)]   # already reported above

    for line in format_verification(verification).splitlines():
        log.info("%s", line)

    failed = [c["item"] for c in verification if c["ok"] is False]
    ok = not failed
    label = name or f"OTD500 {identity.get('serial', 'unknown')}"
    if ok:
        log.info("%s PASSED verification — nothing was changed.", label)
    else:
        log.error("%s FAILED verification: %s", label, ", ".join(failed))
    return {"name": name, "identity": identity, "warnings": [], "failures": [],
            "verification": verification, "ok": ok}


def main():
    p = argparse.ArgumentParser(description="Provision a single Teltonika OTD500.")
    p.add_argument("--site", help="site name -> device becomes otd-<site>")
    p.add_argument("--label-password", help="the device's factory label password "
                   "(else prompted)")
    p.add_argument("--config", default="config/site.config.json",
                   help="shared settings JSON (default: config/site.config.json)")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--username", default=DEFAULT_USERNAME)
    p.add_argument("--scheme", default=DEFAULT_SCHEME, choices=["http", "https"])
    p.add_argument("--no-firmware", action="store_true", help="skip the firmware upgrade")
    p.add_argument("--verify", action="store_true",
                   help="check a finished device against its intended state and "
                        "change nothing (TEC-348); needs --site to know the "
                        "hostname to expect. Exits 0 only on a full PASS.")
    p.add_argument("--serial", help="expected serial (verification)")
    p.add_argument("--imei", help="expected IMEI (verification)")
    p.add_argument("--mac", help="expected LAN MAC (verification)")
    args = p.parse_args()

    if not args.verify and not args.site:
        p.error("--site is required when provisioning a device")

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

    label_pw = args.label_password or (
        "" if args.verify else getpass("Device label password: "))

    client = TeltonikaClient(host=args.host, username=args.username, scheme=args.scheme,
                             verify=not settings.get("insecure", True))
    try:
        if args.verify:
            result = verify_device(client, settings=settings,
                                   site_name=args.site or "")
        else:
            result = configure_device(
                client, label_password=label_pw, site_name=args.site,
                settings=settings,
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
