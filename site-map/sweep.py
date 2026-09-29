#!/usr/bin/env python3
"""Sweep a site's LAN, from that site's own server, read-only.

Two addresses doing two different jobs, and conflating them is the mistake
this module exists to prevent:

    the Tailscale address   WHERE the sweep runs. A login to one site's
                            server over the tailnet, which is what chooses
                            the site you are working on.
    the LAN subnet          WHAT it looks at, from that vantage point.

The second only means anything given the first, which is why the subnet has
a default and the server does not: 192.168.88.0/24 is the subnet at every
site *and* on the bench, so there is a sensible default for what to look at
and none at all for where to look from. Sweeping that subnet from a laptop
finds the bench and reports it as a site - the very collision that makes
every fact in `sites/kela-fob-03.yaml` carry a MAC cross-check.

Read-only throughout. The remote commands are one ICMP echo per address and
a read of the kernel's neighbour table. Nothing logs in to a device, nothing
is written anywhere, and no device credential is needed or accepted. What
comes back proves presence, an address and a MAC - and, through the OUI, a
vendor. It proves no cable and no power feed; `discover.to_yaml` is the half
that refuses to invent them.
"""
from __future__ import annotations

import ipaddress
import re
import shlex
import subprocess
from dataclasses import dataclass, field

# A /24 is 254 hosts and sweeps in a couple of seconds. The cap is set at a
# /22 because the cost is linear in addresses and a fat-fingered /8 would
# queue 16 million pings against production from a web request.
MAX_HOSTS = 1024

# The account the tailnet SSH policy actually permits. ssh's own default is
# the local login name, which the policy refuses - so leaving it to ssh means
# every sweep fails with an error that reads like a key problem and is not
# one. A default that works beats a default that is technically neutral.
DEFAULT_SSH_USER = "kela"

# Tailscale hands out CGNAT space. An address outside it is not refused -
# MagicDNS names and custom setups are legitimate - but it is worth saying,
# because a LAN address typed into this field is the bench collision again.
TAILNET = ipaddress.ip_network("100.64.0.0/10")

SSH_OPTS = [
    # No password or passphrase prompt, ever: this runs behind an HTTP
    # request, where a prompt waiting on a terminal nobody is watching looks
    # exactly like a hang. Fail fast and say why instead.
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=8",
    "-o", "StrictHostKeyChecking=accept-new",
]

MARK_HOST = "### hostname"
MARK_NEIGH = "### neighbours"

# `ip neigh show`: "192.168.88.1 dev enp1 lladdr 20:97:27:36:55:ec REACHABLE".
# Entries mid-resolution have no lladdr at all and are skipped - an address
# the kernel is still asking about is not an observation of anything.
NEIGH_LINE = re.compile(
    r"^\s*(?P<ip>[0-9.]+)\s+dev\s+(?P<dev>\S+)\s+lladdr\s+"
    r"(?P<mac>[0-9A-Fa-f:]{17})\s+(?P<state>\S+)"
)
ARP_LINE = re.compile(r"\((?P<ip>[0-9.]+)\)\s+at\s+[0-9A-Fa-f:]{11,17}")

# FAILED is the kernel recording that it asked and got nothing back. Keeping
# it would turn "did not answer" into a device in the site model.
DEAD_STATES = {"FAILED", "INCOMPLETE"}


class SweepError(Exception):
    """Something went wrong before any observation was made."""


@dataclass
class Sweep:
    server: str
    subnet: str
    hostname: str | None
    arp_text: str
    swept: int
    answered: int
    warnings: list[str] = field(default_factory=list)


