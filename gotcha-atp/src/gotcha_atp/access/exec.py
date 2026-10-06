"""Command execution: results, batched sections, and device SSH.

Remote reads are batched: one SSH round-trip runs a dict of named commands and
returns each one's output, so a stage over a slow tailnet link costs one RTT
rather than one per command.
"""
from __future__ import annotations

import logging
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Optional

import paramiko

# paramiko logs a full traceback from its transport thread for every device
# that does not answer SSH ("Error reading SSH protocol banner"). The failure
# is already caught and reported on the row; the traceback is only noise.
logging.getLogger("paramiko").setLevel(logging.CRITICAL)


class AccessError(RuntimeError):
    """A hop or a device could not be reached; the message is operator-facing."""


def ssh_failure(host: str, e: BaseException) -> str:
    """A device SSH failure in words, not paramiko's."""
    text = f"{type(e).__name__}: {e}".lower()
    if any(s in text for s in ("protocol banner", "no existing session", "connection closed",
                               "eof", "unable to connect", "timed out", "timeout")):
        return f"{host}: no answer on SSH (port 22)"
    return f"{host}: SSH failed ({type(e).__name__}: {e})"


@dataclass
class ExecResult:
    rc: int
    out: str
    err: str = ""
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.rc == 0

    @property
    def text(self) -> str:
        return self.out.strip()


def run_local(argv: list[str], *, timeout: float = 30, env: Optional[dict] = None,
              input_text: Optional[str] = None) -> ExecResult:
    start = time.monotonic()
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           env=env, input=input_text,
                           stdin=None if input_text is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        return ExecResult(124, _s(e.stdout), f"timed out after {timeout:.0f}s",
                          time.monotonic() - start)
    except FileNotFoundError as e:
        return ExecResult(127, "", str(e), time.monotonic() - start)
    return ExecResult(p.returncode, p.stdout, p.stderr, time.monotonic() - start)


def _s(v) -> str:
    if v is None:
        return ""
    return v.decode(errors="replace") if isinstance(v, bytes) else str(v)


_MARK = "@@GATP:"
_MARK_RE = re.compile(r"^@@GATP:([\w.-]+)@@(?::rc=(\d+))?$", re.M)


def sections_script(commands: dict[str, str]) -> str:
    """One shell script that runs every command and frames its output (and exit
    code) with a marker line, in order."""
    parts = []
    for name, cmd in commands.items():
        # A subshell, so an `exit` (or `cd`) in one command cannot end or
        # affect the rest; the end marker starts with a newline so output
        # without a trailing newline cannot swallow it.
        parts.append(f"echo '{_MARK}{name}@@'; ( {cmd}\n ) 2>&1; __rc=$?; "
                     f"printf '\\n{_MARK}{name}@@:rc=%s\\n' \"$__rc\"")
    return "\n".join(parts)


def parse_sections(text: str) -> dict[str, ExecResult]:
    """Inverse of `sections_script`: name -> ExecResult(rc, output)."""
    out: dict[str, ExecResult] = {}
    starts: dict[str, int] = {}
    for m in _MARK_RE.finditer(text):
        name, rc = m.group(1), m.group(2)
        if rc is None:
            starts[name] = m.end() + 1
        elif name in starts:
            out[name] = ExecResult(int(rc), text[starts[name]:m.start()])
    return out


def run_sections(runner: Callable[[str, float], ExecResult], commands: dict[str, str],
                 timeout: float = 60) -> dict[str, ExecResult]:
    """Run `commands` through `runner(script, timeout)` in one round-trip.
    Commands that produced no frame (the connection dropped mid-way) come back
    as rc 255 carrying the transport error, so a caller never KeyErrors."""
    res = runner(sections_script(commands), timeout)
    got = parse_sections(res.out)
    missing_err = res.err.strip() or f"no output (rc {res.rc})"
    return {name: got.get(name, ExecResult(255, "", missing_err)) for name in commands}


class DeviceSSH:
    """SSH to a LAN device (Teltonika, Planet) tunnelled through the unit session.

    paramiko over a `ProxyCommand` that reuses the server's (or, on the backup route, the operator's) ControlMaster, so there
    is no second tailnet login. Host keys are accepted and not persisted: every
    unit's devices sit on the same LAN addresses, so a known_hosts entry would
    be wrong on the next unit by design.
    """

    def __init__(self, proxy_command: str, host: str, username: str, password: str,
                 *, timeout: float = 20) -> None:
        self.host = host
        self.username = username
        self.timeout = timeout
        self._password = password
        self._proxy_command = proxy_command
        self._client: Optional[paramiko.SSHClient] = None

    def connect(self) -> paramiko.SSHClient:
        if self._client is not None:
            return self._client
        if not self._password:
            raise AccessError(f"no password configured for {self.username}@{self.host}")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(self.host, username=self.username, password=self._password,
                           sock=paramiko.ProxyCommand(self._proxy_command),
                           timeout=self.timeout, banner_timeout=self.timeout,
                           auth_timeout=self.timeout, allow_agent=False,
                           look_for_keys=False)
        except paramiko.AuthenticationException:
            raise AccessError(f"SSH login refused for {self.username}@{self.host}")
        except Exception as e:  # noqa: BLE001 — socket/proxy errors come in many types
            raise AccessError(ssh_failure(self.host, e))
        self._client = client
        return client

    def run(self, command: str, timeout: Optional[float] = None) -> ExecResult:
        start = time.monotonic()
        try:
            client = self.connect()
            _, stdout, stderr = client.exec_command(command, timeout=timeout or self.timeout)
            out = stdout.read().decode(errors="replace")
            err = stderr.read().decode(errors="replace")
            rc = stdout.channel.recv_exit_status()
        except AccessError as e:
            return ExecResult(255, "", str(e), time.monotonic() - start)
        except Exception as e:  # noqa: BLE001
            return ExecResult(255, "", ssh_failure(self.host, e), time.monotonic() - start)
        return ExecResult(rc, out, err, time.monotonic() - start)

    def sections(self, commands: dict[str, str], timeout: float = 40) -> dict[str, ExecResult]:
        return run_sections(lambda s, t: self.run(s, t), commands, timeout)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def quote(s: str) -> str:
    return shlex.quote(s)
