#!/usr/bin/env python3
"""What a finished Magos radar or APU is checked against (TEC-851).

The other five bench tools ended a run with rows shaped
`{item, expected, actual, ok}` and grew a mutation-free **Verify** pass out of
them (TEC-348). The Magos pair was left out: it ran a single reachability probe
that answered `{verified, skipped, detail}`, which the shared table, the record
schema and the central summary have nothing to do with. This module is the row
side of closing that — every check the two devices can make, in the one schema,
built so the same functions serve both paths:

    configure run   sets the unit up, then re-reads it on its NEW address
    verify pass     re-reads a finished unit and changes nothing

Both get the same rows, which is the point: an operator pressing Verify at the
end of a batch has to see the same table the provisioning run showed, or the
sweep is checking something else.

There is deliberately **no password row**. Neither Magos tool changes the
device's credentials — the station password IS the factory password unless an
operator changed it in settings — so a row saying "we authenticated" could only
be green, and a green row that cannot go red is exactly the tautology
`docs/verification-rows.md` exists to keep out. A login that fails aborts the
pass with an error instead, which is louder than a red row.
"""
from __future__ import annotations

from typing import Optional

from bench_core import format_verification
from bench_core.run_record import verification_outcome

from apu_configure import (
    APUClient,
    REQUIRED_APU_FIRMWARE,
    firmware_ok,
    firmware_version,
)
from magos_configure import (
    DEFAULT_PASSWORD,
    DEFAULT_USERNAME,
    MagosClient,
    MagosError,
    log,
    prefix_to_netmask,
    set_log_serial,
)

# There is deliberately NO `device clock` row. The bench cannot make a check out
# of the time these units report, in either direction:
#
#   * as an NTP check it is meaningless here. The server the tools point a unit
#     at is on the assembly network, so a unit on this bench has nothing to sync
#     against and drifts for as long as it stays powered — the first APU
#     verified read 90s out for exactly that reason.
#   * as a "was the clock ever set" check it cannot separate the fault from the
#     expected state. Nothing on the bench sets the time, so a fresh unit's
#     clock is whatever its factory image left and however long it sat in a
#     box. A dead RTC and a perfectly good unit awaiting its first sync produce
#     the same reading, and telling them apart would mean power-cycling the unit
#     to see whether the clock survives — which a verify pass must not do.
#
# So the NTP configuration is checked as a read-back (`system_rows`) and the
# clock is not checked at all. Whether the unit ever reaches that server is an
# assembly-side question; see docs/verification-rows.md.


# --- the shared rows ---------------------------------------------------------

def reached_at_row(reached: str, expected_ip: str) -> dict:
    """Where the unit actually answered, against where its record says it was
    put. Effect-based: a unit that fell back to the factory address, or never
    took the new one, answers the sweep somewhere else.

    With no recorded address the row FAILS rather than passing: reading the
    device's own address and calling that the expectation would pass by
    construction, which is the tautology this whole schema exists to remove.
    """
    if not expected_ip:
        return {"item": "reached at",
                "expected": "the address from this unit's configure run",
                "actual": f"{reached} — no recorded address to compare it with",
                "ok": False}
    return {"item": "reached at", "expected": expected_ip, "actual": reached,
            "ok": reached == expected_ip}


def system_rows(client, *, ntp: str, tz: str) -> list[dict]:
    """The NTP and timezone read-back from `GET /system`.

    Read-back, both of them, with no effect-based row behind either: the
    dashboard API exposes the server name, an automatic flag and the zone, and
    nothing else. The effect that would normally corroborate the NTP setting —
    the unit's clock tracking that server — is not available here at all, and
    the note at the top of this module says why it is not faked up. So these two
    rows say the tool wrote what it meant to write, and nothing stronger.
    """
    sysinfo = client.get_system()
    rows = []

    server = str(sysinfo.get("ntpServer") or "")
    automatic = bool(sysinfo.get("ntpAutomatic"))
    if not ntp:
        rows.append({"item": "NTP server",
                     "expected": "whatever the unit came with — this station "
                                 "sets no NTP server",
                     "actual": server or "(unset)", "ok": None})
    else:
        # `ntpAutomatic` back on would mean the unit is picking its own server
        # again, with ours left in the field it no longer reads.
        rows.append({"item": "NTP server", "expected": ntp,
                     "actual": f"{server or '(unset)'} "
                               f"(ntpAutomatic={'on' if automatic else 'off'})",
                     "ok": server == ntp and not automatic})

    zone = str(sysinfo.get("timezone") or "")
    rows.append({"item": "timezone", "expected": tz or "(none set)",
                 "actual": zone or "(unset)",
                 "ok": (zone == tz) if tz else None})
    return rows


