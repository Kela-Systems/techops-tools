#!/usr/bin/env python3
"""
Provision a Teltonika RUTM08 (RutOS) over its REST API + SSH/UCI.

A fresh RUTM08 boots on 192.168.1.1 with a UNIQUE factory password printed on
the device label and forces a password change on first login. Pipeline for one
device (devices are done one at a time, no manifest — the site name and label
password are typed in per device):

  login(label_pw) -> set password "Kelasys123!" -> hostname rut-<site>
    -> timezone Asia/Jerusalem -> NTP client -> firmware (latest-stable)
    -> enable+register RMS -> join Tailscale
    -> [optional] NTP forward + static WAN (ENDS internet — everything above
       this line needs the uplink, so nothing that does may follow it)
    -> verify -> move LAN to 192.168.88.1 (LAST — drops the connection;
       confirmed by reaching the device on the new address)

That is the SERVER role: the main router of a Gotcha server box. The EDGE role
is the router inside a Gotcha edge box, in front of its camera, radars, APUs and
speaker. It takes the same fleet services, then (still before the WAN pin, all
pure UCI) the device port forwards, WebUI/SSH access from the WAN and a DHCP
pool; its WAN is pinned to 192.168.88.20 on the server-box subnet and its LAN
moves to 192.168.89.1. Which role a run is for is picked by the operator, and
`effective_settings` merges that role's block over the shared config.

The RUTM08 is an Ethernet-only router (no modem/SIM), so the OTD's 4G-only and
eSIM steps don't apply, and "internet" means the WAN port is plugged into an
uplink. Everything else is the same RutOS surface as the OTD500, so the device
client is reused from the shared bench_core package (the single
field-tested copy both configurators import).

CLI (single device):
  python3 rutm_configure.py --site haifa-port --label-password 'Xy7Kp2Lm9Qa'
  python3 rutm_configure.py --verify --site haifa-port   # check, change nothing
  python3 rutm_configure.py --role edge --site kela-fob-14 --label-password '...'
"""
from __future__ import annotations

import argparse
import copy
import ipaddress
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
    NTP_CLIENT_INTERVAL,
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
# Unlike the OTD500, the router IS the gateway of the LAN the time server sits
# on, so it reaches it directly and needs no forward of its own (TEC-857).
DEFAULT_RUTM_NTP_SERVER = "192.168.88.10"
# The WAN address every router leaves the bench on, so the OTD500 upstream of it
# has one identical NTP line fleet-wide. The gateway is the OTD's LAN address,
# which is fixed by definition.
DEFAULT_RUTM_WAN_IP = "192.168.1.2"
DEFAULT_RUTM_WAN_NETMASK = "255.255.255.0"
DEFAULT_RUTM_WAN_GATEWAY = "192.168.1.1"

# --- roles ---------------------------------------------------------------------
# server = the main router of a Gotcha server box (everything above).
# edge   = the router inside a Gotcha edge box, putting its devices behind one
#          address on the server-box subnet.
ROLE_SERVER = "server"
ROLE_EDGE = "edge"
ROLES = (ROLE_SERVER, ROLE_EDGE)

