"""HTTP Basic auth for the hub gRPC channel.

Kela hub-servers run with `HUB_REQUIRE_AUTH=true` and `HUB_BASIC_AUTH=true`.
They accept *any* non-empty Basic-auth credentials and record the username as
the caller's identity in audit logs (`enduser.id=basic:<username>`). Without an
`authorization` header, RPCs to such a hub hang.

The `hub_client` library only authenticates via SPIFFE (in-cluster) or by
exchanging a `HUB_API_TOKEN` for a JWT — neither of which applies when running
`hub-admin` from a laptop against a port-forward. So we attach Basic auth at the
channel level instead, mirroring device-fetcher-hub's approach.
"""

import base64
import collections
import os

import grpc

# Easy to spot in hub audit logs; override via env vars if needed.
_DEFAULT_BASIC_USER = "hub-admin"
_DEFAULT_BASIC_PASS = "hub-admin"


def resolve_basic_auth(
    username: str | None, password: str | None
) -> tuple[str, str]:
    """Resolve effective Basic-auth credentials from args -> env -> defaults."""
    user = (
        username
        if username is not None
        else os.environ.get("HUB_BASIC_AUTH_USER") or _DEFAULT_BASIC_USER
    )
    pwd = (
        password
        if password is not None
        else os.environ.get("HUB_BASIC_AUTH_PASSWORD") or _DEFAULT_BASIC_PASS
    )
    return user, pwd


class _ClientCallDetails(
    collections.namedtuple(
        "_ClientCallDetails",
        ("method", "timeout", "metadata", "credentials", "wait_for_ready", "compression"),
    ),
    grpc.ClientCallDetails,
):
    """Concrete ClientCallDetails so interceptors can rebuild it with new metadata."""


class BasicAuthInterceptor(
    grpc.UnaryUnaryClientInterceptor,
    grpc.UnaryStreamClientInterceptor,
    grpc.StreamUnaryClientInterceptor,
    grpc.StreamStreamClientInterceptor,
):
    """Attach an HTTP Basic `authorization` metadata header to every gRPC call."""

    def __init__(self, username: str, password: str):
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._auth_header: tuple[str, str] = ("authorization", f"Basic {token}")

    def _with_auth(self, details: grpc.ClientCallDetails) -> grpc.ClientCallDetails:
        metadata = list(details.metadata or [])
        if not any(k.lower() == "authorization" for k, _ in metadata):
            metadata.append(self._auth_header)
        return _ClientCallDetails(
            method=details.method,
            timeout=details.timeout,
            metadata=metadata,
            credentials=details.credentials,
            wait_for_ready=getattr(details, "wait_for_ready", None),
            compression=getattr(details, "compression", None),
        )

    def intercept_unary_unary(self, continuation, details, request):
        return continuation(self._with_auth(details), request)

    def intercept_unary_stream(self, continuation, details, request):
        return continuation(self._with_auth(details), request)

    def intercept_stream_unary(self, continuation, details, request_iter):
        return continuation(self._with_auth(details), request_iter)

    def intercept_stream_stream(self, continuation, details, request_iter):
        return continuation(self._with_auth(details), request_iter)
