#!/usr/bin/env python3
"""A controllable NTP server for the bench, to find out why a RutOS device
that is configured correctly still will not sync (TEC-846).

The problem with diagnosing this against the real time server is that you get
one answer — "it didn't work" — and no way to vary the input. This serves NTP
from your laptop instead, with the three properties that RutOS's `ntpclient`
is known to reject on, each settable from the command line. Run it once per
hypothesis and read the device's `ntpclient -d` output.

Why it can bind the address the device already expects: the OTD500 is
configured to poll `192.168.1.2`, and the DHCP pool the bench writes starts at
`.100` precisely so that address is never leased (TEC-857). So you can claim it
on your laptop and the device needs NO config change to talk to you — which
matters, because this RutOS build dropped upstream's `-h` flag and takes its
server ONLY from `/etc/config/ntpclient`.

    # macOS: find the interface facing the device, then claim the address
    networksetup -listallhardwareports
    sudo ifconfig en0 alias 192.168.1.2 255.255.255.0

    # serve correct time, and watch whether the device's packets arrive at all
    sudo ./ntp-probe.py

    # the backwards-clock guard: the binary carries the string
    #   "Parsed time is older than the current system time. Not syncing."
    # so a device whose clock runs AHEAD can never sync. Serve the past:
    sudo ./ntp-probe.py --offset -600

    # the round-trip theory TEC-846 recorded: 2252ms against abs(DELAY)>65536
    sudo ./ntp-probe.py --delay 2300

    # a server declaring itself unsynchronised, which ntpclient rejects as LI==3
    sudo ./ntp-probe.py --leap 3

    # done — give the address back
    sudo ifconfig en0 -alias 192.168.1.2

Port 123 is privileged, hence the sudo; `--port` exists for a smoke test
without it. This only ever answers queries, so it changes nothing on the
device — the device decides what to do with what it is told.
"""
import argparse
import logging
import socket
import struct
import sys
import time

# NTP counts seconds from 1900-01-01; Unix from 1970-01-01.
NTP_EPOCH_OFFSET = 2208988800
PACKET_SIZE = 48
NTP_PORT = 123

log = logging.getLogger("ntp-probe")


def to_ntp_timestamp(unix_time: float) -> bytes:
    """A Unix time as NTP's 64-bit fixed point: 32 bits of seconds, 32 of
    fraction."""
    ntp_time = unix_time + NTP_EPOCH_OFFSET
    seconds = int(ntp_time)
    fraction = int((ntp_time - seconds) * (1 << 32))
    return struct.pack("!II", seconds & 0xFFFFFFFF, fraction & 0xFFFFFFFF)


def build_response(request: bytes, *, served_time: float, leap: int,
                   stratum: int) -> bytes:
    """A mode-4 server reply to `request`.

    The originate timestamp is copied from the client's transmit field rather
    than regenerated: `ntpclient` checks it (`ORG!=sent`) and drops the packet
    if it does not match, which would look exactly like the failure being
    investigated.
    """
    client_version = (request[0] >> 3) & 0b111
    li_vn_mode = (leap << 6) | (client_version << 3) | 4   # 4 = server

    # Receive and transmit are both stamped at arrival, so `--delay` shows up as
    # network round-trip (T4-T1) rather than as server stall (T3-T2). Those are
    # different numbers to ntpclient and only the first is what TEC-846 measured.
    stamp = to_ntp_timestamp(served_time)
    return b"".join([
        struct.pack("!BBbb", li_vn_mode, stratum, request[2] or 4, -20),
        struct.pack("!I", 0),          # root delay
        struct.pack("!I", 0),          # root dispersion
        b"LOCL",                       # reference id
        stamp,                         # reference timestamp
        request[40:48],                # originate  = client's transmit (T1)
        stamp,                         # receive    (T2)
        stamp,                         # transmit   (T3)
    ])


def describe(request: bytes) -> str:
    leap = (request[0] >> 6) & 0b11
    version = (request[0] >> 3) & 0b111
    mode = request[0] & 0b111
    return f"LI={leap} VN={version} Mode={mode}"


def serve(args: argparse.Namespace) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((args.bind, args.port))
    except PermissionError:
        log.error("Port %d needs root. Re-run with sudo, or pass --port for a "
                  "smoke test.", args.port)
        return 1
    except OSError as e:
        log.error("Could not bind %s:%d — %s", args.bind, args.port, e)
        log.error("If the address is not on this machine yet:  "
                  "sudo ifconfig <iface> alias %s 255.255.255.0", args.bind)
        return 1

    log.info("Serving NTP on %s:%d", args.bind, args.port)
    if args.offset:
        log.info("Serving time %+g s from real — a NEGATIVE offset asks the "
                 "device to step BACKWARDS, which this RutOS build refuses.",
                 args.offset)
    if args.delay:
        log.info("Holding each reply %d ms, so the device measures that as "
                 "round-trip delay.", args.delay)
    if args.leap == 3:
        log.info("Advertising LI=3 (unsynchronised), which ntpclient rejects.")
    log.info("Waiting for a query. If nothing arrives, the problem is the path "
             "— routing, the firewall zone, or the port forward — not the time.")

    served = 0
    while True:
        try:
            request, peer = sock.recvfrom(1024)
        except KeyboardInterrupt:
            log.info("Stopped after answering %d quer%s.",
                     served, "y" if served == 1 else "ies")
            return 0
        arrival = time.time()
        if len(request) < PACKET_SIZE:
            log.warning("%s:%d sent %d bytes, too short for NTP — ignoring.",
                        peer[0], peer[1], len(request))
            continue

        log.info("Query from %s:%d (%s)", peer[0], peer[1], describe(request))
        response = build_response(request, served_time=arrival + args.offset,
                                  leap=args.leap, stratum=args.stratum)
        if args.delay:
            time.sleep(args.delay / 1000)
        sock.sendto(response, peer)
        served += 1
        log.info("  answered with %s (stratum %d)",
                 time.strftime("%Y-%m-%d %H:%M:%S",
                               time.gmtime(arrival + args.offset)) + " UTC",
                 args.stratum)
        if args.once:
            return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="A controllable NTP server for diagnosing RutOS sync "
                    "failures (TEC-846).")
    parser.add_argument("--bind", default="192.168.1.2",
                        help="Address to serve on. Defaults to the fleet-"
                             "constant address the OTD500 already polls.")
    parser.add_argument("--port", type=int, default=NTP_PORT,
                        help="UDP port (default 123, which needs root).")
    parser.add_argument("--offset", type=float, default=0.0, metavar="SECONDS",
                        help="Serve time this far from real. Negative asks the "
                             "device to step backwards.")
    parser.add_argument("--delay", type=int, default=0, metavar="MS",
                        help="Hold each reply this long, to synthesise "
                             "round-trip delay.")
    parser.add_argument("--stratum", type=int, default=2,
                        help="Advertised stratum. 0 is rejected as STRATUM==0.")
    parser.add_argument("--leap", type=int, default=0, choices=[0, 1, 2, 3],
                        help="Leap indicator. 3 means unsynchronised and is "
                             "rejected as LI==3.")
    parser.add_argument("--once", action="store_true",
                        help="Answer one query and exit.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                        datefmt="%H:%M:%S")
    try:
        return serve(args)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