# Edge defaults, so the role works on a station whose live config has no
# `roles` block yet. A `roles.edge` block in the config is merged over these.
# The prefix keeps RMS, Tailscale and the tailnet from ever holding two
# `rut-<site>` devices for one site.
DEFAULT_EDGE_PREFIX = "rut-edge-"
DEFAULT_EDGE_LAN_IP = "192.168.89.1"
# The WAN cable goes into the server-box switch; the server-role RUTM08 is its
# gateway and resolver. .20 sits below that router's .100-.249 pool.
DEFAULT_EDGE_WAN_IP = "192.168.88.20"
DEFAULT_EDGE_WAN_NETMASK = "255.255.255.0"
DEFAULT_EDGE_WAN_GATEWAY = DEFAULT_RUTM_LAN_IP
# 60, not the 3600 a server config may carry: an edge router has no RTC and
# often boots before the server answers, and the interval is how long it then
# stays on a build-date clock that RMS and Tailscale refuse to talk TLS from.
DEFAULT_EDGE_NTP_INTERVAL = 60
# Leases .200-.249. The devices hold statics below .100, and a pool at all is
# what lets the station follow the LAN move and a later Verify find the unit.
DEFAULT_EDGE_DHCP_START = 200
DEFAULT_EDGE_DHCP_LIMIT = 50
DEFAULT_EDGE_PORT_FORWARDS = [
    {"name": "cam-web", "ext_port": 8080, "dest_ip": "192.168.89.30", "dest_port": 80},
    {"name": "cam-rtsp", "ext_port": 554, "dest_ip": "192.168.89.30", "dest_port": 554},
    {"name": "radar1-web", "ext_port": 8050, "dest_ip": "192.168.89.50", "dest_port": 80},
    {"name": "radar2-web", "ext_port": 8051, "dest_ip": "192.168.89.51", "dest_port": 80},
    {"name": "radar3-web", "ext_port": 8052, "dest_ip": "192.168.89.52", "dest_port": 80},
    {"name": "radar4-web", "ext_port": 8053, "dest_ip": "192.168.89.53", "dest_port": 80},
    {"name": "apu1-web", "ext_port": 8060, "dest_ip": "192.168.89.60", "dest_port": 80},
    {"name": "apu2-web", "ext_port": 8061, "dest_ip": "192.168.89.61", "dest_port": 80},
    {"name": "speaker-web", "ext_port": 8070, "dest_ip": "192.168.89.70", "dest_port": 80},
]
DEFAULT_EDGE_SETTINGS = {
    "name_prefix": DEFAULT_EDGE_PREFIX,
    "lan_ip": DEFAULT_EDGE_LAN_IP,
    "ntp": {"enabled": True, "server": DEFAULT_RUTM_NTP_SERVER,
            "interval": DEFAULT_EDGE_NTP_INTERVAL},
    "wan": {"enabled": True, "ipaddr": DEFAULT_EDGE_WAN_IP,
            "netmask": DEFAULT_EDGE_WAN_NETMASK,
            "gateway": DEFAULT_EDGE_WAN_GATEWAY, "dns": DEFAULT_EDGE_WAN_GATEWAY},
    "ntp_forward": {"enabled": False},
    "dhcp": {"enabled": True, "start": DEFAULT_EDGE_DHCP_START,
             "limit": DEFAULT_EDGE_DHCP_LIMIT},
    "wan_access": {"webui": True, "ssh": True},
    "port_forwards": DEFAULT_EDGE_PORT_FORWARDS,
}
ROLE_DEFAULTS = {ROLE_SERVER: {}, ROLE_EDGE: DEFAULT_EDGE_SETTINGS}

# The router's own services on the WAN. A forward on one of these ports would
# shadow the service WAN access opens, so the two cannot both be configured.
_WAN_ACCESS_PORTS = {22: "ssh", 80: "webui", 443: "webui"}


