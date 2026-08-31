#!/usr/bin/env python3
"""
Provision a single Provision-ISR IP speaker over its CGI API.

A fresh speaker arrives on DHCP (admin/123456) on whatever subnet the bench
router hands out — 192.168.1.0/24, 192.168.2.0/24 or 192.168.88.0/24 — so
unlike the other bench tools there is no fixed factory IP: find_speaker() scans
those /24s for a host answering HTTP whose landing page is the speaker's
("<title>IP Speaker</title>").

Pipeline for one speaker (one at a time):

  login(admin/123456, falling back to the target password for a re-run) ->
    set password "Kelasys123!" -> set NTP 192.168.88.10 -> upload the media
    file to user slot 0 -> address it (LAST — drops the connection; confirmed
    by reaching the speaker where it landed) -> verify

Where it ends up is a choice, not a constant (TEC-848): the UI offers a fixed
address, a .70/.71 cycle, a typed one, or DHCP, and the CLI has --ip / --dhcp.
The default is still the config's static.ip (192.168.88.70).

CLI (single speaker):
  python3 speaker_configure.py                       # scan, then configure
  python3 speaker_configure.py --host 192.168.1.57   # skip the scan
  python3 speaker_configure.py --ip 192.168.88.71    # a different address
  python3 speaker_configure.py --dhcp                # leave it on DHCP
  python3 speaker_configure.py --scan-only           # just report what's found
  python3 speaker_configure.py --verify              # check, change nothing
"""
from __future__ import annotations

import argparse
import ipaddress
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent

import requests

from bench_core import load_settings, make_step_runner, tcp_port_open
from speaker_client import (
    DEFAULT_GATEWAY,
    DEFAULT_INITIAL_PASSWORD,
    DEFAULT_NETMASK,
    DEFAULT_NEW_PASSWORD,
    DEFAULT_NTP_SERVER,
    DEFAULT_SCHEME,
    DEFAULT_STATIC_IP,
    DEFAULT_USERNAME,
    DETECT_SIGNATURE,
    SpeakerClient,
    SpeakerError,
    format_verification,
    log,
    set_log_serial,
)

LOG_LINE_FORMAT = "[%(levelname_lc)s] [%(sn)s] %(message)s"

DEFAULT_SCAN_SUBNETS = ["192.168.1.0/24", "192.168.2.0/24", "192.168.88.0/24"]
DEFAULT_HTTP_PORT = 80
# Seconds to wait for a speaker left on DHCP to reboot, take a lease AND start
# its web server — much longer than a static move, where only the address
# changes. Matches the camera tool's DHCP mode, which does the same thing.
DEFAULT_LEASE_TIMEOUT = 300
CONFIG_DIR = BASE_DIR / "config"

# Scan tuning: 3 x /24 = 762 hosts. With this pool + timeout a full sweep takes
# ~1-2 s, fast enough for the UI's poll loop.
SCAN_WORKERS = 128
SCAN_TCP_TIMEOUT = 0.3
SCAN_HTTP_TIMEOUT = 1.5


DHCP_DEVICE_NAME = "speaker-dhcp"


def device_name(static_ip: str) -> str:
    """speaker-<octet>, or speaker-dhcp for one left on DHCP — there is no
    assigned address to name that one after (TEC-848)."""
    if not static_ip:
        return DHCP_DEVICE_NAME
    return f"speaker-{static_ip.rsplit('.', 1)[-1]}"


def resolve_media(settings: dict) -> Optional[Path]:
    """The media file to upload, or None when not configured. Relative paths in
    the config resolve against the config/ folder. Raises SpeakerError when a
    configured file is missing."""
    rel = (settings.get("media_file") or "").strip()
    if not rel:
        return None
    path = Path(rel) if Path(rel).is_absolute() else CONFIG_DIR / rel
    if not path.is_file():
        raise SpeakerError(f"media file not found: {path} — put it there or fix "
                           "media_file in the config.")
    return path


# --- detection (the speaker arrives on DHCP) -----------------------------------

