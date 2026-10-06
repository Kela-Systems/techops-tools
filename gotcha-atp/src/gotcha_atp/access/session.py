"""SSH sessions to the unit, held open for the whole run (row S0.3).

    server    ssh -M -D <socks> kela@<site>            direct over the tailnet (first choice)
    operator  ssh -M kela@<site>-operator              direct over the tailnet, for the
                                                       operator's own checks only
    backup    when the direct server login fails: ssh -M -D <socks> kela@<site>-operator,
              then kela@192.168.88.10 through it (ProxyCommand)

The server carries everything else: its own checks, the SOCKS forward to the
LAN devices, device SSH (ProxyCommand) and the -L forwards to cluster IPs. The
operator session is independent — when it is missing, only the operator rows
are affected.

Every master is plain OpenSSH; later commands are multiplexed
(`ssh -S <ctl> host -- cmd`) and -L forwards are added with `ssh -O forward`.
Password auth goes through an askpass helper that reads the password from the
master's environment, so it is never on a command line, in a file, or in any
output; the key (or Tailscale SSH) is always tried first.
"""
from __future__ import annotations

import os
import shutil
import socket
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .exec import AccessError, ExecResult, run_local, run_sections

# How the server session was reached.
SERVER_DIRECT = "tailnet"
SERVER_VIA_OPERATOR = "via-operator"

MASTER_TIMEOUT_S = 40

_ASKPASS = """#!/bin/sh
: > "$GOTCHA_ATP_ASKPASS_USED"
printf '%s\\n' "$GOTCHA_ATP_PW"
"""


def _sudo_needs_password(r: ExecResult) -> bool:
    return r.rc == 1 and "a password is required" in (r.err + r.out)


class OperatorUnavailable(AccessError):
    """There is no operator session (no peer, offline, or the login failed)."""


class LoginRefused(AccessError):
    """The host answered and refused the credentials (as opposed to not answering)."""


@dataclass
class Unit:
    site: str
    operator_addr: str = ""     # tailnet IP when known, else MagicDNS name
    server_addr: str = ""
    server_lan: str = "192.168.88.10"
    # The operator's tailnet name when the engineer named one that is not the
    # standard <site>-operator; S0.4 still judges the hostname against the standard.
    operator_name: str = ""
    # False when the tailnet has no operator peer for this unit: no operator
    # session is attempted and the operator rows are amber.
    operator_peer: bool = True

    @property
    def standard_operator_host(self) -> str:
        return f"{self.site}-operator"

    @property
    def operator_host(self) -> str:
        return self.operator_name or self.standard_operator_host

    @property
    def server_host(self) -> str:
        return self.site


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ssh_error(stderr: str, who: str) -> str:
    low = stderr.lower()
    if "permission denied" in low:
        return f"{who} refused the login (wrong kela password, or the key is not authorized)"
    if "could not resolve hostname" in low or "name or service not known" in low:
        return f"{who} does not resolve — is it on the tailnet and is MagicDNS on for this laptop?"
    if "timed out" in low or "no route to host" in low:
        return f"{who} did not answer (timed out) — check the unit is powered and online"
    if "connection refused" in low:
        return f"{who} refused the SSH connection"
    if "host key verification failed" in low or "remote host identification has changed" in low:
        return (f"{who} presented a different host key than ~/.ssh/known_hosts holds — "
                "a re-imaged unit? Remove the old entry and retry")
    last = [line for line in stderr.strip().splitlines() if line.strip()]
    return f"{who}: {last[-1] if last else 'ssh exited without a message'}"


