#!/usr/bin/env python3
"""Shared bench-UI base for the device configurators (OTD500, RUTM08, ...).

Both configurator apps are the same FastAPI shell: a background poll loop that
detects a freshly-plugged device, a worker-thread pipeline run with a live step
log, a rolling-history state served over a WebSocket, and a small set of REST
routes. Everything in this module is device-agnostic; the per-device bits
(detection, the pipeline call, the history-entry shape, the configure route)
are supplied by subclassing `BenchConfigurator` and overriding its hooks.

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

from bench_core import LOG_LINE_FORMAT, normalize_mac

try:
    import requests
except ImportError:  # pragma: no cover - requests is a hard dep in practice
    requests = None

POLL_INTERVAL_SEC = 2.0
DETECT_TIMEOUT_SEC = 1.0

# Per-run JSON logs embed full raw device payloads, so cap how many we keep on an
# operator machine that may run for months without a restart.
JSON_LOG_RETENTION = 500

# Shared static assets (bench.css / bench.js), served by every tool at /shared.
SHARED_STATIC_DIR = Path(__file__).resolve().parent / "static"

# Secret-bearing config paths redacted before the state is sent to the browser.
_REDACT_PATHS = (("new_password",), ("tailscale", "auth_key"), ("tailscale", "api_key"),
                 ("rms", "auth_code"), ("rms", "api_token"))


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


# ── MAC reading (ARP) ───────────────────────────────────────────────────────

def canonical_mac(mac: str) -> str:
    """Zero-pad each octet then strip separators, so macOS's '0:1e:42:aa:bb:1',
    Windows's '20-97-27-2f-df-f0' and a manifest's '00:1E:42:AA:BB:01' all
    compare equal."""
    parts = re.split(r"[:-]", mac)
    if len(parts) == 6:
        mac = ":".join(p.zfill(2) for p in parts)
    return normalize_mac(mac)


_MAC_RE = re.compile(r"([0-9a-fA-F]{1,2}(?:[:-][0-9a-fA-F]{1,2}){5})")


def mac_from_arp_output(text: str) -> Optional[str]:
    """First usable MAC in arp/ip-neigh output. Skips entries that mean 'no
    answer' rather than a device: all-zero (unresolved) and broadcast."""
    for m in _MAC_RE.finditer(text or ""):
        mac = canonical_mac(m.group(1))
        if mac not in ("000000000000", "ffffffffffff"):
            return mac
    return None


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

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.config_path = base_dir / self.config_filename
        self.log_dir = base_dir / "logs"
        self.log_dir.mkdir(exist_ok=True)
        self.logger = setup_device_logging(self.log_dir, self.log_filename, self.logger_name)
        self._prune_logs()  # trim any backlog left by earlier sessions
        self.bench_version = bench_version(base_dir)
        self.logger.info("%s starting — bench version %s", self.title, self.bench_version)
        self.cfg: dict = self.load_config()
        self.state: dict = self.initial_state()
        self.state["config_loaded"] = bool(self.cfg)
        self._live_collector: Optional[StepCollector] = None
        self._run_t0: Optional[float] = None

    # ── config + state (override `initial_state`; usually keep the rest) ──────

    def load_config(self) -> dict:
        if self.config_path.exists():
            with open(self.config_path, "r", encoding="utf-8") as f:
                return {k: v for k, v in json.load(f).items() if not k.startswith("_")}
        self.logger.warning("%s not found — copy the example.", self.config_path.name)
        return {}

    def initial_state(self) -> dict:  # override
        raise NotImplementedError

    def counts(self) -> dict:  # override (manifest progress vs history tally)
        done = sum(1 for h in self.state["history"] if h["status"] == "ok")
        return {"done": done, "error": len(self.state["history"]) - done}

    def extra_public_state(self) -> dict:  # override to add manifest / name_prefix / etc.
        return {}

    def reload(self) -> str:
        """Re-read config from disk. Override to also reload a manifest. Returns
        the status message to show in the UI."""
        self.cfg = self.load_config()
        self.state["config_loaded"] = bool(self.cfg)
        return ("Config reloaded." if self.cfg
                else f"No {self.config_filename} found — copy the example.")

    def public_state(self) -> dict:
        busy = self.state["busy"]
        return {**self.state, "counts": self.counts(),
                "bench_version": self.bench_version,
                "config": redact_config(self.cfg),
                "live_steps": list(self._live_collector.steps)[-200:]
                              if busy and self._live_collector else [],
                "run_seconds": int(time.monotonic() - self._run_t0)
                               if busy and self._run_t0 else None,
                **self.extra_public_state()}

    # ── the pipeline run (shared machinery; override the small hooks) ─────────

    def hostname_for(self, inputs: dict) -> str:  # override
        raise NotImplementedError

    def client_host(self, inputs: dict) -> str:  # override
        return self.cfg.get("host", "")

    def build_client(self, run_cfg: dict, host: str):  # override
        raise NotImplementedError

    def run_pipeline(self, client, run_cfg: dict, inputs: dict) -> dict:  # override
        raise NotImplementedError

    def build_entry(self, result: dict, inputs: dict, duration: int) -> dict:  # override
        raise NotImplementedError

    def on_run_recorded(self, result: dict, inputs: dict, entry: dict) -> None:
        """Hook after an entry is appended to history (e.g. mark a manifest row)."""

    def success_message(self, result: dict, entry: dict, took: str) -> str:
        return (f"Configured {result['hostname']} (SN {entry['serial']}) in {took}. "
                "Unplug it and plug in the next one.")

    def _do_configure(self, inputs: dict) -> dict:
        """Run one device's pipeline in a worker thread, collecting its log."""
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
        if ts.get("enabled"):
            ts["_resolved_auth_key"] = resolve_tailscale_key(
                self.cfg, hostname, label=self.tailscale_label, logger=self.logger)
            run_cfg["tailscale"] = ts

        identity, warnings, error, ok, verification = {}, [], None, False, []
        client = self.build_client(run_cfg, self.client_host(inputs))
        try:
            result = self.run_pipeline(client, run_cfg, inputs)
            identity = result["identity"]
            warnings = result.get("warnings", [])
            verification = result.get("verification", [])
            ok = result.get("ok", False)   # absent "ok" must read as failure, not success
            if not ok:
                problems = list(result.get("failures", []))
                problems += [f"verify:{c['item']}" for c in verification if c["ok"] is False]
                error = "; ".join(problems)
        except BaseException as e:  # noqa: BLE001 — SystemExit + any network error
            error = str(e)
            self.logger.error("Provisioning FAILED: %s", e)
        finally:
            self.logger.removeHandler(collector)
            client.close()

        steps = collector.steps
        return {
            "ok": ok, "hostname": hostname, "identity": identity, "warnings": warnings,
            "error": error, "steps": steps, "verification": verification,
            "log": "\n".join(f"[{s['level']}] [{s['sn']}] {s['msg']}" for s in steps),
        }

    def _prune_logs(self, keep: int = JSON_LOG_RETENTION) -> None:
        prune_json_logs(self.log_dir, keep)

    def _save_log(self, entry: dict) -> Optional[str]:
        ts = datetime.now(timezone.utc)
        name = (f"{ts.strftime('%Y%m%d-%H%M%S')}_{slug(entry.get('hostname'))}_"
                f"{entry.get('status')}.json")
        path = self.log_dir / name
        with contextlib.suppress(OSError):
            path.write_text(json.dumps({**entry, "timestamp": ts.isoformat()},
                                       indent=2, ensure_ascii=False), encoding="utf-8")
            self._prune_logs()  # keep the folder bounded during long-running sessions
            return str(path)
        return None

    async def execute_run(self, inputs: dict, label: str) -> bool:
        """Run one device's pipeline. Returns False if a run is already in
        progress. The busy check-and-set is atomic (no await between them), so
        the poll loop and /api/configure can never start two runs at once."""
        if self.state["busy"]:
            return False
        self.state["busy"] = True
        self.state["phase"] = "configuring"
        self.state["message"] = f"Configuring {label}…"
        self._run_t0 = time.monotonic()
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, self._do_configure, inputs)
            duration = int(time.monotonic() - self._run_t0)
            took = f"{duration // 60}m{duration % 60:02d}s"
            entry = self.build_entry(result, inputs, duration)
            entry["log_file"] = self._save_log(entry)
            self.state["history"].insert(0, entry)
            del self.state["history"][self.history_limit:]
            self.state["last_result"] = entry
            self.on_run_recorded(result, inputs, entry)
            if result["ok"]:
                self.state["phase"] = "configured"
                self.state["message"] = self.success_message(result, entry, took)
            else:
                self.state["phase"] = "error"
                self.state["message"] = f"Failed after {took}: {result['error']}"
        finally:
            self.state["busy"] = False
            self._run_t0 = None
        return True

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

    def _install_loop_exception_handler(self) -> None:
        """Silence the spurious 'forcibly closed' tracebacks Windows logs when a
        browser drops a connection mid-flight.

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

    def build_app(self) -> FastAPI:
        @contextlib.asynccontextmanager
        async def lifespan(_app: FastAPI):
            self._install_loop_exception_handler()
            poller = asyncio.create_task(self._poll_loop())
            try:
                yield
            finally:
                poller.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await poller

        app = FastAPI(title=self.title, lifespan=lifespan)
        app.mount("/static", StaticFiles(directory=str(self.base_dir / "static")),
                  name="static")
        # Shared CSS/JS live in this package so every tool serves one copy.
        app.mount("/shared", StaticFiles(directory=str(SHARED_STATIC_DIR)),
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
