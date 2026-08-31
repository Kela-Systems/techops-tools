#!/usr/bin/env python3
"""Shared bench-UI base for the device configurators (OTD500, RUTM08, ...).

Both configurator apps are the same FastAPI shell: a background poll loop that
detects a freshly-plugged device, a worker-thread pipeline run with a live step
log, a rolling-history state served over a WebSocket, and a small set of REST
routes. Everything in this module is device-agnostic; the per-device bits
(detection, the pipeline call, the history-entry shape, the configure route)
are supplied by subclassing `BenchConfigurator` and overriding its hooks.

A run comes in two kinds (TEC-348). A **configure** run mutates the device; a
**verify** run plugs into the same machinery — same detection, same live log,
same run record — and changes nothing, so a finished unit can be re-checked
before it ships. The mode is threaded through `execute_run(..., mode=...)` and
lands in the record's `kind` field; a tool opts in by setting
`verify_supported` and implementing `verify_pipeline`.

This module deliberately does NOT import the Teltonika client — a configurator
hands back its own client and pipeline, so the same base also fits a device on
a completely different protocol (e.g. the Magos REST dashboard).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import logging.handlers
import os
import re
import socket
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from bench_core import LOG_LINE_FORMAT, mac_from_arp_output
from bench_core.central import spool_run_record, start_central_uploader
from bench_core.config_check import check_config, config_fingerprint
from bench_core.device_label import DeviceLabel, parse_device_label
from bench_core.history import resolve_expected
from bench_core.label_printer import make_label_printer
from bench_core.run_record import KIND_CONFIGURE, KIND_VERIFY

try:
    import requests
except ImportError:  # pragma: no cover - requests is a hard dep in practice
    requests = None

POLL_INTERVAL_SEC = 2.0
DETECT_TIMEOUT_SEC = 1.0

# Phases a finished run leaves behind: the device is still plugged in and its
# result is still on screen, so detection must not reset the page to "detected"
# under the operator. Shared so that adding a phase (`verified`, TEC-348) can't
# be forgotten in one of the six poll loops that check for them.
TERMINAL_PHASES = ("configured", "verified", "error")

# Per-run JSON logs embed full raw device payloads, so cap how many we keep on an
# operator machine that may run for months without a restart.
JSON_LOG_RETENTION = 500

# Shared static assets (bench.css / bench.js), served by every tool at /shared.
SHARED_STATIC_DIR = Path(__file__).resolve().parent / "static"

# Secret-bearing config paths redacted before the state is sent to the browser.
_REDACT_PATHS = (("new_password",), ("tailscale", "auth_key"), ("tailscale", "api_key"),
                 ("rms", "auth_code"), ("rms", "api_token"))

# Keys of a pipeline's return value that `_do_configure` interprets itself.
# Everything else a pipeline returns is passed through to `build_entry`
# untouched, so a per-family finding (the TSW202's firmware note, a device's
# final address) reaches the run record without a detour through the config.
PIPELINE_CORE_KEYS = frozenset({"identity", "warnings", "verification", "ok",
                                "failures"})


# ── Logging ───────────────────────────────────────────────────────────────────

def setup_device_logging(log_dir: Path, log_file: str,
                         logger_name: str = "teltonika") -> logging.Logger:
    """Attach a rolling file handler + console handler to the shared device
    logger (once). The device client logs through this same named logger, so its
    step lines land in this app's log file and live UI feed."""
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(isinstance(h, logging.FileHandler) for h in logger.handlers):
        rolling = logging.handlers.RotatingFileHandler(
            log_dir / log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        rolling.setFormatter(logging.Formatter("%(asctime)s " + LOG_LINE_FORMAT))
        logger.addHandler(rolling)
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(LOG_LINE_FORMAT))
        logger.addHandler(console)
    return logger


