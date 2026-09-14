#!/usr/bin/env python3
"""Read a device's own identity off the device. Read-only, one attempt.

This is what turns `model` and `firmware` from `assumed` into `device-api` —
the strongest evidence tier — because the device itself is the only thing that
authoritatively knows what it is. An OUI gives a vendor; a bench record gives
the model as of the last provisioning run; only this gives the model NOW.

It works by reusing the bench tools' existing identity readers rather than
writing new transports, which matters for two reasons: those readers are
already proven against real hardware, and `bench_core` already carries the
seatbelt this needs (`set_read_only()` plus a `_MUTATING_COMMANDS` screen that
raises `MutationBlocked`).

SAFETY POLICY, because these are production devices:

1. **Dry run by default.** Nothing connects until `--confirm`.
2. **Read-only enforced at the client** wherever the client offers it, not
   just by convention: Teltonika, Raythink and Magos are all put into
   read-only mode, each at the point its own verify-only path does it (see the
   note above the probers — the order differs, and for Magos it has to).
   PlanetClient has no such mode; this path instead reaches only `web_login`
   and a GET, and never `cli()`, `_web_post()` or `ensure_ssh_service()`.
3. **Exactly ONE credential attempt per device. Never iterate.** This is not
   politeness. A PLANET IGS-4215 locks out after three failed logins and then
   answers, prints its banner and hangs up *without prompting* — which is
   indistinguishable from the broken-SSH firmware fault, and sends whoever is
   holding the switch down entirely the wrong path. A probe that tried a
   couple of likely passwords could brick a site's management access for the
   next person.

   One caveat, stated because the guarantee is load-bearing: the budget is one
   *call* per device, and on a Raythink camera one call is two login round
   trips. Its `login()` tries the MD5 hash in UPPER then lower hex, since
   Dahua-derived firmwares disagree on which they accept. It aborts on an
   explicit ERR_LOCKED rather than continuing, so it will not walk a camera
   into a lockout, but a wrong password there costs two attempts, not one.
4. **No firmware step, no config, no reboot.** Identity only.
5. **A placeholder is not a reading.** Several bench clients substitute a
   constant when the device reports no model — see `_reading()` below. Those
   never become claims, because a fabricated `device-api` model would render
   as this tool's strongest evidence tier.
6. A failure leaves the fact `assumed` and records why. It never guesses.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# The bench tools live beside this package, not inside it.
BENCH = Path(__file__).resolve().parent.parent / "bench"


@dataclass
class ProbeResult:
    node: str
    host: str
    prober: str
    ok: bool = False
    model: Optional[str] = None
    firmware: Optional[str] = None
    serial: Optional[str] = None
    mac: Optional[str] = None
    extra: dict = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def learned(self) -> list[str]:
        got = []
        if self.model:
            got.append("model")
        if self.firmware:
            got.append("firmware")
        return got


class ProbeUnavailable(Exception):
    """The prober's dependencies are not importable here."""


def _add_bench_path(*subdirs: str) -> None:
    for path in (BENCH / "bench-core" / "src", *(BENCH / d for d in subdirs)):
        text = str(path)
        if text not in sys.path:
            sys.path.insert(0, text)


# -- what counts as a reading ------------------------------------------
#
# The bench clients all return a dict with every key present, using a
# placeholder when the device did not actually say. Some go further and
# substitute a *plausible-looking constant* — the PLANET returns its
# `EXPECTED_MODEL` and the Raythink returns the string "Raythink" when the
# device reported no model at all.
#
# Those placeholders must never reach the site model. A fabricated model
# stamped `device-api` would render as a proven fact (the strongest tier this
# tool has) while being a hardcoded string nobody read off anything — which is
# strictly worse than an honest blank, because a blank prompts a survey and a
# false ✓ stops one. `bench_core` states the same rule for its own reader: it
# returns "unknown" rather than a family default so callers refuse "instead of
# rubber-stamping a device it couldn't read".
#
# So every field passes through `_reading()`, and anything a prober knows to be
# a fallback is named in its own reject list.

NOT_A_READING = frozenset({"", "unknown", "n/a", "na", "none", "null", "-"})


