#!/usr/bin/env python3
"""
Which generation of Raythink camera is on the bench, and the client for it.

Raythink ships two generations with incompatible control APIs:

    GEN_RPC2  older  fw `1.000.General 00.0.T, build: 2025-04-09`
                     Dahua-OEM RPC2 JSON      -> raythink_camera.py
    GEN_REST  newer  fw `B1.2.01.01.15, 2026-05-14`
                     REST /v1 with X-Token    -> raythink_rest.py

Both ship on the same address with the same credentials and look identical from
the outside, so something has to choose. The firmware version is the definitive
answer — that is the convention the vendor changed generations on — but it
cannot be the FIRST answer: reading it needs a session, and establishing one is
the very thing that differs. So detection runs in two stages:

  1. `detect_generation` probes the two logins unauthenticated, before any
     credential is used. Cheap, and it is also what tells a camera apart from
     whatever else answers port 80 in the bench range.
  2. Once the pipeline is logged in and has read the identity,
     `generation_from_firmware` checks the version string against the generation
     the probe chose. Agreement is the normal case and silent; a disagreement is
     logged loudly, because it means one of the two conventions has moved and
     the next firmware revision will need this file updated.
"""
from __future__ import annotations

import json
import re
from typing import Optional

import requests

from raythink_base import (
    DEFAULT_SCHEME,
    DEFAULT_USERNAME,
    GEN_REST,
    GEN_RPC2,
    GENERATION_LABELS,
    GENERATIONS,
    BaseRaythinkClient,
    CameraError,
    log,
)

# Long enough for a camera on a direct link to answer, short enough that probing
# a whole /24 of candidates stays inside one detection poll.
PROBE_TIMEOUT = 2.0

# The firmware conventions, as the two generations actually report them:
#   newer:  B1.2.01.01.15, 2026-05-14
#   older:  1.000.General 00.0.T, build: 2025-04-09
# Anchored on the parts that carry the generation rather than on the whole
# string, since the version and build numbers themselves vary per unit.
_FW_REST = re.compile(r"^\s*B\d+(?:\.\d+)+\s*,", re.IGNORECASE)
_FW_RPC2 = re.compile(r"General|build:", re.IGNORECASE)


def generation_from_firmware(firmware: str) -> Optional[str]:
    """The generation a firmware version string belongs to, or None if it
    matches neither convention (a version format nobody has seen yet)."""
    fw = (firmware or "").strip()
    if not fw or fw == "unknown":
        return None
    if _FW_REST.match(fw):
        return GEN_REST
    if _FW_RPC2.search(fw):
        return GEN_RPC2
    return None


def _probe_rest(host: str, scheme: str, timeout: float) -> bool:
    """True when `host` answers the REST API's login endpoint.

    Sent with no credentials at all, so it cannot contribute to the account
    lockout the older cameras enforce: what is being tested is whether the route
    exists and answers in the vendor's envelope. A camera of the older
    generation has no /v1 at all and 404s; anything else on port 80 does not
    produce a `Code` field.
    """
    try:
        r = requests.post(f"{scheme}://{host}/v1/token", timeout=timeout, verify=False)
        return "Code" in r.json()
    except (requests.exceptions.RequestException, ValueError, AttributeError, TypeError):
        return False


def _probe_rpc2(host: str, scheme: str, timeout: float) -> bool:
    """True when `host` answers the RPC2 login challenge.

    The unauthenticated first half of the older client's `login()`: a
    Dahua-family device replies to an empty-password `global.login` with a
    `random` nonce to hash against. Anything else answering port 80 in the bench
    range (a router's web UI, a speaker) does not.
    """
    try:
        r = requests.post(f"{scheme}://{host}/RPC2_Login",
                          data=json.dumps({"method": "global.login",
                                           "params": {"userName": DEFAULT_USERNAME,
                                                      "password": "",
                                                      "clientType": "Web3.0",
                                                      "loginType": "Direct"},
                                           "id": 1, "session": 0}),
                          headers={"Content-Type": "application/json"},
                          timeout=timeout, verify=False)
        return bool((r.json().get("params") or {}).get("random"))
    except (requests.exceptions.RequestException, ValueError, AttributeError):
        return False


def detect_generation(host: str, scheme: str = DEFAULT_SCHEME,
                      timeout: float = PROBE_TIMEOUT) -> Optional[str]:
    """Which generation is answering at `host`, or None if it is not a camera.

    REST is probed first because it is the cheaper negative: an older camera
    404s immediately, whereas a newer camera handed the RPC2 probe would have to
    be waited out. Order is not correctness here — the two probes are mutually
    exclusive, since neither generation implements the other's endpoint — only
    speed.
    """
    if _probe_rest(host, scheme, timeout):
        return GEN_REST
    if _probe_rpc2(host, scheme, timeout):
        return GEN_RPC2
    return None


def open_camera(host: str, *, username: str = DEFAULT_USERNAME,
                scheme: str = DEFAULT_SCHEME, verify: bool = False,
                generation: Optional[str] = None,
                timeout: int = 15) -> BaseRaythinkClient:
    """A client for the camera at `host`, of the right generation.

    `generation` skips the probe when the caller already knows — the bench UI's
    detection loop has just established it, and re-probing would spend a round
    trip re-learning it. An unknown or absent one is probed for here.

    Raises CameraError when nothing at `host` looks like either generation, so a
    caller gets one clear failure instead of a login error against a protocol the
    device never spoke.
    """
    gen = generation if generation in GENERATIONS else detect_generation(host, scheme)
    if gen is None:
        raise CameraError(
            f"Nothing at {host} answered either Raythink API (no REST /v1/token "
            "and no RPC2 login challenge) — is it powered and cabled, and is "
            "this PC on its subnet?")

    # Imported here rather than at module load: each client module imports
    # `raythink_base`, and only this function needs both of them.
    if gen == GEN_REST:
        from raythink_rest import RaythinkRestClient as cls
    else:
        from raythink_camera import RaythinkCameraClient as cls
    return cls(host=host, username=username, scheme=scheme, verify=verify,
               timeout=timeout)


def check_firmware_generation(identity: dict, generation: str) -> None:
    """Cross-check the firmware version against the generation the probe chose.

    Silent when they agree, which is every normal run. When they disagree the
    device is still driven over the API that actually answered — that is a fact,
    not a guess — but the mismatch is logged, because it means either a firmware
    revision has changed the version convention or a camera is running a build
    that speaks one generation's API while reporting the other's version. Both
    need a human to look, and neither is a reason to abort a run that is working.
    """
    fw = (identity or {}).get("firmware", "")
    claimed = generation_from_firmware(fw)
    if claimed is None:
        log.info("Firmware '%s' matches neither version convention — continuing on "
                 "the %s API, which is the one that answered.",
                 fw, GENERATION_LABELS.get(generation, generation))
    elif claimed != generation:
        log.warning("Firmware '%s' looks like a %s camera, but the device answered "
                    "the %s API. Continuing on the API that answered — check "
                    "whether the firmware version convention has changed.",
                    fw, GENERATION_LABELS.get(claimed, claimed),
                    GENERATION_LABELS.get(generation, generation))


__all__ = ["GENERATIONS", "GENERATION_LABELS", "GEN_REST", "GEN_RPC2",
           "PROBE_TIMEOUT", "check_firmware_generation", "detect_generation",
           "generation_from_firmware", "open_camera"]
