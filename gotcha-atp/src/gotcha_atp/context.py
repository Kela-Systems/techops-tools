"""What a stage gets to work with: the release, the open session, credentials,
facts gathered by earlier stages, and the rows produced so far."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .access.creds import Credentials, Redactor
from .access.exec import DeviceSSH, ExecResult
from .access.session import Session, Unit
from .model import PASS, Row
from .release import Release


@dataclass
class Context:
    unit: Unit
    creds: Credentials
    redact: Redactor
    release: Optional[Release] = None
    release_error: str = ""
    session: Optional[Session] = None
    # Shared facts: build-info, inventory, the operator's LAN address, the
    # baseline counters S6.12 compares against, ...
    facts: dict[str, Any] = field(default_factory=dict)
    results: dict[str, Row] = field(default_factory=dict)
    # Rows from the previous run, so a re-run of S6 can see that S3.5 passed.
    prior: dict[str, Row] = field(default_factory=dict)
    log: Callable[[str], None] = lambda msg: None
    cancel: threading.Event = field(default_factory=threading.Event)
    # Put questions to the engineer in the middle of a stage (S9's power cut):
    # {row id: {"answer", "note"}}. None when the run has no engineer to ask.
    ask: Optional[Callable[[list], dict]] = None
    _device_ssh: dict[str, DeviceSSH] = field(default_factory=dict)

    # ── rows ─────────────────────────────────────────────────────────────────

    def row(self, row_id: str) -> Optional[Row]:
        return self.results.get(row_id) or self.prior.get(row_id)

    def passed(self, row_id: str) -> bool:
        r = self.row(row_id)
        return r is not None and r.state == PASS

    # ── commands ─────────────────────────────────────────────────────────────

    def server(self, script: str, timeout: float = 30) -> ExecResult:
        return self.session.exec("server", script, timeout)

    def operator(self, script: str, timeout: float = 30) -> ExecResult:
        return self.session.exec("operator", script, timeout)

    def server_sections(self, commands: dict[str, str], timeout: float = 60) -> dict[str, ExecResult]:
        return self.session.sections("server", commands, timeout)

    def operator_sections(self, commands: dict[str, str], timeout: float = 60) -> dict[str, ExecResult]:
        return self.session.sections("operator", commands, timeout)

    def device_ssh(self, host: str, family: str = "teltonika") -> DeviceSSH:
        """One cached SSH connection per LAN device, closed at the end of the run."""
        if host not in self._device_ssh:
            login = self.creds.device(family)
            self._device_ssh[host] = DeviceSSH(self.session.proxy_command(host, 22),
                                               host, login.username, login.password)
        return self._device_ssh[host]

    def close_devices(self) -> None:
        for ssh in self._device_ssh.values():
            ssh.close()
        self._device_ssh.clear()
        hub = self.facts.pop("_hub", None)
        if hub is not None:
            hub.close()
