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
"""
from __future__ import annotations

import argparse
import json
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
    device_name,
    format_verification,
    host_iface_for,
    log,
    normalize_mac,  # noqa: F401  (re-exported for rutm_app)
    register_in_rms,
    renew_host_dhcp,
    set_log_serial,
)

# --- RUTM08 defaults ----------------------------------------------------------
DEFAULT_RUTM_PREFIX = "rut-"
DEFAULT_RUTM_LAN_IP = "192.168.88.1"


class RutmClient(TeltonikaClient):
    """TeltonikaClient with the RUTM08 specifics: wired-WAN internet wait,
    correct model fallback, and the LAN-move step."""

    def get_identity(self) -> dict:
        identity = super().get_identity()
        # The shared client falls back to model='OTD500' when mnfinfo is missing.
        if identity.get("model") == "OTD500" and not identity.get("raw", {}).get("mnfinfo"):
            identity["model"] = "RUTM08"
        return identity

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

    # --- LAN move (must run LAST) --------------------------------------------
    def current_lan_ip(self) -> str:
        return self.ssh_exec("uci get network.lan.ipaddr 2>/dev/null", check=False).strip()

    def move_lan(self, new_ip: str, wait: int = 120) -> dict:
        """Point the LAN interface at `new_ip`. We are talking to the device OVER
        that LAN, so the network restart drops the connection by design: commit
        synchronously, restart fire-and-forget, then confirm by reaching the
        device on the new address (renewing the host's DHCP lease so the laptop
        follows it into the new subnet). The DHCP pool follows the interface
        subnet automatically, so only ipaddr needs changing.

        Returns a verification-style check dict; never raises after the commit
        (past that point the device is moving whether we can see it or not)."""
        cur = self.current_lan_ip()
        if cur == new_ip:
            log.info("LAN IP is already %s; skipping the move.", new_ip)
            return {"item": "LAN IP", "expected": new_ip,
                    "actual": f"{new_ip} (already set)", "ok": True}

        # Resolve the host-side interface while the device is still reachable.
        iface = host_iface_for(self.host)
        log.info("Moving the LAN from %s to %s — the connection will drop ...",
                 cur or self.host, new_ip)
        self.ssh_exec(f"uci set network.lan.ipaddr='{new_ip}' && uci commit network")
        self._fire_and_forget("sleep 1; /etc/init.d/network restart")
        self.close()
        self.host = new_ip
        self.base = f"{self.scheme}://{new_ip}/api"

        port = 443 if self.scheme == "https" else 80
        deadline = time.time() + wait
        time.sleep(5)
        renew_host_dhcp(iface)
        renewed_again = False
        while time.time() < deadline:
            if self._port_open(port):
                log.info("Device is answering on %s.", new_ip)
                return {"item": "LAN IP", "expected": new_ip,
                        "actual": f"answering on {new_ip}", "ok": True}
            if not renewed_again and time.time() > deadline - wait / 2:
                renew_host_dhcp(iface)
                renewed_again = True
            time.sleep(3)
        log.warning("Device did not answer on %s within %ds — it may still be fine; "
                    "check that the laptop picked up a lease in the new subnet.",
                    new_ip, wait)
        return {"item": "LAN IP", "expected": new_ip,
                "actual": f"no answer on {new_ip} after {wait}s "
                          "(laptop lease may be stale)", "ok": False}


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

    # Critical step: aborts the run on failure.
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
        else:
            log.info("RMS api_token/company_id not set — device enabled on-device only "
                     "(register it in the RMS cloud manually or via rms_register.py).")

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


def load_settings(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}


def main():
    p = argparse.ArgumentParser(description="Provision a single Teltonika RUTM08.")
    p.add_argument("--site", required=True, help="site name -> hostname rut-<site>")
    p.add_argument("--label-password",
                   help="factory password from the device label (prompts if omitted; "
                        "pass '' for a device already on the shared password)")
    p.add_argument("--config", default=str(BASE_DIR / "rutm.config.json"),
                   help="shared settings JSON (default: rutm.config.json)")
    args = p.parse_args()

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_LINE_FORMAT))
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    settings = load_settings(args.config)
    label_pw = args.label_password
    if label_pw is None:
        label_pw = getpass("Label password (empty = already on the shared password): ")

    set_log_serial(None)
    client = RutmClient(
        host=settings.get("host", DEFAULT_HOST),
        username=settings.get("username", DEFAULT_USERNAME),
        scheme=settings.get("scheme", DEFAULT_SCHEME),
        verify=not settings.get("insecure", True),
    )
    try:
        result = configure_rutm(client, site_name=args.site,
                                initial_password=label_pw, settings=settings)
    finally:
        client.close()
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