def _merge(base: dict, over: dict) -> dict:
    """`over` merged into a copy of `base`: dicts merge, lists and scalars
    replace, and keys starting with `_` (comments) are skipped."""
    out = copy.deepcopy(base)
    for key, value in over.items():
        if key.startswith("_"):
            continue
        if isinstance(value, dict):
            out[key] = _merge(out[key] if isinstance(out.get(key), dict) else {}, value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def effective_settings(cfg: dict, role: str = ROLE_SERVER) -> dict:
    """The settings one run uses for `role`: the shared top-level config, then
    the role's built-in defaults, then the config's `roles.<role>` block.

    The `roles` block itself is dropped, so for the server role on a config
    without one this returns the config unchanged — the server pipeline sees
    exactly what it always has.
    """
    if role not in ROLES:
        raise SystemExit(f"Unknown RUTM08 role '{role}' — expected one of {', '.join(ROLES)}.")
    base = {k: v for k, v in (cfg or {}).items() if k != "roles"}
    block = ((cfg or {}).get("roles") or {}).get(role) or {}
    return _merge(_merge(base, ROLE_DEFAULTS[role]), block if isinstance(block, dict) else {})


def edge_settings_problems(settings: dict) -> list[str]:
    """Why `settings` cannot provision an edge router, or [] when it can.

    The one that matters most is the LAN: running the server LAN on an edge
    unit puts a second 192.168.88.1 on the server-box subnet, and that box's
    every device then has two gateways answering to one address.
    """
    problems = []
    lan_ip = (settings.get("lan_ip") or "").strip()
    wan = settings.get("wan", {}) or {}
    if not lan_ip:
        problems.append("lan_ip is not set")
    elif lan_ip == DEFAULT_RUTM_LAN_IP:
        problems.append(f"lan_ip is {DEFAULT_RUTM_LAN_IP}, the server LAN — an edge "
                        "unit on it puts a second .1 on the server-box subnet")
    if not wan.get("enabled"):
        problems.append("WAN is not enabled — an edge router must sit on a static "
                        "address on the server-box subnet")
    try:
        wan_net = ipaddress.ip_interface(
            f"{wan.get('ipaddr', DEFAULT_EDGE_WAN_IP)}/"
            f"{wan.get('netmask', DEFAULT_EDGE_WAN_NETMASK)}").network
        if lan_ip and ipaddress.ip_address(lan_ip) in wan_net:
            problems.append(f"lan_ip {lan_ip} is inside the WAN subnet {wan_net}")
    except ValueError as e:
        problems.append(f"the WAN or LAN address is not valid ({e})")
    access = settings.get("wan_access", {}) or {}
    for rule in settings.get("port_forwards") or []:
        try:
            port = int(rule.get("ext_port"))
        except (TypeError, ValueError):
            problems.append(f"port forward '{rule.get('name', '?')}' has no valid ext_port")
            continue
        service = _WAN_ACCESS_PORTS.get(port)
        if service and access.get(service):
            problems.append(f"port forward '{rule.get('name', '?')}' uses WAN port {port}, "
                            f"which wan_access.{service} opens for the router itself")
    return problems


def check_edge_settings(settings: dict) -> None:
    """Raise SystemExit, before anything touches the device, if the edge
    settings are unsafe to apply."""
    problems = edge_settings_problems(settings)
    if problems:
        raise SystemExit("Refusing to provision as an edge router: " + "; ".join(problems))


def _edge_reserved(settings: dict) -> list[str]:
    """The device statics behind an edge router, in config order — the
    addresses its DHCP pool must never hand out."""
    seen: list[str] = []
    for rule in settings.get("port_forwards") or []:
        ip = str(rule.get("dest_ip") or "")
        if ip and ip not in seen:
            seen.append(ip)
    return seen


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

def time_rows(client: RutmClient, settings: dict,
              role: str = ROLE_SERVER) -> list[dict]:
    """The NTP-client, WAN-address and NTP-forward rows, identical on the
    configure and verify paths so a QA sweep asks what the run asked.

    All three are read-backs. The time server is on the assembly network and
    the WAN faces the site's OTD500, so neither is reachable from this bench —
    docs/verification-rows.md records why that means no sync row. An edge
    router never carries the NTP forward, so it never gets that row.
    """
    ntp = settings.get("ntp", {}) or {}
    wan = settings.get("wan", {}) or {}
    forward = settings.get("ntp_forward", {}) or {}
    rows: list[dict] = []
    if ntp.get("enabled", True):
        rows.append(client.ntp_client_check(
            ntp.get("server", DEFAULT_RUTM_NTP_SERVER),
            interval=int(ntp.get("interval", NTP_CLIENT_INTERVAL))))
        # The config row above reads the FILE. This one asks whether the running
        # daemon ever read it — the restart that makes a commit live is
        # best-effort, and a device that skipped it looks perfect on paper.
        rows.append(client.ntp_daemon_check())
    if wan.get("enabled"):
        rows.append(client.wan_static_check(
            wan.get("ipaddr", DEFAULT_RUTM_WAN_IP),
            netmask=wan.get("netmask", DEFAULT_RUTM_WAN_NETMASK),
            gateway=wan.get("gateway", DEFAULT_RUTM_WAN_GATEWAY)))
    if forward.get("enabled") and role != ROLE_EDGE:
        rows.append(client.ntp_forward_check(
            dest_ip=forward.get("dest_ip", DEFAULT_RUTM_NTP_SERVER),
            src_ip=forward.get("src_ip", DEFAULT_RUTM_WAN_GATEWAY)))
    return rows


def edge_rows(client: RutmClient, settings: dict) -> list[dict]:
    """The port-forward, WAN-access and DHCP-pool rows of an edge router,
    identical on the configure and verify paths.

    Read-backs, like `time_rows`. The camera, radars and APUs are not on the
    bench, so no row claims a forward reaches one (docs/verification-rows.md).
    """
    rows = [client.port_forwards_check(settings.get("port_forwards") or [])]
    access = settings.get("wan_access", {}) or {}
    if access.get("webui") or access.get("ssh"):
        rows.append(client.wan_access_check(webui=bool(access.get("webui")),
                                            ssh=bool(access.get("ssh"))))
    dhcp = settings.get("dhcp", {}) or {}
    if dhcp.get("enabled"):
        rows.append(client.dhcp_pool_check(
            int(dhcp.get("start", DEFAULT_EDGE_DHCP_START)),
            int(dhcp.get("limit", DEFAULT_EDGE_DHCP_LIMIT)),
            reserved=_edge_reserved(settings), require_served=True))
    return rows


def configure_rutm(client: RutmClient, *, site_name: str, initial_password: str,
                   settings: dict, role: str = ROLE_SERVER) -> dict:
    """Run the full provisioning pipeline for ONE RUTM08. Logs every step through
    the shared 'teltonika' logger. Returns identity + per-step failures + verification.
    Raises SystemExit on a hard failure (login, password change, firmware).

    `settings` are already the role's (`effective_settings`); `role` picks the
    steps. It is an argument rather than a key so the server role's settings
    are byte-for-byte what they were before roles existed."""
    edge = role == ROLE_EDGE
    if edge:
        # Before the login, which is before anything can change.
        check_edge_settings(settings)
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
    # Time source (TEC-857), beside the timezone it shares a zoneName with. The
    # router is the gateway of the LAN the server sits on, so this is a direct
    # address — no forward involved, unlike the OTD500 upstream of it. Pure UCI
    # and nothing online is needed, so it belongs before the firmware step: a
    # keep-settings sysupgrade preserves it, and a run that reboots into an
    # unconfigured clock is one failure away from shipping.
    ntp = settings.get("ntp", {}) or {}
    if ntp.get("enabled", True):
        _step("ntp", lambda: client.set_ntp_client(
            ntp.get("server", DEFAULT_RUTM_NTP_SERVER),
            interval=int(ntp.get("interval", NTP_CLIENT_INTERVAL)),
            zonename=settings.get("timezone", DEFAULT_TIMEZONE)))

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

    # Right after the firmware block, because the reboot is what breaks the
    # clock: a RUTM08 has no battery-backed clock, so it comes back seeded from
    # the firmware image's build date — a real unit came up 110 days in the past.
    # Everything below that talks TLS from the DEVICE then fails certificate
    # validation, which is opkg (the Tailscale package) and the on-device RMS
    # client. It runs unconditionally rather than only after a reboot, since a
    # unit can also arrive already booted into a fresh image and never synced.
    _step("clock", client.ensure_clock_sane)

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

    # The WAN block runs LAST of the mutating steps, and after everything that
    # needs to be online (TEC-857). Pinning the WAN to a static address is what
    # ENDS internet on the bench: the port stops taking a lease from the uplink
    # and starts waiting for a gateway that only exists at the site. FOTA, RMS
    # registration and Tailscale all sit above this line for that reason.
    #
    # It also means the bench must not have a TSW202 on the same segment during
    # this window — the switch's factory address is this same 192.168.1.2 (see
    # the bench README).
    if edge:
        # Pure UCI, so they could run anywhere above the WAN pin; they run here,
        # beside it, because together they ARE the edge box's network. The
        # forwards are reconciled (stale owned rules deleted), so the step runs
        # even with an empty list. The pool follows the LAN move, which is what
        # lets move_lan renew the station's lease onto the new subnet.
        forwards = settings.get("port_forwards") or []
        _step("port-forwards", lambda: client.set_port_forwards(forwards))
        access = settings.get("wan_access", {}) or {}
        if access.get("webui") or access.get("ssh"):
            _step("wan-access", lambda: client.set_wan_access(
                webui=bool(access.get("webui")), ssh=bool(access.get("ssh"))))
        dhcp = settings.get("dhcp", {}) or {}
        if dhcp.get("enabled"):
            _step("dhcp-pool", lambda: client.set_dhcp_pool(
                int(dhcp.get("start", DEFAULT_EDGE_DHCP_START)),
                int(dhcp.get("limit", DEFAULT_EDGE_DHCP_LIMIT)), serve=True))
    forward = settings.get("ntp_forward", {}) or {}
    # The upstream OTD500 reaches the time server through the SERVER router; an
    # edge router never carries the forward, whatever its settings say.
    if forward.get("enabled") and not edge:
        _step("ntp-forward", lambda: client.set_ntp_port_forward(
            dest_ip=forward.get("dest_ip", DEFAULT_RUTM_NTP_SERVER),
            src_ip=forward.get("src_ip", DEFAULT_RUTM_WAN_GATEWAY)))
    wan = settings.get("wan", {}) or {}
    if wan.get("enabled"):
        _step("wan-static", lambda: client.set_wan_static(
            wan.get("ipaddr", DEFAULT_RUTM_WAN_IP),
            netmask=wan.get("netmask", DEFAULT_RUTM_WAN_NETMASK),
            gateway=wan.get("gateway", DEFAULT_RUTM_WAN_GATEWAY),
            dns=wan.get("dns", "")))

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
        verification += time_rows(client, settings, role)
        if edge:
            verification += edge_rows(client, settings)
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
            "failures": failures, "verification": verification, "ok": ok,
            **_role_fields(settings, role)}


