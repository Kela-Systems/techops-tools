"""WebSocket sampling through the session's SOCKS forward (S6.3b / S6.3c).

The radar's own sweep stream (ws://<radar>/radar/v1/detections, session cookie
from the dashboard login) and the APU's per-radar proxy
(ws://<apu>/radars/<instance_id>/v1/detections, Basic auth) are sampled for a
fixed window and only *counted* — nothing is ever sent on the socket.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

from websockets.asyncio.client import connect


async def _sample(uri: str, seconds: float, proxy: str,
                  headers: Optional[dict]) -> dict:
    counts: dict = {"frames": 0, "sweeps": 0, "detections": 0, "op_state": 0,
                    "other": 0, "error": None}
    deadline = time.monotonic() + seconds
    try:
        async with connect(uri, proxy=proxy, additional_headers=headers or {},
                           open_timeout=10, max_size=8 * 1024 * 1024) as ws:
            while (left := deadline - time.monotonic()) > 0:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=left)
                except asyncio.TimeoutError:
                    break
                counts["frames"] += 1
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    counts["other"] += 1
                    continue
                op = msg.get("op") if isinstance(msg, dict) else None
                if op == "detections":
                    counts["sweeps"] += 1
                    payload = msg.get("payload")
                    if isinstance(payload, dict):
                        payload = payload.get("detections", [])
                    counts["detections"] += len(payload) if isinstance(payload, list) else 0
                elif op == "op_state":
                    counts["op_state"] += 1
                else:
                    counts["other"] += 1
    except Exception as e:  # noqa: BLE001 — connect/handshake/proxy errors vary
        counts["error"] = f"{type(e).__name__}: {e}"
    return counts


def sample(uri: str, seconds: float, *, proxy: str,
           headers: Optional[dict] = None) -> dict:
    """Count frames on `uri` for `seconds`. Never raises; `error` says why a
    sample came back empty."""
    return asyncio.run(_sample(uri, seconds, proxy, headers))