def is_speaker(hostport: str, scheme: str = DEFAULT_SCHEME,
               timeout: float = SCAN_HTTP_TIMEOUT) -> bool:
    """True when `hostport` serves the speaker's (unauthenticated) landing page."""
    try:
        r = requests.get(f"{scheme}://{hostport}/", timeout=timeout)
        return DETECT_SIGNATURE in (r.text or "")
    except requests.exceptions.RequestException:
        return False


def scan_for_speaker(subnets: list[str], port: int = DEFAULT_HTTP_PORT,
                     scheme: str = DEFAULT_SCHEME,
                     first_guess: Optional[str] = None) -> Optional[str]:
    """Find a speaker on the bench subnets. Returns 'ip' (or 'ip:port' when the
    port isn't 80), or None. `first_guess` (the last place we saw one, or the
    target static IP) is probed before the full sweep so re-detection is
    instant."""
    def hostport(ip: str) -> str:
        return ip if port == 80 else f"{ip}:{port}"

    if first_guess:
        guess_ip = first_guess.split(":")[0]
        if tcp_port_open(guess_ip, port, timeout=SCAN_TCP_TIMEOUT) and \
                is_speaker(hostport(guess_ip), scheme):
            return hostport(guess_ip)

    candidates: list[str] = []
    for subnet in subnets:
        try:
            candidates += [str(h) for h in ipaddress.ip_network(subnet).hosts()]
        except ValueError:
            log.warning("Skipping invalid scan subnet %r.", subnet)

    open_hosts: list[str] = []
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        futures = {pool.submit(tcp_port_open, ip, port, SCAN_TCP_TIMEOUT): ip
                   for ip in candidates}
        for fut in as_completed(futures):
            if fut.result():
                open_hosts.append(futures[fut])

    # Only now do the (slower) HTTP signature check, on the few hosts that
    # actually answer — the bench subnets have a handful of devices at most.
    for ip in sorted(open_hosts, key=ipaddress.ip_address):
        if is_speaker(hostport(ip), scheme):
            return hostport(ip)
    return None


# --- pipeline (shared by CLI + web UI) -------------------------------------------