def network_settings(net: dict, iface: Optional[str] = None) -> dict:
    """The addressing a device is configured with, from either of the two
    `/networking` schemas `set_network` writes (see the magos_configure module
    docstring): per-interface with a separate netmask on firmware >= 3.x, one
    flat CIDR address on the legacy one.
    """
    ports = net.get("netInterfaces")
    if isinstance(ports, dict) and ports:
        port = ((ports.get(iface) if iface else None)
                or ports.get("port1") or next(iter(ports.values())))
        address = str(port.get("ip4Address") or "")
        return {"method": str(port.get("ip4Method") or ""),
                "ip": address.split("/")[0],
                "netmask": str(port.get("ip4Netmask") or ""),
                "gateway": str(port.get("ip4Gateway") or ""),
                "dns": [str(d) for d in (port.get("ip4DNS") or [])]}
    address = str(net.get("ip4Address") or "")
    ip, _, prefix = address.partition("/")
    return {"method": str(net.get("ip4Method") or ""), "ip": ip,
            "netmask": prefix_to_netmask(prefix) if prefix else "",
            "gateway": str(net.get("ip4Gateway") or ""),
            "dns": [str(d) for d in (net.get("ip4DNS") or [])]}


def network_rows(client, *, expected_ip: str, netmask: str, gateway: str,
                 dns: str, iface: Optional[str] = None) -> list[dict]:
    """The address config read back off the device. Read-back — corroborated by
    `reached at`, which is the effect — with one thing checked beyond the values
    themselves: `ip4Method` has to say `manual`, so a DHCP lease that happens to
    hand out the right address today is not mistaken for the assignment.
    """
    got = network_settings(client.get_networking(), iface)
    manual = got["method"].lower() == "manual"

    if expected_ip:
        ip_ok = got["ip"] == expected_ip and manual
        ip_actual = f"{got['ip'] or '(unset)'} (ip4Method={got['method'] or 'unset'})"
    else:
        ip_ok = False
        ip_actual = (f"{got['ip'] or '(unset)'} — no recorded address to compare "
                     "it with")
    rows = [{"item": "static IP",
             "expected": expected_ip or "the address from this unit's configure run",
             "actual": ip_actual, "ok": ip_ok},
            {"item": "netmask", "expected": netmask,
             "actual": got["netmask"] or "(unset)",
             "ok": (got["netmask"] == netmask) if netmask else None},
            {"item": "gateway", "expected": gateway,
             "actual": got["gateway"] or "(unset)",
             "ok": (got["gateway"] == gateway) if gateway else None}]

    # The tools write one DNS server and the API stores a list, first = primary.
    primary = got["dns"][0] if got["dns"] else ""
    rows.append({"item": "DNS", "expected": dns,
                 "actual": ", ".join(got["dns"]) or "(none)",
                 "ok": (primary == dns) if dns else None})
    return rows


# --- radar ------------------------------------------------------------------

def rf_channel_row(client: MagosClient, expected_channel: str) -> dict:
    """Which RF channel the radar is transmitting on.

    A known gap, and honestly amber rather than quietly green. `set_channel`
    pushes the variant over the detections WebSocket and the firmware documents
    no read for it, so `current_variant` goes looking and reports nothing it
    cannot corroborate against the unit's own variant list.
    """
    channel = str(expected_channel or "").strip().lower()
    if not channel or channel == "other":
        # A manual-IP run never assigned one, so there is no intent to check.
        return {"item": "RF channel",
                "expected": "no channel was assigned on this unit's configure run",
                "actual": "not checked", "ok": None}
    variant = f"chan{channel}" if channel.isdigit() else channel
    current = client.current_variant()
    if current is None:
        return {"item": "RF channel", "expected": variant,
                "actual": "this radar does not report the channel it is on, so "
                          "the assignment cannot be re-read",
                "ok": None}
    return {"item": "RF channel", "expected": variant, "actual": current,
            "ok": current == variant}