class Session:
    def __init__(self, unit: Unit, *, user: str = "kela", password: str = "") -> None:
        self.unit = unit
        self.user = user
        self._password = password
        self._dir = Path(tempfile.mkdtemp(prefix="gatp-", dir="/tmp"))
        self._askpass = self._dir / "askpass"
        self._askpass_used = self._dir / "askpass.used"
        self._askpass.write_text(_ASKPASS)
        self._askpass.chmod(stat.S_IRWXU)
        self._ctl_srv = str(self._dir / "srv")
        self._ctl_op = str(self._dir / "op")
        self._srv_dest = f"{user}@{unit.server_addr or unit.server_host}"
        self._op_dest = f"{user}@{unit.operator_addr or unit.operator_host}"
        # The master that carries -D and the ProxyCommand to LAN devices.
        self._lan_ctl, self._lan_dest = self._ctl_srv, self._srv_dest
        self._procs: list[subprocess.Popen] = []
        self.server_path: Optional[str] = None
        self.operator_ok = False
        self.operator_error = ""
        self.socks_port = 0
        self.forwards: dict[tuple[str, int], int] = {}
        self.opened = False
        self.info: dict = {}

    @property
    def route(self) -> Optional[str]:
        return self.server_path

    @property
    def has_operator(self) -> bool:
        return self.operator_ok

    def _opts(self) -> list[str]:
        return ["-o", "StrictHostKeyChecking=accept-new",
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                "-o", "ConnectTimeout=15", "-o", "NumberOfPasswordPrompts=1",
                "-o", "PreferredAuthentications=publickey,keyboard-interactive,password",
                "-o", "LogLevel=ERROR"]

    def _env(self) -> dict:
        env = dict(os.environ)
        env.update(SSH_ASKPASS=str(self._askpass), SSH_ASKPASS_REQUIRE="force",
                   DISPLAY=env.get("DISPLAY", ":0"), GOTCHA_ATP_PW=self._password,
                   GOTCHA_ATP_ASKPASS_USED=str(self._askpass_used))
        return env

    # ── lifecycle ────────────────────────────────────────────────────────────

    def open(self) -> dict:
        """Open the server session (direct first, through the operator as the
        backup) and the operator session. Raises AccessError only when the
        server cannot be reached either way."""
        if shutil.which("ssh") is None:
            raise AccessError("OpenSSH client (ssh) is not installed on this laptop")
        self.socks_port = _free_port()
        socks = ["-D", f"127.0.0.1:{self.socks_port}"]
        site = self.unit.server_host

        direct_error = ""
        try:
            self._master(self._ctl_srv, self._srv_dest, f"server {site} (tailnet)",
                         [*socks, "-o", f"HostKeyAlias={site}"])
            self.server_path = SERVER_DIRECT
        except AccessError as e:
            direct_error = str(e)

        if self.unit.operator_peer:
            try:
                extra = ["-o", f"HostKeyAlias={self.unit.operator_host}"]
                if self.server_path is None:
                    extra += socks          # the operator becomes the way onto the LAN
                self._master(self._ctl_op, self._op_dest, self.unit.operator_host, extra)
                self.operator_ok = True
            except AccessError as e:
                self.operator_error = str(e)
        else:
            self.operator_error = f"no {self.unit.operator_host} peer on the tailnet"

        if self.server_path is None:
            if not self.operator_ok:
                raise AccessError(f"{direct_error}; the backup route through the operator "
                                  f"is not available either ({self.operator_error})")
            proxy = f"ssh -S {self._ctl_op} -o ControlMaster=no -W %h:%p {self._op_dest}"
            self._srv_dest = f"{self.user}@{self.unit.server_lan}"
            try:
                self._master(self._ctl_srv, self._srv_dest,
                             f"server {self.unit.server_lan} (through the operator)",
                             ["-o", f"ProxyCommand={proxy}", "-o", f"HostKeyAlias={site}"])
            except AccessError as e:
                raise AccessError(f"{direct_error}; through the operator: {e}")
            self.server_path = SERVER_VIA_OPERATOR
            self._lan_ctl, self._lan_dest = self._ctl_op, self._op_dest

        self.opened = True
        self.info = {
            "server": f"{self.user}@{site}" if self.server_path == SERVER_DIRECT
            else f"{self.user}@{self.unit.server_lan}",
            "server_addr": self._srv_dest.split("@", 1)[1],
            "server_path": self.server_path,
            "server_note": direct_error,
            "operator": f"{self.user}@{self.unit.operator_host}" if self.unit.operator_peer else None,
            "operator_ok": self.operator_ok,
            "operator_error": self.operator_error,
            "socks_port": self.socks_port,
            "rtt_ms": self._rtt_ms(),
            "auth": "password" if self._askpass_used.exists() else "key / Tailscale SSH",
        }
        return self.info

    def _master(self, ctl: str, dest: str, who: str, extra: list[str]) -> None:
        # ControlPersist=no keeps the master in the foreground as our child: a
        # user's `Host * / ControlPersist` would otherwise fork it into the
        # background right after login, and the exit of the process we started
        # would look like a failed login.
        argv = ["ssh", "-M", "-N", "-S", ctl, *self._opts(),
                "-o", "ControlPersist=no", "-o", "ExitOnForwardFailure=yes", *extra]
        if not self._password:
            argv += ["-o", "BatchMode=yes"]
        argv.append(dest)
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True, env=self._env(),
                                start_new_session=True)
        self._procs.append(proc)
        deadline = time.monotonic() + MASTER_TIMEOUT_S
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                if proc.returncode == 0 and _mux(ctl, "check", dest).ok:
                    return
                stderr = proc.stderr.read() if proc.stderr else ""
                if "permission denied" in stderr.lower():
                    if not self._password:
                        raise LoginRefused(
                            f"{who} did not accept this laptop's SSH key, and no kela password "
                            "was given — type it on Home, or set [ssh] password in "
                            "~/.config/gotcha-atp/config.toml")
                    raise LoginRefused(_ssh_error(stderr, who))
                raise AccessError(_ssh_error(stderr, who))
            if _mux(ctl, "check", dest).ok:
                return
            time.sleep(0.4)
        proc.terminate()
        raise AccessError(f"{who} did not finish logging in within {MASTER_TIMEOUT_S}s")

    def _rtt_ms(self) -> Optional[float]:
        samples = []
        for _ in range(3):
            r = self._ssh(self._ctl_srv, self._srv_dest, "true", 10)
            if r.ok:
                samples.append(r.seconds * 1000)
        return round(min(samples), 1) if samples else None

    def disconnect(self) -> None:
        """Drop the SSH masters but keep this object (and its askpass) usable —
        S9 does this before the power cut and `reconnect`s after it."""
        for ctl, dest in ((self._ctl_srv, self._srv_dest), (self._ctl_op, self._op_dest)):
            if Path(ctl).exists():
                _mux(ctl, "exit", dest)
        for p in self._procs:
            if p.poll() is None:
                p.terminate()
        self._procs.clear()
        self.opened = False
        self.server_path, self.operator_ok, self.operator_error = None, False, ""
        self.forwards = {}
        self._srv_dest = f"{self.user}@{self.unit.server_addr or self.unit.server_host}"
        self._lan_ctl, self._lan_dest = self._ctl_srv, self._srv_dest

    def reconnect(self) -> dict:
        """Log in again on the same object, so whoever holds it keeps a live session."""
        self.disconnect()
        return self.open()

    def close(self) -> None:
        self.disconnect()
        shutil.rmtree(self._dir, ignore_errors=True)

    def __enter__(self) -> "Session":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── commands ─────────────────────────────────────────────────────────────

    def _ssh(self, ctl: str, dest: str, script: str, timeout: float,
             input_text: Optional[str] = None) -> ExecResult:
        return run_local(["ssh", "-S", ctl, "-o", "ControlMaster=no", "-o", "BatchMode=yes",
                          dest, "--", script], timeout=timeout, input_text=input_text)

    def _target(self, target: str) -> tuple[str, str]:
        if target == "operator":
            if not self.operator_ok:
                raise OperatorUnavailable(f"operator unreachable — {self.operator_error}")
            return self._ctl_op, self._op_dest
        if target == "server":
            return self._ctl_srv, self._srv_dest
        raise ValueError(f"unknown target {target!r}")

    def exec(self, target: str, script: str, timeout: float = 30,
             input_text: Optional[str] = None) -> ExecResult:
        """`input_text` goes to the remote command's stdin (never its command line)."""
        ctl, dest = self._target(target)
        return self._ssh(ctl, dest, script, timeout, input_text)

    def as_root(self, target: str, command: str, password: str, timeout: float = 60) -> ExecResult:
        """`command` under sudo: passwordless first; when sudo wants a password,
        again with the login password on stdin (`sudo -S -p ''`). The password
        never appears in a command line or in the output."""
        r = self.exec(target, f"sudo -n {command}", timeout)
        if not _sudo_needs_password(r) or not password:
            return r
        return self.exec(target, f"sudo -S -p '' {command}", timeout, input_text=password + "\n")

    def sections(self, target: str, commands: dict[str, str],
                 timeout: float = 60) -> dict[str, ExecResult]:
        ctl, dest = self._target(target)
        return run_sections(lambda s, t: self._ssh(ctl, dest, s, t), commands, timeout)

    def forward(self, host: str, port: int) -> int:
        """`-L 127.0.0.1:<lport>:host:port` on the server session (cluster IPs
        are only reachable from the server). Returns the local port."""
        key = (host, port)
        if key in self.forwards:
            return self.forwards[key]
        lport = _free_port()
        r = _mux(self._ctl_srv, "forward", self._srv_dest, "-L", f"127.0.0.1:{lport}:{host}:{port}")
        if not r.ok:
            raise AccessError(f"could not forward {host}:{port} through the server: "
                              f"{r.err.strip() or r.rc}")
        self.forwards[key] = lport
        return lport

    def proxy_command(self, host: str, port: int = 22) -> str:
        """ProxyCommand reaching host:port on the unit LAN (through the server,
        or the operator on the backup route)."""
        return f"ssh -S {self._lan_ctl} -o ControlMaster=no -W {host}:{port} {self._lan_dest}"

    @property
    def socks_url(self) -> str:
        return f"socks5://127.0.0.1:{self.socks_port}"


def _mux(ctl: str, op: str, dest: str, *extra: str) -> ExecResult:
    """`ssh -O <op>` against one of our control sockets."""
    return run_local(["ssh", "-S", ctl, "-o", "ControlMaster=no", "-O", op, *extra, dest],
                     timeout=10)


def key_auth_works(host: str, user: str = "kela", timeout: float = 12) -> bool:
    """True when publickey alone logs in — Home hides the password field then.
    ControlPath=none: a user's own persisted master must not answer for the key."""
    r = run_local(["ssh", "-o", "ControlPath=none", "-o", "BatchMode=yes",
                   "-o", "PreferredAuthentications=publickey",
                   "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=8",
                   "-o", "LogLevel=ERROR", f"{user}@{host}", "true"], timeout=timeout)
    return r.ok
