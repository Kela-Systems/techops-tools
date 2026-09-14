#!/usr/bin/env python3
"""Read-only client for the bench-central run-record collector.

Why this exists: an ARP sweep gives a MAC and, through an OUI lookup, a
vendor. It can never give a model. bench-central already holds the model —
every bench tool reads it off the device with `get_identity()` and ships the
run record here, keyed on the MAC. So the model question is answered by a
join against data already collected, with no site visit and no device touched.

    ARP            MAC @ this address, right now
    bench-central  model + firmware for that MAC, as the device reported it
    together       the model of the thing at that address, right now

**Read-only, deliberately.** Only GET is issued, and there is no code here
that can POST. The collector has no auth by design — the tailnet is its
perimeter — so a client that could write would be a client that could corrupt
the audit trail by accident.

Two limits worth knowing before trusting the coverage number:

1. **Central shipping is opt-in per station.** `bench/README.md`: "When the
   variable is unset (the default), nothing is spooled and no uploader runs."
   A station without `BENCH_CENTRAL_URL` has never reported anything, so its
   devices are simply absent.
2. **It only knows devices a bench tool configured.** An operator station or
   a third-party box that never went through the bench will not be there.

A miss is therefore not evidence of anything. It is reported as unknown, and
the site model keeps `assumed` for that claim.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

DEFAULT_TIMEOUT = 10.0

# MAC storage format is NOT consistent in the archive, and assuming it is was
# a real bug here. Confirmed against a live collector (284 runs):
#
#   rutm / otd / tsw   20972749801A         stripped, upper case
#   magos / speaker /  e0:23:3b:d0:01:d4    colon-separated, lower case
#   raythink           both forms appear
#
# `?q=` is a SQL LIKE substring match, so case does not matter (SQLite LIKE is
# ASCII-case-insensitive) but SEPARATORS do: querying the stripped form finds
# none of the colon-separated rows. So every lookup tries both spellings.
_SEP = re.compile(r"[^0-9a-fA-F]")


class CentralError(Exception):
    """The collector could not be reached or answered unusably."""


def canonical_mac(mac: str) -> str:
    """Match bench_core.canonical_mac: zero-pad octets, strip separators."""
    parts = re.split(r"[:-]", mac.strip())
    if len(parts) == 6:
        mac = "".join(p.zfill(2) for p in parts)
    return _SEP.sub("", mac).lower()


def query_spellings(mac: str) -> list[str]:
    """Every way this MAC might be spelled in the archive.

    Returned longest-first so the more specific query runs first. Case is
    ignored by LIKE, so only separators need covering.
    """
    canon = canonical_mac(mac)
    if len(canon) != 12:
        return []
    colons = ":".join(canon[i:i + 2] for i in range(0, 12, 2))
    return [colons, canon]


@dataclass
class DeviceFacts:
    """What bench-central knows about one MAC."""

    mac: str
    model: Optional[str] = None
    firmware: Optional[str] = None
    serial: Optional[str] = None
    tool: Optional[str] = None
    last_run: Optional[str] = None
    site: Optional[str] = None
    hostname: Optional[str] = None
    runs_seen: int = 0

    @property
    def found(self) -> bool:
        return self.runs_seen > 0

    @property
    def has_identity(self) -> bool:
        return bool(self.model)


class Client:
    """GET-only wrapper over the collector's read API."""

    def __init__(self, base_url: str, timeout: float = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # -- transport -----------------------------------------------------

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise CentralError(f"{url} returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise CentralError(
                f"cannot reach {self.base_url}: {exc.reason}. It lives on the "
                f"tailnet - is Tailscale up?"
            ) from exc
        except OSError as exc:
            raise CentralError(f"cannot reach {self.base_url}: {exc}") from exc

        try:
            return json.loads(body)
        except ValueError as exc:
            raise CentralError(f"{url} did not return JSON") from exc

    # -- endpoints -----------------------------------------------------

    def health(self) -> dict:
        """Liveness plus row counts. Use it to tell 'empty' from 'unreachable'."""
        return self._get("/api/v1/health")

    def runs_for_mac(self, mac: str, limit: int = 20) -> list[dict]:
        """Run summaries whose MAC matches, newest first.

        `q` is a substring search across serial/mac/hostname/site/operator/
        model, so results are filtered again here on the MAC itself — a bare
        substring could match another column by coincidence.
        """
        needle = canonical_mac(mac)
        spellings = query_spellings(mac)
        if not spellings:
            raise CentralError(f"'{mac}' is not a 48-bit MAC address")

        # Both spellings, results merged and de-duplicated on run_id. Then
        # filtered on the MAC column itself: `q` is a substring search across
        # serial/mac/hostname/site/operator/model, so it can match another
        # column by coincidence (a run whose SITE contained a MAC was seen).
        seen: dict[str, dict] = {}
        for spelling in spellings:
            payload = self._get("/api/v1/runs", {"q": spelling, "limit": limit})
            for run in payload.get("runs") or []:
                if canonical_mac(str(run.get("mac") or "")) != needle:
                    continue
                key = str(run.get("run_id") or id(run))
                seen.setdefault(key, run)
        return list(seen.values())

    def facts_for_mac(self, mac: str) -> DeviceFacts:
        """Collapse a MAC's run history into the identity it reported.

        Newest first, and the newest non-empty value wins per field: a later
        run may have read a firmware the earlier one could not, and a verify
        run carries less than a configure run.
        """
        needle = canonical_mac(mac)
        runs = self.runs_for_mac(needle)
        facts = DeviceFacts(mac=needle, runs_seen=len(runs))
        if not runs:
            return facts

        def newest(field: str) -> Optional[str]:
            for run in runs:
                value = run.get(field)
                if value:
                    return str(value)
            return None

        facts.model = newest("model")
        facts.firmware = newest("firmware")
        facts.serial = newest("serial")
        facts.tool = newest("tool")
        facts.site = newest("site")
        facts.hostname = newest("hostname")
        facts.last_run = newest("timestamp") or newest("received_at")
        return facts