def check_server(
    server: str, default_user: str | None = DEFAULT_SSH_USER
) -> tuple[str, str, list[str]]:
    """Validate the field. Returns (ssh target, bare host, warnings).

    The field takes `user@host` as well as a bare address, and the username
    matters more than it looks. Under Tailscale SSH the tailnet policy says
    which local accounts you may log in as, and `ssh <ip>` with no user asks
    for the one you happen to be called on your laptop - which the policy
    rejects with "does not permit you to SSH as user", closing the connection
    before any command runs. That reads like a key problem and is not one.
    """
    server = (server or "").strip()
    if not server:
        raise SweepError(
            "No Tailscale address. This field chooses which site you are "
            "working on, so there is no default for it."
        )

    user, _, host = server.rpartition("@")
    user = user or (default_user or "")
    if user and not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", user):
        raise SweepError(f"{user!r} is not a usable SSH username.")

    warnings: list[str] = []
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # A MagicDNS name is fine; anything with shell metacharacters is not.
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,253}", host):
            raise SweepError(f"{host!r} is not an address or a hostname.")
    else:
        if addr.version != 4:
            raise SweepError("IPv6 is not handled here yet; use the v4 address.")
        if addr not in TAILNET:
            warnings.append(
                f"{host} is outside Tailscale's {TAILNET} range. If that is a "
                f"LAN address you are sweeping from the wrong side of the "
                f"tunnel, and 192.168.88.0/24 is also the bench subnet."
            )
    if not user:
        warnings.append(
            f"No SSH username given, so ssh will try your local account. "
            f"Under Tailscale SSH the tailnet policy decides which accounts "
            f"are allowed, and your own name is usually not one - use "
            f"user@{host} or start serve.py with --ssh-user."
        )
    return (f"{user}@{host}" if user else host), host, warnings


def hosts(subnet: str) -> tuple[list[str], ipaddress.IPv4Network]:
    """Every host address in the subnet, as strings."""
    try:
        net = ipaddress.ip_network((subnet or "").strip(), strict=False)
    except ValueError as exc:
        raise SweepError(f"{subnet!r} is not a subnet in CIDR form: {exc}")
    if net.version != 4:
        raise SweepError("IPv6 subnets are not handled here yet.")
    # Counted before the list is built, not after. `net.hosts()` on a /8 is
    # 16 million strings, so a cap applied to len(addrs) would have to
    # materialise the very thing it exists to refuse.
    if net.num_addresses > MAX_HOSTS + 2:
        raise SweepError(
            f"{net} is {net.num_addresses} addresses and the cap is "
            f"{MAX_HOSTS} (a /22). Sweep the /24 the devices are on."
        )
    addrs = [str(a) for a in net.hosts()] or [str(net.network_address)]
    return addrs, net


def remote_script(addrs: list[str]) -> str:
    """The shell run on the site server. Read-only, and it must stay so."""
    for addr in addrs:
        # These come from ipaddress.hosts(), so this can only fail if someone
        # later wires user input straight in. Cheap place to stop that.
        if not re.fullmatch(r"[0-9.]+", addr):
            raise SweepError(f"refusing to sweep {addr!r}")
    quoted = " ".join(shlex.quote(a) for a in addrs)
    return (
        f"echo {shlex.quote(MARK_HOST)}; hostname; "
        f"for a in {quoted}; do ping -n -c1 -W1 \"$a\" >/dev/null 2>&1 & done; "
        f"wait; echo {shlex.quote(MARK_NEIGH)}; "
        f"ip neigh show 2>/dev/null || arp -an 2>/dev/null"
    )


def _within(ip: str, net: ipaddress.IPv4Network | None) -> bool:
    if net is None:
        return True
    try:
        return ipaddress.ip_address(ip) in net
    except ValueError:
        return False


def as_arp(text: str, net: ipaddress.IPv4Network | None = None) -> str:
    """Normalise a neighbour table into the `arp -an` shape oui.parse_arp reads.

    The site server is Linux and net-tools is not installed by default there,
    so `ip neigh` is what actually answers. Converting here keeps the one ARP
    parser in `oui` rather than growing a second format for it to guess at.

    Filtered to the subnet that was swept, because the table is the host's
    and not the sweep's. A site server running k3s carries a neighbour per
    pod on `cni0` - 37 of them on kela-tlv-dev-03 - and every one would
    otherwise arrive in the site model as a device with no vendor, swamping
    the eleven real ones. Nothing pinged them and nothing knows what they
    are; they are simply another subnet that happens to share a kernel.
    """
    out = []
    for line in text.splitlines():
        match = NEIGH_LINE.match(line)
        if match:
            if match.group("state").upper() in DEAD_STATES:
                continue
            if not _within(match.group("ip"), net):
                continue
            out.append(
                f"? ({match.group('ip')}) at {match.group('mac').lower()} "
                f"[ether] on {match.group('dev')}"
            )
        else:
            arp = ARP_LINE.search(line)
            if arp and _within(arp.group("ip"), net):
                out.append(line.rstrip())
    return "\n".join(out) + ("\n" if out else "")