def radar_rows(client: MagosClient, *, settings: dict, expected: dict) -> list[dict]:
    """Every radar row except `reached at` and `prior run`, which belong to the
    caller: one comes from where the unit was found, the other from whether it
    has a configure record at all."""
    ip = str(expected.get("ip") or "").split("/")[0]
    return (system_rows(client, ntp=settings.get("ntp", ""),
                        tz=settings.get("timezone", ""))
            + [rf_channel_row(client, expected.get("channel", ""))]
            + network_rows(client, expected_ip=ip,
                           netmask=settings.get("netmask", ""),
                           gateway=settings.get("gateway", ""),
                           dns=settings.get("dns", "")))


# --- APU --------------------------------------------------------------------

def firmware_row(version: Optional[str]) -> dict:
    """The firmware the APU is running. Effect-based: this is the version that
    is live, and the multi-radar assignment below only exists on 3.1.2+."""
    return {"item": "firmware",
            "expected": f"{REQUIRED_APU_FIRMWARE} (or an rc of it)",
            "actual": version or "unreadable", "ok": firmware_ok(version)}


def _radar_host(entry) -> str:
    """The radar address out of an assignment entry, whichever way this firmware
    spells it — `remote_base_url` is a full URL going in ("http://1.2.3.4")."""
    if not isinstance(entry, dict):
        return str(entry or "")
    value = str(entry.get("remote_base_url") or entry.get("ip") or "")
    return value.split("//")[-1].split("/")[0]


def radars_row(client: APUClient, expected_radars: list) -> dict:
    """Which radars the APU is set to control, against the pair its configure
    run assigned. Read-back: the same settings object `set_radars` wrote.
    """
    want = [str(r.get("ip") or "") for r in (expected_radars or [])
            if isinstance(r, dict) and r.get("ip")]
    if not want:
        return {"item": "controlled radars",
                "expected": "no radars were assigned on this unit's configure run",
                "actual": "not checked", "ok": None}
    got = client.get_radars()
    if got is None:
        return {"item": "controlled radars", "expected": ", ".join(want),
                "actual": "this APU does not report its radar assignment, so it "
                          "cannot be re-read",
                "ok": None}
    hosts = [_radar_host(entry) for entry in got]
    return {"item": "controlled radars", "expected": ", ".join(want),
            "actual": ", ".join(hosts) or "(none assigned)",
            "ok": sorted(hosts) == sorted(want)}


def apu_rows(client: APUClient, *, settings: dict, expected: dict,
             firmware: Optional[str] = None) -> list[dict]:
    """Every APU row except `reached at` and `prior run` (see `radar_rows`)."""
    ip = str(expected.get("ip") or "").split("/")[0]
    return ([firmware_row(firmware)]
            + system_rows(client, ntp=settings.get("ntp", ""),
                          tz=settings.get("timezone", ""))
            + [radars_row(client, expected.get("radars") or [])]
            + network_rows(client, expected_ip=ip,
                           netmask=settings.get("netmask", ""),
                           gateway=settings.get("gateway", ""),
                           dns=settings.get("dns", ""),
                           iface=settings.get("iface")))


# --- the verify pass --------------------------------------------------------

def _login(client, settings: dict) -> None:
    """Log in with the station's credentials. A failure aborts the pass: a unit
    nobody can log into cannot be checked at all, and saying so is more use than
    a table of amber rows."""
    username = settings.get("username") or DEFAULT_USERNAME
    password = settings.get("password") or DEFAULT_PASSWORD
    try:
        client.login(username, password)
    except MagosError as e:
        raise MagosError(
            f"Cannot log in as '{username}' ({e}) — this unit is on different "
            "credentials, so nothing about it can be checked.")


