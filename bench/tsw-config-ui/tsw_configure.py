#!/usr/bin/env python3
"""
Provision a Teltonika TSW202 managed switch (RutOS) over its REST API + SSH/UCI.

A fresh TSW202 boots on 192.168.1.2 — note the .2, NOT the .1 the OTD500 and
RUTM08 share — with a unique factory password printed on the device label, and
forces a password change on first login. Pipeline for one switch:

  login(label_pw) -> set password <shared> -> firmware floor
    -> NTP server 192.168.88.10 -> timezone Asia/Jerusalem -> verify
    -> move the management IP to 192.168.88.2 (LAST — drops the connection;
       confirmed by reaching the switch on the new address)

Where that last step lands is the operator's choice, not this file's (TEC-848):
192.168.88.2 is the default, but a switch may be sent to a typed address or
left on DHCP instead. See `configure_tsw`'s `ip_mode`.

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
  python3 tsw_configure.py --verify        # check a finished switch, change nothing
"""
from __future__ import annotations

import argparse
import logging
import os
import shlex
import sys
from getpass import getpass
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent

from bench_core import (
    shared_new_password,
    DEFAULT_SCHEME,
    DEFAULT_TIMEZONE,
    DEFAULT_USERNAME,
    LOG_LINE_FORMAT,
    NTP_CLIENT_PACKAGE,
    NTP_SERVER_TYPES,
    POSIX_TZ,
    UCI_NTP_ENABLED,
    UCI_NTP_SECTION,
    UCI_NTP_SERVER,
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
from bench_core.ip_mode import MODE_DHCP

# --- TSW202 factory + target defaults -----------------------------------------
DEFAULT_TSW_HOST = "192.168.1.2"
DEFAULT_TSW_LAN_IP = "192.168.88.2"
DEFAULT_TSW_NETMASK = "255.255.255.0"
DEFAULT_TSW_GATEWAY = "192.168.88.1"     # the RUTM08 on the operational subnet
DEFAULT_TSW_NTP_SERVER = "192.168.88.10"
DEFAULT_TSW_MIN_FIRMWARE = "TSW2_R_00.01.07.1"

# Where to look for a switch that was left on DHCP and has therefore gone
# somewhere this tool did not choose: the factory subnet and the management one.
# A lease is likelier on the latter, but a bench with its own server on 192.168.1
# is the reason both are swept.
DEFAULT_TSW_DHCP_SUBNETS = ["192.168.88.0/24", "192.168.1.0/24"]
# Seconds to wait for a switch on DHCP to restart its network, take a lease and
# answer there. Longer than a static move: the address is not ours to predict,
# so this covers a DHCP server that is slow to answer as well as the switch.
DEFAULT_TSW_LEASE_TIMEOUT = 300

# The model this tool provisions. Prefix-matched, so a TSW212 is refused rather
# than provisioned with a TSW202 image.
EXPECTED_MODEL = "TSW202"

# The UCI timeserver paths (UCI_NTP_*) come from bench_core, which the routers
# write to as well since TEC-857. Two copies of the same three strings is one
# edit away from the switch and the routers disagreeing about where NTP lives.


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
    def _stock_server_sections(self) -> list[str]:
        """The `system` sections that carry a time server of their own, in
        `uci show` order.

        Discovered rather than named, for the reason `_ntp_client_sections`
        discovers the routers': the numbering is a build detail. `system.ntp` is
        typed `timeserver` and so is never in here — it is the section the list
        option lives on, not a server in its own right.
        """
        types, _ = self._uci_package("system")
        return [s for t in NTP_SERVER_TYPES for s in self._uci_sections(types, t)]

    def set_ntp_server(self, server: str) -> None:
        """Point the switch's NTP client at `server`, as its ONLY time source.

        `server` is a UCI *list*, so it is cleared and re-added rather than
        `set` — otherwise the factory pool entries stay in front of ours and the
        switch keeps asking the internet for the time it should be getting from
        the site. The section is created when absent: `set_timezone` writes into
        the same one, but only ever options that already exist.

        Clearing that list is not enough on its own. A TSW202 also keeps the
        factory `time1-4.google.com` as a section PER SERVER — the shape its
        WebUI renders, and the shape `configured_ntp_servers` was widened to
        find — which a write that only touches `system.ntp.server` leaves
        untouched. A switch off this bench read back `configured 192.168.88.10,
        time1..4.google.com` while its ntpd polled ours alone: benign only until
        a Save & Apply or a config migration promotes them back into the live
        list, and a failover timeout each on a site with no internet. That is
        what TEC-857 deletes them for on the routers; the switch was missed.
        """
        log.info("Setting the NTP server to %s ...", server)
        self.ssh_exec(
            f"uci -q get {UCI_NTP_SECTION} >/dev/null 2>&1 || "
            f"uci set {UCI_NTP_SECTION}=timeserver")
        cmds = [f"uci -q delete {UCI_NTP_SERVER}; "
                f"uci add_list {self._uci_arg(UCI_NTP_SERVER, server)}"]
        # Reverse order: anonymous sections are addressed by index, so deleting
        # from the front renumbers the ones still to go.
        stock = self._stock_server_sections()
        if stock:
            log.info("Deleting %d stock time server section(s) the switch still "
                     "carries: %s", len(stock), ", ".join(stock))
        cmds += [f"uci delete {shlex.quote(f'system.{s}')}" for s in reversed(stock)]
        cmds.append(f"uci set {UCI_NTP_ENABLED}='1'")
        cmds.append("uci commit system")
        self.ssh_exec(" && ".join(cmds))
        self.ssh_exec("/etc/init.d/sysntpd restart", check=False)

        # And the OTHER time subsystem, on a build that has it. The switch was
        # assumed to carry no `ntpclient` package — one off the bench does, its
        # four factory `time1-4.google.com` sitting in a section each, which
        # nothing done to `system` above reaches. `configured_ntp_servers`
        # already scans that package, so those servers failed the NTP row on a
        # switch whose live daemon polled ours alone. Same write-path gap as
        # TEC-857 closed for `system`, one package over.
        if self._uci_package(NTP_CLIENT_PACKAGE)[0]:
            self.set_ntp_client(server)
        log.info("NTP server set.")

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

        The timezone and NTP rows check EFFECT, not the options we wrote. An
        earlier version read `system.ntp.zoneName` and `system.ntp.server`
        straight back and passed a switch that was still on UTC with four Google
        servers: `uci set` creates an option whether or not the device consumes
        it, so a read-back of our own write only proves the write happened.
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

        checks.append(self.timezone_check(zonename))

        servers = self.configured_ntp_servers()
        running = self.running_ntp_servers()
        enabled = self.ssh_exec(f"uci -q get {UCI_NTP_ENABLED}", check=False).strip()
        # Only ours, enabled, and actually being polled by the live daemon. A
        # pool entry left behind is a real finding (the switch would drift to
        # internet time on a site that has none), and so is a correct config the
        # daemon never picked up.
        add("NTP server", f"{ntp_server} (only, enabled, polled)",
            f"configured {', '.join(servers) or '(none)'} "
            f"(enabled={enabled or '?'}); "
            f"ntpd polling {', '.join(running) or 'nothing'}",
            servers == [ntp_server] and enabled == "1"
            and running == [ntp_server])

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
                  settings: dict, target_ip: Optional[str] = None,
                  ip_mode: str = "", mac: str = "") -> dict:
    """Run the full provisioning pipeline for ONE TSW202. Logs every step through
    the shared 'teltonika' logger. Returns identity + per-step failures +
    verification. Raises SystemExit on a hard failure (login, password change, a
    firmware flash that goes wrong).

    `target_ip` is where the management interface should end up, overriding the
    config's `lan_ip` — the address the operator typed, or the fixed one the UI
    is set to. `ip_mode` of `"dhcp"` means don't assign anything at all and find
    the switch again by `mac` afterwards; passing None/"" for both keeps the
    long-standing behaviour of moving to `lan_ip`, which is what the CLI does.
    """
    new_password = shared_new_password(settings)
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

    # Management address LAST — after this the switch is no longer on the
    # address we are talking to it on, whichever way it goes.
    dhcp = ip_mode == MODE_DHCP
    lan_ip = "" if dhcp else (target_ip or settings.get("lan_ip") or "").strip()
    reached_at = client.host
    if dhcp or lan_ip:
        try:
            if dhcp:
                row = client.move_lan_dhcp(
                    mac=mac,
                    subnets=settings.get("dhcp_subnets", DEFAULT_TSW_DHCP_SUBNETS),
                    wait=int(settings.get("lease_timeout",
                                          DEFAULT_TSW_LEASE_TIMEOUT)))
            else:
                # renew_dhcp=False: a switch serves no DHCP, and renewing would
                # drop the station's static bench address.
                row = client.move_lan(
                    lan_ip,
                    netmask=settings.get("netmask", DEFAULT_TSW_NETMASK),
                    gateway=settings.get("gateway", DEFAULT_TSW_GATEWAY),
                    renew_dhcp=False)
            verification.append(row)
            # Where the switch actually IS, which on DHCP is the only way to
            # know it at all. Only claimed when the move was confirmed: both
            # helpers point the client at the new address before checking it
            # answers, so client.host alone would report an address we asked
            # for and never reached.
            reached_at = client.host if row.get("ok") else ""
        except SystemExit as e:
            failures.append(f"lan-ip: {e}")
            log.error("Step 'lan-ip' FAILED: %s", e)
            reached_at = ""

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
            "ip_mode": ip_mode, "reached_at": reached_at,
            "firmware_note": firmware_note}