def configure_speaker(client: SpeakerClient, *, settings: dict,
                      media_path: Optional[str] = None,
                      target_ip: Optional[str] = None,
                      ip_mode: str = "static", mac: str = "") -> dict:
    """Run the full provisioning pipeline for ONE speaker. Logs every step
    through the 'speaker' logger. Returns identity + per-step failures +
    verification. Raises SpeakerError on a hard failure (login, password).

    `target_ip` is where the speaker should end up, chosen per batch in the UI
    (TEC-848): a bench-assigned address, or `""` to leave it on DHCP. None means
    "the config's static.ip", which is what the CLI and the pre-TEC-848
    behaviour both want. `ip_mode` is carried through to the run record so the
    label and bench-central know which of the two happened; `mac` is how a DHCP
    run finds the speaker again afterwards.
    """
    static = settings.get("static", {}) or {}
    if target_ip is None:
        target_ip = static.get("ip", DEFAULT_STATIC_IP)
    dhcp_mode = not target_ip
    netmask = static.get("netmask", DEFAULT_NETMASK)
    gateway = static.get("gateway", DEFAULT_GATEWAY)
    dns1 = static.get("dns1", gateway)
    dns2 = static.get("dns2", "")
    ntp = settings.get("ntp", {}) or {}
    ntp_server = ntp.get("server", DEFAULT_NTP_SERVER)
    initial_pw = settings.get("initial_password", DEFAULT_INITIAL_PASSWORD)
    new_pw = settings.get("new_password", DEFAULT_NEW_PASSWORD)
    media_slot = int(settings.get("media_slot", 0))
    name = device_name(target_ip)

    set_log_serial(None)

    # 1. Login. Fresh speaker: admin/123456. Re-run of a half-configured unit:
    # it is already on the target password — fall back to it.
    try:
        client.login(initial_pw)
    except SpeakerError as e:
        if new_pw and new_pw != initial_pw:
            log.info("Factory password rejected (%s) — trying the target password "
                     "(speaker may already be partly configured).", e)
            client.login(new_pw)
        else:
            raise

    identity = client.get_identity()

    # 2. Password change (critical — aborts the run on failure).
    client.set_password(new_pw)

    failures, _step = make_step_runner(log, SpeakerError)

    # 3. NTP.
    _step("ntp", lambda: client.set_ntp(
        ntp_server,
        timezone=int(ntp.get("timezone", 720)),
        interval=int(ntp.get("interval", 10))))

    # 4. Media file (skipped with a warning when none is configured).
    media_name = ""
    if media_path:
        media_name = Path(media_path).name
        _step("media-upload", lambda: client.upload_media(media_path, media_slot))
    else:
        log.warning("No media file configured — skipping the upload step.")

    # 5. Addressing — LAST. After this the speaker answers on target_ip, or on
    # whatever its DHCP server gave it. Neither call raises after the write;
    # both return a verification row and repoint the client.
    verification: list[dict] = []
    if dhcp_mode:
        verification.append(client.set_dhcp(
            mac=mac, subnets=settings.get("scan_subnets", DEFAULT_SCAN_SUBNETS),
            wait=int(settings.get("lease_timeout", DEFAULT_LEASE_TIMEOUT))))
    else:
        verification.append(client.set_static_ip(
            target_ip, netmask, gateway, dns1, dns2,
            wait=int(settings.get("ip_move_timeout", 120))))

    # 6. Verify wherever it landed (re-login there first, if it answers).
    where = client.host.split(":")[0]
    if client.port_open():
        try:
            client.relogin([new_pw], settle=2)
            verification += client.verify_configuration(
                new_password=new_pw, ntp_server=ntp_server, ip=target_ip,
                netmask=netmask, gateway=gateway,
                media_name=media_name, media_slot=media_slot, dhcp=dhcp_mode)
            for line in format_verification(verification).splitlines():
                log.info("%s", line)
        except SpeakerError as e:
            log.warning("Could not verify on the new address: %s", e)
    else:
        log.warning("Speaker is not answering on %s — skipping read-back verification.",
                    where)

    verify_failed = [c["item"] for c in verification if c["ok"] is False]
    ok = not failures and not verify_failed
    if ok:
        log.info("Provisioning complete for %s (%s) — all steps verified.",
                 name, target_ip or f"DHCP, currently {where}")
    else:
        problems = failures + [f"verify:{i}" for i in verify_failed]
        log.error("Provisioning of %s finished with problems: %s", name, " | ".join(problems))
    # `ip` is what the BENCH assigned, so it is empty on a DHCP run — the lease
    # is the site's to change and recording it as ours would be a claim the
    # next reader (a QA label, bench-central) would act on. Where the speaker
    # actually is right now rides along separately as `reached_at`.
    return {"name": name, "hostname": name, "identity": identity, "warnings": [],
            "failures": failures, "verification": verification, "ok": ok,
            "ip": target_ip, "ip_mode": ip_mode, "reached_at": where}


# --- verify-only pass (TEC-348) ----------------------------------------------