def parse_output(
    text: str, net: ipaddress.IPv4Network | None = None
) -> tuple[str | None, str]:
    """Split the remote output into (hostname, arp text)."""
    hostname = None
    head, mark, tail = text.partition(MARK_NEIGH)
    if not mark:
        return None, as_arp(text, net)
    lines = [ln.strip() for ln in head.splitlines() if ln.strip()]
    if len(lines) >= 2 and lines[0] == MARK_HOST:
        hostname = lines[1]
    return hostname, as_arp(tail, net)


def _why(stderr: str, returncode: int, host: str,
         asked_for: str = "") -> str:
    """ssh's own words, plus a next step only where they support one.

    An earlier version appended a line about BatchMode and keys to every
    failure. "Connection closed by <host> port 22" is not an authentication
    failure at all - under Tailscale SSH it is usually the tailnet policy
    refusing the username - so that advice sent the reader to check a key
    that was working fine. A hint now has to be earned by what ssh said.
    """
    text = (stderr or "").strip()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    said = lines[-1] if lines else f"exit {returncode}"

    if "does not permit you to SSH as user" in text or "look up local user" in text:
        wanted = re.search(r'user "([^"]+)"', text)
        named = wanted.group(1) if wanted else ""
        why = (
            # No user was given anywhere, so ssh fell back to the local login
            # name - which is the common case and not obvious from the error.
            f" ssh tried {named!r} because that is your local account and "
            f"no user was given."
            if named and not asked_for else
            f" That account was asked for explicitly."
            if named else ""
        )
        return (
            f"{text.splitlines()[0]} - Tailscale SSH refused the username, "
            f"not a key.{why} Put a permitted account in the field "
            f"(kela@{host}) or start serve.py with --ssh-user."
        )
    if "Permission denied" in text:
        return (
            f"{said} BatchMode is on, so a password-only host fails here "
            f"rather than prompting - check your key, or `tailscale status`."
        )
    if "Could not resolve" in text or "Name or service not known" in text:
        return f"{said} Is Tailscale up, and is that the right name?"
    if "Operation timed out" in text or "No route to host" in text:
        return f"{said} Check `tailscale status` for {host}."
    return said


def run(
    server: str,
    subnet: str = "192.168.88.0/24",
    *,
    timeout: float = 180.0,
    user: str | None = DEFAULT_SSH_USER,
    runner=None,
) -> Sweep:
    """Ping-sweep `subnet` from `server` and read the neighbour table back.

    `runner` is resolved here rather than defaulted in the signature: a
    default of `subprocess.run` binds at import and then no amount of
    patching `sweep.subprocess` reaches it, which quietly sends a test suite
    off to ssh real addresses and wait for them to time out.
    """
    runner = runner or subprocess.run
    target, host, warnings = check_server(server, user)
    requested_user = target.rpartition("@")[0]
    addrs, net = hosts(subnet)

    try:
        server_addr = ipaddress.ip_address(host)
    except ValueError:
        server_addr = None
    if server_addr is not None and server_addr in net:
        # Then this is not a tailnet hop at all, and the thing being swept is
        # whatever subnet the client is sitting on. On 192.168.88.0/24 that is
        # the bench, which answers, looks plausible and is the wrong site.
        warnings.append(
            f"{host} is inside {net}, so this is not going over the "
            f"tailnet. Anything found is on the local segment - on "
            f"192.168.88.0/24 that is likely the bench, not a site."
        )

    command = ["ssh", *SSH_OPTS, target, remote_script(addrs)]
    try:
        done = runner(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SweepError(
            f"the sweep of {net} from {target} did not finish in "
            f"{timeout:.0f}s."
        )
    except FileNotFoundError:
        raise SweepError("no `ssh` on PATH, so there is no way to reach a site.")

    if done.returncode != 0:
        raise SweepError(
            f"ssh {target} failed: "
            f"{_why(done.stderr, done.returncode, host, requested_user)}"
        )

    hostname, arp_text = parse_output(done.stdout, net)
    answered = len([ln for ln in arp_text.splitlines() if ln.strip()])
    if not answered:
        warnings.append(
            f"nothing in {net} answered. The sweep ran, so this is a finding "
            f"about the subnet rather than a failure - is that the right one?"
        )
    return Sweep(
        server=server,
        subnet=str(net),
        hostname=hostname,
        arp_text=arp_text,
        swept=len(addrs),
        answered=answered,
        warnings=warnings,
    )
