#!/usr/bin/env python3
"""Shared bench engine for the Magos configurators (radar + APU).

The radar UI (app.py) and the APU UI (apu_app.py) are the same iterative
plug-in → detect → configure → unplug workflow with the same auto / cycle modes,
the same settings panel, and the same per-unit logging — they differ only in the
device they talk to and a couple of fields (the APU also sets the controlled
radar). All of that common machinery lives here in `MagosBench`; each app is a
thin subclass that fills in the device-specific hooks.

This reuses the small generic bits from the shared `bench_core` package
(`StepCollector`, `slug`) so the Magos tools and the Teltonika tools share one
infrastructure layer. The device talking still goes through the Magos clients
(magos_configure.MagosClient / apu_configure.APUClient), which both log through
the "magos" logger.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import logging.handlers
import os
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

# Generic bench-UI helper shared with the Teltonika tools.
from bench_core.bench_ui import slug

# The Magos device clients log through the "magos" logger; reuse its line format
# so step lines render the same in every tool.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from magos_configure import (  # noqa: E402
    LOG_LINE_FORMAT,
    is_on_link,
    probe_http,
)

POLL_INTERVAL_SEC = 2.0
DETECT_TIMEOUT_SEC = 1.0
# A device must miss this many consecutive polls before we treat it as
# unplugged — a single blip during its IP change must not restart the cycle.
MISS_THRESHOLD = 3
HISTORY_MAX = 200
# A hands-free mode (auto/cycle) left armed with nothing plugged in for this
# long disarms itself, so a unit plugged in much later isn't silently
# reconfigured by a mode someone walked away from and forgot to turn off.
AUTO_IDLE_TIMEOUT_SEC = 600  # 10 minutes
# When the last dashboard tab disconnects (closed), the server stops itself
# after this grace window. The grace lets a page *refresh* — which briefly
# drops the WebSocket and immediately reopens it — reconnect without killing
# the process.
SHUTDOWN_GRACE_SEC = 5


class MagosBench:
    """Iterative single-device bench engine. Subclass it, set the class
    attributes, and override the device hooks marked below."""

    # ── identity / wiring (override as class attributes) ──────────────────────
    title: str = "Magos Configurator"
    port: int = 8001
    html_file: str = "index.html"
    log_file: str = "magos-config.log"   # rolling human log under logs/
    log_prefix: str = ""                  # per-unit JSON filename prefix
    logger_name: str = "magos"
    device_word: str = "radar"            # used in user-facing messages
    channel_ips: dict = {}                # CHANNEL_IPS / APU_CHANNEL_IPS
    uses_radar_ip: bool = False           # APU also targets a controlled radar

    def __init__(self, base_dir: Path, default_cfg: dict) -> None:
        self.base_dir = base_dir
        self.log_dir = base_dir / "logs"
        self.log_dir.mkdir(exist_ok=True)
        self.log = self._setup_logging()
        self.cfg: dict = dict(default_cfg)
        self._misses = 0
        # Monotonic clock + last-activity stamp drive the hands-free idle
        # timeout. `_now` is an attribute so tests can inject a fake clock.
        self._now = time.monotonic
        self._last_activity = self._now()
        # Connected dashboard tabs (WebSocket clients). When this hits zero the
        # server shuts itself down after a short grace; `_server` is the running
        # uvicorn server we ask to exit, set once in run().
        self._clients = 0
        self._shutdown_task: Optional[asyncio.Task] = None
        self._server: Optional["uvicorn.Server"] = None
        self.state: dict = self._initial_state()

    # ── logging ───────────────────────────────────────────────────────────────

    def _setup_logging(self) -> logging.Logger:
        logger = logging.getLogger(self.logger_name)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if not any(isinstance(h, logging.FileHandler) for h in logger.handlers):
            rolling = logging.handlers.RotatingFileHandler(
                self.log_dir / self.log_file, maxBytes=2_000_000, backupCount=3,
                encoding="utf-8")
            rolling.setFormatter(logging.Formatter("%(asctime)s " + LOG_LINE_FORMAT))
            logger.addHandler(rolling)
            console = logging.StreamHandler()
            console.setFormatter(logging.Formatter(LOG_LINE_FORMAT))
            logger.addHandler(console)
        return logger

    # ── state ───────────────────────────────────────────────────────────────

    def _initial_auto(self) -> dict:
        auto = {"enabled": False, "channel": None, "ip": None}
        if self.uses_radar_ip:
            auto["radar_ip"] = None
        return auto

    def _initial_state(self) -> dict:
        return {
            "phase": "waiting",          # waiting|detected|configuring|configured|error
            "detected": False,
            "active_host": None,         # the factory IP the current unit answered on
            "busy": False,
            "message": f"Waiting for a {self.device_word} at {self._hosts_str()}...",
            "last_result": None,
            "history": [],
            # Auto mode: configure each detected unit to this armed target.
            "auto": self._initial_auto(),
            # Cycle mode: units are provisioned in groups of 4, each taking the
            # next channel (0->1->2->3, wrapping). `index` is the channel the NEXT
            # detected unit will get; `count` is total configured.
            "cycle": {"enabled": False, "index": 0, "count": 0},
            # Serial of the last successfully configured unit — auto/cycle mode
            # refuses to reconfigure the same unit if it briefly reappears.
            "last_ok_serial": None,
            # Set when no local adapter can reach the factory subnet.
            "net_warning": None,
        }

    def _hosts_str(self) -> str:
        return " / ".join(self.cfg.get("hosts", [])) or "(no hosts set)"

    @property
    def cycle_channels(self) -> list:
        return list(self.channel_ips)

    # ── device hooks (override in the subclass) ───────────────────────────────

    def resolve_target(self, channel: Optional[str], ip: Optional[str],
                       radar_ip: Optional[str] = None) -> Optional[dict]:
        """Map a (channel, ip[, radar_ip]) choice to a target dict
        ({"channel","ip",[ "radar_ip"]}) or None when nothing is selected."""
        raise NotImplementedError

    def do_configure(self, target: dict, host: str,
                     avoid_serial: Optional[str]) -> dict:
        """Run the device pipeline in a worker thread; never raises. Returns a
        result dict with: ok, skipped, ip (address shown in history), identity,
        raw, steps, log, error, verified, verify_detail."""
        raise NotImplementedError

    def build_entry(self, target: dict, host: str, result: dict) -> dict:
        """Build the per-unit history entry from a configure result."""
        raise NotImplementedError

    def success_message(self, ident: dict, result: dict, target: dict) -> str:
        raise NotImplementedError

    def extra_public_state(self) -> dict:
        return {}

    def print_banner(self) -> None:
        pass

    # ── detection helpers ─────────────────────────────────────────────────────

    def _split_host_port(self, host_str: str) -> tuple[str, int]:
        default_port = 443 if self.cfg.get("scheme") == "https" else 80
        if ":" in host_str:
            host, port = host_str.rsplit(":", 1)
            try:
                return host, int(port)
            except ValueError:
                return host_str, default_port
        return host_str, default_port

    def first_reachable_host(self) -> Optional[str]:
        """First factory host where a dashboard answers HTTP (not just an open
        port — a router squatting on the IP no longer reads as 'detected')."""
        for candidate in self.cfg.get("hosts", []):
            if probe_http(candidate, scheme=self.cfg["scheme"],
                          timeout=DETECT_TIMEOUT_SEC, verify=not self.cfg["insecure"]):
                return candidate
        return None

    def net_warning(self) -> Optional[str]:
        """Warn when no local adapter sits on the factory subnet — without this,
        a wrong NIC setup is indistinguishable from 'nothing plugged in'."""
        if not self.cfg.get("hosts"):
            return "No factory hosts configured."
        for candidate in self.cfg["hosts"]:
            host, _ = self._split_host_port(candidate)
            if is_on_link(host):
                return None
        return (f"This PC has no network adapter on the factory subnet "
                f"({self._hosts_str()}) — {self.device_word}s cannot be detected. "
                "Set the adapter to a static 192.168.40.x address.")

    # ── per-unit logging ──────────────────────────────────────────────────────

    def _save_log(self, entry: dict, raw: dict) -> Optional[str]:
        ts = datetime.now(timezone.utc)
        name = (f"{self.log_prefix}{ts.strftime('%Y%m%d-%H%M%S')}_"
                f"{slug(entry.get('serial') or 'unknown')}_{entry.get('status')}.json")
        path = self.log_dir / name
        payload = {**entry, "timestamp": ts.isoformat(), "raw_identity_payloads": raw}
        try:
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                            encoding="utf-8")
        except OSError as e:
            self.log.warning("Could not write log file %s: %s", path, e)
            return None
        return str(path)

    # ── public state ──────────────────────────────────────────────────────────

    def public_state(self) -> dict:
        return {
            **self.state,
            "config": {k: v for k, v in self.cfg.items() if k != "password"},
            "password_set": bool(self.cfg.get("password")),
            "channel_ips": self.channel_ips,
            **self.extra_public_state(),
        }

    # ── the configuration run (shared orchestration) ──────────────────────────

    async def run_configuration(self, target: dict, host: str,
                                guard_repeat: bool = False) -> Optional[dict]:
        """Apply one unit's config, record it, save logs. With guard_repeat
        (auto/cycle), a unit that's the one just configured is skipped (returns
        None and records nothing)."""
        self.state["busy"] = True
        self.state["phase"] = "configuring"
        self.state["message"] = f"Configuring {self.device_word} ({host}) as {target['ip']}..."
        avoid = self.state["last_ok_serial"] if guard_repeat else None

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, self.do_configure, target, host, avoid)
        ident = result["identity"]

        if result["skipped"]:
            self.state["busy"] = False
            self.state["phase"] = "configured"
            self.state["message"] = (
                f"{self.device_word.capitalize()} SN {avoid} was already configured "
                f"and is still answering on {host} — unplug it and plug in the next one.")
            return None

        entry = self.build_entry(target, host, result)
        entry["log_file"] = self._save_log(entry, result["raw"])

        self.state["history"].insert(0, entry)
        del self.state["history"][HISTORY_MAX:]
        self.state["last_result"] = entry
        self.state["busy"] = False

        if result["ok"]:
            if ident["serial"] not in (None, "", "unknown"):
                self.state["last_ok_serial"] = ident["serial"]
            self.state["phase"] = "configured"
            self.state["message"] = self.success_message(ident, result, target)
        else:
            self.state["phase"] = "error"
            self.state["message"] = f"Configuration failed: {result['error']}"
        return entry

    # ── detection loop ────────────────────────────────────────────────────────

    async def poll_step(self, active: Optional[str]) -> None:
        """One detection-loop iteration given the reachable host (or None).
        Split out from the loop so the auto/cycle decision logic is testable."""
        reachable = active is not None
        self._misses = 0 if reachable else self._misses + 1
        self.state["detected"] = reachable
        self.state["active_host"] = active

        phase = self.state["phase"]
        auto = self.state["auto"]
        cycle = self.state["cycle"]
        word = self.device_word

        # A unit on the link counts as bench activity and keeps a hands-free
        # mode armed.
        if reachable:
            self._last_activity = self._now()

        # Idle auto-disarm: an armed auto/cycle mode with nothing plugged in for
        # AUTO_IDLE_TIMEOUT_SEC turns itself off, so the next unit plugged in
        # long after isn't silently reconfigured.
        if (not self.state["busy"] and not reachable
                and (auto["enabled"] or cycle["enabled"])
                and self._now() - self._last_activity >= AUTO_IDLE_TIMEOUT_SEC):
            self._disarm_idle()
            return

        if self.state["busy"]:
            pass  # a configuration is running; don't touch the state machine
        elif reachable:
            if phase in ("waiting", "detected") and cycle["enabled"]:
                channel = self.cycle_channels[cycle["index"]]
                target = self.resolve_target(channel, None, None)
                entry = await self.run_configuration(target, active, guard_repeat=True)
                if entry and entry["status"] == "ok":
                    cycle["count"] += 1
                    cycle["index"] = (cycle["index"] + 1) % len(self.cycle_channels)
            elif phase in ("waiting", "detected") and auto["enabled"]:
                target = self.resolve_target(auto["channel"], auto["ip"],
                                             auto.get("radar_ip"))
                if target:
                    await self.run_configuration(target, active, guard_repeat=True)
                elif phase != "detected":
                    self.state["phase"] = "detected"
                    self.state["message"] = (f"{word.capitalize()} detected at {active} "
                                             "(auto armed, but no target set).")
            elif phase == "waiting":
                self.state["phase"] = "detected"
                self.state["message"] = (f"{word.capitalize()} detected at {active}. "
                                         "Pick a channel and configure it.")
            # phase == configured/error while still plugged in: wait for unplug.
        else:  # nothing reachable for MISS_THRESHOLD polls in a row
            if phase in ("detected", "configured", "error") and self._misses >= MISS_THRESHOLD:
                self.state["phase"] = "waiting"
                if cycle["enabled"]:
                    nxt = self.cycle_channels[cycle["index"]]
                    self.state["message"] = (
                        f"Cycle mode ON — plug in the next {word} (it will be channel "
                        f"{nxt} → {self.channel_ips[nxt]}).")
                elif auto["enabled"]:
                    self.state["message"] = f"Auto mode ON — plug in the next {word}..."
                else:
                    self.state["message"] = f"Plug in the next {word}..."

    async def _poll_loop(self) -> None:
        loop = asyncio.get_event_loop()
        while True:
            try:
                active = await loop.run_in_executor(None, self.first_reachable_host)
                self.state["net_warning"] = await loop.run_in_executor(None, self.net_warning)
                await self.poll_step(active)
            except Exception:
                self.log.exception("Detection loop error — recovering on next poll.")
            await asyncio.sleep(POLL_INTERVAL_SEC)

    # ── request handlers (shared logic behind the per-app routes) ──────────────

    def apply_settings(self, updates: dict) -> dict:
        for key, value in updates.items():
            if value is None:
                continue
            if key == "hosts":
                cleaned = [h.strip() for h in value if h and h.strip()]
                if cleaned:
                    self.cfg["hosts"] = cleaned
            else:
                self.cfg[key] = value
        if self.state["phase"] == "waiting":
            self.state["message"] = f"Waiting for a {self.device_word} at {self._hosts_str()}..."
        return self.public_state()

    async def configure_request(self, channel: Optional[str], ip: Optional[str],
                                radar_ip: Optional[str] = None) -> dict:
        if self.state["phase"] != "detected":
            return {"error": f"No {self.device_word} is currently detected to configure."}
        if self.state["busy"]:
            return {"error": "A configuration is already in progress."}
        target = self.resolve_target(channel, ip, radar_ip)
        if not target:
            return {"error": "Provide a channel (0-3) or a manual IP."}
        host = self.state["active_host"] or (self.cfg["hosts"][0] if self.cfg.get("hosts") else None)
        if not host:
            return {"error": "No factory host configured."}
        await self.run_configuration(target, host)
        return self.public_state()

    def set_auto(self, enabled: bool, channel: Optional[str], ip: Optional[str],
                 radar_ip: Optional[str] = None) -> dict:
        target = self.resolve_target(channel, ip, radar_ip)
        if enabled and not target:
            return {"error": "Pick a channel or enter an IP before turning on auto mode."}
        auto = {"enabled": enabled, "channel": channel, "ip": ip}
        if self.uses_radar_ip:
            auto["radar_ip"] = radar_ip
        self.state["auto"] = auto
        if enabled:
            self.state["cycle"]["enabled"] = False  # auto + cycle are mutually exclusive
            extra = (f" (radar {target['radar_ip']})"
                     if self.uses_radar_ip and target.get("radar_ip") else "")
            self.state["message"] = (f"Auto mode ON — the next {self.device_word} will be "
                                     f"configured as {target['ip']}{extra}.")
        elif self.state["phase"] not in ("configuring",):
            self.state["message"] = self._idle_message("Auto mode off.")
        return self.public_state()

    def set_cycle(self, enabled: bool, start_channel: Optional[str] = None) -> dict:
        if enabled:
            channels = self.cycle_channels
            start_index = 0
            if start_channel not in (None, ""):
                sc = str(start_channel).strip()
                if sc not in channels:
                    return {"error": f"Unknown start channel '{sc}'. "
                                     f"Pick one of: {', '.join(channels)}."}
                start_index = channels.index(sc)
            self._last_activity = self._now()       # arm restarts the idle clock
            self.state["cycle"] = {"enabled": True, "index": start_index, "count": 0}
            self.state["auto"] = self._initial_auto()
            first = channels[start_index]
            self.state["message"] = self._cycle_start_message(first)
        else:
            self.state["cycle"]["enabled"] = False
            if self.state["phase"] not in ("configuring",):
                self.state["message"] = self._idle_message("Cycle stopped.")
        return self.public_state()

    def dismiss(self) -> dict:
        self.state["last_result"] = None
        self.state["phase"] = "detected" if self.state["detected"] else "waiting"
        if self.state["detected"]:
            self.state["message"] = (f"{self.device_word.capitalize()} detected at "
                                     f"{self.state['active_host']}. Pick a channel and configure it.")
        else:
            self.state["message"] = f"Waiting for a {self.device_word} at {self._hosts_str()}..."
        return self.public_state()

    def _disarm_idle(self) -> None:
        """Turn off whichever hands-free mode is armed after an idle stretch and
        drop back to plain waiting, so a stray unit later isn't auto-configured."""
        mode = "Cycle" if self.state["cycle"]["enabled"] else "Auto"
        self.state["auto"] = self._initial_auto()
        self.state["cycle"]["enabled"] = False
        self.state["phase"] = "waiting"
        minutes = AUTO_IDLE_TIMEOUT_SEC // 60
        self.state["message"] = (
            f"{mode} mode turned off automatically after {minutes} minutes with "
            f"no {self.device_word} plugged in — toggle it back on to resume.")
        self.log.info("%s mode auto-disarmed after %ds idle.",
                      mode, AUTO_IDLE_TIMEOUT_SEC)

    def _idle_message(self, prefix: str) -> str:
        if self.state["detected"]:
            return f"{prefix} {self.device_word.capitalize()} detected at {self.state['active_host']}."
        return f"Waiting for a {self.device_word} at {self._hosts_str()}..."

    def _cycle_start_message(self, first_channel: str) -> str:
        return (f"Cycle started — plug in {self.device_word}s one by one. First → "
                f"channel {first_channel} ({self.channel_ips[first_channel]}).")

    # ── lifecycle: stop when the last browser tab closes ───────────────────────

    def _note_client_connect(self) -> None:
        """A dashboard tab opened (or reconnected after a refresh)."""
        self._clients += 1
        self._cancel_pending_shutdown()

    def _note_client_disconnect(self) -> None:
        """A dashboard tab closed. With none left, arm the shutdown timer."""
        self._clients = max(0, self._clients - 1)
        if self._clients == 0:
            self._schedule_shutdown()

    def _cancel_pending_shutdown(self) -> None:
        if self._shutdown_task is not None and not self._shutdown_task.done():
            self._shutdown_task.cancel()
        self._shutdown_task = None

    def _schedule_shutdown(self) -> None:
        self._cancel_pending_shutdown()
        self._shutdown_task = asyncio.create_task(self._shutdown_after_grace())

    async def _shutdown_after_grace(self) -> None:
        """Stop the server unless a tab reconnects within the grace window (a
        refresh) and cancels this."""
        try:
            await asyncio.sleep(SHUTDOWN_GRACE_SEC)
        except asyncio.CancelledError:
            return
        if self._clients == 0:
            self.log.info("Dashboard closed — stopping %s (no tab reconnected "
                          "within %ds).", self.title, SHUTDOWN_GRACE_SEC)
            if self._server is not None:
                self._server.should_exit = True

    # ── FastAPI app ────────────────────────────────────────────────────────────

    def register_routes(self, app: FastAPI) -> None:
        """Subclass adds /api/settings, /api/configure, /api/auto, /api/cycle
        with its own request bodies, delegating to the handlers above."""

    def build_app(self) -> FastAPI:
        @contextlib.asynccontextmanager
        async def lifespan(_app: FastAPI):
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

        @app.get("/")
        async def index():
            return FileResponse(str(self.base_dir / "static" / self.html_file))

        @app.get("/api/state")
        async def get_state():
            return self.public_state()

        @app.post("/api/dismiss")
        async def dismiss():
            return self.dismiss()

        @app.websocket("/ws/state")
        async def ws_state(websocket: WebSocket):
            await websocket.accept()
            self._note_client_connect()
            try:
                while True:
                    await websocket.send_json(self.public_state())
                    await asyncio.sleep(1)
            except (WebSocketDisconnect, Exception):
                pass
            finally:
                self._note_client_disconnect()

        self.register_routes(app)
        return app

    # ── CLI entrypoint ──────────────────────────────────────────────────────────

    def run(self) -> None:
        # 127.0.0.1 (not "localhost") avoids browsers auto-upgrading to HTTPS via
        # HSTS — this is an HTTP-only server, and HTTPS would return 400.
        url = f"http://127.0.0.1:{self.port}"
        print(f"Starting {self.title} at {url}")
        print("  (open it as http://, NOT https:// — this is a plain-HTTP local server)")
        self.print_banner()
        app = self.build_app()
        # The bench dashboard launcher opens one tab itself, so it sets
        # BENCH_NO_BROWSER to stop each tool spawning its own.
        if not os.environ.get("BENCH_NO_BROWSER"):
            with contextlib.suppress(Exception):
                webbrowser.open(url)
        # Build the server explicitly (rather than uvicorn.run) so we keep a
        # handle to ask it to stop when the dashboard tab is closed.
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                log_level="warning")
        self._server = uvicorn.Server(config)
        self._server.run()
