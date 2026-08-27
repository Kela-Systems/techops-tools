#!/usr/bin/env python3
"""
Provision a Teltonika TSW202 managed switch (RutOS) over its REST API + SSH/UCI.

A fresh TSW202 boots on 192.168.1.2 — note the .2, NOT the .1 the OTD500 and
RUTM08 share — with a unique factory password printed on the device label, and
forces a password change on first login. Pipeline for one switch:

  login(label_pw) -> set password "Kelasys123!" -> firmware floor
    -> NTP server 192.168.88.10 -> timezone Asia/Jerusalem -> verify
    -> move the management IP to 192.168.88.2 (LAST — drops the connection;
       confirmed by reaching the switch on the new address)

Two things separate this from the router pipelines next door:

* **Firmware is a floor, not a pin.** The config names the minimum acceptable
  version; a switch that arrives OLDER is flashed from a local image, and one
  that arrives NEWER is passed through with a note in the run record rather
  than being downgraded. (Teltonika ships a "Stable" and a newer "Latest" for
  this family, so arriving-newer is the common case, not an edge one.)
* **The switch does not serve DHCP**, so the LAN move must not renew the
  station's lease — see `move_lan(renew_dhcp=False)` below. The station needs an
  address on the target subnet for the move to be confirmable.
* **Its management address is not on `network.lan`**, unlike every RutOS router,
  which is why `move_lan` discovers the section off the device. See the README.

There is no RMS, Tailscale, SIM or internet-wait step: the baseline in TEC-791
is password, firmware, time, address. The device client is the shared
`TeltonikaClient` from bench_core; this file adds only what a switch needs on
top of it.

CLI (single device):
  python3 tsw_configure.py --label-password 'Xy7Kp2Lm9Qa'
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from getpass import getpass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

from bench_core import (
    DEFAULT_NEW_PASSWORD,
    DEFAULT_SCHEME,
    DEFAULT_TIMEZONE,
    DEFAULT_USERNAME,
    LOG_LINE_FORMAT,
    POSIX_TZ,
    UCI_TIMEZONE,
    UCI_ZONENAME,
    TeltonikaClient,
    assert_device_model,
    format_verification,
    fw_version_at_least,
    fw_versions_match,
    load_settings,
    log,
    make_step_runner,
    set_log_serial,
)

# --- TSW202 factory + target defaults -----------------------------------------
DEFAULT_TSW_HOST = "192.168.1.2"
DEFAULT_TSW_LAN_IP = "192.168.88.2"
DEFAULT_TSW_NETMASK = "255.255.255.0"
DEFAULT_TSW_GATEWAY = "192.168.88.1"     # the RUTM08 on the operational subnet
DEFAULT_TSW_NTP_SERVER = "192.168.88.10"
DEFAULT_TSW_MIN_FIRMWARE = "TSW2_R_00.01.07.1"

# The model this tool provisions. Prefix-matched, so a TSW212 is refused rather
# than provisioned with a TSW202 image.
EXPECTED_MODEL = "TSW202"

# UCI paths. The timeserver section is the standard OpenWrt/RutOS one that
# set_timezone() already writes into.
UCI_NTP_SECTION = "system.ntp"
UCI_NTP_SERVER = "system.ntp.server"
UCI_NTP_ENABLED = "system.ntp.enabled"


class TswClient(TeltonikaClient):
    """TeltonikaClient plus the three things a TSW202 needs that a router does not.

    Kept here rather than in bench_core until a second device wants them: no
    other tool sets an NTP *server* (the routers get time over their WAN), and
    no other tool verifies a firmware floor.
    """

    # --- identity -----------------------------------------------------------
    def get_identity(self) -> dict:
        """The shared identity read, with a board-level fallback for the model.

        `assert_device_model` refuses to provision a device whose model it
        cannot read, which is the right call — but it means an `mnfinfo` object
        this firmware line doesn't ship would fail every single run. The switch
        knows what it is in two other places; ask them before giving up.
        """
        identity = super().get_identity()
        if (identity.get("model") or "unknown").lower() != "unknown":
            return identity
        model = self._board_model()
        if model:
            log.info("Model came from the board info (%s) — mnfinfo did not "
                     "report one.", model)
            identity["model"] = model
        return identity

    def _board_model(self) -> str:
        """The model as the board reports it, or "" — tried in order of how
        specific the answer is."""
        probes = (
            "ubus call system board 2>/dev/null | sed -n 's/.*\"model\": *\"\\([^\"]*\\)\".*/\\1/p'",
            "sed -n 's/.*\"model\": *\"\\([^\"]*\\)\".*/\\1/p' /etc/board.json 2>/dev/null",
            "tr -d '\\000' < /proc/device-tree/model 2>/dev/null",
        )
        for probe in probes:
            out = self.ssh_exec(probe, check=False).strip().splitlines()
            answer = next((ln.strip() for ln in out if ln.strip()), "")
            # Board strings are often "Teltonika TSW202" — keep the whole thing;
            # assert_device_model prefix-matches, so strip the vendor first.
            answer = answer.removeprefix("Teltonika").strip()
            if answer:
                return answer
        return ""

    # --- NTP ----------------------------------------------------------------
    def set_ntp_server(self, server: str) -> None:
        """Point the switch's NTP client at `server`, as its ONLY time source.

        `server` is a UCI *list*, so it is cleared and re-added rather than
        `set` — otherwise the factory pool entries stay in front of ours and the
        switch keeps asking the internet for the time it should be getting from
        the site. The section is created when absent: `set_timezone` writes into
        the same one, but only ever options that already exist.
        """
        log.info("Setting the NTP server to %s ...", server)
        self.ssh_exec(
            f"uci -q get {UCI_NTP_SECTION} >/dev/null 2>&1 || "
            f"uci set {UCI_NTP_SECTION}=timeserver")
        self.ssh_exec(
            f"uci -q delete {UCI_NTP_SERVER}; "
            f"uci add_list {self._uci_arg(UCI_NTP_SERVER, server)} && "
            f"uci set {UCI_NTP_ENABLED}='1' && "
            f"uci commit system")
        self.ssh_exec("/etc/init.d/sysntpd restart", check=False)
        log.info("NTP server set.")

    def ntp_servers(self) -> list[str]:
        """The configured NTP servers, in order. UCI prints a list as one
        space-separated line."""
        out = self.ssh_exec(f"uci -q get {UCI_NTP_SERVER}", check=False).strip()
        return out.split()

    # --- verification -------------------------------------------------------
    def verify_configuration(self, *, new_password: str, zonename: str,
                             ntp_server: str,
                             minimum_firmware: str = "") -> list[dict]:
        """Read every setting back off the switch and confirm it took.

        Deliberately NOT an extension of the base's `verify_configuration`: that
        one's parameters describe a router's surface (SIM, RMS, Tailscale,
        eSIM), all of which a switch would pass as "skipped" only to be filtered
        out again. Same return shape — {item, expected, actual, ok} with ok as
        True / False / None — so the UI table and the run record are identical.

        No check may carry a password on either side (TEC-349): these rows are
        written to the run record verbatim and shipped to bench-central, and on
        the failing path the password still in use is the device's own label
        password. The outcome is all a reader needs.
        """
        checks: list[dict] = []

        def add(item, expected, actual, ok):
            checks.append({"item": item, "expected": expected,
                           "actual": actual, "ok": ok})

        on_shared = self.password == new_password
        add("admin/root password", "the shared password",
            "in use" if on_shared else "NOT set — the switch is still on another "
                                       "password",
            on_shared)

        zn = self.ssh_exec(f"uci get {UCI_ZONENAME} 2>/dev/null", check=False).strip()
        tz = self.ssh_exec(f"uci get {UCI_TIMEZONE} 2>/dev/null", check=False).strip()
        add("timezone", zonename, f"{zn} ({tz})", zn == zonename)

        servers = self.ntp_servers()
        enabled = self.ssh_exec(f"uci -q get {UCI_NTP_ENABLED}", check=False).strip()
        # Only ours, and enabled. A pool entry left behind is a real finding: the
        # switch would drift to internet time on a site that has none.
        add("NTP server", f"{ntp_server} (only, enabled)",
            f"{', '.join(servers) or '(none)'} (enabled={enabled or '?'})",
            servers == [ntp_server] and enabled == "1")

        fw = self.ssh_exec("cat /etc/version 2>/dev/null", check=False).strip()
        if minimum_firmware:
            add("firmware", f"{minimum_firmware} or newer", fw or "unknown",
                fw_version_at_least(fw, minimum_firmware))
        else:
            add("firmware", "(any)", fw or "unknown", None)

        return checks


# --- firmware floor -----------------------------------------------------------

def apply_firmware_floor(client: TswClient, settings: dict,
                         failures: list[str]) -> tuple[str, list[str]]:
    """Bring the switch up to the configured minimum firmware, if it is below it.

    Returns `(note, warnings)` — the note goes into the run record's `device`
    block so a reader can tell "already fine" from "we flashed it" from "this
    one shipped newer than our floor" without re-reading the step log.

    Where the criticality sits is deliberate: a flash that RUNS is critical (it
    reboots, and a half-flashed switch must abort the run), but a *missing
    image* is a station config problem — recorded as a failure so the run
    reports it, with the remaining steps still applied to the device.
    """
    fw_cfg = settings.get("firmware", {}) or {}
    minimum = (fw_cfg.get("minimum_version") or "").strip()
    if not minimum:
        return "no floor configured — firmware left as-is", []

    current = client.ssh_exec("cat /etc/version 2>/dev/null", check=False).strip()
    if fw_versions_match(current, minimum):
        log.info("Firmware is %s — the configured floor; nothing to do.", current)
        return f"{current} — at the floor", []
    if fw_version_at_least(current, minimum):
        # Newer than the floor. Teltonika's "Latest" runs ahead of its "Stable"
        # for this family, so this is routine; downgrading would be the
        # surprising action, not passing it through.
        note = f"{current} is newer than the {minimum} floor — left as-is"
        log.warning("Firmware %s is NEWER than the configured floor %s — leaving "
                    "it alone (the config names a minimum, not a pin).",
                    current, minimum)
        return note, [note]

    bin_path = fw_cfg.get("bin_path") or ""
    if not bin_path or not os.path.exists(bin_path):
        message = (f"firmware: the switch is on {current or 'an unreadable version'}, "
                   f"below the {minimum} floor, and the image is missing "
                   f"({bin_path or 'no bin_path configured'}) — download it from "
                   "Teltonika and set firmware.bin_path")
        failures.append(message)
        log.error("Step 'firmware' FAILED: %s", message)
        return f"{current or 'unknown'} — below {minimum}, image missing", []

    log.info("Firmware %s is below the %s floor — flashing the local image.",
             current or "unknown", minimum)
    client.upgrade_firmware(bin_path=bin_path,
                            keep_settings=fw_cfg.get("keep_settings", True))
    after = client.get_identity().get("firmware", "unknown")
    return f"{current or 'unknown'} -> {after} (flashed to meet {minimum})", []


# --- pipeline (shared by CLI + web UI) ----------------------------------------

def configure_tsw(client: TswClient, *, initial_password: str,
                  settings: dict) -> dict:
    """Run the full provisioning pipeline for ONE TSW202. Logs every step through
    the shared 'teltonika' logger. Returns identity + per-step failures +
    verification. Raises SystemExit on a hard failure (login, password change, a
    firmware flash that goes wrong)."""
    new_password = settings.get("new_password", DEFAULT_NEW_PASSWORD)
    ntp_server = settings.get("ntp_server", DEFAULT_TSW_NTP_SERVER)
    zonename = settings.get("timezone", DEFAULT_TIMEZONE)
    minimum = ((settings.get("firmware", {}) or {}).get("minimum_version") or "").strip()

    # Fail before touching the device rather than after the password change:
    # set_timezone needs a POSIX mapping for the zone, and finding that out
    # three steps in leaves the switch half-configured.
    if zonename not in POSIX_TZ:
        raise SystemExit(f"No POSIX TZ mapping for '{zonename}'; add it to "
                         "bench_core.POSIX_TZ or fix `timezone` in the config.")

    # Let SSH fall back to the shared password for the whole run, so a switch
    # left half-changed by an earlier failed run still provisions cleanly.
    client._ssh_alt_passwords = [new_password]

    # Fresh switch: the label password works. Re-run of one that already got
    # past the password step: the shared password works. An EMPTY initial
    # password means "this one is already on the shared password".
    if initial_password and initial_password != new_password:
        try:
            client.login(initial_password)
        except SystemExit as e:
            log.info("Label password rejected (%s) — retrying with the shared "
                     "password (switch may be half-provisioned by an earlier run).", e)
            client.login(new_password)
    else:
        client.login(new_password)

    identity = client.get_identity()
    # The switch's factory address (192.168.1.2) doesn't collide with the
    # routers' (192.168.1.1), so a mix-up is less likely here than on those
    # tools — but a TSW212 or a re-addressed router would still land on this
    # tab, and this tool would flash it with a TSW202 image.
    assert_device_model(identity, EXPECTED_MODEL, "TSW202 configurator")

    # Critical step: aborts the run on failure.
    client.set_admin_password(new_password)

    # Every later step is run through _step so a failure is recorded (not
    # swallowed) and the run reports it instead of falsely "completing".
    failures, _step = make_step_runner(log)

    firmware_note, warnings = apply_firmware_floor(client, settings, failures)

    _step("ntp", lambda: client.set_ntp_server(ntp_server))
    _step("timezone", lambda: client.set_timezone(zonename))

    # Verify while the switch is still reachable on its current address.
    verification: list[dict] = []
    try:
        verification = client.verify_configuration(
            new_password=new_password, zonename=zonename,
            ntp_server=ntp_server, minimum_firmware=minimum)
        for line in format_verification(verification).splitlines():
            log.info("%s", line)
    except SystemExit as e:
        log.error("Verification step could not run: %s", e)

    # Management IP LAST — after this the switch answers on lan_ip, not the
    # factory address. renew_dhcp=False: a switch serves no DHCP, and renewing
    # would drop the station's static bench address.
    lan_ip = (settings.get("lan_ip") or "").strip()
    if lan_ip:
        try:
            verification.append(client.move_lan(
                lan_ip,
                netmask=settings.get("netmask", DEFAULT_TSW_NETMASK),
                gateway=settings.get("gateway", DEFAULT_TSW_GATEWAY),
                renew_dhcp=False))
        except SystemExit as e:
            failures.append(f"lan-ip: {e}")
            log.error("Step 'lan-ip' FAILED: %s", e)

    verify_failed = [c["item"] for c in verification if c["ok"] is False]
    ok = not failures and not verify_failed
    name = f"TSW202 {identity.get('serial', 'unknown')}"
    if ok:
        log.info("Provisioning complete for %s — all steps verified.", name)
    else:
        problems = failures + [f"verify:{i}" for i in verify_failed]
        log.error("Provisioning of %s finished with problems: %s", name, " | ".join(problems))
    return {"identity": identity, "warnings": warnings, "failures": failures,
            "verification": verification, "ok": ok, "ip": lan_ip,
            "firmware_note": firmware_note}


def main():
    p = argparse.ArgumentParser(description="Provision a single Teltonika TSW202.")
    p.add_argument("--label-password",
                   help="factory password from the device label (prompts if omitted; "
                        "pass '' for a switch already on the shared password)")
    p.add_argument("--config", default=str(BASE_DIR / "config" / "tsw.config.json"),
                   help="shared settings JSON (default: config/tsw.config.json)")
    args = p.parse_args()

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_LINE_FORMAT))
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    settings = load_settings(args.config)
    # The UI resolves bin_path against the app folder; do the same here so the
    # committed relative default works from any working directory.
    fw_cfg = settings.get("firmware", {}) or {}
    if fw_cfg.get("bin_path"):
        fw_cfg["bin_path"] = str(BASE_DIR / fw_cfg["bin_path"])
        settings["firmware"] = fw_cfg

    label_pw = args.label_password
    if label_pw is None:
        label_pw = getpass("Label password (empty = already on the shared password): ")

    set_log_serial(None)
    client = TswClient(
        host=settings.get("host", DEFAULT_TSW_HOST),
        username=settings.get("username", DEFAULT_USERNAME),
        scheme=settings.get("scheme", DEFAULT_SCHEME),
        verify=not settings.get("insecure", True),
    )
    try:
        result = configure_tsw(client, initial_password=label_pw, settings=settings)
    finally:
        client.close()
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