def _reading(value, reject: frozenset = frozenset()) -> Optional[str]:
    """The value if the device genuinely reported it, else None.

    None means "no claim", and a no-claim leaves the fact `assumed` rather
    than recording a guess as evidence.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.casefold() in NOT_A_READING:
        return None
    if text.casefold() in {r.casefold() for r in reject}:
        return None
    return text


def _identity(raw: dict, *, model_reject: frozenset = frozenset(),
              extra: Optional[dict] = None) -> dict:
    """Normalise a bench client's identity dict into claims we can stand behind."""
    return {
        "model": _reading(raw.get("model"), model_reject),
        "firmware": _reading(raw.get("firmware")),
        "serial": _reading(raw.get("serial")),
        "mac": _reading(raw.get("mac")),
        "extra": extra or {},
    }


# -- probers ----------------------------------------------------------
#
# Each takes (host, password) and returns a dict of identity fields. They are
# thin on purpose: the reading logic belongs to the bench tool that owns the
# device, and duplicating it here would mean two versions to keep correct.
#
# The call order below is the one each bench tool's own verify-only path uses:
# **log in, THEN arm the read-only guard, THEN read.** That order is not
# cosmetic. The Magos guard sits on `_post`, and its login IS a POST, so arming
# the guard first would refuse the login itself. Teltonika is the one exception
# and goes the other way — its guard screens shell command strings, so it can
# be armed before anything happens, which is strictly safer and is what it does.


def probe_teltonika(host: str, password: str) -> dict:
    """RUTM08 / TSW202 / OTD500, via bench_core.TeltonikaClient.

    `get_identity()` needs BOTH transports, and they carry different facts:
    serial/MAC/model come from `ubus call mnfinfo get` over SSH, while
    **firmware comes only from the REST endpoint** `/system/device/status` —
    which needs the bearer token that `login()` sets. Skipping the login costs
    the firmware silently, returning "unknown" on an otherwise perfect run.

    Both reads pass the read-only screen: it blocks `ubus call <x> set|write|
    reload|restart|connect`, and `ubus call mnfinfo get` is none of those.
    """
    _add_bench_path()
    try:
        from bench_core import TeltonikaClient
    except ImportError as exc:
        raise ProbeUnavailable(f"bench_core not importable: {exc}") from exc

    client = TeltonikaClient(host=host)
    # Before anything else. The seatbelt is worthless if it goes on later, and
    # unlike the other clients this one's guard does not gate its own login.
    client.set_read_only(True)
    # REST login: without it there is no token, and firmware never arrives.
    # This also stores the password for the SSH leg.
    client.login(password)
    return _identity(client.get_identity())


def probe_planet(host: str, password: str) -> dict:
    """PLANET IGS-4215, via PlanetClient's HTTP identity read.

    Deliberately the HTTP path and not the CLI: identity is served off the
    system-info page, so this needs no shell session at all. That avoids both
    the concurrent-CLI-session limit and any exposure to the SSH lockout.

    **This prober does not report a model, and that is correct.** The switch
    serves no model field; `get_identity()` fills the key from the *System
    Name*, which is an operator-set hostname, falling back to a hardcoded
    `EXPECTED_MODEL` constant. Neither is read off the hardware, so both are
    rejected here. Firmware IS genuine, and so is `dual_power` — the one power
    fact any protocol on this site can establish, since the switch reports
    whether its second supply is actually energised.

    Nothing here touches `ensure_ssh_service()`, which would turn the SSH
    server on — a write.
    """
    _add_bench_path("planet-config-ui")
    try:
        from planet_configure import PlanetClient, EXPECTED_MODEL
    except ImportError as exc:
        raise ProbeUnavailable(f"planet_configure not importable: {exc}") from exc

    FAKE_PLANET_MODELS = frozenset({EXPECTED_MODEL})

    switch = PlanetClient(host=host)
    switch.web_login(password)
    raw = switch.get_identity()
    # PlanetClient has no set_read_only(): it is an SSH/CLI client whose writes
    # all go through cli()/web_post, and this path calls neither — only
    # web_login and a GET of the system-info page.
    identity = _identity(
        raw,
        extra={"dual_power": raw.get("dual_power"),
               # Kept as a note because it IS a genuine reading — of the
               # switch's name, which is a different fact from its model.
               "system_name": _reading(raw.get("model"), FAKE_PLANET_MODELS)},
    )
    # Unconditional, not a reject list. There is no value this field could
    # hold that would be a model read off the hardware: the switch serves no
    # model over HTTP at all, so the key is either someone's hostname or
    # `EXPECTED_MODEL`. A shortlist candidate is the honest home for what the
    # OUI and the faceplate tell us, and `candidates` already carries that.
    identity["model"] = None
    return identity


