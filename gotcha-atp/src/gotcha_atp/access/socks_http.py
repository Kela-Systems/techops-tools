"""HTTP to LAN devices through the session's SOCKS forward.

Every device speaks plain HTTP on the unit LAN except where noted; device
certificates are self-signed, so verification is off (the transport that
matters — the tailnet — is SSH).
"""
from __future__ import annotations

import httpx

DEFAULT_TIMEOUT = 12.0


def client(socks_url: str, *, timeout: float = DEFAULT_TIMEOUT) -> httpx.Client:
    """A fresh client (own cookie jar) per device: dashboard sessions are
    cookie-based and must not bleed between devices on the same port."""
    return httpx.Client(proxy=socks_url, timeout=timeout, verify=False,
                        follow_redirects=False)
