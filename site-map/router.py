#!/usr/bin/env python3
"""Survey a site from its Teltonika router, read-only.

The router is the right vantage point, and not only because it is the
internet leg. It is the site's DHCP server, DNS resolver and default gateway,
so *everything* at the site has talked to it - where a site server's
neighbour table holds only what the server itself exchanged traffic with.
It is also the one device a FOB certainly has; a server is site-specific.

Four reads, and between them they answer more than an ARP sweep can:

    /proc/sys/kernel/hostname   which site this is. `hostname` is not a
                                binary on RutOS - BusyBox ash, rc 127.
    ip -4 addr show             the router's OWN legs. A host has no ARP
                                entry for itself, so the vantage point is
                                invisible to a sweep of it - which is why
                                the server used to be missing from the
                                diagram when the server was the vantage
                                point. Read here, the router arrives with
                                br-lan, wan and tailscale0 all three.
    /tmp/dhcp.leases            MAC -> hostname, from the lease table. This
                                is how a camera stops being
                                `hangzhou_juru_technology_1` and becomes
                                the serial on its own label.
    ip neigh show               who answered, and at what MAC.

Transport is `bench_core.TeltonikaClient`, the same client `probe.py` uses,
for one reason that matters: `set_read_only(True)` refuses a command that
changes the device *by construction* rather than by careful reading. The
routers take root plus the shared password and reject keys entirely
(`Permission denied (publickey,password)` with Tailscale SSH off), so raw
`ssh -o BatchMode=yes` cannot reach them and rolling keys onto a dozen
production routers to make a read-only tool work is the wrong trade.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import sweep as sweep_mod

# RutOS carries no `hostname` binary, and BusyBox ash reports rc 127 for it.
CMD_HOSTNAME = "cat /proc/sys/kernel/hostname"
CMD_ADDRS = "ip -4 addr show"
CMD_LEASES = "cat /tmp/dhcp.leases 2>/dev/null || true"
# Static reservations. A device configured with a fixed address holds no
# lease, so /tmp/dhcp.leases never names it - but the reservation does.
CMD_RESERVATIONS = "uci show dhcp 2>/dev/null | grep -E '@host' || true"
CMD_NEIGH = "ip neigh show"
# `ip -4 addr` prints no link/ether line at all, so the MAC - which is the
# identity everything else in this model is keyed on - comes from sysfs.
CMD_MACS = (
    'for d in /sys/class/net/*; do '
    'echo "$(basename $d) $(cat $d/address 2>/dev/null)"; done'
)

# Reverse DNS through the router's own resolver, for whatever the lease table
# and the reservations between them did not name. This is all `nmap -sn`
# hostname discovery does, and nmap is not installed on RutOS anyway. It
# answers for anything dnsmasq knows (expandhosts is on) and NXDOMAINs for a
# radar, which has no lease and no reservation - nothing short of reading the
# device will name one of those.
CMD_RDNS = (
    'for a in %s; do n=$(nslookup "$a" 127.0.0.1 2>/dev/null '
    "| sed -n 's/.*name = \\(.*\\)\\./\\1/p' | head -1); "
    '[ -n "$n" ] && echo "$a $n"; done'
)

# The router's own bridge forwarding database: MAC -> the physical port it was
# learned on. This is the one read that sees a switch with no address at all.
# A switch does not need to answer anything to be proven: several MACs learned
# on one port means several devices behind one cable, and that is a switch
# whether or not it holds an address. An unmanaged one never sources a frame,
# so it is in nobody's table - but its POSITION is still provable, because the
# port its clients were learned on is the port it hangs off.
CMD_FDB = "bridge fdb show 2>/dev/null || true"
# Which ports have a cable in them at all, so a dark port is not read as a
# port with nothing behind it.
CMD_LINK = "ip -o link show 2>/dev/null || true"
# The device the default route points at. At every FOB surveyed this is a
# SECOND Teltonika - the outdoor SIM unit - which means what the map draws as
# the internet leg is the indoor router, and the real one is a hop further
# out and on the WAN subnet, where no LAN sweep will ever find it.
# `$2 == "via"` matters: a route can be device-scoped with no gateway at all
# (`default dev usb0`, which is how the one tethered-modem site routes), and
# blindly taking the third field reported the interface name as if it were an
# upstream address.
CMD_UPSTREAM = (
    "ip route show default 2>/dev/null | head -1 | "
    "awk '$2==\"via\"{print \"gateway\", $3} "
    "$2==\"dev\"{print \"device\", $3}'; "
    "gw=$(ip route show default 2>/dev/null | awk '$2==\"via\"{print $3; exit}'); "
    '[ -n "$gw" ] && ip neigh show "$gw" 2>/dev/null || true'
)

# Passive capture of one spanning-tree BPDU per port. Listening only: no
# frame is sent, nothing on the device is changed, and the capture is bounded
# by `timeout` and by -c so it cannot sit on a production router.
#
# Why this is worth twelve seconds: a BPDU is generated by a bridge and
# CONSUMED by the next bridge along rather than forwarded, so one arriving on
# a router port names the nearest bridge on that port - the switch the cable
# actually lands on, which no amount of reading the router's own tables can
# establish. The frame also carries the root bridge of that fabric and the
# sender's path cost to it, and a non-zero cost proves a further bridge
# exists beyond the one we can see.
#
# tcpdump is not on $PATH on RutOS; it ships under /usr/local.
TCPDUMP_PATHS = ("/usr/local/usr/sbin/tcpdump", "/usr/sbin/tcpdump",
                 "/usr/bin/tcpdump")
BPDU_SECONDS = 12


# MikroTik's neighbour-discovery protocol. Broadcast every 30s, to nobody in
# particular, with no authentication: the device says its model, its RouterOS
# version and its serial number out loud. Three of the sites surveyed run a
# MikroTik as their switch and nobody has credentials for any of them, so
# this is the only read that will ever settle what they are.
MNDP_PORT = 5678
MNDP_SECONDS = 35

# MNDP TLV types worth keeping.
MNDP_FIELDS = {1: "mac", 5: "identity", 7: "version", 8: "platform",
               11: "serial", 12: "board", 16: "interface"}


def cmd_mndp(port: str, seconds: int = MNDP_SECONDS) -> str:
    return (
        f'TD=""; for p in {" ".join(TCPDUMP_PATHS)}; do '
        f'[ -x "$p" ] && TD="$p" && break; done; '
        f'[ -n "$TD" ] || {{ echo NO_TCPDUMP; exit 0; }}; '
        f'timeout {seconds} "$TD" -i {port} -nn -x -s 400 -c 1 '
        f"'udp port {MNDP_PORT}' 2>/dev/null || true"
    )


def parse_mndp(text: str) -> dict:
    """A captured MNDP broadcast -> what the device says it is.

    Parsed from the hex dump as real TLVs rather than scraped out of
    tcpdump's printable rendering: the strings sit next to binary uptime and
    address fields, and a regex over `-A` output would be guessing where one
    value ends and the next begins.
    """
    if not text or "NO_TCPDUMP" in text:
        return {}
    blob = bytes.fromhex("".join(
        part for line in text.splitlines()
        if ":" in line and line.strip().startswith("0x")
        for part in line.split(":", 1)[1].split()
    ) or "")
    if len(blob) < 32:
        return {}
    ihl = (blob[0] & 0x0F) * 4          # IP header, then UDP, then 4 of MNDP
    at = ihl + 8 + 4
    out: dict = {}
    while at + 4 <= len(blob):
        kind = int.from_bytes(blob[at:at + 2], "big")
        size = int.from_bytes(blob[at + 2:at + 4], "big")
        at += 4
        value = blob[at:at + size]
        at += size
        name = MNDP_FIELDS.get(kind)
        if not name or len(value) != size:
            continue
        if name == "mac" and size == 6:
            out["mac"] = ":".join(f"{b:02x}" for b in value)
        elif name != "mac":
            text_value = value.rstrip(b"\x00").decode("utf-8", "replace").strip()
            if text_value:
                out[name] = text_value
    return out


def cmd_bpdu(port: str, seconds: int = BPDU_SECONDS) -> str:
    """Capture one BPDU on `port`, or print NO_TCPDUMP.

    A plain loop, and not a `(...)` group: parentheses are a SUBSHELL, so the
    first version of this set $TD inside one and the assignment evaporated
    before the next command could use it. Every site reported NO_TCPDUMP on a
    router that has tcpdump installed.
    """
    return (
        f'TD=""; for p in {" ".join(TCPDUMP_PATHS)}; do '
        f'[ -x "$p" ] && TD="$p" && break; done; '
        f'[ -n "$TD" ] || {{ echo NO_TCPDUMP; exit 0; }}; '
        f'timeout {seconds} "$TD" -i {port} -nn -e -s 200 -c 1 -v '
        f"'ether dst 01:80:c2:00:00:00' 2>/dev/null || true"
    )


# Interfaces that are never a site leg worth drawing.
SKIP_IFACES = ("lo",)
# An interface whose name says it faces outward, whatever its address.
EXTERNAL_NAMES = ("tailscale", "wan", "wwan", "ppp", "mob", "usb")

ADDR_LINE = re.compile(
    r"^\s*inet\s+(?P<addr>[0-9.]+)/(?P<prefix>\d+)"
)
IFACE_LINE = re.compile(r"^\d+:\s+(?P<name>[^:@]+)[:@]")


class RouterError(Exception):
    pass


@dataclass
class Iface:
    name: str
    addr: str
    prefix: int
    scope: str
    mac: str | None = None


@dataclass
class Survey:
    host: str
    hostname: str | None
    identity: dict = field(default_factory=dict)
    subnet: str | None = None
    interfaces: list[Iface] = field(default_factory=list)
    leases: dict = field(default_factory=dict)   # canonical MAC -> hostname
    by_addr: dict = field(default_factory=dict)  # address -> hostname
    arp_text: str = ""
    # port -> MACs learned on it, and port -> has-carrier. Together these are
    # the fan-out: the only evidence at this vantage point that distinguishes
    # "ten devices on ten cables" from "ten devices behind one".
    fdb: dict = field(default_factory=dict)
    ports: dict = field(default_factory=dict)
    upstream: dict = field(default_factory=dict)
    # port -> the BPDU captured on it, when listening was asked for.
    bpdu: dict = field(default_factory=dict)
    # port -> the MNDP broadcast heard on it, same run.
    mndp: dict = field(default_factory=dict)
    swept: int = 0
    answered: int = 0
    warnings: list[str] = field(default_factory=list)


def _add_bench_path() -> None:
    """Put bench-core and its venv on the path, as probe.py does."""
    bench = Path(__file__).resolve().parent.parent / "bench"
    candidates = [bench / "bench-core" / "src"]
    candidates.extend((bench / ".venv" / "lib").glob("python*/site-packages"))
    for path in candidates:
        if path.is_dir() and str(path) not in sys.path:
            sys.path.insert(0, str(path))


def connect(host: str, password: str):
    """A read-only client for one router."""
    _add_bench_path()
    try:
        import bench_core
        from bench_core import TeltonikaClient
    except ImportError as exc:
        raise RouterError(
            f"bench_core is not importable ({exc}); the router transport "
            f"needs the bench checkout beside this one."
        ) from exc

    client = TeltonikaClient(host=host)
    # Before anything else. A seatbelt fastened later is not a seatbelt, and
    # this client's guard does not gate its own login.
    client.set_read_only(True)
    try:
        # The REST login also stores the password for the SSH leg.
        client.login(password)
    except Exception as exc:
        raise RouterError(
            f"cannot log in to the router at {host}: {exc}. The routers take "
            f"root plus the shared password; pass it with --password."
        ) from exc
    return client, bench_core


def parse_addrs(text: str) -> list[Iface]:
    """`ip -4 addr show` -> the interfaces worth drawing.

    Scope is decided by name first, because a name is the device's own word
    for which way the interface faces. `tailscale0` carries a /32 out of
    100.64/10 and `wan` here holds 192.168.1.164 - a private address that is
    emphatically not this site's LAN.
    """
    out: list[Iface] = []
    current = None
    for line in text.splitlines():
        match = IFACE_LINE.match(line)
        if match:
            current = match.group("name").strip()
            continue
        addr = ADDR_LINE.match(line)
        if addr and current and current not in SKIP_IFACES:
            low = current.casefold()
            out.append(Iface(
                name=current,
                addr=addr.group("addr"),
                prefix=int(addr.group("prefix")),
                scope=("external" if any(n in low for n in EXTERNAL_NAMES)
                       else "internal"),
            ))
    return out


def parse_macs(text: str) -> dict:
    """`<iface> <mac>` per line -> name -> MAC, skipping the ones with none."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and len(parts[1]) == 17:
            out[parts[0]] = parts[1].lower()
    return out


