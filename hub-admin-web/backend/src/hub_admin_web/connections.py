"""Per-context connection pool — kubectl port-forwards + gRPC channels.

Unlike the CLI's ``hub_admin.client.connect`` (one port-forward per command,
torn down via signal handlers), the web backend keeps one long-lived
port-forward + channel per kubectl context, created lazily on first use and
reaped after an idle timeout. No signal handlers — uvicorn owns those; cleanup
happens in the reaper thread and on app shutdown.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field

import grpc
from hub_client import HubClient

from hub_admin.auth import BasicAuthInterceptor, resolve_basic_auth

logger = logging.getLogger(__name__)

# The hub-server service port inside the cluster (the CLI's default too).
REMOTE_PORT = int(os.environ.get("HUB_REMOTE_PORT", "8001"))
NAMESPACE = os.environ.get("HUB_NAMESPACE", "kela")
IDLE_TIMEOUT_S = float(os.environ.get("HUB_CONN_IDLE_TIMEOUT_S", "900"))
_REAP_INTERVAL_S = 60.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout: float = 10.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


class ConnectionError_(RuntimeError):
    """Port-forward / channel setup failure (mapped to HTTP 502)."""


@dataclass
class ManagedConnection:
    context: str
    namespace: str
    local_port: int
    pf_process: subprocess.Popen
    hub_client: HubClient
    channel: grpc.Channel
    last_used: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_used = time.time()

    def is_alive(self) -> bool:
        return self.pf_process.poll() is None

    def close(self) -> None:
        try:
            self.hub_client.close()
        except Exception:
            pass
        if self.pf_process.poll() is None:
            self.pf_process.terminate()
            try:
                self.pf_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.pf_process.kill()


class ConnectionManager:
    def __init__(self, namespace: str = NAMESPACE):
        self._namespace = namespace
        self._connections: dict[str, ManagedConnection] = {}
        self._lock = threading.Lock()
        self._reaper: threading.Thread | None = None
        self._stop = threading.Event()

    # ── contexts ─────────────────────────────────────────────────────────

    @staticmethod
    def list_contexts() -> list[str]:
        result = subprocess.run(
            ["kubectl", "config", "get-contexts", "-o", "name"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            raise ConnectionError_(
                f"kubectl config get-contexts failed: {result.stderr.strip()}"
            )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    # ── connection lifecycle ─────────────────────────────────────────────

    def get(self, context: str) -> ManagedConnection:
        """Return a live connection for ``context``, creating it if needed."""
        with self._lock:
            conn = self._connections.get(context)
            if conn is not None:
                if conn.is_alive():
                    conn.touch()
                    return conn
                logger.warning("port-forward for %s died, reconnecting", context)
                conn.close()
                del self._connections[context]

            conn = self._connect(context)
            self._connections[context] = conn
            self._ensure_reaper()
            return conn

    def _connect(self, context: str) -> ManagedConnection:
        local_port = _free_port()
        pf = subprocess.Popen(
            [
                "kubectl", "port-forward",
                "--context", context,
                "-n", self._namespace,
                "svc/hub-server", f"{local_port}:{REMOTE_PORT}",
                "--address", "127.0.0.1",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if not _wait_for_port("127.0.0.1", local_port):
            stderr = ""
            pf.terminate()
            try:
                pf.wait(timeout=5)
                stderr = pf.stderr.read().decode() if pf.stderr else ""
            except subprocess.TimeoutExpired:
                pf.kill()
            raise ConnectionError_(
                f"Port-forward to {context} failed: {stderr.strip() or 'timeout'}"
            )

        target = f"localhost:{local_port}"
        api_token = os.environ.get("HUB_API_TOKEN") or None
        hub_client = HubClient(target, api_token=api_token)
        channel = hub_client._channel
        if api_token is None:
            # Kela hubs run with HUB_BASIC_AUTH=true; without an authorization
            # header RPCs hang. See hub_admin.auth for details.
            user, pwd = resolve_basic_auth(None, None)
            channel = grpc.intercept_channel(channel, BasicAuthInterceptor(user, pwd))

        logger.info("connected to %s via localhost:%s", context, local_port)
        return ManagedConnection(
            context=context,
            namespace=self._namespace,
            local_port=local_port,
            pf_process=pf,
            hub_client=hub_client,
            channel=channel,
        )

    def invalidate(self, context: str) -> None:
        """Drop the cached connection (e.g. after a hub-server restart kills
        the pod behind the port-forward)."""
        with self._lock:
            conn = self._connections.pop(context, None)
            if conn is not None:
                conn.close()

    # ── reaping / shutdown ───────────────────────────────────────────────

    def _ensure_reaper(self) -> None:
        if self._reaper is None or not self._reaper.is_alive():
            self._reaper = threading.Thread(
                target=self._reap_loop, name="conn-reaper", daemon=True
            )
            self._reaper.start()

    def _reap_loop(self) -> None:
        while not self._stop.wait(_REAP_INTERVAL_S):
            now = time.time()
            with self._lock:
                for context, conn in list(self._connections.items()):
                    if not conn.is_alive() or now - conn.last_used > IDLE_TIMEOUT_S:
                        logger.info("closing idle connection to %s", context)
                        conn.close()
                        del self._connections[context]

    def close_all(self) -> None:
        self._stop.set()
        with self._lock:
            for conn in self._connections.values():
                conn.close()
            self._connections.clear()


manager = ConnectionManager()