def verify_speaker(client: SpeakerClient, *, settings: dict,
                   media_path: Optional[str] = None, resolve=None,
                   target_ip: Optional[str] = None,
                   ip_mode: str = "") -> dict:
    """Check ONE finished speaker against its intended state, changing nothing.

    Most expectations are station-wide (netmask, gateway, NTP server, media
    file, shared password) and come straight from the config. The ADDRESS is
    not, since TEC-848: the operator picks a mode per batch, so where a given
    speaker was supposed to end up is a fact about that unit and is recovered
    from its own configure record — exactly as the camera tool does it.

    `resolve` is `BenchConfigurator.verify_resolver`'s callable, or None for a
    CLI run. Besides the address it answers "was this speaker ever provisioned
    by us at all", which is the one row that cannot come from the config and
    the one that stops an unprovisioned unit earning a QA label (TEC-352).

    `target_ip`/`ip_mode` let an operator state the expectation instead, for a
    unit whose record is missing or wrong. `target_ip=""` with `ip_mode="dhcp"`
    means "this one was left on DHCP", which is a claim, not an absence.

    Two rows here are effect-based rather than read-back:

      * "reached at" — we are talking to this speaker on the address it was
        given. The scan found it there, which is the same evidence the
        configure run's `set_static_ip` row waits for. Left out entirely under
        DHCP, where no address was ever promised.
      * "admin password" — the login below is the check. A speaker that still
        answers to the factory password has not had it changed, whatever any
        settings page says.
    """
    static = settings.get("static", {}) or {}
    if target_ip is None and not ip_mode:
        target_ip = static.get("ip", DEFAULT_STATIC_IP)
    netmask = static.get("netmask", DEFAULT_NETMASK)
    gateway = static.get("gateway", DEFAULT_GATEWAY)
    ntp = settings.get("ntp", {}) or {}
    ntp_server = ntp.get("server", DEFAULT_NTP_SERVER)
    initial_pw = settings.get("initial_password", DEFAULT_INITIAL_PASSWORD)
    new_pw = settings.get("new_password", DEFAULT_NEW_PASSWORD)
    media_slot = int(settings.get("media_slot", 0))
    media_name = Path(media_path).name if media_path else ""

    set_log_serial(None)

    verification: list[dict] = []

    # The login IS the password check. Falling back to the factory password
    # tells us WHY it failed rather than just that it did — and a speaker that
    # accepts 123456 is the single worst thing a sweep can find.
    still_factory = False
    try:
        client.login(new_pw)
    except SpeakerError as e:
        log.info("Target password rejected (%s) — trying the factory password.", e)
        try:
            client.login(initial_pw)
            still_factory = True
        except SpeakerError:
            raise SpeakerError(
                "Cannot log in with either the shared or the factory password — "
                "this speaker is on neither, so nothing can be checked.")

    # Everything past the login is a read, enforced rather than intended.
    client.set_read_only()

    identity = client.get_identity()

    # Only now is there a serial to look the unit's configure record up by, so
    # this is the first point at which "which address was THIS speaker given"
    # can be answered. An operator-stated expectation wins over the record;
    # the config is the last resort, for a unit provisioned before modes
    # existed (when every speaker went to static.ip).
    prior_run_row = None
    if resolve is not None:
        expected, prior_run_row = resolve(identity)
        if target_ip is None and not ip_mode:
            ip_mode = str(expected.get("ip_mode") or "")
            target_ip = expected.get("ip") if "ip" in expected else None
    if target_ip is None:
        target_ip = "" if ip_mode == "dhcp" else static.get("ip", DEFAULT_STATIC_IP)
    dhcp_mode = not target_ip
    name = device_name(target_ip)
    if prior_run_row is not None:
        verification.append(prior_run_row)

    # The address we reached it on. `client.host` may carry a port (a dev
    # port-forward), so compare the host half. Under DHCP nothing was promised,
    # so there is no address to hold the speaker to — `verify_configuration`
    # asks the only answerable question instead (is it set to ask for a lease).
    reached = client.host.split(":")[0]
    if not dhcp_mode:
        verification.append({
            "item": "reached at", "expected": target_ip, "actual": reached,
            "ok": reached == target_ip})

    if still_factory:
        verification.append({
            "item": "admin password", "expected": "the shared password",
            "actual": "NOT set — the speaker still answers to the factory "
                      "password", "ok": False})
    verification += [c for c in client.verify_configuration(
        new_password=new_pw, ntp_server=ntp_server, ip=target_ip,
        netmask=netmask, gateway=gateway,
        media_name=media_name, media_slot=media_slot, dhcp=dhcp_mode,
    ) if not (still_factory and c["item"] == "admin password")]

    for line in format_verification(verification).splitlines():
        log.info("%s", line)

    failed = [c["item"] for c in verification if c["ok"] is False]
    ok = not failed
    if ok:
        log.info("%s (%s) PASSED verification — nothing was changed.", name, reached)
    else:
        log.error("%s (%s) FAILED verification: %s", name, reached, ", ".join(failed))
    return {"name": name, "hostname": name, "identity": identity, "warnings": [],
            "failures": [], "verification": verification, "ok": ok,
            "ip": target_ip,
            "ip_mode": ip_mode or ("dhcp" if dhcp_mode else "static"),
            "reached_at": reached}


