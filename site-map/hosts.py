#!/usr/bin/env python3
"""Read a Linux host's own identity, over the tailnet, read-only.

This is the step that turns `vendor Dell` into
`Dell Pro Max Tower T2 FCT2250`. It is not a trick: `/sys/class/dmi/id` is
world-readable, so a plain SSH session as an ordinary user is enough - no
sudo, no extra credential, nothing written. `sites/kela-fob-03.yaml` records
the same reading taken by hand.

It fills three gaps at once for the hosts it can reach:

    model       product_name from DMI
    firmware    bios_version - the firmware analogue on a PC. The OS version
                is recorded separately in the notes, because software moves
                on its own schedule and is not firmware.
    interfaces  the host's own legs, so a site server arrives with its LAN
                NIC *and* tailscale0 rather than just the address the router
                happened to see.

**Every reading is accepted only after the host's own MAC matches the one the
router observed at that address.** That is not belt-and-braces:
192.168.88.0/24 is the subnet at every site and on the bench, and a bench
station carries a 192.168.88.10 alias of its own - so a session opened to the
wrong machine returns an answer that looks entirely valid. The MAC match is
what rules that out, and it is the same cross-check every confirmed fact in
the curated site file carries.

Not every host can be read, and the ones that cannot are a finding rather
than a failure. The operator station at kela-fob-03 advertises password auth
only, so a key is never offered:

    kela@100.120.150.95: Permission denied (password).

Making that work is an sshd config change - a write to production - so this
reports it and moves on.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field

# Where the tailscale CLI lives. The Mac app does not put it on PATH.
TS_BINARIES = (
    "tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    "/usr/bin/tailscale",
    "/usr/local/bin/tailscale",
)

SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=8",
    "-o", "StrictHostKeyChecking=accept-new",
]

DEFAULT_USER = "kela"
# The operator stations take a different account, and their sshd advertises
# password auth only - a key installed by ssh-copy-id is never offered. So
# they need both a different user and a password, where a site server needs
# neither.
ADMIN_USER = "KelaAdmin"

# All world-readable, all reads. `product_serial` is root-only on these hosts
# and is deliberately not attempted: it would need sudo, which is a different
# kind of access than this module is willing to ask for.
DMI_FIELDS = ("sys_vendor", "product_name", "board_name", "bios_version",
              "bios_date", "chassis_type")

REMOTE = (
    "for f in " + " ".join(DMI_FIELDS) + "; do "
    'printf "dmi.%s=%s\\n" "$f" "$(cat /sys/class/dmi/id/$f 2>/dev/null)"; done; '
    'printf "os=%s\\n" "$(. /etc/os-release 2>/dev/null; echo $PRETTY_NAME)"; '
    'printf "kernel=%s\\n" "$(uname -r)"; '
    'printf "host=%s\\n" "$(cat /proc/sys/kernel/hostname)"; '
    "ip -o link 2>/dev/null | "
    "sed -n 's/^[0-9]*: \\([^:]*\\).*link\\/ether \\([0-9a-f:]*\\).*/link.\\1=\\2/p'; "
    "ip -o -4 addr 2>/dev/null | "
    "sed -n 's/^[0-9]*: \\([^ ]*\\) *inet \\([0-9.]*\\)\\/\\([0-9]*\\).*/addr.\\1=\\2\\/\\3/p'"
)

EXTERNAL_NAMES = ("tailscale", "wan", "wwan", "ppp", "mob", "usb")
SKIP_IFACES = ("lo",)

# Container and virtualisation plumbing, which is not a site network leg.
# The site server runs k3s, so it carries `cni0` at 10.42.0.1 and
# `flannel.1` - and both arrived on the diagram as external interfaces of
# the server, which is the same noise as the 10.42.0.x pod neighbours the
# sweep already filters. `tailscale0` is deliberately not in here: it is a
# real leg, and how the site is reached.
SKIP_PREFIXES = ("flannel", "cni", "docker", "veth", "virbr", "br-int",
                 "kube", "cali", "tunl", "dummy", "nodelocal", "vxlan")


@dataclass
class HostReading:
    host: str
    hostname: str | None = None
    vendor: str | None = None
    model: str | None = None
    firmware: str | None = None
    board: str | None = None
    os: str | None = None
    kernel: str | None = None
    macs: dict = field(default_factory=dict)      # iface -> mac
    addrs: dict = field(default_factory=dict)     # iface -> addr/prefix
    error: str | None = None


def tailscale_path() -> str | None:
    for candidate in TS_BINARIES:
        found = shutil.which(candidate) or (
            candidate if candidate.startswith("/") and shutil.which(candidate)
            else None
        )
        if found:
            return found
    for candidate in TS_BINARIES:
        if candidate.startswith("/"):
            try:
                if subprocess.run([candidate, "version"], capture_output=True,
                                  timeout=10).returncode == 0:
                    return candidate
            except (OSError, subprocess.SubprocessError):
                continue
    return None


def tailnet_nodes(runner=None) -> dict:
    """node name -> tailnet address, for the nodes that are online.

    An offline node is left out: its address is still listed, and trying to
    ssh to it just waits for a timeout inside an HTTP request.
    """
    runner = runner or subprocess.run
    binary = tailscale_path()
    if not binary:
        return {}
    try:
        done = runner([binary, "status"], capture_output=True, text=True,
                      timeout=30)
    except (OSError, subprocess.SubprocessError):
        return {}
    if done.returncode != 0:
        return {}

    nodes = {}
    for line in (done.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 2 or not re.match(r"^\d+\.\d+\.\d+\.\d+$", parts[0]):
            continue
        if "offline" in line:
            continue
        nodes[parts[1].casefold()] = parts[0]
    return nodes


def parse_reading(host: str, text: str) -> HostReading:
    out = HostReading(host=host)
    for line in text.splitlines():
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not value:
            continue
        if key == "dmi.sys_vendor":
            out.vendor = value
        elif key == "dmi.product_name":
            out.model = value
        elif key == "dmi.board_name":
            out.board = value
        elif key == "dmi.bios_version":
            out.firmware = value
        elif key == "os":
            out.os = value
        elif key == "kernel":
            out.kernel = value
        elif key == "host":
            out.hostname = value
        elif key.startswith("link."):
            out.macs[key[5:]] = value.lower()
        elif key.startswith("addr."):
            out.addrs[key[5:]] = value
    return out


def read_with_password(host: str, user: str, password: str, *,
                       timeout: float = 30.0) -> HostReading:
    """The same read, on a host that will only take a password.

    paramiko rather than `ssh`: BatchMode is on for a reason, and the way to
    pass a password to the `ssh` binary is to turn the prompt back on and
    feed it - which is how a survey ends up hanging on a prompt nobody can
    see. paramiko takes the password as an argument and never prompts.
    """
    try:
        import paramiko
    except ImportError as exc:
        return HostReading(host=host, error=f"paramiko not importable: {exc}")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host, username=user, password=password,
                       timeout=timeout, allow_agent=False,
                       look_for_keys=False)
        _in, out, err = client.exec_command(REMOTE, timeout=timeout)
        text = out.read().decode("utf-8", "replace")
        if out.channel.recv_exit_status() != 0 and not text.strip():
            detail = err.read().decode("utf-8", "replace").strip()
            return HostReading(host=host, error=detail or "command failed")
        return parse_reading(host, text)
    except Exception as exc:
        # Deliberately not echoing the exception's own text verbatim for an
        # auth failure: paramiko includes the username, and there is no value
        # in it appearing in a log next to "authentication failed".
        name = type(exc).__name__
        if "Auth" in name:
            return HostReading(
                host=host,
                error=f"authentication failed for {user} (password auth)")
        return HostReading(host=host, error=f"{name}: {exc}")
    finally:
        try:
            client.close()
        except Exception:
            pass


def read(host: str, user: str = DEFAULT_USER, *, timeout: float = 30.0,
         runner=None) -> HostReading:
    """One SSH session, one command, nothing written."""
    runner = runner or subprocess.run
    command = ["ssh", *SSH_OPTS, f"{user}@{host}", REMOTE]
    try:
        done = runner(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return HostReading(host=host, error=f"{host} did not answer in "
                                            f"{timeout:.0f}s")
    except (OSError, subprocess.SubprocessError) as exc:
        return HostReading(host=host, error=f"cannot run ssh: {exc}")
    if done.returncode != 0:
        detail = [ln for ln in (done.stderr or "").splitlines() if ln.strip()]
        return HostReading(host=host,
                           error=(detail[-1] if detail else
                                  f"ssh exited {done.returncode}"))
    return parse_reading(host, done.stdout)


def _canon(mac: str) -> str:
    return (mac or "").replace(":", "").replace("-", "").upper()


def interfaces_from(reading: HostReading, subnet=None) -> list[dict]:
    import ipaddress

    out = []
    for name, value in reading.addrs.items():
        low = name.casefold()
        if name in SKIP_IFACES or low.startswith(SKIP_PREFIXES):
            continue
        addr, _, prefix = value.partition("/")
        scope = "external" if any(n in low for n in EXTERNAL_NAMES) else "internal"
        if scope == "internal" and subnet is not None:
            try:
                if ipaddress.IPv4Address(addr) not in subnet:
                    scope = "external"
            except ValueError:
                pass
        out.append({"name": name, "addr": addr, "scope": scope,
                    "prefix": int(prefix) if prefix.isdigit() else None})
    return out


def enrich(found, names: dict, subnet=None, *, user: str = DEFAULT_USER,
           nodes: dict | None = None, reader=None,
           admin_user: str = ADMIN_USER, password: str | None = None,
           password_reader=None, log=None) -> list[str]:
    """Read identity off every host we can reach. Returns warnings.

    `names` maps canonical MAC -> hostname, from the router's lease table;
    the hostname is what locates the device on the tailnet.
    """
    reader = reader or read
    nodes = tailnet_nodes() if nodes is None else nodes
    warnings: list[str] = []
    if not nodes:
        return ["no tailscale node list, so no host identity was read. Is "
                "Tailscale up?"]

    for item in found:
        mac = _canon(item.entry.mac)
        hostname = names.get(mac)
        if not hostname:
            continue
        address = nodes.get(hostname.casefold())
        if not address:
            continue

        reading = reader(address, user)
        if log:
            log.command(f"{hostname} ({address}) as {user}", "read DMI + ip addr",
                        rc=0 if not reading.error else 1,
                        output=(f"model={reading.model} firmware={reading.firmware} "
                                f"macs={sorted(reading.macs.values())} "
                                f"addrs={reading.addrs}") if not reading.error else None,
                        error=reading.error)

        # A host that refuses the key may still take the admin account and a
        # password. Only tried when one is configured, and only after the
        # key has actually been refused - so the normal path stays keys-only.
        if reading.error and password and "denied" in reading.error.lower():
            retry = (password_reader or read_with_password)(
                address, admin_user, password)
            if not retry.error:
                reading = retry
            else:
                reading.error = (
                    f"{reading.error} Retried as {admin_user}: {retry.error}"
                )

        if reading.error:
            # A host that will not let us in is a finding about the host.
            warnings.append(f"{hostname} ({address}): {reading.error}")
            item.notes.append(
                f"Identity could not be read over the tailnet at {address}: "
                f"{reading.error} Nothing about its model or firmware is "
                f"established as a result."
            )
            continue

        # The cross-check, before anything is believed. 192.168.88.0/24 is
        # the subnet at every site AND on the bench, so a session opened to
        # the wrong machine answers convincingly.
        if mac and mac not in {_canon(m) for m in reading.macs.values()}:
            warnings.append(
                f"{hostname} ({address}) reported MACs "
                f"{sorted(reading.macs.values())}, none matching the "
                f"{item.entry.mac} the router observed - reading DISCARDED."
            )
            item.notes.append(
                f"A host answering as `{hostname}` at {address} did not "
                f"report the MAC the router observed at this address, so its "
                f"identity was discarded rather than recorded. That is the "
                f"bench-collision guard doing its job."
            )
            continue

        if reading.model:
            item.read_model = reading.model
            item.evidence["model"] = "device-api"
        if reading.firmware:
            item.read_firmware = reading.firmware
            item.evidence["firmware"] = "device-api"
        legs = interfaces_from(reading, subnet)
        if legs:
            item.interfaces = legs
            item.evidence["addr"] = "device-api"

        detail = [
            f"Identity read on the host itself over the tailnet at {address}, "
            f"from /sys/class/dmi/id - world-readable, so no sudo and no "
            f"extra credential.",
            f"Vendor {reading.vendor or 'unread'}, model "
            f"{reading.model or 'unread'}, board {reading.board or 'unread'}. "
            f"`firmware` holds the BIOS version, which is the firmware "
            f"analogue on a PC.",
        ]
        if reading.os:
            detail.append(
                f"OS {reading.os} on kernel {reading.kernel or 'unread'}. "
                f"Recorded here rather than as firmware: software moves on "
                f"its own schedule."
            )
        detail.append(
            f"Accepted because the host reported the MAC the router had "
            f"already observed at this address. Without that match a bench "
            f"machine on the colliding 192.168.88.0/24 would answer just as "
            f"convincingly."
        )
        item.notes.extend(detail)

    return warnings