class StepCollector(logging.Handler):
    """Captures each log record of one run as a timestamped step."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.steps: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.steps.append({
            "time": datetime.fromtimestamp(record.created, timezone.utc).strftime("%H:%M:%S"),
            "level": record.levelname.lower(),
            "sn": getattr(record, "sn", "-"),
            "msg": record.getMessage(),
        })


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", text or "unknown")


def prune_json_logs(log_dir: Path, keep: int = JSON_LOG_RETENTION) -> None:
    """Keep only the newest `keep` per-run JSON files in `log_dir`. Each embeds
    full raw device payloads, so on a machine that runs for months (worst: the
    radar/APU tools in cycle mode) they would otherwise accumulate until the disk
    fills. Best-effort — never let a cleanup failure break a run. Shared by both
    bench bases (BenchConfigurator and MagosBench) since it's the same pattern."""
    with contextlib.suppress(OSError):
        files = sorted(log_dir.glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in files[keep:]:
            with contextlib.suppress(OSError):
                stale.unlink()


def save_run_record(log_dir: Path, entry: dict, *, name_stem: Optional[str],
                    prefix: str = "", extra: Optional[dict] = None,
                    logger: Optional[logging.Logger] = None) -> Optional[str]:
    """Write one run record to its per-run JSON file under `log_dir` and keep
    the folder bounded. The ONE writer shared by both bench bases
    (BenchConfigurator and MagosBench) — every record lands on disk through
    here, so central shipping (TEC-347) can hook a single choke point.

    The filename timestamp comes from the record's own `timestamp` (stamped by
    build_run_entry), so the name and the content always agree; a record
    without one (not produced today) gets stamped with the write time.
    Best-effort: a failed write is logged and the run carries on with
    log_file=None rather than failing."""
    ts = None
    with contextlib.suppress(TypeError, ValueError):
        ts = datetime.fromisoformat(entry.get("timestamp") or "")
    if ts is None:
        ts = datetime.now(timezone.utc)
        entry = {**entry, "timestamp": ts.isoformat()}
    # Queue for central upload (TEC-573). The record only — `extra` (raw device
    # payloads) stays in the local file. Independent of the local write below,
    # and never blocks or fails the run.
    spool_run_record(log_dir, entry)
    name = (f"{prefix}{ts.strftime('%Y%m%d-%H%M%S')}_{slug(name_stem)}_"
            f"{entry.get('status')}.json")
    path = log_dir / name
    try:
        path.write_text(json.dumps({**entry, **(extra or {})},
                                   indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        if logger:
            logger.warning("Could not write log file %s: %s", path, e)
        return None
    prune_json_logs(log_dir)  # keep the folder bounded during long sessions
    return str(path)


def bench_version(base_dir: Path) -> str:
    """Short git revision this bench is running: the BENCH_VERSION exported by the
    launcher, else a direct git lookup, else 'unknown'. Lets an engineer tell 5+
    stations apart when debugging 'works on my bench'."""
    env = os.environ.get("BENCH_VERSION")
    if env:
        return env
    with contextlib.suppress(Exception):
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(base_dir),
                             capture_output=True, text=True, timeout=3)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    return "unknown"


# ── Run provenance (who / where / which code) ────────────────────────────────
#
# Every run record must say who ran it, on which station, and with which bench
# code (TEC-345) — without this, "works on my bench" debugging and QA tracing
# across 5+ stations is guesswork.

# Station-level, not per-tool: the six tools run side by side on one bench PC,
# so the operator name lives in ONE file at the bench root (the tool folders
# are siblings under bench/) and setting it in any tool covers all of them.
OPERATOR_FILENAME = ".bench-operator.json"


def station_id() -> str:
    """Stable identifier of this bench machine: BENCH_STATION_ID if the station
    was given an explicit ID, else the machine's hostname."""
    return os.environ.get("BENCH_STATION_ID") or socket.gethostname() or "unknown"


class OperatorStore:
    """The station-level operator name (entered at day start, badge scan or
    typed) shared by every tool on this bench.

    Backed by one small JSON file so it survives restarts and a name set in one
    tool's page shows up in the other five without restarting them. Reads are
    mtime-cached (the state feed polls once a second per client); writes go
    through an atomic replace so a concurrent reader never sees a torn file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._cached = ""
        self._mtime: Optional[float] = None

    def get(self) -> str:
        """The current operator name, or '' when none is set."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._cached, self._mtime = "", None
            return ""
        if mtime != self._mtime:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._cached = str(data.get("operator", "")).strip()
            except (OSError, ValueError):
                self._cached = ""
            self._mtime = mtime
        return self._cached

    def set(self, name: str) -> str:
        """Persist the operator name (stripped, length-capped). An empty name
        clears it. Returns the stored value."""
        name = (name or "").strip()[:64]
        payload = {"operator": name,
                   "updated": datetime.now(timezone.utc).isoformat()}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, self.path)
        self._cached, self._mtime = name, None  # next get() re-reads/re-caches
        return name


def run_stamp(operator: str, station: str, version: str,
              config_hash: str) -> dict:
    """The provenance fields stamped into every run record by both bench bases
    (BenchConfigurator and MagosBench). `config_hash` pins which station config
    the device was provisioned under (TEC-356), so drift across stations is
    visible once records are centralized."""
    return {"operator": operator or "unknown",
            "station_id": station,
            "bench_version": version,
            "config_hash": config_hash}


class OperatorBody(BaseModel):
    """Request body of the shared POST /api/operator route."""
    operator: str = ""


class VerifyBody(BaseModel):
    """Request body of the shared POST /api/verify route (TEC-348).

    Everything is optional: the point of the mode is that an operator can press
    Verify on a finished unit without knowing anything about it, and the
    expectations are recovered from the unit's configure record.

    `expected` is the escape hatch for the case the lookup can't cover — a unit
    provisioned before records were kept, or one an engineer wants checked
    against what it SHOULD be rather than what it was mistakenly set to. Keys
    are the per-family `device` fields (site_name, hostname, ip, ...); blank
    values are ignored rather than treated as a claim.
    """
    expected: dict = {}


# ── Device-label scanning ────────────────────────────────────────────────────
#
# Scanning the QR on a device's sticker instead of retyping the factory
# password off it (TEC-349). The browser posts the raw scanned string here and
# never parses it, so there is one parser (`bench_core.device_label`), the
# password never enters the page, and the label's MAC can be checked against
# the MAC the tool independently read off the plugged-in device over ARP.
#
# A scan is *armed*, not applied: it sits here until the operator presses
# Configure, and it is dropped as soon as it stops being trustworthy — the
# device changed, a run finished, or it simply got old. Nothing here is
# persisted; a restart loses an armed scan, which is the right outcome.

# How long an armed scan stays usable. Long enough for an operator to be
# interrupted mid-device, short enough that a scan can't be applied to whatever
# is on the bench an hour later.
LABEL_SCAN_TTL_SEC = 600.0


class LabelScanBody(BaseModel):
    """Request body of POST /api/label-scan — the scanner's raw output.

    Deliberately unparsed: whatever the wedge typed, verbatim, including any
    configured prefix character. Note that this field can hold a password, so
    it must never be logged or echoed back.
    """
    raw: str = ""


# ── MAC reading (ARP) ───────────────────────────────────────────────────────
# canonical_mac / mac_from_arp_output (and the whole-cache arp_table +
# find_ip_by_mac) live in bench_core next to the other host-side network
# helpers, so a device client can use them without importing this web shell.

def read_device_mac(ip: str) -> Optional[str]:
    """LAN MAC of `ip` via the ARP table (pinging first to populate it).

    Command syntax differs per OS — Windows ping has no -c (it's -n), its arp
    needs -a, and it prints dash-separated MACs; some Linuxes ship `ip neigh`
    but no `arp` — so each platform gets a primary command plus a fallback."""
    if sys.platform == "win32":
        ping_cmd = ["ping", "-n", "1", "-w", "1000", ip]
        arp_cmds = [["arp", "-a", ip]]
    else:
        ping_cmd = ["ping", "-c", "1", "-t", "1", ip]
        arp_cmds = [["arp", "-n", ip], ["ip", "neigh", "show", ip]]
    with contextlib.suppress(Exception):
        subprocess.run(ping_cmd, capture_output=True, timeout=2)
    for cmd in arp_cmds:
        with contextlib.suppress(Exception):
            out = subprocess.run(cmd, capture_output=True,
                                 text=True, timeout=2).stdout
            mac = mac_from_arp_output(out)
            if mac:
                return mac
    return None


# ── Tailscale per-device key minting ─────────────────────────────────────────

def resolve_tailscale_key(cfg: dict, hostname: str, *, label: str,
                          logger: logging.Logger) -> str:
    """Return an auth key for this device: a freshly minted pre-authorized key
    (if configured) or the static reusable key from config. `label` is the
    device family used in the key description (e.g. 'otd', 'rutm')."""
    ts = cfg.get("tailscale", {}) or {}
    if not ts.get("mint_per_device"):
        return ts.get("auth_key", "")
    if requests is None:
        logger.warning("requests unavailable; falling back to static Tailscale key.")
        return ts.get("auth_key", "")
    try:
        r = requests.post(
            f"https://api.tailscale.com/api/v2/tailnet/{ts['tailnet']}/keys",
            auth=(ts["api_key"], ""),
            json={"capabilities": {"devices": {"create": {
                "reusable": False, "ephemeral": False, "preauthorized": True,
                "tags": ts.get("tags", []),
            }}}, "description": f"{label} {hostname}"},
            timeout=15,
        )
        r.raise_for_status()
        logger.info("Minted a fresh Tailscale key for %s.", hostname)
        return r.json().get("key", "")
    except Exception as e:  # noqa: BLE001
        logger.error("Tailscale key minting failed (%s); using static key.", e)
        return ts.get("auth_key", "")


def redact_config(cfg: dict) -> dict:
    """Deep-copy `cfg` with secret-bearing fields masked, for sending to the UI."""
    safe = json.loads(json.dumps(cfg))
    for path in _REDACT_PATHS:
        node = safe
        for key in path[:-1]:
            node = node.get(key, {}) if isinstance(node, dict) else {}
        if isinstance(node, dict) and path[-1] in node and node[path[-1]]:
            node[path[-1]] = "••••••"
    return safe


# ── Static assets ────────────────────────────────────────────────────────────

class _RevalidatedStatic(StaticFiles):
    """Static files a browser must not reuse without asking us first.

    Stations pull new bench.js/bench.css from bench-central while operator tabs
    stay open for days, and StaticFiles sends no Cache-Control at all — which
    licenses a browser to reuse a cached copy on its own heuristic, without
    revalidating. A tab then runs the JS from *before* an update against a
    server from after it. That surfaced as TEC-349's scan capture being dead on
    a station whose /api/state advertised the feature as enabled: the page had
    no code to act on it, so scans landed in whatever field had focus and the
    scanner's Enter submitted the form. A hard refresh "fixed" it, which is not
    something an operator will think to try.

    `no-cache` still caches — it only forces revalidation — so the usual answer
    is a 304 with no body, over loopback.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


# ── Event-loop noise suppression (Windows) ───────────────────────────────────

def install_loop_exception_handler() -> None:
    """Silence the spurious 'forcibly closed' tracebacks Windows logs when a
    browser drops a connection mid-flight. Shared by both bench bases
    (BenchConfigurator and MagosBench) — call it from the app's lifespan, once
    the event loop is running.

    Each page polls /api/state and holds a 1s-cadence WebSocket, and the
    launcher dashboard fires an aborting no-cors fetch at every port every
    few seconds — so tabs closing, refreshes, and reconnects routinely reset
    connections the server hasn't finished writing to. On Windows asyncio's
    ProactorEventLoop reports that as `ConnectionResetError: [WinError 10054]`
    straight to the loop's default exception handler, dumping a noisy (but
    harmless) traceback. We drop just that case and defer everything else to
    the default handler so real errors are untouched."""
    loop = asyncio.get_running_loop()
    default_handler = loop.get_exception_handler()

    def handler(lp, context):
        exc = context.get("exception")
        # WinError 10054 surfaces as ConnectionResetError; also catch the
        # broader ConnectionError family (aborted/closed) from a dropped peer.
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
            return
        if default_handler is not None:
            default_handler(lp, context)
        else:
            lp.default_exception_handler(context)

    loop.set_exception_handler(handler)


# ── The configurator base ────────────────────────────────────────────────────

class BenchConfigurator:
    """Base for a single-device bench configurator. Subclass it, set the class
    attributes, and override the hooks marked below."""

    # --- identity / wiring (override as class attributes) ---
    title: str = "Device Configurator"
    port: int = 8000
    html_file: str = "index.html"        # filename under static/
    config_filename: str = "config.json"
    log_filename: str = "config.log"
    logger_name: str = "teltonika"
    tailscale_label: str = "device"      # used in the Tailscale key description
    history_limit: int = 20
    # Opt in to reading the device's factory password off its QR label
    # (TEC-349). Off by default: only devices whose sticker carries a parseable
    # label — the Teltonika families — have anything to gain, and a tool that
    # doesn't opt in gets no route and no UI for it.
    label_scan_enabled: bool = False
    # Opt in to the mutation-free Verify pass (TEC-348). Off by default so a
    # tool that hasn't implemented `verify_pipeline` gets no route and no
    # button, rather than a button that errors — and so the tool it IS wired up
    # on is a deliberate choice per family.
    verify_supported: bool = False
    # The tool name used to look a device's configure record up (must match the
    # `tool` its build_entry passes to build_run_entry). Set per subclass.
    record_tool: str = ""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.config_path = base_dir / self.config_filename
        self.log_dir = base_dir / "logs"
        self.log_dir.mkdir(exist_ok=True)
        self.logger = setup_device_logging(self.log_dir, self.log_filename, self.logger_name)
        self._prune_logs()  # trim any backlog left by earlier sessions
        # Ships queued run records to the collector when the station has
        # BENCH_CENTRAL_URL set (TEC-573); None (fully off) otherwise.
        self.central_uploader = start_central_uploader(self.log_dir, self.logger)
        self.bench_version = bench_version(base_dir)
        self.station_id = station_id()
        self.operator_store = OperatorStore(base_dir.parent / OPERATOR_FILENAME)
        # Prints the QA label on a verified-OK run (TEC-352). Station-level,
        # like the operator name: one printer serves all seven tools.
        self.label_printer = make_label_printer(base_dir.parent, self.logger)
        self.logger.info("%s starting — bench version %s, station %s",
                         self.title, self.bench_version, self.station_id)
        self.cfg: dict = self.load_config()
        self.config_hash: str = ""
        self.config_warnings: list[str] = []
        self._check_config()
        self.state: dict = self.initial_state()
        self.state["config_loaded"] = bool(self.cfg)
        self._live_collector: Optional[StepCollector] = None
        self._run_t0: Optional[float] = None
        # Which kind of run is in flight, or was the last one to finish. Drives
        # the page's wording (a verify pass must not report "Configured") and
        # the record's `kind` field.
        self._run_kind: str = KIND_CONFIGURE
        # The armed device-label scan (TEC-349), and why the last scan was
        # refused if it was. In memory only — see LABEL_SCAN_TTL_SEC.
        self._armed_label: Optional[DeviceLabel] = None
        self._armed_at: float = 0.0
        self._label_scan_problem: Optional[str] = None

    # ── config + state (override `initial_state`; usually keep the rest) ──────

    def load_config(self) -> dict:
        if self.config_path.exists():
            with open(self.config_path, "r", encoding="utf-8") as f:
                return {k: v for k, v in json.load(f).items() if not k.startswith("_")}
        self.logger.warning("%s not found — copy the example.", self.config_path.name)
        return {}

    def example_config_path(self) -> Optional[Path]:
        """The tool's committed `*.config.example.json` — the reference for
        which fields a live config must carry. None when the tool has none."""
        example = Path(str(self.config_path).replace(".config.json",
                                                     ".config.example.json"))
        return example if example != self.config_path and example.exists() else None

    def _check_config(self) -> None:
        """Startup config self-check (TEC-356): fingerprint the redacted config
        and flag placeholder values, expired tokens and fields missing vs the
        example — so a mis-configured station announces itself at launch
        instead of failing (or silently falling back) mid-run."""
        self.config_hash = config_fingerprint(redact_config(self.cfg))
        if not self.config_path.exists():
            self.config_warnings = [
                f"{self.config_filename} not found — copy the example config "
                "and fill in this station's values."]
        else:
            example = None
            example_path = self.example_config_path()
            if example_path:
                with contextlib.suppress(OSError, ValueError):
                    example = json.loads(example_path.read_text(encoding="utf-8"))
            self.config_warnings = check_config(self.cfg, example)
        for warning in self.config_warnings:
            self.logger.warning("Config check: %s", warning)

    def initial_state(self) -> dict:  # override
        raise NotImplementedError

    def counts(self) -> dict:  # override (manifest progress vs history tally)
        """The session tally. Configure and verify runs are counted separately:
        an end-of-batch QA sweep re-checks every unit already in the done pile,
        and folding those into `done` would report twice as many devices
        provisioned as the bench actually saw."""
        configures = [h for h in self.state["history"]
                      if h.get("kind", KIND_CONFIGURE) == KIND_CONFIGURE]
        verifies = [h for h in self.state["history"]
                    if h.get("kind") == KIND_VERIFY]
        done = sum(1 for h in configures if h["status"] == "ok")
        passed = sum(1 for h in verifies if h["status"] == "ok")
        return {"done": done, "error": len(configures) - done,
                "verified": passed, "verify_failed": len(verifies) - passed}

    def extra_public_state(self) -> dict:  # override to add manifest / name_prefix / etc.
        return {}

    def reload(self) -> str:
        """Re-read config from disk. Override to also reload a manifest. Returns
        the status message to show in the UI."""
        self.cfg = self.load_config()
        self._check_config()
        self.state["config_loaded"] = bool(self.cfg)
        return ("Config reloaded." if self.cfg
                else f"No {self.config_filename} found — copy the example.")

    def public_state(self) -> dict:
        busy = self.state["busy"]
        return {**self.state, "counts": self.counts(),
                "bench_version": self.bench_version,
                "station_id": self.station_id,
                "operator": self.operator_store.get(),
                "config": redact_config(self.cfg),
                "config_hash": self.config_hash,
                "config_warnings": self.config_warnings,
                "live_steps": list(self._live_collector.steps)[-200:]
                              if busy and self._live_collector else [],
                "run_seconds": int(time.monotonic() - self._run_t0)
                               if busy and self._run_t0 else None,
                "label_scan": self.label_scan_state(),
                "printer": self.label_printer.status(),
                # Which kind of run is in flight / was last shown, so the page
                # can label a verify pass as one instead of saying "Configured".
                "verify_supported": self.verify_supported,
                "run_kind": self._run_kind,
                **self.extra_public_state()}

    # ── device-label scanning (TEC-349) ──────────────────────────────────────

    def arm_label(self, raw: str) -> dict:
        """Take one raw scan and hold its password for the next Configure.

        Returns `{}` when armed, or `{"error": ...}` when the scan is refused —
        and refusing is the interesting half. The label carries the device's LAN
        MAC, and the tool already knows the MAC of the device on the bench from
        ARP, so scanning the wrong unit is detectable here rather than surfacing
        later as an inexplicable login failure against the right device.
        """
        if not self.label_scan_enabled:
            return {"error": "This tool does not read device labels."}

        label = parse_device_label(raw)
        if label is None:
            # Deliberately says nothing about the content: an unparsed scan can
            # still be a password, and this string reaches the browser.
            size = len(raw or "")
            return self._refuse_label(
                f"That scan ({size} character{'' if size == 1 else 's'}) is not "
                "a device label. Scan the QR code on the device's sticker.")

        active = self.state.get("active_mac")
        if label.matches_mac(active) is False:
            return self._refuse_label(
                f"The scanned label belongs to MAC {label.mac}, but the device "
                f"plugged in is {active}. Scan the label on the device that is "
                "actually connected.")
        if not label.password:
            return self._refuse_label(
                f"The scanned label (SN {label.serial or 'unknown'}) carries no "
                "password. To use the shared password instead, leave the "
                "password field empty and press Configure.")

        self._armed_label = label
        self._armed_at = time.monotonic()
        self._label_scan_problem = None
        # Everything here is off the label except the password itself.
        self.logger.info(
            "Label scanned: SN %s, MAC %s, batch %s%s.",
            label.serial or "unknown", label.mac or "unknown",
            label.batch or "unknown",
            "" if active else " — the device's own MAC could not be read, so "
                              "the label was not cross-checked")
        return {}

    def _refuse_label(self, message: str) -> dict:
        """Reject a scan, and drop any previously armed one with it: the
        operator's most recent scan is their intent, so leaving an older one
        armed would apply a password they no longer expect."""
        self._armed_label = None
        self._label_scan_problem = (message, self.state.get("active_mac"))
        self.logger.warning("Label scan refused: %s", message)
        return {"error": message}

    def armed_label(self) -> Optional[DeviceLabel]:
        """The armed scan, if it is still trustworthy. Drops it otherwise."""
        label = self._armed_label
        if label is None:
            return None
        if time.monotonic() - self._armed_at > LABEL_SCAN_TTL_SEC:
            self.clear_armed_label("it expired")
            return None
        # Re-checked on every read, not just at arm time: the MAC may have been
        # unreadable when the label was scanned and resolved since, and the
        # device on the bench can change under a page that is still open.
        if label.matches_mac(self.state.get("active_mac")) is False:
            self.clear_armed_label("a different device is plugged in")
            return None
        return label

    def clear_armed_label(self, reason: Optional[str] = None) -> None:
        if self._armed_label is not None and reason:
            self.logger.info("Discarded the scanned label — %s.", reason)
        self._armed_label = None
        self._label_scan_problem = None

    def label_scan_state(self) -> dict:
        """The scan half of /api/state. Carries the label's identity fields and
        whether it matches the plugged-in device — never the password."""
        if not self.label_scan_enabled:
            return {"enabled": False}
        label = self.armed_label()
        armed = None
        if label is not None:
            armed = {**label.redacted(),
                     "matches_active": label.matches_mac(
                         self.state.get("active_mac"))}
        problem = None
        if self._label_scan_problem is not None:
            message, at_mac = self._label_scan_problem
            # A refusal is about one device. Once something else is on the
            # bench the warning is stale, so it retires itself.
            if at_mac == self.state.get("active_mac"):
                problem = message
            else:
                self._label_scan_problem = None
        return {"enabled": True, "armed": armed, "problem": problem}

    def resolve_label_password(self, typed: str) -> tuple[str, str]:
        """The password to log in with, and where it came from.

        A typed value always wins, so the operator can override a scan (or work
        a device whose sticker is unreadable) without turning anything off. The
        empty result is not a failure: both pipelines read it as "try the shared
        password", which is how an already-provisioned device is re-run.
        """
        typed = (typed or "").strip()
        if typed:
            return typed, "typed"
        label = self.armed_label()
        if label is not None and label.password:
            return label.password, "scan"
        return "", "shared-fallback"

    def password_source(self, inputs: dict, password_key: str) -> str:
        """Where a run's login password came from, for the run record:
        `"scan"`, `"typed"` or `"shared-fallback"`.

        `/api/configure` decides this and passes it through `inputs`. The
        fallback covers a caller that drives `execute_run` directly (the tests
        do) and infers the only two possibilities left, so a record is never
        stamped with a provenance that isn't true.
        """
        return inputs.get("password_source") or (
            "typed" if inputs.get(password_key) else "shared-fallback")

    # ── the pipeline run (shared machinery; override the small hooks) ─────────

    def hostname_for(self, inputs: dict) -> str:  # override
        raise NotImplementedError

    def client_host(self, inputs: dict) -> str:  # override
        return self.cfg.get("host", "")

    def build_client(self, run_cfg: dict, host: str):  # override
        raise NotImplementedError

    def run_pipeline(self, client, run_cfg: dict, inputs: dict) -> dict:  # override
        raise NotImplementedError

    def verify_pipeline(self, client, run_cfg: dict, inputs: dict) -> dict:  # override
        """Check a finished device against its intended state, mutating NOTHING
        (TEC-348). Same return contract as `run_pipeline`.

        A verify pipeline is handed `self.verify_resolver(inputs)` and calls it
        once, right after it has read the device's identity — see there for why
        the timing matters.
        """
        raise NotImplementedError

    def verify_resolver(self, inputs: dict):
        """A callable `(identity) -> (expected, prior_run_row)` for a verify
        pipeline to invoke once it knows what device it is talking to.

        A callable rather than pre-computed values because the lookup key is the
        serial, and the serial comes off the device — so it cannot be resolved
        before the pipeline logs in. The MAC read over ARP during detection is
        the fallback for a unit whose serial won't read.

        `prior_run_row` is a FAILING row when no configure record exists
        anywhere. A pipeline must include it, not drop it: without it a device
        nobody ever provisioned verifies green, and TEC-352 prints it a label.
        """
        def resolve(identity: dict) -> tuple[dict, Optional[dict]]:
            expected, prior_row, source = resolve_expected(
                self.log_dir,
                serial=(identity or {}).get("serial", "") or "",
                mac=(identity or {}).get("mac", "") or inputs.get("mac", "") or "",
                tool=self.record_tool,
                overrides=inputs.get("expected_overrides") or {})
            inputs["expected"] = expected
            inputs["expected_source"] = source
            return expected, prior_row

        return resolve

    def build_entry(self, result: dict, inputs: dict, duration: int) -> dict:  # override
        """Build the run-record entry via bench_core.run_record.build_run_entry
        (the canonical schema, TEC-346) — per-family fields go in `device`.

        `kind` is stamped by `execute_run` afterwards, so a hook that has no
        idea the verify mode exists still produces a correctly labelled record.
        """
        raise NotImplementedError

    def on_run_recorded(self, result: dict, inputs: dict, entry: dict) -> None:
        """Hook after an entry is appended to history (e.g. mark a manifest row)."""

    def success_message(self, result: dict, entry: dict, took: str) -> str:
        return (f"Configured {result['hostname']} (SN {entry['serial']}) in {took}. "
                "Unplug it and plug in the next one.")

    def verify_message(self, result: dict, entry: dict, took: str) -> str:
        """What the page says after a verify pass. Deliberately does NOT say
        "configured" — the operator has to be able to tell a QA check from a
        provision at a glance, since only one of them changed the device."""
        name = result.get("hostname") or entry["serial"]
        return (f"{name} PASSED verification in {took} — nothing was changed. "
                "Unplug it and plug in the next one.")

    def _do_configure(self, inputs: dict) -> dict:
        """Run one device's configure pipeline. A named seam, not a shim: the
        per-tool tests stub this out to drive `execute_run` without a device."""
        return self._do_run(inputs, KIND_CONFIGURE)

    def _do_verify(self, inputs: dict) -> dict:
        """Run one device's verify pass. The `_do_configure` seam's twin."""
        return self._do_run(inputs, KIND_VERIFY)

    def _do_run(self, inputs: dict, mode: str = KIND_CONFIGURE) -> dict:
        """Run one device's pipeline in a worker thread, collecting its log.

        `mode` picks the pipeline: `run_pipeline` (mutates) or `verify_pipeline`
        (mutates nothing). Everything else — the run config, the live log, the
        result contract, the failure handling — is identical, which is the
        point: a verify pass is a first-class run, not a special case bolted on
        beside one.
        """
        verifying = mode == KIND_VERIFY
        collector = StepCollector()
        self._live_collector = collector
        self.logger.addHandler(collector)
        from bench_core import set_log_serial
        set_log_serial(None)

        hostname = self.hostname_for(inputs)
        run_cfg = json.loads(json.dumps(self.cfg))
        fw = run_cfg.get("firmware", {}) or {}
        if fw.get("bin_path"):
            # Resolve relative to the app folder, not the process CWD.
            fw["bin_path"] = str(self.base_dir / fw["bin_path"])
            run_cfg["firmware"] = fw
        ts = run_cfg.get("tailscale", {}) or {}
        # Minting a Tailscale key is a mutation of the tailnet, not of the
        # device, but it is still a side effect and a verify pass has no use for
        # one: it reads the node's existing address off the device.
        if ts.get("enabled") and not verifying:
            ts["_resolved_auth_key"] = resolve_tailscale_key(
                self.cfg, hostname, label=self.tailscale_label, logger=self.logger)
            run_cfg["tailscale"] = ts

        identity, warnings, error, ok, verification = {}, [], None, False, []
        extras: dict = {}
        client = self.build_client(run_cfg, self.client_host(inputs))
        try:
            result = (self.verify_pipeline(client, run_cfg, inputs) if verifying
                      else self.run_pipeline(client, run_cfg, inputs))
            identity = result["identity"]
            warnings = result.get("warnings", [])
            verification = result.get("verification", [])
            ok = result.get("ok", False)   # absent "ok" must read as failure, not success
            if not ok:
                problems = list(result.get("failures", []))
                problems += [f"verify:{c['item']}" for c in verification if c["ok"] is False]
                error = "; ".join(problems)
            # Whatever else the pipeline discovered rides along to build_entry,
            # so a per-family finding doesn't have to be reconstructed from the
            # config (which cannot know it) or dug out of the step log. The
            # keys below always win, so a pipeline cannot overwrite the core.
            extras = {k: v for k, v in result.items() if k not in PIPELINE_CORE_KEYS}
        except BaseException as e:  # noqa: BLE001 — SystemExit + any network error
            error = str(e)
            self.logger.error("%s FAILED: %s",
                              "Verification" if verifying else "Provisioning", e)
        finally:
            self.logger.removeHandler(collector)
            client.close()

        steps = collector.steps
        return {
            **extras,
            "ok": ok, "hostname": hostname, "identity": identity, "warnings": warnings,
            "error": error, "steps": steps, "verification": verification,
            "log": "\n".join(f"[{s['level']}] [{s['sn']}] {s['msg']}" for s in steps),
        }

    def _prune_logs(self, keep: int = JSON_LOG_RETENTION) -> None:
        prune_json_logs(self.log_dir, keep)

    def run_stamp(self) -> dict:
        """Provenance fields (operator / station_id / bench_version /
        config_hash) added to every history entry and per-run JSON."""
        return run_stamp(self.operator_store.get(), self.station_id,
                         self.bench_version, self.config_hash)

    def log_name_stem(self, entry: dict) -> Optional[str]:  # override
        """What to name this run's JSON file after. The hostname by default;
        override for a family that doesn't set one (see the TSW202, which uses
        the serial)."""
        return entry.get("device", {}).get("hostname")

    def _save_log(self, entry: dict) -> Optional[str]:
        # Verify records get a `verify_` filename prefix so the two kinds of run
        # are tellable apart in logs/ without opening anything — an operator
        # asked to send "the log for that unit" picks the right file. Kept here
        # rather than in the naming hook so a tool can't opt out of it by
        # overriding the name.
        prefix = "verify_" if entry.get("kind") == KIND_VERIFY else ""
        return save_run_record(self.log_dir, entry, prefix=prefix,
                               name_stem=self.log_name_stem(entry),
                               logger=self.logger)

    async def execute_run(self, inputs: dict, label: str, *,
                          mode: str = KIND_CONFIGURE) -> bool:
        """Run one device's pipeline. Returns False if a run is already in
        progress. The busy check-and-set is atomic (no await between them), so
        the poll loop, /api/configure and /api/verify can never start two runs
        at once — which is also what stops a Verify press landing in the middle
        of a provision.

        `mode` is `"configure"` or `"verify"` (TEC-348) and is stamped onto the
        record here, so none of the per-tool `build_entry` hooks has to know the
        verify mode exists.
        """
        if self.state["busy"]:
            return False
        verifying = mode == KIND_VERIFY
        self.state["busy"] = True
        self._run_kind = mode
        self.state["phase"] = "verifying" if verifying else "configuring"
        self.state["message"] = (f"Verifying {label}…" if verifying
                                 else f"Configuring {label}…")
        self._run_t0 = time.monotonic()
        try:
            loop = asyncio.get_event_loop()
            runner = self._do_verify if verifying else self._do_configure
            result = await loop.run_in_executor(None, runner, inputs)
            duration = int(time.monotonic() - self._run_t0)
            took = f"{duration // 60}m{duration % 60:02d}s"
            entry = self.build_entry(result, inputs, duration)
            entry["kind"] = mode
            entry.update(self.run_stamp())  # who / where / which code (TEC-345)
            # Print the QA label BEFORE the record is written: _save_log both
            # writes the per-run JSON and spools it to central, so a `label`
            # block attached afterwards would be missing from the audit trail
            # on both. Kept here rather than behind `on_run_recorded` so a
            # subclass cannot opt out of the gate by overriding a hook — same
            # reasoning as `_save_log`'s own filename prefix (TEC-352).
            # In the executor because both transports block: a dead printer
            # would otherwise hold the event loop, and with it the 1 Hz state
            # feed, for the connect timeout. It mutates `entry` in place.
            await loop.run_in_executor(None, self.label_printer.print_run, entry)
            entry["log_file"] = self._save_log(entry)
            self.state["history"].insert(0, entry)
            del self.state["history"][self.history_limit:]
            self.state["last_result"] = entry
            self.on_run_recorded(result, inputs, entry)
            if result["ok"]:
                self.state["phase"] = "verified" if verifying else "configured"
                self.state["message"] = (self.verify_message(result, entry, took)
                                         if verifying
                                         else self.success_message(result, entry, took))
            else:
                self.state["phase"] = "error"
                self.state["message"] = (
                    f"{'FAILED verification' if verifying else 'Failed'} after "
                    f"{took}: {result['error']}")
        finally:
            self.state["busy"] = False
            self._run_t0 = None
            # One scan, one device. Holding it past the run would let a retry
            # (or the next unit, if the MAC can't be read) reuse it silently.
            self.clear_armed_label()
        return True

    async def execute_verify(self, inputs: dict, label: str) -> bool:
        """Run one device's mutation-free verify pass (TEC-348)."""
        return await self.execute_run(inputs, label, mode=KIND_VERIFY)

    # ── detection loop (override `poll_once`) ─────────────────────────────────

    async def poll_once(self, loop) -> None:  # override
        raise NotImplementedError

    async def _poll_loop(self) -> None:
        loop = asyncio.get_event_loop()
        while True:
            # An unhandled exception here would silently kill detection for good
            # (the UI keeps serving its last state), so recover and keep polling.
            try:
                await self.poll_once(loop)
            except Exception:
                self.logger.exception("Detection loop error — recovering on next poll.")
                self.state["message"] = (f"Internal error in the detection loop "
                                         f"(see logs/{self.log_filename}).")
            await asyncio.sleep(POLL_INTERVAL_SEC)

    # ── routes (override `register_routes` for /api/configure etc.) ───────────

    def register_routes(self, app: FastAPI) -> None:  # override
        """Register device-specific routes (e.g. /api/configure, /api/auto)."""

    def dismiss_message(self) -> str:
        return "Plug in the next device…"

    # ── the verify route (shared, TEC-348) ───────────────────────────────────

    def verify_inputs(self, body: "VerifyBody") -> dict:
        """The `inputs` dict for a verify run. Override to add per-tool fields.

        Deliberately shared rather than per-tool: a Verify press means the same
        thing on every tool (check the unit that is plugged in, against what it
        was configured to be) and duplicating that across five
        `register_routes` is how the five drift apart. There is no password
        field — a finished unit is on the station's shared password, and one
        that isn't will fail its password row, which is the correct outcome.
        """
        return {"host": self.state.get("active_host") or self.cfg.get("host", ""),
                "mac": self.state.get("active_mac"),
                "expected_overrides": dict(body.expected or {}),
                "password_source": "shared-fallback"}

    def verify_label(self, inputs: dict) -> str:
        """How the run is described in the "Verifying …" message. The device's
        real identity isn't known until the pipeline logs in, so this is the
        address it answered on."""
        return inputs.get("host") or "the connected device"

    def build_app(self) -> FastAPI:
        @contextlib.asynccontextmanager
        async def lifespan(_app: FastAPI):
            install_loop_exception_handler()
            poller = asyncio.create_task(self._poll_loop())
            try:
                yield
            finally:
                poller.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await poller

        app = FastAPI(title=self.title, lifespan=lifespan)
        app.mount("/static", _RevalidatedStatic(directory=str(self.base_dir / "static")),
                  name="static")
        # Shared CSS/JS live in this package so every tool serves one copy.
        app.mount("/shared", _RevalidatedStatic(directory=str(SHARED_STATIC_DIR)),
                  name="shared")

        @app.get("/")
        async def index():
            return FileResponse(str(self.base_dir / "static" / self.html_file))

        @app.get("/api/state")
        async def get_state():
            return self.public_state()

        @app.post("/api/reload")
        async def reload_route():
            self.state["message"] = self.reload()
            return self.public_state()

        @app.post("/api/dismiss")
        async def dismiss():
            self.state["last_result"] = None
            self.state["phase"] = "waiting"
            self.state["message"] = self.dismiss_message()
            self.clear_armed_label()
            return self.public_state()

        @app.post("/api/operator")
        async def set_operator(body: OperatorBody):
            name = self.operator_store.set(body.operator)
            self.logger.info("Operator set to '%s'.", name or "(cleared)")
            return self.public_state()

        if self.label_scan_enabled:
            @app.post("/api/label-scan")
            async def label_scan(body: LabelScanBody):
                refusal = self.arm_label(body.raw)
                if refusal:
                    return refusal
                return self.public_state()

        if self.verify_supported:
            @app.post("/api/verify")
            async def verify(body: VerifyBody):
                # Same two guards as every /api/configure: a device has to
                # actually be there, and one run at a time (execute_run's
                # check-and-set is what makes the second one airtight).
                if not self.state.get("detected"):
                    return {"error": "No device is currently detected."}
                inputs = self.verify_inputs(body)
                if not await self.execute_verify(inputs,
                                                 self.verify_label(inputs)):
                    return {"error": "A run is already in progress."}
                return self.public_state()

        @app.websocket("/ws/state")
        async def ws_state(websocket: WebSocket):
            await websocket.accept()
            try:
                while True:
                    await websocket.send_json(self.public_state())
                    await asyncio.sleep(1)
            except WebSocketDisconnect:
                pass  # client closed the tab — normal
            except Exception:
                # Don't swallow real bugs silently; log so they're diagnosable.
                self.logger.exception("WebSocket state push failed.")

        self.register_routes(app)
        return app

    # ── CLI entry (probe self-test + banner + serve) ──────────────────────────

    def print_banner(self) -> None:  # override to add device-specific lines
        pass

    def run(self) -> None:
        # Bench-laptop self-test: `--probe-mac [ip]` reads the MAC of `ip`
        # (default: the configured host) and exits — run it against any reachable
        # LAN device to confirm this platform's ping/arp detection works.
        if "--probe-mac" in sys.argv:
            i = sys.argv.index("--probe-mac")
            probe_ip = (sys.argv[i + 1] if len(sys.argv) > i + 1
                        else self.cfg.get("host", ""))
            probe_mac = read_device_mac(probe_ip)
            print(f"{probe_ip} -> MAC {probe_mac or 'NOT FOUND'}")
            sys.exit(0 if probe_mac else 1)

        url = f"http://127.0.0.1:{self.port}"
        print(f"Starting {self.title} at {url}")
        print(f"  version      : {self.bench_version}")
        print(f"  station      : {self.station_id}")
        print(f"  operator     : {self.operator_store.get() or '(not set — enter it on the page)'}")
        print(f"  config hash  : {self.config_hash}")
        for warning in self.config_warnings:
            print(f"  CONFIG WARNING: {warning}")
        self.print_banner()
        app = self.build_app()
        # Tools are launched by the bench dashboard, which opens the one browser
        # tab itself — so a tool never auto-opens a tab unless explicitly asked
        # with BENCH_OPEN_BROWSER=1 (e.g. when running this one tool on its own).
        # (Opt-in, not opt-out: relying on the launcher to unset a var was
        # fragile on Windows and left every tool opening an about:blank tab.)
        if os.environ.get("BENCH_OPEN_BROWSER"):
            with contextlib.suppress(Exception):
                webbrowser.open(url)
        uvicorn.run(app, host="127.0.0.1", port=self.port, log_level="warning")
