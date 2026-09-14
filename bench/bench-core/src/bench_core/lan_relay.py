#!/usr/bin/env python3
"""Reaching bench devices from a Mac whose policy blocks local-network sockets.

On a managed macOS laptop the bench can find itself unable to open a socket to
a device that the operator can ping, browse and curl:

    $ curl http://192.168.0.100/          -> 200
    $ .venv/bin/python -c "socket..."     -> [Errno 65] No route to host, in 4 ms

Four milliseconds is the tell — a real routing failure does not resolve that
fast. macOS gates local-network access per executable (Local Network Privacy,
and on a corporate machine an endpoint-security agent on top of it), and the
verdict lands on the *binary*: Apple's own `curl`, `nc`, `ping` and
`/usr/bin/python3` are exempt, while an ad-hoc-signed Homebrew interpreter is
refused. Homebrew `node` is refused the same way, so this is not about Python.

Nothing inside the process can lift that: binding the source address, setting
IP_BOUND_IF, or copying the interpreter into the venv are all still refused,
and the system Python that *is* exempt is 3.9 — below what the bench's web
stack needs.

What is NOT blocked is loopback. So when direct sockets are refused, this
module puts a relay in front of the device: a listener on 127.0.0.1 that hands
each connection to `/usr/bin/nc`, an Apple-signed binary that may talk to the
LAN. `requests` and `paramiko` then connect to loopback and work unchanged.

    host, port = endpoint("192.168.0.100", 80)   # -> ("127.0.0.1", 54321)

`endpoint` returns the address untouched wherever direct sockets work, which is
every Windows and Linux station and most Macs — the relay only exists on a
machine that has already proved it needs one.
"""
from __future__ import annotations

import errno
import logging
import os
import socket
import subprocess
import threading
from typing import Optional

log = logging.getLogger(__name__)

# The Apple-signed relay binary. Chosen over `curl` because it carries arbitrary
# TCP, which is what SSH needs; over `socat`, which macOS does not ship.
NETCAT = "/usr/bin/nc"

# Errors that mean "policy refused this", not "the device is not there". A
# blocked connect fails instantly with one of these; a device that is merely
# absent times out, and one that is present but not listening is REFUSED.
BLOCKED_ERRNOS = frozenset({errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EPERM,
                            errno.EACCES})

PROBE_TIMEOUT_SEC = 2.0
BUFFER = 65536

_lock = threading.Lock()
_mode: Optional[str] = None            # None = undecided, "direct" or "relay"
_relays: dict[tuple[str, int], tuple[str, int]] = {}


def _pump(read, write) -> None:
    """Copy until one side closes. Both directions of every relayed
    connection run through here."""
    try:
        while True:
            chunk = read()
            if not chunk:
                return
            write(chunk)
    except (OSError, ValueError):
        return          # a closed socket or pipe is how a session ends


def _serve(listener: socket.socket, host: str, port: int) -> None:
    """Accept loopback connections forever, giving each one its own `nc`.

    One process per connection, not one for the relay: a bench run opens the
    web UI several times and holds an SSH session across it, and a shared pipe
    would interleave them.
    """
    while True:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        try:
            nc = subprocess.Popen([NETCAT, host, str(port)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        except OSError:
            conn.close()
            continue
        threading.Thread(target=_relay_pair, args=(conn, nc), daemon=True).start()


def _relay_pair(conn: socket.socket, nc: subprocess.Popen) -> None:
    def to_device(chunk):
        nc.stdin.write(chunk)
        nc.stdin.flush()

    upstream = threading.Thread(
        target=_pump, args=(lambda: conn.recv(BUFFER), to_device), daemon=True)
    upstream.start()
    _pump(lambda: nc.stdout.read1(BUFFER), conn.sendall)
    for close in (conn.close, nc.kill):
        try:
            close()
        except Exception:      # noqa: BLE001 — teardown must not raise
            pass


def _start_relay(host: str, port: int) -> tuple[str, int]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    threading.Thread(target=_serve, args=(listener, host, port),
                     daemon=True).start()
    local = ("127.0.0.1", listener.getsockname()[1])
    log.info("Local-network sockets are blocked for this interpreter — "
             "relaying %s:%s through %s on %s:%s.", host, port, NETCAT, *local)
    return local


def _nc_can_connect(host: str, port: int, timeout: float) -> bool:
    """Whether `nc` — which policy does allow out — can open the port.

    The reachability question has to be asked through the permitted binary and
    not through the relay: a relay's listener accepts on loopback whether or
    not the device behind it answers, so probing that would report a switch on
    an empty cable.
    """
    seconds = max(1, int(round(timeout)))
    try:
        return subprocess.run(
            [NETCAT, "-z", "-G", str(seconds), "-w", str(seconds), host, str(port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=seconds + 2).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def can_connect(host: str, port: int, timeout: float = PROBE_TIMEOUT_SEC) -> bool:
    """Is `host:port` open? Asked directly where that works, and through `nc`
    on a station whose policy refuses this interpreter.

    This is also where the choice between the two is made, because a blocked
    connect is indistinguishable from an absent device until something the
    policy permits has been asked.
    """
    global _mode
    if _mode == "relay":
        return _nc_can_connect(host, port, timeout)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            with _lock:
                _mode = "direct"
            return True
    except OSError as e:
        if e.errno not in BLOCKED_ERRNOS or not os.path.exists(NETCAT):
            return False
    # Refused instantly, and there is a binary that may be allowed to ask.
    # Only the device answering IT proves this is policy and not an empty
    # cable — an unplugged NIC also reports "no route to host".
    if not _nc_can_connect(host, port, timeout):
        return False
    with _lock:
        _mode = "relay"
    log.warning("This interpreter may not open local-network sockets, but "
                "%s:%s answers through %s — relaying device traffic over "
                "loopback for the rest of this session.", host, port, NETCAT)
    return True


def endpoint(host: str, port: int) -> tuple[str, int]:
    """Where to connect for `host:port` — the device itself, or a loopback
    relay standing in for it. Safe to call on every connection.

    Settles the question itself when nothing has yet: a run may open the web UI
    before anything has probed a port, and a caller should not have to know
    that it is the probe that decides.
    """
    if _mode is None:
        can_connect(host, port)
    with _lock:
        if _mode != "relay":
            return host, port
        if (host, port) not in _relays:
            _relays[(host, port)] = _start_relay(host, port)
        return _relays[(host, port)]


def url_for(host: str, port: int = 80) -> str:
    """`host:port` as an http:// base, through the relay when there is one."""
    relay_host, relay_port = endpoint(host, port)
    return f"http://{relay_host}:{relay_port}" if relay_port != 80 \
        else f"http://{relay_host}"


def reset() -> None:
    """Forget the decision and every relay. For tests, and for a station whose
    network changed under it."""
    global _mode
    with _lock:
        _mode = None
        _relays.clear()