def main():
    p = argparse.ArgumentParser(description="Provision a single Provision-ISR IP speaker.")
    p.add_argument("--host", default="",
                   help="speaker address, optionally with a port (e.g. 192.168.1.57 "
                        "or 127.0.0.1:8080 on a dev port-forward); skips the scan")
    p.add_argument("--media", default="",
                   help="media file to upload (overrides media_file in the config)")
    p.add_argument("--scan-only", action="store_true",
                   help="scan the bench subnets for a speaker, print it, and exit")
    p.add_argument("--ip", default="",
                   help="the address to move the speaker to, overriding static.ip "
                        "in the config (TEC-848)")
    p.add_argument("--dhcp", action="store_true",
                   help="leave the speaker on DHCP instead of moving it to a "
                        "static address (TEC-848); it is found again by MAC")
    p.add_argument("--verify", action="store_true",
                   help="check a finished speaker against the config and change "
                        "nothing (TEC-348). Exits 0 only on a full PASS.")
    p.add_argument("--config", default=str(BASE_DIR / "config" / "speaker.config.json"),
                   help="shared settings JSON (default: config/speaker.config.json)")
    args = p.parse_args()
    if args.dhcp and args.ip:
        p.error("--dhcp and --ip contradict each other: one leaves the address "
                "to the site's DHCP server, the other states it.")

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_LINE_FORMAT))
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    settings = load_settings(args.config)
    subnets = settings.get("scan_subnets", DEFAULT_SCAN_SUBNETS)
    port = int(settings.get("http_port", DEFAULT_HTTP_PORT))
    scheme = settings.get("scheme", DEFAULT_SCHEME)

    host = args.host
    if not host:
        log.info("Scanning %s for a speaker ...", ", ".join(subnets))
        found = scan_for_speaker(subnets, port, scheme,
                                 first_guess=(settings.get("static", {}) or {}).get("ip"))
        if not found:
            sys.exit("No speaker found on the bench subnets — is it powered and cabled?")
        log.info("Speaker found at %s.", found)
        host = found
    if args.scan_only:
        print(host)
        sys.exit(0)

    if args.media:
        media_path = Path(args.media)
        if not media_path.is_file():
            sys.exit(f"--media file not found: {media_path}")
    else:
        try:
            media_path = resolve_media(settings)
        except SpeakerError as e:
            sys.exit(str(e))

    client = SpeakerClient(host=host,
                           username=settings.get("username", DEFAULT_USERNAME),
                           scheme=scheme)
    # None means "the config's static.ip", which is the CLI's long-standing
    # default; "" means DHCP, which is a choice and not an absence.
    target_ip = "" if args.dhcp else (args.ip or None)
    mac = ""
    if args.dhcp:
        # Imported here, not at module scope: read_device_mac lives in the web
        # shell, and only a DHCP run needs it — a CLI on a station without the
        # [ui] extra should still be able to provision a speaker.
        from bench_core.bench_ui import read_device_mac
        mac = read_device_mac(host.split(":")[0]) or ""
    try:
        if args.verify:
            result = verify_speaker(
                client, settings=settings,
                media_path=str(media_path) if media_path else None,
                target_ip=target_ip, ip_mode="dhcp" if args.dhcp else "")
        else:
            result = configure_speaker(
                client, settings=settings,
                media_path=str(media_path) if media_path else None,
                target_ip=target_ip,
                ip_mode="dhcp" if args.dhcp else "static", mac=mac)
    except SpeakerError as e:
        log.error("%s FAILED: %s", "Verification" if args.verify else "Provisioning", e)
        sys.exit(1)          # the finally below closes the client exactly once
    finally:
        client.close()
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
