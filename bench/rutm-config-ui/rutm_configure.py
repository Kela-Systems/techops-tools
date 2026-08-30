#!/usr/bin/env python3
"""
Provision a Teltonika RUTM08 (RutOS) over its REST API + SSH/UCI.

A fresh RUTM08 boots on 192.168.1.1 with a UNIQUE factory password printed on
the device label and forces a password change on first login. Pipeline for one
device (devices are done one at a time, no manifest — the site name and label
password are typed in per device):

  login(label_pw) -> set password "Kelasys123!" -> hostname rut-<site>
    -> timezone Asia/Jerusalem -> firmware (latest-stable) -> enable+register RMS
    -> join Tailscale -> verify -> move LAN to 192.168.88.1 (LAST — drops the
       connection; confirmed by reaching the device on the new address)

The RUTM08 is an Ethernet-only router (no modem/SIM), so the OTD's 4G-only and
eSIM steps don't apply, and "internet" means the WAN port is plugged into an
uplink. Everything else is the same RutOS surface as the OTD500, so the device
client is reused from the shared bench_core package (the single
field-tested copy both configurators import).

CLI (single device):
  python3 rutm_configure.py --site haifa-port --label-password 'Xy7Kp2Lm9Qa'
  python3 rutm_configure.py --verify --site haifa-port   # check, change nothing
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from getpass import getpass
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent

from bench_core import (
    DEFAULT_HOST,
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
)

# --- RUTM08 defaults ----------------------------------------------------------
DEFAULT_RUTM_PREFIX = "rut-"
DEFAULT_RUTM_LAN_IP = "192.168.88.1"


class RutmClient(TeltonikaClient):
    """TeltonikaClient with the one RUTM08 specific left: the wired-WAN internet
    wait. (Model no longer needs fixing up here: the shared client reads the real
    model from mnfinfo/REST and returns 'unknown' when it can't, so the guard
    refuses to guess rather than defaulting to a family name. `move_lan` moved to
    the shared client when the TSW202 tool needed it too — the RUTM08 call is
    unchanged, since a router needs neither netmask/gateway stated nor the host
    DHCP renew turned off.)"""

    def wait_for_internet(self, timeout: int = 180, target: str = "8.8.8.8") -> bool:
        """Same stability probe as the OTD's mobile wait (ICMP, HTTP fallback,
        3 consecutive successes), minus the modem/SIM diagnostics — on a RUTM08
        internet means the WAN port is plugged into a live uplink."""
        log.info("Waiting for internet over the WAN port ...")
        probe = (f"(ping -c1 -W3 {target} >/dev/null 2>&1 || "
                 "wget -q -T5 -O /dev/null http://detectportal.firefox.com/success.txt "
                 ">/dev/null 2>&1) && echo OK || echo NO")
        needed_streak = 3
        deadline = time.time() + timeout
        streak = 0
        warned = False
        while time.time() < deadline:
            if self.ssh_exec(probe, check=False).strip().endswith("OK"):
                streak += 1
                if streak >= needed_streak:
                    log.info("Internet is up and stable.")
                    self._online = True
                    return True
                time.sleep(2)
                continue
            streak = 0
            if not warned:
                log.info("...no internet yet — is the WAN port plugged into an uplink?")
                warned = True
            time.sleep(5)
        log.warning("No internet after %ds — check the WAN cable/uplink.", timeout)
        self._online = False
        return False


# --- pipeline (shared by CLI + web UI) ----------------------------------------

def configure_rutm(client: RutmClient, *, site_name: str, initial_password: str,
                   settings: dict) -> dict:
    """Run the full provisioning pipeline for ONE RUTM08. Logs every step through
    the shared 'teltonika' logger. Returns identity + per-step failures + verification.
    Raises SystemExit on a hard failure (login, password change, firmware)."""
    name = device_name(site_name, settings.get("name_prefix", DEFAULT_RUTM_PREFIX))
    new_password = settings.get("new_password", DEFAULT_NEW_PASSWORD)

    # Let SSH fall back to the shared password for the whole run, so a device
    # left half-changed by an earlier failed run still provisions cleanly.
    client._ssh_alt_passwords = [new_password]

    # Fresh router: the label password works. Re-run of a device that already
    # got past the password step: the shared password works. An EMPTY initial
    # password means "this one is already on the shared password" — skip the
    # label attempt entirely.
    if initial_password and initial_password != new_password:
        try:
            client.login(initial_password)
        except SystemExit as e:
            log.info("Label password rejected (%s) — retrying with the shared "
                     "password (device may be half-provisioned by an earlier run).", e)
            client.login(new_password)
    else:
        client.login(new_password)

    identity = client.get_identity()
    # Guard against a wrong-tab mix-up: OTD500 and RUTM08 share the factory IP,
    # so detection alone can't tell them apart. Abort before we name/register an
    # OTD as a RUTM.
    assert_device_model(identity, "RUTM", "RUTM08 configurator")

    # Critical step: aborts the run on failure.
    client.set_admin_password(new_password)

    # Every later step is run through _step so a failure is recorded (not
    # swallowed) and the run reports it instead of falsely "completing".
    failures, _step = make_step_runner(log)

    _step("hostname", lambda: client.set_hostname(name))
    _step("timezone", lambda: client.set_timezone(settings.get("timezone", DEFAULT_TIMEZONE)))

    net_timeout = int(settings.get("internet_timeout", 180))
    wait_net = settings.get("wait_for_internet", True)

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
            raise SystemExit("FOTA needs internet — plug the WAN port into an uplink.")
        client.upgrade_firmware(fota=True, keep_settings=fw.get("keep_settings", True),
                                net_wait=net_timeout)
        client.get_identity()
    elif mode == "rms":
        log.info("Firmware upgrade deferred to RMS (pending action on first connect).")

    # RMS has two halves: enable the on-device client, then register the unit in
    # the RMS cloud by serial+MAC so it actually appears in the account.
    rms = settings.get("rms", {}) or {}
    if rms.get("enabled"):
        _step("rms-enable", lambda: client.enable_rms(rms.get("auth_code", "")))
        if rms.get("api_token") and rms.get("company_id"):
            _step("rms-register", lambda: register_in_rms(
                rms.get("api_token", ""), rms.get("company_id", ""),
                name=name, serial=identity.get("serial", ""),
                mac=identity.get("mac", ""), device_password=new_password,
                device_series=rms.get("device_series", "rutm"),
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

    ts = settings.get("tailscale", {}) or {}
    if ts.get("enabled"):
        if wait_net and not client.ensure_online(net_timeout):
            failures.append("tailscale: no internet — could not join (check the WAN uplink)")
            log.error("Tailscale: no internet — cannot join.")
        else:
            _step("tailscale", lambda: client.join_tailscale(
                ts.get("_resolved_auth_key") or ts.get("auth_key", ""),
                name, ts.get("login_server", "")))

    # Verify while the device is still reachable on its current address.
    verification: list[dict] = []
    try:
        verification = client.verify_configuration(
            hostname=name,
            zonename=settings.get("timezone", DEFAULT_TIMEZONE),
            new_password=new_password,
            sim_4g=False,
            rms=bool(rms.get("enabled")),
            tailscale=bool(ts.get("enabled")),
            esim=False,
            expected_firmware=(fw.get("expected_version") or client.fw_target or ""),
            rms_api_token=rms.get("api_token", ""),
            serial=identity.get("serial", ""),
        )
        verification = [c for c in verification if c["item"] != "SIM 4G-only"]
        for line in format_verification(verification).splitlines():
            log.info("%s", line)
    except SystemExit as e:
        log.error("Verification step could not run: %s", e)

    # LAN move LAST — after this the device answers on lan_ip, not 192.168.1.1.
    lan_ip = (settings.get("lan_ip") or "").strip()
    if lan_ip:
        try:
            verification.append(client.move_lan(lan_ip))
        except SystemExit as e:
            failures.append(f"lan-ip: {e}")
            log.error("Step 'lan-ip' FAILED: %s", e)

    verify_failed = [c["item"] for c in verification if c["ok"] is False]
    ok = not failures and not verify_failed
    if ok:
        log.info("Provisioning complete for %s — all steps verified.", name)
    else:
        problems = failures + [f"verify:{i}" for i in verify_failed]
        log.error("Provisioning of %s finished with problems: %s", name, " | ".join(problems))
    return {"name": name, "identity": identity, "warnings": [],
            "failures": failures, "verification": verification, "ok": ok}


# --- verify-only pass (TEC-348) -----------------------------------------------

def verify_rutm(client: RutmClient, *, settings: dict, resolve=None,
                site_name: str = "") -> dict:
    """Check ONE finished RUTM08 against its intended state, changing nothing.

    Unlike the TSW202, one expectation here IS per-unit: the hostname is built
    from a site name somebody typed during the original run, and on a QA sweep
    nobody remembers it. So `resolve` (`BenchConfigurator.verify_resolver`) reads
    it back out of the device's own configure record, and `site_name` lets an
    operator state it instead.

    The LAN IP row is where this mode earns its keep. On a configure run the
    check is "did the router come back on 192.168.88.1 after we restarted its
    network", and a no-answer there is genuinely inconclusive — the station's
    DHCP lease may be stale. Here the router is in front of us at a known
    address, so `lan_ip_check` reports a fact instead of a maybe.
    """
    new_password = settings.get("new_password", DEFAULT_NEW_PASSWORD)
    rms = settings.get("rms", {}) or {}
    ts = settings.get("tailscale", {}) or {}
    fw = settings.get("firmware", {}) or {}

    client.login(new_password)
    # Everything past the login is a read, enforced rather than intended.
    client.set_read_only()

    identity = client.get_identity()
    assert_device_model(identity, "RUTM", "RUTM08 configurator")

    expected, prior_run_row = resolve(identity) if resolve else ({}, None)
    # An explicit site name wins over the record: an engineer checking a unit
    # against what it SHOULD be needs to be able to say so.
    prefix = settings.get("name_prefix", DEFAULT_RUTM_PREFIX)
    name = (device_name(site_name, prefix) if site_name
            else expected.get("hostname") or "")

    verification: list[dict] = []
    if prior_run_row is not None:
        verification.append(prior_run_row)
    if not name:
        # No recorded hostname and none supplied. Reporting the device's own
        # hostname back to itself would pass by construction, which is the
        # tautology this whole issue is about — so say what is missing instead.
        actual = client.ssh_exec("uci get system.system.hostname 2>/dev/null",
                                 check=False).strip()
        verification.append({
            "item": "hostname",
            "expected": "the name from this unit's configure run",
            "actual": f"{actual or '(unset)'} — no recorded name to compare it with",
            "ok": False})

    verification += [c for c in client.verify_configuration(
        hostname=name,
        zonename=settings.get("timezone", DEFAULT_TIMEZONE),
        new_password=new_password,
        sim_4g=False,
        rms=bool(rms.get("enabled")),
        tailscale=bool(ts.get("enabled")),
        esim=False,
        expected_firmware=(fw.get("expected_version") or ""),
        rms_api_token=rms.get("api_token", ""),
        serial=identity.get("serial", ""),
    ) if c["item"] != "SIM 4G-only"           # no modem on a RUTM08
        and not (c["item"] == "hostname" and not name)]  # already reported above

    lan_ip = (settings.get("lan_ip") or "").strip()
    if lan_ip:
        verification.append(client.lan_ip_check(lan_ip))

    for line in format_verification(verification).splitlines():
        log.info("%s", line)

    failed = [c["item"] for c in verification if c["ok"] is False]
    ok = not failed
    label = name or f"RUTM08 {identity.get('serial', 'unknown')}"
    if ok:
        log.info("%s PASSED verification — nothing was changed.", label)
    else:
        log.error("%s FAILED verification: %s", label, ", ".join(failed))
    return {"name": name, "identity": identity, "warnings": [], "failures": [],
            "verification": verification, "ok": ok}


def main():
    p = argparse.ArgumentParser(description="Provision a single Teltonika RUTM08.")
    p.add_argument("--site", help="site name -> hostname rut-<site>")
    p.add_argument("--label-password",
                   help="factory password from the device label (prompts if omitted; "
                        "pass '' for a device already on the shared password)")
    p.add_argument("--verify", action="store_true",
                   help="check a finished router against its intended state and "
                        "change nothing (TEC-348); needs --site to know the "
                        "hostname to expect. Exits 0 only on a full PASS.")
    p.add_argument("--config", default=str(BASE_DIR / "config" / "rutm.config.json"),
                   help="shared settings JSON (default: config/rutm.config.json)")
    args = p.parse_args()

    if not args.verify and not args.site:
        p.error("--site is required when provisioning a device")

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_LINE_FORMAT))
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    settings = load_settings(args.config)
    label_pw = args.label_password
    if label_pw is None and not args.verify:
        label_pw = getpass("Label password (empty = already on the shared password): ")

    set_log_serial(None)
    # A finished router answers on its FINAL address, not the factory one.
    host = (settings.get("lan_ip", DEFAULT_RUTM_LAN_IP) if args.verify
            else settings.get("host", DEFAULT_HOST))
    client = RutmClient(
        host=host,
        username=settings.get("username", DEFAULT_USERNAME),
        scheme=settings.get("scheme", DEFAULT_SCHEME),
        verify=not settings.get("insecure", True),
    )
    try:
        result = (verify_rutm(client, settings=settings, site_name=args.site or "")
                  if args.verify
                  else configure_rutm(client, site_name=args.site,
                                      initial_password=label_pw, settings=settings))
    finally:
        client.close()
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