# --- verify-only pass (TEC-348) -----------------------------------------------

def verify_tsw(client: TswClient, *, settings: dict, resolve=None,
               target_ip: Optional[str] = None, ip_mode: str = "") -> dict:
    """Check ONE finished TSW202 against the baseline, changing nothing.

    Almost nothing about a TSW202's intended state is per-unit: password,
    firmware floor, NTP server and timezone all come from the station config,
    so there is nothing to look up. The management address is the exception
    since TEC-848 — it is now whatever that unit's run chose, so the
    expectation comes from the unit's own configure record via `resolve`, which
    also answers "was this switch ever provisioned by us at all", the one row
    that could never come from the config.

    `resolve` is `BenchConfigurator.verify_resolver`'s callable, or None for a
    CLI run — in which case there is no prior-run row and an engineer is taking
    responsibility for knowing the unit is one of ours. `target_ip` / `ip_mode`
    override what the record says, for the engineer who knows where this one
    was sent.

    The switch is logged into on the SHARED password. That is not an assumption
    to be worked around: a finished unit is on it, and one that is not fails the
    password row, which is the correct and useful outcome.
    """
    new_password = shared_new_password(settings)
    ntp_server = settings.get("ntp_server", DEFAULT_TSW_NTP_SERVER)
    zonename = settings.get("timezone", DEFAULT_TIMEZONE)
    minimum = ((settings.get("firmware", {}) or {}).get("minimum_version") or "").strip()

    client.login(new_password)
    # Everything past the login is a read. The client enforces that rather than
    # trusting this function to stay read-only as it is edited.
    client.set_read_only()

    identity = client.get_identity()
    assert_device_model(identity, EXPECTED_MODEL, "TSW202 configurator")

    verification: list[dict] = []
    # Only now is there a serial to look this unit's configure record up by, so
    # this is the first point at which "which address was THIS switch given"
    # can be answered. An operator-stated expectation wins over the record; the
    # config's lan_ip is the last resort, for a unit provisioned before modes
    # existed (when every switch went there).
    prior_run_row = None
    if resolve is not None:
        expected, prior_run_row = resolve(identity)
        if target_ip is None and not ip_mode:
            ip_mode = str(expected.get("ip_mode") or "")
            target_ip = expected.get("ip") if "ip" in expected else None
    if target_ip is None:
        target_ip = "" if ip_mode == MODE_DHCP else (settings.get("lan_ip") or "")
    target_ip = target_ip.strip()
    dhcp_mode = ip_mode == MODE_DHCP or not target_ip

    if prior_run_row is not None:
        verification.append(prior_run_row)
    verification += client.verify_configuration(
        new_password=new_password, zonename=zonename,
        ntp_server=ntp_server, minimum_firmware=minimum)

    # The management address, asked by having reached the switch rather than by
    # moving it — see TeltonikaClient.lan_ip_check / lan_dhcp_check.
    if dhcp_mode:
        verification.append(client.lan_dhcp_check())
    else:
        verification.append(client.lan_ip_check(target_ip))

    for line in format_verification(verification).splitlines():
        log.info("%s", line)

    failed = [c["item"] for c in verification if c["ok"] is False]
    ok = not failed
    name = f"TSW202 {identity.get('serial', 'unknown')}"
    if ok:
        log.info("%s PASSED verification — nothing was changed.", name)
    else:
        log.error("%s FAILED verification: %s", name, ", ".join(failed))
    # `ip` is what the bench assigned this unit, empty under DHCP; `reached_at`
    # is where it was actually found, which is the finding on a verify pass.
    # The firmware NOTE is about what the firmware step decided to do; there is
    # no firmware step here (the firmware version itself is still checked).
    return {"identity": identity, "warnings": [], "failures": [],
            "verification": verification, "ok": ok,
            "ip": "" if dhcp_mode else target_ip,
            "ip_mode": ip_mode or ("dhcp" if dhcp_mode else "static"),
            "reached_at": client.host,
            "firmware_note": "no firmware step on a verify run"}