def _finish(rows: list[dict], *, identity: dict, reached: str, word: str) -> dict:
    """Log the table, then hand back the shared pipeline result contract."""
    for line in format_verification(rows).splitlines():
        log.info("%s", line)
    verified, detail = verification_outcome(rows)
    failed = [c["item"] for c in rows if c["ok"] is False]
    ok = not failed
    name = identity.get("serial") or "unknown"
    if ok:
        log.info("%s SN %s (%s) PASSED verification — nothing was changed.",
                 word, name, reached)
    else:
        log.error("%s SN %s (%s) FAILED verification: %s",
                  word, name, reached, ", ".join(failed))
    return {"identity": identity, "verification": rows, "ok": ok,
            "verified": verified, "verify_detail": detail, "ip": reached}


def verify_radar(client: MagosClient, *, settings: dict, resolve=None,
                 reached: str = "") -> dict:
    """Check ONE finished radar against its intended state, changing nothing.

    The radar's per-unit intent — which channel it is and therefore which
    address it was given — lives in the configure record, so `resolve` recovers
    it; everything else (NTP, timezone, gateway, DNS, netmask) is station-wide
    and comes from the settings.
    """
    set_log_serial(None)
    _login(client, settings)
    # Everything past the login is a read, enforced rather than intended.
    client.set_read_only()

    identity = client.get_identity()
    expected, prior_row = resolve(identity) if resolve else ({}, None)

    rows: list[dict] = []
    if prior_row is not None:
        rows.append(prior_row)
    rows.append(reached_at_row(reached or client.host,
                               str(expected.get("ip") or "").split("/")[0]))
    rows += radar_rows(client, settings=settings, expected=expected)
    return _finish(rows, identity=identity, reached=reached or client.host,
                   word="Radar")


def verify_apu(client: APUClient, *, settings: dict, resolve=None,
               reached: str = "") -> dict:
    """Check ONE finished APU against its intended state, changing nothing.

    Same shape as `verify_radar`, plus the two things only an APU has: the
    firmware floor the multi-radar assignment depends on, and the pair of radars
    it was told to control.
    """
    set_log_serial(None)
    _login(client, settings)
    client.set_read_only()

    identity = client.get_identity()
    firmware = firmware_version(identity.get("raw", {}))
    expected, prior_row = resolve(identity) if resolve else ({}, None)

    rows: list[dict] = []
    if prior_row is not None:
        rows.append(prior_row)
    rows.append(reached_at_row(reached or client.host,
                               str(expected.get("ip") or "").split("/")[0]))
    rows += apu_rows(client, settings=settings, expected=expected,
                     firmware=firmware)
    result = _finish(rows, identity=identity, reached=reached or client.host,
                     word="APU")
    result["firmware"] = firmware
    return result


# --- the configure run's own re-read ----------------------------------------

def _unreadable_row(host: str, error: Exception) -> dict:
    """Amber, not red. The unit HAS been provisioned by the time this runs; a
    re-read that could not happen says nothing about whether the writes landed,
    and failing the run on it would send a good unit back."""
    return {"item": "re-read after configuring",
            "expected": f"the settings read back from {host}",
            "actual": f"could not re-read the unit at {host}: {error}",
            "ok": None}


def recheck_radar_at(host: str, *, settings: dict, expected: dict) -> list[dict]:
    """Re-read a radar we have just configured, on its NEW address, changing
    nothing — so a configure run records the same rows a Verify pass would."""
    try:
        client = MagosClient(host, scheme=settings.get("scheme", "http"),
                             verify=not settings.get("insecure", False))
        _login(client, settings)
        client.set_read_only()
        return radar_rows(client, settings=settings, expected=expected)
    except Exception as e:  # noqa: BLE001 — MagosError + any network/JSON failure
        log.warning("Could not re-read the radar at %s: %s", host, e)
        return [_unreadable_row(host, e)]


def recheck_apu_at(host: str, *, settings: dict, expected: dict,
                   firmware: Optional[str] = None) -> list[dict]:
    """`recheck_radar_at` for an APU."""
    try:
        client = APUClient(host, scheme=settings.get("scheme", "http"),
                           verify=not settings.get("insecure", False))
        _login(client, settings)
        client.set_read_only()
        return apu_rows(client, settings=settings, expected=expected,
                        firmware=firmware)
    except Exception as e:  # noqa: BLE001
        log.warning("Could not re-read the APU at %s: %s", host, e)
        return [_unreadable_row(host, e)]