def _role_fields(settings: dict, role: str) -> dict:
    """What the run record says about the role: which one, the LAN the router
    ended on, and on an edge unit the WAN address an installer types from the
    server box."""
    fields = {"role": role, "lan_ip": (settings.get("lan_ip") or "").strip()}
    if role == ROLE_EDGE:
        fields["wan_ip"] = (settings.get("wan", {}) or {}).get("ipaddr", DEFAULT_EDGE_WAN_IP)
    return fields


# --- verify-only pass (TEC-348) -----------------------------------------------

def verify_role(expected: dict, *, role: str = "",
                fallback_role: str = ROLE_SERVER) -> str:
    """Which role a verify pass checks a unit as.

    In order: one stated for this pass (`role`, or `expected["role"]`, where an
    operator override lands ahead of the record); the role its configure
    record carries, with a record from before roles existed reading as server;
    and only when there is no record at all, `fallback_role` — what the
    station can see (the address the unit answered on, then the toggle).
    """
    chosen = role or expected.get("role") or ""
    if not chosen:
        # `hostname` is written into every RUTM configure record, and an
        # operator override never carries it, so it is how a record shows.
        chosen = ROLE_SERVER if "hostname" in expected else (fallback_role or ROLE_SERVER)
    if chosen not in ROLES:
        raise SystemExit(f"Unknown RUTM08 role '{chosen}' — expected one of "
                         f"{', '.join(ROLES)}.")
    return chosen