def probe_raythink(host: str, password: str) -> dict:
    """Raythink thermal camera, via RaythinkCameraClient's RPC2 API.

    Model is rejected when it comes back as the bare vendor string, which is
    what `get_identity()` substitutes when `deviceType` is absent.

    NOTE ON ATTEMPTS: this client's `login()` tries the MD5 hash in both hex
    cases, because Dahua-derived firmwares disagree on which they accept. That
    is two challenge+login round trips for one wrong password, not one. It
    bails immediately on an explicit ERR_LOCKED, so it will not push a camera
    toward lockout, but the per-device budget here is one CALL, not one packet.
    """
    _add_bench_path("raythink-config-ui")
    try:
        from raythink_camera import RaythinkCameraClient
    except ImportError as exc:
        raise ProbeUnavailable(f"raythink_camera not importable: {exc}") from exc

    camera = RaythinkCameraClient(host=host)
    camera.login(password)
    # Everything past the login is a read, enforced rather than intended.
    camera.set_read_only()
    return _identity(camera.get_identity(),
                     model_reject=frozenset({"Raythink"}))


def probe_magos_radar(host: str, password: str, username: str = "admin") -> dict:
    """Magos AR-300 / SR-series, via the dashboard HTTP API.

    The login is required and takes a username as well: `/dshb/v1` reads answer
    only for a session. The read-only guard goes on AFTER it, because the guard
    sits on `_post` and the login is the one POST that changes nothing.
    """
    _add_bench_path("magos-config-ui")
    try:
        from magos_configure import MagosClient, DEFAULT_USERNAME
    except ImportError as exc:
        raise ProbeUnavailable(f"magos_configure not importable: {exc}") from exc

    client = MagosClient(host=host)
    client.login(username or DEFAULT_USERNAME, password)
    # Everything past the login is a read, enforced rather than intended.
    client.set_read_only()
    identity = _identity(client.get_identity())

    # `get_identity()` returns no firmware key at all — the shared
    # `fetch_identity()` it delegates to reports serial/MAC/model and nothing
    # else. Firmware lives on `GET /system` instead, and it is not one string:
    # a radar reports FOUR independently-versioned components (System
    # Software, Dashboard, DSP, FPGA). `firmware` takes System Software,
    # because that is the one the release notes and the bench's own
    # expectations are written against; the rest ride along as notes rather
    # than being flattened into one field, since an FPGA version that moved
    # while System Software stood still is exactly the kind of mismatch
    # someone needs to see.
    if identity["firmware"] is None:
        try:
            components = client.get_system().get("swComponents") or []
        except Exception:  # noqa: BLE001 — firmware is a bonus; identity stands without it
            components = []
        versions = {c.get("name"): c.get("version")
                    for c in components if isinstance(c, dict)}
        identity["firmware"] = _reading(versions.get("System Software"))
        for name, version in versions.items():
            if name and name != "System Software":
                identity["extra"][f"{name.lower()} version"] = version
    return identity


# whose kind is vague still routes if its vendor is known.
PROBERS: dict[str, Callable[[str, str], dict]] = {
    "router": probe_teltonika,
    "switch": probe_teltonika,
    "modem": probe_teltonika,
    "network-device": probe_teltonika,
    "poe-switch": probe_planet,
    "camera": probe_raythink,
    "radar": probe_magos_radar,
    "magos-device": probe_magos_radar,
}

