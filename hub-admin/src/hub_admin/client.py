"""Connection lifecycle — port-forward management and gRPC channel."""

import atexit
import contextlib
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass

import grpc
from hub_client import HubClient

from hub_admin.auth import BasicAuthInterceptor, resolve_basic_auth
from hub_admin.config import HubConfig


@dataclass
class HubConnection:
    hub_client: HubClient
    channel: grpc.Channel
    context: str
    namespace: str
    _pf_process: subprocess.Popen | None = None

    def close(self):
        self.hub_client.close()
        if self._pf_process:
            self._pf_process.terminate()
            self._pf_process.wait()


def _wait_for_port(host: str, port: int, timeout: float = 10.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _start_port_forward(context: str, namespace: str, port: int) -> subprocess.Popen:
    pf = subprocess.Popen(
        [
            "kubectl", "port-forward",
            "--context", context,
            "-n", namespace,
            "svc/hub-server", f"{port}:{port}",
            "--address", "0.0.0.0",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    def cleanup(*args):
        pf.terminate()
        pf.wait()
        if args:
            sys.exit(0)

    atexit.register(cleanup)
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    if not _wait_for_port("127.0.0.1", port):
        stderr = pf.stderr.read().decode() if pf.stderr else ""
        pf.terminate()
        raise RuntimeError(f"Port-forward failed to start: {stderr or 'timeout'}")

    return pf


@contextlib.contextmanager
def connect(
    config: HubConfig,
    skip_port_forward: bool = False,
    api_token: str | None = None,
    username: str | None = None,
    password: str | None = None,
):
    """Yield a HubConnection. Manages port-forward lifecycle.

    Auth: if `api_token` is given, HubClient exchanges it for a JWT (or uses
    SPIFFE / HUB_API_TOKEN from the env). Otherwise we attach HTTP Basic auth
    at the channel level, because Kela hubs run with HUB_BASIC_AUTH=true and
    accept any non-empty credentials — without an authorization header the
    RPCs simply hang. Basic creds resolve from args -> HUB_BASIC_AUTH_USER/
    HUB_BASIC_AUTH_PASSWORD -> a default identifier.
    """
    pf = None
    if not skip_port_forward:
        pf = _start_port_forward(config.context, config.namespace, config.port)

    target = f"localhost:{config.port}"
    hub_client = HubClient(target, api_token=api_token)
    channel = hub_client._channel
    if api_token is None:
        user, pwd = resolve_basic_auth(username, password)
        channel = grpc.intercept_channel(channel, BasicAuthInterceptor(user, pwd))

    conn = HubConnection(
        hub_client=hub_client,
        channel=channel,
        context=config.context,
        namespace=config.namespace,
        _pf_process=pf,
    )
    try:
        yield conn
    finally:
        conn.close()