def verify_rutm(client: RutmClient, *, settings: dict, resolve=None,
                site_name: str = "", role: str = "",
                fallback_role: str = ROLE_SERVER) -> dict:
    """Check ONE finished RUTM08 against its intended state, changing nothing.

    Unlike the TSW202, one expectation here IS per-unit: the hostname is built
    from a site name somebody typed during the original run, and on a QA sweep
    nobody remembers it. So `resolve` (`BenchConfigurator.verify_resolver`) reads
    it back out of the device's own configure record, and `site_name` lets an
    operator state it instead.

    The role is per-unit too, and resolved the same way (`verify_role`): an
    edge router re-checked while the station's toggle says Server is still
    checked as edge. That is why `settings` is the BASE config, roles block and
    all, rather than settings already merged for the toggle — the role's
    settings can only be worked out once the record has been read.

    The LAN IP row is where this mode earns its keep. On a configure run the
    check is "did the router come back on 192.168.88.1 after we restarted its
    network", and a no-answer there is genuinely inconclusive — the station's
    DHCP lease may be stale. Here the router is in front of us at a known
    address, so `lan_ip_check` reports a fact instead of a maybe.
    """
    base = settings
    client.login(base.get("new_password", DEFAULT_NEW_PASSWORD))
    # Everything past the login is a read, enforced rather than intended.
    client.set_read_only()

    identity = client.get_identity()
    assert_device_model(identity, "RUTM", "RUTM08 configurator")

    expected, prior_run_row = resolve(identity) if resolve else ({}, None)
    role = verify_role(expected, role=role, fallback_role=fallback_role)
    log.info("Checking this unit as the %s role.", role)
    settings = effective_settings(base, role)
    new_password = settings.get("new_password", DEFAULT_NEW_PASSWORD)
    rms = settings.get("rms", {}) or {}
    ts = settings.get("tailscale", {}) or {}
    fw = settings.get("firmware", {}) or {}
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

    verification += time_rows(client, settings, role)
    if role == ROLE_EDGE:
        verification += edge_rows(client, settings)

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
            "verification": verification, "ok": ok, **_role_fields(settings, role)}