VENDOR_PROBERS = (
    ("teltonika", probe_teltonika),
    ("planet", probe_planet),
    ("juru", probe_raythink),
    ("raythink", probe_raythink),
    ("magos", probe_magos_radar),
)

# Kinds nothing here can read. Named rather than silently skipped, so the
# report says why.
UNPROBEABLE = {
    "operator-station": "a workstation has no bench identity API; read the "
                        "chassis or query the OS",
    "apu": "the APU's identity comes from its own dashboard, and the bench "
           "APU tool refuses units below firmware 3.1.2",
    "server": "not a bench-managed device",
    "speaker": "supported by the bench speaker tool, but no prober is wired "
               "here yet",
    "mains": "not a device",
    "psu": "no management interface",
}


def choose_prober(kind: str, vendor: Optional[str]) -> tuple[Optional[Callable], str]:
    """Return (prober, label). A missing prober is reported, never guessed."""
    if kind in PROBERS:
        return PROBERS[kind], PROBERS[kind].__name__
    if vendor:
        low = vendor.casefold()
        for needle, prober in VENDOR_PROBERS:
            if needle in low:
                return prober, prober.__name__
    if kind in UNPROBEABLE:
        return None, f"no prober: {UNPROBEABLE[kind]}"
    return None, f"no prober for kind '{kind}'"


def plan(site) -> list[tuple[str, str, Optional[Callable], str]]:
    """What a run would do, without doing any of it."""
    rows = []
    for name in sorted(site.nodes):
        node = site.nodes[name]
        if not node.addr:
            rows.append((name, "", None, "no address to reach"))
            continue
        prober, label = choose_prober(node.kind, node.vendor)
        rows.append((name, node.addr, prober, label))
    return rows


def run(site, password: str, only: Optional[set] = None) -> list[ProbeResult]:
    """Probe each reachable node once. Never more than once."""
    results: list[ProbeResult] = []
    for name, host, prober, label in plan(site):
        if only and name not in only:
            continue
        if prober is None:
            results.append(ProbeResult(node=name, host=host, prober=label,
                                       error=label))
            continue

        result = ProbeResult(node=name, host=host, prober=label)
        try:
            identity = prober(host, password)
        except ProbeUnavailable as exc:
            result.error = str(exc)
        except Exception as exc:
            # One attempt, whatever went wrong. Retrying a credential is how
            # a switch gets locked out, so the policy is: report and move on.
            result.error = f"{type(exc).__name__}: {exc}"
        else:
            result.model = identity.get("model")
            result.firmware = identity.get("firmware")
            result.serial = identity.get("serial")
            result.mac = identity.get("mac")
            result.extra = identity.get("extra") or {}
            result.ok = bool(result.model or result.firmware)
            if not result.ok:
                result.error = "connected but reported no model or firmware"
        results.append(result)
    return results


def as_yaml_patch(results: list[ProbeResult]) -> str:
    """Render what was learned as mergeable site-model blocks.

    Emitted as a patch to merge rather than written straight into the site
    file, because a site file carries hand-written notes and survey findings
    that no automated pass should overwrite. Evidence is set per claim: the
    probe establishes model and firmware, and leaves everything else as it
    was — a device reporting its model tells you nothing about its cabling.
    """
    lines = [
        "# Learned by `sitemap.py probe` — read off each device's own API.",
        "# Merge these into the site file's `nodes:` block. Evidence is set",
        "# per claim: `device-api` covers model and firmware only. Cabling and",
        "# power are untouched, because a device's identity says nothing about",
        "# what it is plugged into.",
        "nodes:",
    ]
    for result in sorted(results, key=lambda r: r.node):
        if not result.ok:
            continue
        lines.append(f"  {result.node}:")
        if result.model:
            lines.append(f"    model: {result.model!r}")
        if result.firmware:
            lines.append(f"    firmware: {result.firmware!r}")
        if result.serial:
            lines.append(f"    # serial: {result.serial}")
        for key, value in (result.extra or {}).items():
            lines.append(f"    # {key}: {value}")
        lines.append("    evidence:")
        lines.append('      "*": arp')
        for claim in result.learned:
            lines.append(f"      {claim}: device-api")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