def main():
    p = argparse.ArgumentParser(description="Provision a single Teltonika TSW202.")
    p.add_argument("--label-password",
                   help="factory password from the device label (prompts if omitted; "
                        "pass '' for a switch already on the shared password)")
    p.add_argument("--verify", action="store_true",
                   help="check a finished switch against the baseline and change "
                        "nothing (TEC-348); exits 0 only on a full PASS")
    p.add_argument("--ip", default="",
                   help="management address to leave the switch on, overriding "
                        "lan_ip from the config (on --verify: the address to "
                        "expect it at)")
    p.add_argument("--dhcp", action="store_true",
                   help="leave the switch on DHCP instead of assigning it an "
                        "address; it is found again by MAC afterwards")
    p.add_argument("--config", default=str(BASE_DIR / "config" / "tsw.config.json"),
                   help="shared settings JSON (default: config/tsw.config.json)")
    args = p.parse_args()
    if args.dhcp and args.ip:
        p.error("--dhcp and --ip contradict each other: one leaves the address "
                "to the site's DHCP server, the other states it.")

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
    if label_pw is None and not args.verify:
        label_pw = getpass("Label password (empty = already on the shared password): ")

    set_log_serial(None)
    ip_mode = MODE_DHCP if args.dhcp else ""
    # A verify run reaches a finished switch on its FINAL address, not the
    # factory one — that is where the unit it is checking actually is. Under
    # --dhcp there is no such address to aim at, so the engineer names one with
    # --ip; the switch is wherever its lease put it and only they know where.
    host = (args.ip or settings.get("lan_ip", DEFAULT_TSW_LAN_IP)) if args.verify \
        else settings.get("host", DEFAULT_TSW_HOST)
    if args.verify and args.dhcp and not args.ip:
        sys.exit("--verify --dhcp needs --ip: a switch left on DHCP is wherever "
                 "its lease put it, so this tool cannot guess where to reach it.")
    client = TswClient(
        host=host,
        username=settings.get("username", DEFAULT_USERNAME),
        scheme=settings.get("scheme", DEFAULT_SCHEME),
        verify=not settings.get("insecure", True),
    )
    try:
        if args.verify:
            result = verify_tsw(client, settings=settings,
                                target_ip="" if args.dhcp else (args.ip or None),
                                ip_mode=ip_mode)
        else:
            # The MAC is only needed to find a switch that was left on DHCP.
            # Imported lazily so a CLI run without the [ui] extra still works.
            mac = ""
            if args.dhcp:
                from bench_core.bench_ui import read_device_mac
                mac = read_device_mac(host) or ""
            result = configure_tsw(client, initial_password=label_pw,
                                   settings=settings, target_ip=args.ip or None,
                                   ip_mode=ip_mode, mac=mac)
    finally:
        client.close()
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
