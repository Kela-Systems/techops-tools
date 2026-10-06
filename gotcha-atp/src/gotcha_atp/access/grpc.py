"""Hub gRPC through a -L forward on the server hop.

hub-server's ClusterIP is only reachable from the server, so the channel is
`127.0.0.1:<lport>` -> (server session) -> <ClusterIP>:8001. Stubs are generated from
the vendored proto/ into src/gotcha_atp/_pb/ on first use (or by
scripts/gen_protos.py), and regenerated whenever a .proto is newer.
"""
from __future__ import annotations

import importlib
import os
import sys

# The tool forks subprocesses (ssh, ffprobe) while channels are open; gRPC's
# fork handler only prints "FD from fork parent still in poll list" for it.
os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "false")
import threading
from pathlib import Path
from types import ModuleType
from typing import Any, Optional

from .. import PACKAGE_DIR, PROJECT_DIR
from .exec import AccessError

PROTO_DIR = PROJECT_DIR / "proto"
PB_DIR = PACKAGE_DIR / "_pb"

HUB_NAMESPACE = "kela"
HUB_SERVICE = "hub-server"
HUB_GRPC_PORT = 8001
KUBECTL = "sudo -n k3s kubectl"

# Kela hubs run with HUB_REQUIRE_AUTH + HUB_BASIC_AUTH: any non-empty Basic
# credentials are accepted and the username is recorded as the caller in the
# hub's audit log (enduser.id=basic:<user>). Without an authorization header
# every RPC but health is refused. Same approach as hub-admin, own code.
BASIC_USER = "gotcha-atp"


def _basic_auth_channel(channel, user: str):
    import base64
    import collections

    import grpc

    header = ("authorization", "Basic " + base64.b64encode(f"{user}:{user}".encode()).decode())

    class _Details(collections.namedtuple("_Details", ("method", "timeout", "metadata", "credentials",
                                                       "wait_for_ready", "compression")),
                   grpc.ClientCallDetails):
        pass

    class _Auth(grpc.UnaryUnaryClientInterceptor, grpc.UnaryStreamClientInterceptor):
        def _d(self, d):
            md = list(d.metadata or [])
            if not any(k.lower() == "authorization" for k, _ in md):
                md.append(header)
            return _Details(d.method, d.timeout, md, d.credentials,
                            getattr(d, "wait_for_ready", None), getattr(d, "compression", None))

        def intercept_unary_unary(self, cont, d, req):
            return cont(self._d(d), req)

        def intercept_unary_stream(self, cont, d, req):
            return cont(self._d(d), req)

    return grpc.intercept_channel(channel, _Auth())

_gen_lock = threading.Lock()


def _protos() -> list[Path]:
    return sorted(PROTO_DIR.rglob("*.proto"))


def _stale() -> bool:
    stamp = PB_DIR / ".generated"
    if not stamp.exists():
        return True
    built = stamp.stat().st_mtime
    return any(p.stat().st_mtime > built for p in _protos())


def ensure_stubs(force: bool = False) -> Path:
    """Generate the Python stubs if missing or stale; put them on sys.path."""
    with _gen_lock:
        if force or _stale():
            from grpc_tools import protoc  # heavy import, only when generating
            import grpc_tools
            PB_DIR.mkdir(parents=True, exist_ok=True)
            wkt = Path(grpc_tools.__file__).parent / "_proto"
            args = ["grpc_tools.protoc", f"-I{PROTO_DIR}", f"-I{wkt}",
                    f"--python_out={PB_DIR}", f"--grpc_python_out={PB_DIR}",
                    *[str(p.relative_to(PROTO_DIR)) for p in _protos()]]
            if protoc.main(args) != 0:
                raise RuntimeError("protoc failed generating the hub stubs (see stderr)")
            # protoc emits namespace packages; explicit __init__.py files make
            # them importable on every Python without surprises.
            for d in [PB_DIR, *[p for p in PB_DIR.rglob("*") if p.is_dir()]]:
                (d / "__init__.py").touch()
            (PB_DIR / ".generated").touch()
        if str(PB_DIR) not in sys.path:
            sys.path.insert(0, str(PB_DIR))
    return PB_DIR


def pb(module: str) -> ModuleType:
    """Import a generated module, e.g. pb('kela.system.v1alpha1.system_pb2')."""
    ensure_stubs()
    return importlib.import_module(module)


class Hub:
    """Lazy channel to hub-server. Read-only use: health, GetStatus, List*."""

    def __init__(self, session: Any, *, namespace: str = HUB_NAMESPACE,
                 service: str = HUB_SERVICE, port: int = HUB_GRPC_PORT) -> None:
        self.session = session
        self.namespace = namespace
        self.service = service
        self.port = port
        self.cluster_ip: Optional[str] = None
        self._channel = None

    def channel(self):
        if self._channel is not None:
            return self._channel
        import grpc
        r = self.session.exec("server", f"{KUBECTL} get svc -n {self.namespace} "
                              f"{self.service} -o jsonpath='{{.spec.clusterIP}}'", 20)
        ip = r.text
        if not r.ok or not ip:
            raise AccessError(f"cannot resolve {self.service} ClusterIP: "
                              f"{(r.err or r.out).strip() or r.rc}")
        self.cluster_ip = ip
        lport = self.session.forward(ip, self.port)
        self._raw_channel = grpc.insecure_channel(f"127.0.0.1:{lport}")
        self._channel = _basic_auth_channel(self._raw_channel, BASIC_USER)
        return self._channel

    def stub(self, module: str, name: str):
        """A service stub, e.g. hub.stub('kela.system.v1alpha1.system_pb2_grpc', 'SystemServiceStub')."""
        return getattr(pb(module), name)(self.channel())

    def health(self, timeout: float = 10) -> str:
        """grpc.health.v1.Health/Check — 'SERVING', 'NOT_SERVING', ..."""
        health_pb2 = pb("grpc_health.v1.health_pb2")
        health_grpc = pb("grpc_health.v1.health_pb2_grpc")
        stub = health_grpc.HealthStub(self.channel())
        resp = stub.Check(health_pb2.HealthCheckRequest(service=""), timeout=timeout)
        return health_pb2.HealthCheckResponse.ServingStatus.Name(resp.status)

    def close(self) -> None:
        if self._channel is not None:
            self._raw_channel.close()
            self._channel = None