def main():
    p = argparse.ArgumentParser(description="Provision a single Teltonika RUTM08.")
    p.add_argument("--site", help="site name -> hostname rut-<site> (rut-edge-<site> with --role edge)")
    p.add_argument("--label-password",
                   help="factory password from the device label (prompts if omitted; "
                        "pass '' for a device already on the shared password)")
    p.add_argument("--verify", action="store_true",
                   help="check a finished router against its intended state and "
                        "change nothing (TEC-348); needs --site to know the "
                        "hostname to expect. Exits 0 only on a full PASS.")
    p.add_argument("--config", default=str(BASE_DIR / "config" / "rutm.config.json"),
                   help="shared settings JSON (default: config/rutm.config.json)")
    p.add_argument("--role", choices=ROLES, default=ROLE_SERVER,
                   help="server = main router of a server box (default); edge = "
                        "router inside a Gotcha edge box (hostname rut-edge-<site>)")
    args = p.parse_args()

    if not args.verify and not args.site:
        p.error("--site is required when provisioning a device")

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_LINE_FORMAT))
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    base = load_settings(args.config)
    settings = effective_settings(base, args.role)
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
        # There is no record lookup on the CLI, so the stated role is the role.
        result = (verify_rutm(client, settings=base, site_name=args.site or "",
                              role=args.role)
                  if args.verify
                  else configure_rutm(client, site_name=args.site,
                                      initial_password=label_pw, settings=settings,
                                      role=args.role))
    finally:
        client.close()
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