def parse_reservations(text: str) -> dict:
    """`uci show dhcp` @host sections -> canonical MAC -> name.

    Sections arrive as separate lines per option, so they are grouped by
    index before being read.
    """
    sections: dict = {}
    for line in text.splitlines():
        match = re.match(r"dhcp\.@host\[(\d+)\]\.(\w+)='([^']*)'", line.strip())
        if match:
            index, key, value = match.groups()
            sections.setdefault(index, {})[key] = value
    out = {}
    for fields in sections.values():
        mac, name = fields.get("mac"), fields.get("name")
        if mac and name:
            out[_canon(mac)] = name
    return out


def parse_rdns(text: str) -> dict:
    """`<addr> <name>` per line -> addr -> name, with the domain trimmed."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            name = parts[1].rstrip(".")
            for suffix in (".lan", ".local", ".localdomain"):
                if name.endswith(suffix):
                    name = name[: -len(suffix)]
            if name:
                out[parts[0]] = name
    return out


def parse_fdb(text: str) -> dict:
    """`bridge fdb show` -> {port: [mac, ...]}, learned entries only.

    `permanent` and `self` rows are the bridge's own addresses. They are not
    learned from traffic, so they are evidence about the router and about
    nothing else - counting them would make every port look like it had one
    more device behind it than it does.
    """
    out: dict[str, set] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[1] != "dev":
            continue
        if "permanent" in parts or "self" in parts:
            continue
        # Kept as printed, not canonicalised: these end up in notes a person
        # reads. Matching canonicalises both sides instead.
        mac = parts[0].lower()
        if not _canon(mac) or set(mac) <= set(":0"):
            continue
        out.setdefault(parts[2], set()).add(mac)
    return {port: sorted(macs) for port, macs in sorted(out.items())}


def parse_links(text: str) -> dict:
    """`ip -o link show` -> {interface: has_carrier}."""
    out = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        head = line.split(":", 2)
        if len(head) < 3:
            continue
        name = head[1].strip().split("@")[0]
        flags = line[line.find("<") + 1:line.find(">")] if "<" in line else ""
        out[name] = "LOWER_UP" in flags and "NO-CARRIER" not in flags
    return out


# 802.1D-2004 recommended path costs. The interesting one is 200000: a
# hundred-megabit link inside a fabric carrying cameras is a defect, and this
# is the only place a survey from the router can see it.
LINK_COST = {2000: "10 Gb", 20000: "1 Gb", 200000: "100 Mb", 2000000: "10 Mb"}


def parse_bpdu(text: str) -> dict:
    """One captured BPDU -> what it says about the bridge that sent it.

    `bridge-id` and `root-id` are `<priority>.<mac>.<port-id>` and
    `<priority>.<mac>`; the bridge MAC is the identity that matches an ARP
    entry, while the source MAC of the frame is the sending PORT's own MAC
    and usually differs by a digit or two.
    """
    if not text or "NO_TCPDUMP" in text:
        return {}
    out: dict = {}
    got = re.search(r"bridge-id\s+([0-9a-f]{1,4})\.([0-9a-f:]{17})\.([0-9a-f]{4})",
                    text)
    if got:
        out["bridge_priority"] = int(got.group(1), 16)
        out["bridge_mac"] = got.group(2)
        # Port-id is priority in the top nibble and the port number below it.
        out["port_id"] = int(got.group(3), 16) & 0x0FFF
    root = re.search(r"root-id\s+([0-9a-f]{1,4})\.([0-9a-f:]{17})", text)
    if root:
        out["root_mac"] = root.group(2)
    cost = re.search(r"root-pathcost\s+(\d+)", text)
    if cost:
        out["root_cost"] = int(cost.group(1))
        out["root_hop"] = LINK_COST.get(out["root_cost"])
    src = re.search(r"([0-9a-f:]{17})\s+>\s+01:80:c2", text)
    if src:
        out["src_mac"] = src.group(1)
    if "802.1w" in text:
        out["protocol"] = "rstp"
    elif "STP" in text:
        out["protocol"] = "stp"
    return out


def parse_upstream(text: str) -> dict:
    """The default gateway and its MAC, from CMD_UPSTREAM."""
    out: dict = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "gateway" and len(parts) > 1:
            out["addr"] = parts[1]
        elif parts[0] == "device" and len(parts) > 1:
            out["device"] = parts[1]
        elif "lladdr" in parts:
            # Printed form: this one is only ever shown to a person.
            out["mac"] = parts[parts.index("lladdr") + 1].lower()
            if not out.get("addr"):
                out["addr"] = parts[0]
    return out


def parse_leases(text: str) -> dict:
    """`/tmp/dhcp.leases` -> canonical MAC -> hostname.

    Format is `<expiry> <mac> <addr> <hostname> <client-id>`. A hostname of
    `*` means the client sent none, and recording that as a name would be
    worse than leaving the vendor-derived one.
    """
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        mac, name = parts[1], parts[3]
        if name in ("*", "", "?"):
            continue
        out[mac.replace(":", "").replace("-", "").upper()] = name
    return out


def survey(host: str, password: str, subnet: str | None = None,
           log=None, listen: bool = False) -> Survey:
    """Ping-sweep the subnet from the router and read what it knows.

    `subnet` may be None, and usually should be: the router tells us its own
    br-lan address and prefix, so the LAN is a fact we can read rather than
    a question to ask. A value passed here overrides that, for sweeping some
    other range from the same vantage point.
    """
    client, bench_core = connect(host, password)
    warnings: list[str] = []

    def read(command: str) -> str:
        timer = log.timed("router", command) if log else None
        if timer:
            timer.__enter__()
        try:
            out = client.ssh_exec(command, check=False) or ""
        except bench_core.MutationBlocked:
            raise
        except Exception as exc:
            warnings.append(f"`{command}` failed on the router: {exc}")
            if timer:
                timer.done(error=f"{type(exc).__name__}: {exc}")
            return ""
        if timer:
            timer.done(rc=0, output=out)
        return out

    identity = {}
    bpdu: dict = {}
    mndp: dict = {}
    fdb: dict = {}
    ports: dict = {}
    upstream: dict = {}
    try:
        # We are already authenticated, so this costs nothing and is the
        # difference between "vendor Teltonika" and "RUTM08 on
        # RUTM_R_00.07.24.3". `get_identity()` uses both transports: serial
        # and MAC come over SSH, firmware only from the REST endpoint.
        try:
            identity = client.get_identity() or {}
        except Exception as exc:
            warnings.append(f"could not read the router's own identity: {exc}")

        hostname = read(CMD_HOSTNAME).strip() or None
        interfaces = parse_addrs(read(CMD_ADDRS))
        macs = parse_macs(read(CMD_MACS))
        for iface in interfaces:
            iface.mac = macs.get(iface.name)

        # The LAN, from the router rather than from whoever is asking.
        lan = lan_subnet(interfaces)
        if subnet:
            addrs, net = sweep_mod.hosts(subnet)
            if lan and str(net) != lan:
                warnings.append(
                    f"sweeping {net} as asked, but the router's br-lan puts "
                    f"this site's LAN on {lan}. Anything found outside "
                    f"{lan} is not on this site's own subnet."
                )
        elif lan:
            addrs, net = sweep_mod.hosts(lan)
            if log:
                log.step("subnet read from the router", subnet=lan)
        else:
            raise RouterError(
                "the router reported no LAN interface, so there is no subnet "
                "to sweep and none was given. Pass one explicitly."
            )
        # Three sources, weakest last: an active lease is the device
        # speaking now, a reservation is what someone configured, and
        # reverse DNS is whatever the resolver will admit to.
        leases = parse_reservations(read(CMD_RESERVATIONS))
        leases.update(parse_leases(read(CMD_LEASES)))

        # The sweep itself. Bounded concurrency: an MT7621 running the site's
        # internet is not the place to fork 254 pings at once.
        quoted = " ".join(addrs)
        read(
            f"for a in {quoted}; do ping -n -c1 -W1 \"$a\" >/dev/null 2>&1 & "
            f"done; wait"
        )
        arp_text = sweep_mod.as_arp(read(CMD_NEIGH), net)

        # Only the addresses that answered, not all 254.
        answered_addrs = re.findall(r"\(([0-9.]+)\)", arp_text)
        by_addr = {}
        if answered_addrs:
            by_addr = parse_rdns(read(CMD_RDNS % " ".join(answered_addrs)))

        # Read AFTER the sweep on purpose. The forwarding database is learned
        # from traffic and ages out in about five minutes, so a device that
        # has been quiet is simply absent from it. The sweep we just ran is
        # what makes every reachable device speak.
        fdb = parse_fdb(read(CMD_FDB))
        ports = parse_links(read(CMD_LINK))
        upstream = parse_upstream(read(CMD_UPSTREAM))

        # Opt-in: twelve seconds per live port, and worth it where the switch
        # layer is ambiguous. Passive - the capture sends nothing.
        if listen:
            for port in sorted(p for p, up in ports.items()
                               if up and p.startswith("lan")):
                frame = parse_bpdu(read(cmd_bpdu(port)))
                if frame:
                    bpdu[port] = frame
                elif log:
                    log.step("no BPDU on this port", port=port,
                             seconds=BPDU_SECONDS)
                # MikroTik announces every 30s, so this waits longer than
                # the BPDU capture and is skipped where nothing on the port
                # is a MikroTik to begin with.
                heard = parse_mndp(read(cmd_mndp(port)))
                if heard:
                    mndp[port] = heard
                elif log:
                    log.step("no MNDP broadcast on this port", port=port,
                             seconds=MNDP_SECONDS)
    finally:
        try:
            client.close()
        except Exception:
            pass

    if not interfaces:
        warnings.append(
            "could not read the router's own interfaces, so the router will "
            "be drawn from the address you reached it on only."
        )
    answered = len([ln for ln in arp_text.splitlines() if ln.strip()])
    if not answered:
        warnings.append(
            f"nothing in {net} answered from the router. The sweep ran, so "
            f"that is a finding about the subnet rather than a failure."
        )
    return Survey(
        host=host, hostname=hostname, identity=identity, subnet=str(net),
        interfaces=interfaces, leases=leases,
        arp_text=arp_text, swept=len(addrs), answered=answered,
        by_addr=by_addr, warnings=warnings,
        fdb=fdb, ports=ports, upstream=upstream, bpdu=bpdu, mndp=mndp,
    )


def lan_subnet(interfaces: list) -> str | None:
    """The site's own subnet, from the router's internal leg.

    This is why the page does not have to ask. `br-lan 192.168.88.1/24` is
    the LAN, stated by the device that serves DHCP on it - which beats a
    default typed into a field, and gets a site on some other range right
    without anyone remembering to change it.
    """
    import ipaddress
    for iface in interfaces:
        if iface.scope != "internal" or not iface.addr or not iface.prefix:
            continue
        try:
            return str(ipaddress.ip_interface(
                f"{iface.addr}/{iface.prefix}").network)
        except ValueError:
            continue
    return None


# -- the router as a device ---------------------------------------------

# A hostname like `rut-kela-fob-03` names the router; the site is what is
# left once the device prefix is gone, and that is the name the site file
# already carries.
ROUTER_PREFIXES = ("rut-", "rutm-", "rutx-", "router-")

# What a lease hostname says the device IS. This is the answer to a question
# a MAC cannot settle: a site server and an operator station are both Dell,
# both on the e8:cf:83 OUI, and the remaining three bytes are Dell's own
# allocation sequence - so the only thing distinguishing them in a MAC is an
# accident of the purchase order. The hostname is the device's own word, and
# it beats the address plan (.10-20 server, .29 operator) which is a bench
# convention a site is free to depart from.
#
# Suffixes are checked before the bare site name, so `<site>-operator` is not
# read as `<site>`.
# Checked in order, and `operator` comes first for a reason:
# `kela-fob-03-operator` carries the site name too, so a server rule that
# ran first would call the operator station a server.
HOSTNAME_KINDS = (
    ("operator", "operator-station"),
    ("oc-station", "operator-station"),
    ("mediaserver", "server"),
    ("-host", "server"),
    ("udp-gateway", "server"),
    # A switch still on its factory hostname says so: five of the seven sites
    # surveyed name theirs `TSW202` or `Teltonika-TSW202` in the lease table.
    # Without these the switch stayed kind `network-device` and the diagram
    # had to fall back to guessing from the vendor.
    ("tsw", "switch"),
    ("psw", "switch"),
)


def site_name_from(hostname: str | None, host: str) -> str:
    if not hostname:
        return f"sweep-{host.replace('.', '-')}"
    low = hostname.casefold()
    for prefix in ROUTER_PREFIXES:
        if low.startswith(prefix):
            return hostname[len(prefix):]
    return hostname


def merge_vantage_point(found: list, result: Survey, subnet,
                        oui_table: dict | None = None) -> list:
    """Add the router to the device list, or enrich the entry already there.

    Both cases are real and the difference matters. A host has no ARP entry
    for *itself*, so the vantage point is normally the one device a sweep
    cannot see - that is the hole this fills. But the sweep pings every
    address in the subnet from the router, including the router's own, so
    the router often DOES end up in its own neighbour table. Inserting
    blindly then emits two `router:` keys in the YAML, and the second wins:
    the ARP-derived one silently overwrote the read-from-the-device one,
    taking all three interfaces with it.

    So: match on MAC, then on address, and enrich in place. Either way the
    device ends up with what the box itself said, which outranks an ARP
    entry for every claim they both cover.
    """
    fresh = as_device(result, subnet)
    if oui_table and fresh.entry.mac:
        # The router arrives from `ip addr`, not from the ARP table, so
        # nothing had resolved its OUI - and a Teltonika router was reading
        # as "vendor unknown" on its own map.
        import oui
        oui.resolve([fresh.entry], oui_table)
    mac = _canon(fresh.entry.mac)

    existing = None
    for item in found:
        if mac and _canon(item.entry.mac) == mac:
            existing = item
            break
        if fresh.entry.ip and item.entry.ip == fresh.entry.ip:
            existing = item
            break

    if existing is None:
        # The name has to be free, and at three of seven sites surveyed it was
        # not. `discover.name_devices` names 192.168.88.1 `router` straight
        # from the address plan, and at those sites .1 is a MikroTik switch
        # while the Teltonika's br-lan is .2 - so both wanted `router`, the
        # YAML got two `router:` keys, and the later one won. The device the
        # survey was RUN FROM was silently absent from its own map, taking
        # its model, firmware and three interfaces with it.
        taken = {item.name for item in found}
        if fresh.name in taken:
            clash = next(i for i in found if i.name == fresh.name)
            fresh.notes.append(
                f"Named `{fresh.name}` here; the address plan had already "
                f"given that name to {clash.entry.ip}, which is a different "
                f"device ({clash.vendor or 'vendor unknown'}). The plan is a "
                f"bench convention, and this site does not follow it."
            )
            _rename(clash, found)
        return [fresh] + found

    existing.kind = fresh.kind
    existing.interfaces = fresh.interfaces
    if fresh.entry.vendor and not existing.entry.vendor:
        existing.entry.vendor = fresh.entry.vendor
    if fresh.read_model:
        existing.read_model = fresh.read_model
    if fresh.read_firmware:
        existing.read_firmware = fresh.read_firmware
    existing.evidence = {**existing.evidence, **{
        k: v for k, v in fresh.evidence.items() if k != "*"}}
    existing.roles = sorted(set(existing.roles) | set(fresh.roles))
    existing.addr_source = fresh.addr_source
    # Read on the device beats an ARP observation for presence and address
    # alike, so the stronger source replaces the weaker one.
    existing.evidence = {**existing.evidence, "*": "device-api"}
    existing.notes = fresh.notes + existing.notes
    return found


def _rename(item, found: list) -> None:
    """Move a device off a name the vantage point needs, without inventing one.

    Renamed from what it actually is - its vendor - rather than from the
    address plan that mislabelled it. A reading beats a convention: the
    gateway was read off the device, so whatever else holds `.1` is not the
    router however the plan numbers it.
    """
    import discover as _discover

    base = _discover._slug(item.vendor or item.kind or "device")
    taken = {other.name for other in found if other is not item}
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base}_{n}"
    item.notes.append(
        f"Renamed from `{item.name}` to `{name}`: the address plan calls "
        f"{item.entry.ip} the router, but the router was read off the device "
        f"at the other end of this survey. A plan is a convention; this is a "
        f"reading, and the reading wins."
    )
    # ... and the kind the plan handed it goes with the name. Left alone, a
    # MikroTik switch stayed kind `router` and the diagram drew two of them.
    if item.kind == "router":
        item.kind = _discover._kind_for_vendor(item.vendor)
    item.name = name


def as_device(result: Survey, subnet) -> "object":
    """The router, as a device in its own right.

    This is the hole the vantage point leaves in any sweep: a host has no
    ARP entry for itself, so whatever you sweep from is the one device
    missing from the result. It was the server before, when the server was
    the vantage point; it would be the router now. So the router is added
    from its own `ip addr`, which is a stronger reading than ARP - its
    presence and every one of its addresses come from the device itself.
    """
    import discover as discover_mod
    import oui

    internal = [i for i in result.interfaces if i.scope == "internal"]
    on_subnet = [i for i in internal
                 if _in(i.addr, subnet)] or internal
    primary = on_subnet[0].addr if on_subnet else result.host

    # Keyed on the LAN leg's MAC: that is the identity an ARP table would
    # have recorded, so the router matches itself across both paths.
    primary_iface = on_subnet[0] if on_subnet else None
    entry = oui.Entry(mac=(primary_iface.mac if primary_iface else None) or "",
                      ip=primary, hostname=result.hostname)
    item = discover_mod.Discovered(entry=entry)
    item.name = "router"
    item.kind = "router"
    # `vendor` is a read-only property derived from the OUI, and there is no
    # MAC to derive it from here - the router reported addresses, not its own
    # LAN MAC. Left unset rather than asserted.
    item.evidence = {"*": "device-api"}

    # The manufacturing name comes back as `RUTM0800XXXX`, which is a product
    # code with the variant masked out; the clean model sits in the device
    # status block. Prefer the readable one and fall back to the other.
    raw = result.identity or {}
    static = (((raw.get("raw") or {}).get("device_status") or {})
              .get("data") or {}).get("static") or {}
    model = static.get("model") or raw.get("model")
    firmware = static.get("fw_version") or raw.get("firmware")
    if model:
        item.read_model = str(model)
        item.evidence["model"] = "device-api"
    if firmware:
        item.read_firmware = str(firmware)
        item.evidence["firmware"] = "device-api"
    item.addr_source = "static-manual"
    # Read off the device, not inferred from the address: this is the box the
    # session was opened to, and these are the legs it reported.
    item.roles = ["default-gateway"]
    item.interfaces = [
        {"name": i.name, "addr": i.addr, "scope": i.scope, "prefix": i.prefix}
        for i in result.interfaces
    ]
    legs = ", ".join(f"{i.name} {i.addr}/{i.prefix}" for i in result.interfaces)
    item.notes = [
        f"The site's router, and the host this survey ran from. Its "
        f"interfaces were read on the device with `ip -4 addr show`: {legs}. "
        f"Presence and addresses are therefore established by the device "
        f"itself rather than by an ARP entry - a host never appears in its "
        f"own neighbour table, which is why the vantage point has to be "
        f"added here or it is the one device missing from the map."
    ]
    if result.leases:
        item.notes.append(
            f"Serving {len(result.leases)} DHCP lease(s) with hostnames, read "
            f"from /tmp/dhcp.leases. That makes the gateway role a reading "
            f"rather than an inference from holding .1."
        )
    return item


def _in(addr: str, subnet) -> bool:
    import ipaddress
    try:
        return ipaddress.IPv4Address(addr) in subnet
    except (ValueError, TypeError):
        return False


def _canon(mac: str) -> str:
    return (mac or "").replace(":", "").replace("-", "").replace(".", "").upper()


def _squash(text: str) -> str:
    """Lower-case, letters and digits only. `Teltonika-TSW202` and
    `Teltonika TSW202` are the same name written by two people."""
    return "".join(c for c in text.casefold() if c.isalnum())


def _names_model(hostname: str, device) -> bool:
    """Does this DHCP hostname name that catalogue model?

    Matched against the aliases as well as the full model string, and with
    separators squashed. A plain substring test on the model alone said yes
    to `TSW202` and no to `Teltonika-TSW202`, because the catalogue spells it
    `Teltonika TSW202` and a hyphen is not a space. One character, and the
    switch lost its model on three of the five sites that named it.
    """
    want = _squash(hostname)
    if not want:
        return False
    names = [device.model, *(getattr(device, "aliases", None) or [])]
    return any(want in _squash(n) or _squash(n) in want
               for n in names if n)


def kind_from_hostname(hostname: str, site_name: str | None = None):
    """(kind, why) for a hostname, or (None, "") if it says nothing.

    Substring rather than suffix: the convention is that an operator station
    has `operator` in its name *somewhere* and a server carries the site's
    own name. `afb8-oc-station` and `fob-91-hamamis-mediaserver` both exist,
    so anchoring on the end misses real hosts.
    """
    low = hostname.casefold()
    for needle, kind in HOSTNAME_KINDS:
        if needle in low:
            return kind, (
                f"its hostname `{hostname}` contains `{needle}`. A MAC "
                f"cannot tell a server from an operator station - both are "
                f"Dell on the same OUI - but the device's own hostname can."
            )
    if site_name:
        short = site_name.casefold()
        if low == short or short in low:
            return "server", (
                f"its hostname `{hostname}` carries the site's own name, "
                f"which is what the site server is called. An operator "
                f"station has `operator` in its name as well."
            )
    return None, ""


@dataclass
class FanOut:
    """What one physical router port turned out to be carrying."""

    port: str
    macs: list           # everything learned on this port
    known: list          # node names among them
    switches: list       # node names among them that read as a switch
    unknown: list        # MACs that answered nothing on the sweep

    @property
    def fans_out(self) -> bool:
        return len(self.macs) > 1

    @property
    def hidden_switch(self) -> bool:
        """Several devices behind one cable and no switch among them.

        Whatever is fanning out is not in anybody's table, which is what an
        unmanaged switch looks like from every angle: it never sources a
        frame, so it has no entry anywhere. Its POSITION is still proven -
        it is on this port - and its clients are exactly these MACs. Its
        model, serial and firmware are unknowable by any protocol.
        """
        return self.fans_out and not self.switches


def vantage_name(found: list, result: Survey) -> str:
    """Whatever the vantage point ended up called in this device list.

    Nearly always `router`, and NOT reliably: `merge_vantage_point` enriches
    an ARP-derived entry in place when one exists for the same MAC, and that
    entry was named by the address plan - which calls `.2` `switch_mgmt`. A
    fan-out edge parented on the literal string `router` would then point at
    a node that does not exist, and a dangling edge fails to load rather than
    drawing wrong, but only at the site unlucky enough to have both.
    """
    own = {_canon(i.mac) for i in result.interfaces if getattr(i, "mac", None)}
    own.discard("")
    for item in found:
        if item.entry.mac and _canon(item.entry.mac) in own:
            return item.name
    return "router"


def fan_out(found: list, result: Survey, router_name: str = "router") -> list:
    """Read the forwarding database as fan-out, one entry per live port.

    Ports with no carrier are skipped: a dark port carries nothing, and
    saying so about a port with no cable in it is noise rather than a
    finding.
    """
    import topology

    by_mac = {_canon(item.entry.mac): item for item in found
              if item.entry.mac}
    own = {_canon(i.mac) for i in result.interfaces if getattr(i, "mac", None)}
    own.discard("")

    out = []
    for port, macs in (result.fdb or {}).items():
        if result.ports and not result.ports.get(port, True):
            continue
        macs = [m for m in macs if _canon(m) not in own]
        if not macs:
            continue
        known, switches, unknown = [], [], []
        for mac in macs:
            item = by_mac.get(_canon(mac))
            if item is None:
                unknown.append(mac)
                continue
            known.append(item.name)
            if item.name != router_name and topology._is_switch(item)[0]:
                switches.append(item.name)
        out.append(FanOut(port=port, macs=macs, known=known,
                          switches=switches, unknown=unknown))
    return out


def fdb_edges(fans: list, router_name: str = "router") -> list:
    """The cabling the forwarding database actually proves.

    Deliberately narrow. The database says which port a MAC was learned on,
    which is not the same as what it is plugged into, so only two shapes are
    claimed as links:

      one MAC on a port      that device is on the far end of that cable
      one switch among many  that switch's uplink is this port; the rest are
                             behind it, but which of ITS ports each one is on
                             needs the switch's own table, not this one

    Everything else - a port fanning out with no switch, or with two - gets
    no edge at all. A wrong line on a diagram is worse than a missing one,
    because somebody will wire to it.
    """
    edges = []
    for fan in fans:
        if not fan.fans_out and len(fan.known) == 1:
            edges.append({
                "from": router_name, "to": fan.known[0], "port": fan.port,
                "evidence": "switch-table",
                "notes": (
                    f"the only MAC the router learned on {fan.port}, so this "
                    f"device is on the far end of that cable. A switch with "
                    f"exactly one device behind it would look identical - "
                    f"that is the one case no table can separate."
                ),
            })
        elif len(fan.switches) == 1:
            others = len(fan.macs) - 1
            edges.append({
                "from": router_name, "to": fan.switches[0], "port": fan.port,
                "evidence": "switch-table",
                "notes": (
                    f"the router learned this switch's MAC on {fan.port}, "
                    f"along with {others} other device(s) behind the same "
                    f"cable - so this is the switch's uplink. Which of the "
                    f"switch's own ports each device sits on needs the "
                    f"switch's MAC-address table, not the router's."
                ),
            })
    return edges


def fan_out_notes(fans: list) -> list:
    """What the fan-out says, for the warnings list."""
    notes = []
    for fan in fans:
        if fan.hidden_switch:
            notes.append(
                f"{len(fan.macs)} MACs were learned on the router's "
                f"{fan.port} and not one of them reads as a switch, so an "
                f"unmanaged switch is on that port: several devices behind "
                f"one cable is a switch whether or not it holds an address. "
                f"Its position and its clients are proven; its model cannot "
                f"be read by any protocol, because it never sources a frame."
            )
        elif len(fan.switches) > 1:
            notes.append(
                f"{fan.port} carries {len(fan.switches)} switches "
                f"({', '.join(fan.switches)}) behind one cable, so which is "
                f"upstream of which is not settled here - their own "
                f"MAC-address tables settle it in one read each."
            )
        if fan.unknown:
            notes.append(
                f"{len(fan.unknown)} MAC(s) on {fan.port} answered nothing "
                f"in the sweep: {', '.join(fan.unknown)}. The bridge learned "
                f"them, so they are present and talking - they just hold no "
                f"address on this subnet."
            )
    return notes


def stp_facts(found: list, result: Survey,
              router_name: str = "router") -> tuple[list, list]:
    """What the captured BPDUs establish. Returns (edges, notes).

    Three different kinds of fact come out of one frame, and they are worth
    keeping apart:

      the sender is a bridge     only bridges speak STP, so this is the first
                                 evidence-backed `switch` kind we can get for
                                 a device whose model nobody has read
      it is the nearest bridge   on that port, so the router's cable lands on
                                 it - which settles the chain order that the
                                 router's own forwarding table cannot
      the root, and the cost     a non-zero path cost from the nearest bridge
                                 means ANOTHER bridge sits beyond it, and the
                                 cost names the speed of the link between
                                 them. 200000 is a 100 Mb hop, which inside a
                                 fabric carrying cameras is a defect.
    """
    by_mac = {_canon(item.entry.mac): item for item in found
              if item.entry.mac}
    edges, notes = [], []

    for port, frame in sorted((result.bpdu or {}).items()):
        bridge = frame.get("bridge_mac")
        item = by_mac.get(_canon(bridge)) if bridge else None
        root = frame.get("root_mac")
        root_item = by_mac.get(_canon(root)) if root else None
        cost = frame.get("root_cost")
        hop = frame.get("root_hop")

        if item is None:
            notes.append(
                f"A {frame.get('protocol', 'stp').upper()} BPDU arrived on "
                f"{port} from bridge {bridge}, which answered nothing in the "
                f"sweep. It is a switch - only bridges speak STP - and it "
                f"holds no address on this subnet."
            )
            continue

        # Only bridges speak STP. This is a reading, not a vendor lead.
        if item.kind not in ("switch", "poe-switch"):
            item.notes.append(
                f"Sends {frame.get('protocol', 'stp').upper()} BPDUs on the "
                f"router's {port}, so it is a bridge: only a switch "
                f"participates in spanning tree. That makes `switch` a "
                f"reading here rather than an inference from the vendor."
            )
            item.kind = "switch"
        edges.append({
            "from": router_name, "to": item.name, "port": port,
            "peer_port": (f"port {frame['port_id']}"
                          if frame.get("port_id") else None),
            "evidence": "stp",
            "notes": (
                f"a BPDU from this bridge arrived on {port}, and a BPDU is "
                f"consumed by the next bridge along rather than forwarded - "
                f"so this is the nearest bridge on that cable. An unmanaged "
                f"switch in between would forward BPDUs and leave no trace, "
                f"so the claim is `nearest bridge`, not `directly attached`."
            ),
        })

        # The root named in the frame is a bridge too: only a bridge has a
        # bridge-id and only a bridge can be elected root. It never sent us
        # anything - it sits behind the one that did - so nothing else in a
        # survey can establish what it is.
        if root_item is not None and _canon(root) != _canon(bridge):
            if root_item.kind not in ("switch", "poe-switch"):
                root_item.notes.append(
                    f"Named as the spanning-tree root in a BPDU the router "
                    f"received on {port}. Only a bridge has a bridge-id and "
                    f"only a bridge can be elected root, so this is a switch "
                    f"- read, not inferred - even though it sent the router "
                    f"nothing itself."
                )
                root_item.kind = "switch"

        who = item.name
        if root and _canon(root) == _canon(bridge):
            notes.append(
                f"{who} is the spanning-tree root of the fabric on {port} "
                f"(path cost 0), and the nearest bridge to the router on it. "
                f"Everything else on that leg is below it."
            )
        elif cost:
            beyond = root_item.name if root_item else f"bridge {root}"
            notes.append(
                f"{who} is the nearest bridge on {port}, but the root of that "
                f"fabric is {beyond} at a path cost of {cost}"
                + (f" - one {hop} link" if hop else "")
                + f". A non-zero cost is proof of a further bridge beyond "
                f"{who}, whether or not it holds an address."
            )
            if cost and cost >= 200000:
                notes.append(
                    f"DEFECT: the link between {who} and {beyond} on {port} "
                    f"is costed {cost}, which 802.1D assigns to 100 Mb. "
                    f"Everything behind {who} reaches the router through a "
                    f"fast-ethernet bottleneck - a bad cable, a bad port, or "
                    f"an old unit."
                )
    return edges, notes


def mndp_facts(found: list, result: Survey) -> list:
    """Apply what a device broadcast about itself. Returns notes.

    This is a model and a firmware read off the device's own word, with no
    credential involved, which is the only reason three sites' MikroTik
    switches are identifiable at all - nobody has RouterOS logins for them.
    """
    by_mac = {_canon(item.entry.mac): item for item in found
              if item.entry.mac}
    notes = []
    for port, heard in sorted((result.mndp or {}).items()):
        mac = heard.get("mac")
        item = by_mac.get(_canon(mac)) if mac else None
        board = heard.get("board")
        version = heard.get("version")
        if item is None:
            notes.append(
                f"A device broadcast its identity on {port} - "
                f"{board or heard.get('platform') or 'unnamed'} at {mac} - "
                f"and answered nothing in the sweep, so it is present and "
                f"talking while holding no address on this subnet."
            )
            continue
        if board and not item.read_model:
            item.read_model = board
            item.evidence = {**item.evidence, "model": "discovery"}
        if version and not item.read_firmware:
            item.read_firmware = version
            item.evidence = {**item.evidence, "firmware": "discovery"}
        told = []
        if board:
            told.append(f"model `{board}`")
        if version:
            told.append(f"firmware `{version}`")
        if heard.get("serial"):
            told.append(f"serial `{heard['serial']}`")
        if heard.get("interface"):
            told.append(f"sent from its `{heard['interface']}`")
        if told:
            item.notes.append(
                "Broadcast its own identity on the router's "
                f"{port} (MikroTik neighbour discovery, UDP "
                f"{MNDP_PORT}): {', '.join(told)}. Unauthenticated and "
                f"unsolicited - the device's own word, and the only read "
                f"available for a switch nobody has credentials for."
            )
    return notes


def upstream_note(result: Survey, oui_table: dict | None = None) -> str | None:
    """The device the site's default route points at.

    Worth saying out loud because it is not on this subnet and no sweep of
    the LAN will ever see it: at every FOB surveyed the default route leads
    to a SECOND Teltonika, the outdoor SIM unit. What the diagram draws as
    the internet leg is the indoor router; the real one is a hop further out.
    """
    up = result.upstream or {}
    addr = up.get("addr")
    if not addr:
        device = up.get("device")
        if not device:
            return None
        # No gateway address at all: the default route is device-scoped, so
        # the upstream link is point-to-point and there is nothing on it to
        # name. A tethered modem routes this way.
        return (
            f"The default route leaves through `{device}` with no gateway "
            f"address, so the upstream link is point-to-point and nothing on "
            f"it can be identified from here. That is a modem on the "
            f"interface rather than a router beyond it."
        )
    mac = (result.upstream or {}).get("mac")
    vendor = None
    if mac and oui_table:
        import oui
        entry = oui.Entry(ip=addr, mac=mac)
        oui.resolve([entry], oui_table)
        vendor = entry.vendor.name if entry.vendor else None
    who = f" ({vendor})" if vendor else ""
    return (
        f"The default route leaves through {addr}"
        f"{f' at {mac}' if mac else ''}{who}, which is upstream of this "
        f"subnet and so invisible to any sweep of it. At a FOB that is the "
        f"outdoor SIM unit: the internet leg is one hop beyond the router "
        f"this survey ran from."
    )


def lease_notes(found, leases: dict, site_name: str | None = None,
                by_addr: dict | None = None) -> None:
    """Record each device's DHCP hostname, without renaming it.

    The hostname is a real fact - the device's own word, from the router's
    lease table - and a better tell than the last octet of an address. It is
    NOT used as the node name: names are referenced by `net:` edges in site
    files, so changing how they are derived would rewrite identities across
    every existing model. Recorded as a note and a candidate basis instead.
    """
    import devices

    for item in found:
        name = leases.get(_canon(item.entry.mac))
        source = "the router's lease table or a static reservation"
        if not name and by_addr:
            name = by_addr.get(item.entry.ip or "")
            source = "reverse DNS through the router's own resolver"
        if not name:
            continue
        item.notes.append(
            f"Hostname `{name}`, from {source}. The "
            f"device's own word for what it is - worth more than the "
            f"address plan, which is a bench convention."
        )
        # An unprovisioned device still answers to its factory hostname, and
        # a factory hostname is a model number. That is how the curated
        # kela-fob-03 file pinned .118 as a TSW202 without ever logging in to
        # it - the lease was the only thing that would say.
        # What the device says it is. Overrides a kind derived from the
        # vendor, which for two Dells says nothing useful at all.
        kind, why = kind_from_hostname(name, site_name)
        if kind:
            item.kind = kind
            item.notes.append(f"Identified as a {kind} because {why}")

        matches = [d.model for d in devices.CATALOGUE
                   if _names_model(name, d)]
        if matches and not item.model:
            item.candidates = sorted(set(item.candidates) | set(matches))
            item.candidate_basis = (
                f"the DHCP lease for this MAC carries the hostname `{name}`, "
                f"which is a model number - so this unit is still on the "
                f"factory hostname it shipped with. A lease is a strong lead "
                f"and not a reading: nothing has asked the device itself."
            )
