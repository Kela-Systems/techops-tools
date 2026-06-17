#!/usr/bin/env python3
"""Magos Configurator – iterative radar-provisioning Web UI (FastAPI).

Workflow this UI is built around:

  1. Plug a fresh radar into the laptop (it boots on a factory IP such as
     192.168.40.50 or 192.168.40.60, with admin:password).
  2. The UI polls those factory IPs and shows "radar detected".
  3. You pick the channel (0-3) or a manual IP, hit Configure.
  4. The radar gets its NTP + static IP and drops off the factory subnet.
  5. The UI goes back to "waiting" — unplug it, plug the next one, repeat.

The actual device talking is delegated to MagosClient in magos_configure.py
(next to this file), so the CLI and the UI share one code path. This server
only adds the detection loop + iterative state machine.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import logging.handlers
import re
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Reuse the CLI's client + defaults (magos_configure.py lives next to this file).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from magos_configure import (  # noqa: E402
    CHANNEL_IPS,
    DEFAULT_DNS,
    DEFAULT_GATEWAY,
    DEFAULT_HOST,
    DEFAULT_NETMASK,
    DEFAULT_NTP,
    DEFAULT_PASSWORD,
    DEFAULT_TIMEZONE,
    DEFAULT_USERNAME,
    LOG_LINE_FORMAT,
    MagosClient,
    is_on_link,
    probe_http,
    set_log_serial,
    to_cidr,
    verify_device_at,
)

BASE_DIR = Path(__file__).resolve().parent
POLL_INTERVAL_SEC = 2.0
DETECT_TIMEOUT_SEC = 1.0
# A device must miss this many consecutive polls before we treat it as
# unplugged — a single blip during its IP change must not restart the cycle.
MISS_THRESHOLD = 3
HISTORY_MAX = 200

# Factory IPs a fresh radar may boot on — detection checks each in order.
DEFAULT_HOSTS = [DEFAULT_HOST, "192.168.40.60"]

# ── Logging: everything the device client does flows through the "magos"
# logger. We keep one rolling human-readable log of every step across all
# radars, and additionally save a structured JSON file per configuration.
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

device_logger = logging.getLogger("magos")
device_logger.setLevel(logging.INFO)
device_logger.propagate = False
if not any(isinstance(h, logging.FileHandler) for h in device_logger.handlers):
    _rolling = logging.handlers.RotatingFileHandler(
        LOG_DIR / "magos-config.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    _rolling.setFormatter(logging.Formatter("%(asctime)s " + LOG_LINE_FORMAT))
    device_logger.addHandler(_rolling)
    _console = logging.StreamHandler()  # also echo to the server console
    _console.setFormatter(logging.Formatter(LOG_LINE_FORMAT))
    device_logger.addHandler(_console)


class _StepCollector(logging.Handler):
    """Captures each log record of one configuration run as a timestamped step."""

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


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", text or "unknown")

@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    poller = asyncio.create_task(_poll_loop())
    try:
        yield
    finally:
        poller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller


app = FastAPI(title="Magos Configurator", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ── Mutable server config (editable from the UI, pre-filled with factory defaults)
cfg: dict = {
    "hosts": list(DEFAULT_HOSTS),   # factory IPs to watch for a fresh radar
    "scheme": "http",
    "insecure": False,
    "username": DEFAULT_USERNAME,
    "password": DEFAULT_PASSWORD,
    "ntp": DEFAULT_NTP,
    "timezone": DEFAULT_TIMEZONE,
    "gateway": DEFAULT_GATEWAY,
    "dns": DEFAULT_DNS,
    "netmask": DEFAULT_NETMASK,
}


def _hosts_str() -> str:
    return " / ".join(cfg["hosts"]) or "(no hosts set)"


# ── Iterative state machine
#   waiting     -> no radar reachable at any factory IP
#   detected    -> radar reachable, ready to configure
#   configuring -> applying NTP + networking
#   configured  -> done; waiting for this radar to be unplugged
#   error       -> last configure failed; needs dismiss / retry
state: dict = {
    "phase": "waiting",
    "detected": False,
    "active_host": None,            # the factory IP the current radar answered on
    "busy": False,
    "message": f"Waiting for a radar at {_hosts_str()}...",
    "last_result": None,
    "history": [],  # list of per-radar result entries
    # Auto mode: when enabled, the next detected radar is configured to this
    # armed target without any clicking.
    "auto": {"enabled": False, "channel": None, "ip": None},
    # Cycle mode: radars are provisioned in groups of 4. Each detected radar
    # takes the next channel (0->1->2->3, wrapping for the next group). `index`
    # is the channel the NEXT detected radar will get; `count` is total configured.
    "cycle": {"enabled": False, "index": 0, "count": 0},
    # Serial of the last successfully configured radar — auto/cycle mode refuses
    # to reconfigure the same unit if it briefly reappears on the factory IP.
    "last_ok_serial": None,
    # Set when no local adapter can reach the factory subnet (shown in the UI).
    "net_warning": None,
}

CYCLE_CHANNELS = list(CHANNEL_IPS)  # ["0","1","2","3"]


# ── Pydantic bodies ───────────────────────────────────────────────────────────

class SettingsBody(BaseModel):
    hosts: Optional[list[str]] = None
    scheme: Optional[str] = None
    insecure: Optional[bool] = None
    username: Optional[str] = None
    password: Optional[str] = None
    ntp: Optional[str] = None
    timezone: Optional[str] = None
    gateway: Optional[str] = None
    dns: Optional[str] = None
    netmask: Optional[str] = None


class ConfigureBody(BaseModel):
    channel: Optional[str] = None  # "0".."3" or "other"/None
    ip: Optional[str] = None       # plain or CIDR, used for manual/other


class AutoBody(BaseModel):
    enabled: bool
    channel: Optional[str] = None
    ip: Optional[str] = None


class CycleBody(BaseModel):
    enabled: bool


# ── Helpers ─────────────────────────────────────────────────────────────────

def _split_host_port(host_str: str, scheme: str) -> tuple[str, int]:
    default_port = 443 if scheme == "https" else 80
    if ":" in host_str:
        host, port = host_str.rsplit(":", 1)
        try:
            return host, int(port)
        except ValueError:
            return host_str, default_port
    return host_str, default_port


def first_reachable_host() -> Optional[str]:
    """Return the first factory host where a dashboard answers HTTP.

    An HTTP-level probe, not a bare TCP connect — a router or captive portal
    squatting on the factory IP no longer reads as "radar detected".
    """
    for candidate in cfg["hosts"]:
        if probe_http(candidate, scheme=cfg["scheme"], timeout=DETECT_TIMEOUT_SEC,
                      verify=not cfg["insecure"]):
            return candidate
    return None


def _net_warning() -> Optional[str]:
    """Warn when no local adapter sits on the factory subnet — without this,
    a wrong NIC setup is indistinguishable from 'no radar plugged in'."""
    if not cfg["hosts"]:
        return "No factory hosts configured."
    for candidate in cfg["hosts"]:
        host, _ = _split_host_port(candidate, cfg["scheme"])
        if is_on_link(host):
            return None
    return (f"This PC has no network adapter on the factory subnet ({_hosts_str()}) — "
            "radars cannot be detected. Set the adapter to a static 192.168.40.x address.")


def do_configure(ip: str, host: str, avoid_serial: Optional[str] = None,
                 channel: Optional[str] = None) -> dict:
    """Run login -> identity -> NTP -> RF channel -> networking -> verify. Never raises.

    Returns identity (SN/MAC/model), the per-step log, and the raw API payloads
    used for identity (handy for spotting real field names on new firmware).
    Runs in a worker thread, so it uses its own log collector.

    If `avoid_serial` matches the device's serial, the run is skipped — auto
    mode passes the last configured serial so a unit that briefly reappears on
    the factory IP (slow to apply its new address) isn't configured twice.

    When `channel` is a channel number ("0".."3"), the radar's RF channel is set
    to the matching variant before the IP change (firmware >= 3.x; older radars
    are left untouched). Manual-IP runs ("other"/None) don't touch the channel.
    """
    ip_cidr = to_cidr(ip, cfg["netmask"])
    collector = _StepCollector()
    device_logger.addHandler(collector)
    set_log_serial(None)  # reset; get_identity() will set the real serial

    identity = {"serial": "unknown", "mac": "unknown", "model": "unknown"}
    raw: dict = {}
    error: Optional[str] = None
    ok = False
    skipped = False
    verified = False
    verify_detail: Optional[str] = None
    try:
        device_logger.info("Detected radar at %s — starting configuration.", host)
        client = MagosClient(host, scheme=cfg["scheme"], verify=not cfg["insecure"])
        client.login(cfg["username"], cfg["password"])
        ident = client.get_identity()
        raw = ident.pop("raw", {})
        identity = ident
        if avoid_serial and identity.get("serial") not in (None, "", "unknown") \
                and identity["serial"] == avoid_serial:
            skipped = True
            device_logger.warning(
                "Same radar as the previous run (SN %s) is still answering on the "
                "factory IP — skipping. Unplug it before the next one.", avoid_serial)
        else:
            if cfg["ntp"]:
                client.set_ntp(cfg["ntp"], cfg["timezone"])
            # Set the RF channel before networking — the IP change drops the link.
            if channel in CHANNEL_IPS:
                client.set_channel(channel)
            client.set_network(ip_cidr, cfg["gateway"], cfg["dns"])
            vres = verify_device_at(ip_cidr, scheme=cfg["scheme"],
                                    username=cfg["username"], password=cfg["password"],
                                    expect_substring=ip.split("/")[0],
                                    verify_tls=not cfg["insecure"])
            verified = vres["verified"]
            verify_detail = vres["detail"]
            device_logger.info("Configuration complete — radar should now be at %s.", ip_cidr)
            ok = True
    except Exception as e:  # MagosError + any requests/network error
        error = str(e)
        device_logger.error("Configuration FAILED: %s", e)
    finally:
        device_logger.removeHandler(collector)

    steps = collector.steps
    log_text = "\n".join(f"[{s['level']}] [{s['sn']}] {s['msg']}" for s in steps)
    return {
        "ok": ok, "skipped": skipped, "ip_cidr": ip_cidr, "identity": identity,
        "raw": raw, "steps": steps, "log": log_text, "error": error,
        "verified": verified, "verify_detail": verify_detail,
    }


def _save_radar_log(entry: dict, raw: dict) -> Optional[str]:
    """Write a structured JSON record for one configuration to logs/. Best-effort."""
    ts = datetime.now(timezone.utc)
    name = f"{ts.strftime('%Y%m%d-%H%M%S')}_{_slug(entry.get('serial') or 'unknown')}_{entry.get('status')}.json"
    path = LOG_DIR / name
    payload = {**entry, "timestamp": ts.isoformat(), "raw_identity_payloads": raw}
    try:
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        device_logger.warning("Could not write log file %s: %s", path, e)
        return None
    return str(path)


def _public_state() -> dict:
    return {
        **state,
        "config": {k: v for k, v in cfg.items() if k != "password"},
        "password_set": bool(cfg["password"]),
        "channel_ips": CHANNEL_IPS,
    }


def _resolve_target(channel: Optional[str], ip: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Map a (channel, ip) choice to (target_ip, channel_label) or (None, None)."""
    ch = (channel or "").strip().lower()
    if ch in CHANNEL_IPS:
        return CHANNEL_IPS[ch], ch
    if ip and ip.strip():
        return ip.strip(), (ch or "other")
    return None, None


async def _run_configuration(channel: str, ip: str, host: str,
                             guard_repeat: bool = False) -> Optional[dict]:
    """Shared by manual configure + auto mode: apply config, record, save logs.

    With guard_repeat (auto mode), the run is skipped if the detected device is
    the unit that was just configured — returns None and records nothing.
    """
    state["busy"] = True
    state["phase"] = "configuring"
    state["message"] = f"Configuring radar ({host}) as {ip}..."
    avoid = state["last_ok_serial"] if guard_repeat else None

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, do_configure, ip, host, avoid, channel)
    ident = result["identity"]

    if result["skipped"]:
        state["busy"] = False
        state["phase"] = "configured"
        state["message"] = (
            f"Radar SN {avoid} was already configured and is still answering on {host} — "
            "unplug it and plug in the next one."
        )
        return None

    entry = {
        "channel": channel,
        "ip": result["ip_cidr"],
        "from_host": host,
        "ntp": cfg["ntp"],
        "timezone": cfg["timezone"],
        "serial": ident["serial"],
        "mac": ident["mac"],
        "model": ident["model"],
        "status": "ok" if result["ok"] else "error",
        "error": result["error"],
        "verified": result["verified"],
        "verify_detail": result["verify_detail"],
        "steps": result["steps"],
        "log": result["log"],
        "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
    }
    entry["log_file"] = _save_radar_log(entry, result["raw"])

    state["history"].insert(0, entry)
    del state["history"][HISTORY_MAX:]
    state["last_result"] = entry
    state["busy"] = False

    if result["ok"]:
        if ident["serial"] not in (None, "", "unknown"):
            state["last_ok_serial"] = ident["serial"]
        state["phase"] = "configured"
        verified_note = (" Verified at the new IP." if result["verified"]
                         else f" NOT verified: {result['verify_detail']}.")
        state["message"] = (
            f"Configured {ident['model']} (SN {ident['serial']}) as {result['ip_cidr']}."
            + verified_note + " Unplug it and plug in the next one."
        )
    else:
        state["phase"] = "error"
        state["message"] = f"Configuration failed: {result['error']}"
    return entry


# ── Background detection loop ─────────────────────────────────────────────────

_misses = 0  # consecutive polls with nothing reachable (debounce, see MISS_THRESHOLD)


async def _poll_step(active: Optional[str]) -> None:
    """One detection-loop iteration given the currently-reachable host (or None).

    Split out from `_poll_loop` so the auto/cycle decision logic is testable.
    """
    global _misses
    reachable = active is not None
    _misses = 0 if reachable else _misses + 1
    state["detected"] = reachable
    state["active_host"] = active

    phase = state["phase"]
    auto = state["auto"]
    cycle = state["cycle"]

    if state["busy"]:
        pass  # a configuration is running; don't touch the state machine
    elif reachable:
        if phase in ("waiting", "detected") and cycle["enabled"]:
            channel = CYCLE_CHANNELS[cycle["index"]]
            target_ip, _ = _resolve_target(channel, None)
            entry = await _run_configuration(channel, target_ip, active, guard_repeat=True)
            if entry and entry["status"] == "ok":
                cycle["count"] += 1
                cycle["index"] = (cycle["index"] + 1) % len(CYCLE_CHANNELS)
        elif phase in ("waiting", "detected") and auto["enabled"]:
            target_ip, channel = _resolve_target(auto["channel"], auto["ip"])
            if target_ip:
                # Auto-configure this radar, then fall through to "configured".
                await _run_configuration(channel, target_ip, active, guard_repeat=True)
            elif phase != "detected":
                state["phase"] = "detected"
                state["message"] = f"Radar detected at {active} (auto armed, but no target set)."
        elif phase == "waiting":
            state["phase"] = "detected"
            state["message"] = f"Radar detected at {active}. Pick a channel and configure it."
        # phase == configured/error while still plugged in: wait for unplug.
    else:  # nothing reachable for MISS_THRESHOLD polls in a row
        if phase in ("detected", "configured", "error") and _misses >= MISS_THRESHOLD:
            state["phase"] = "waiting"
            if cycle["enabled"]:
                nxt = CYCLE_CHANNELS[cycle["index"]]
                state["message"] = (
                    f"Cycle mode ON — plug in the next radar (it will be channel {nxt} "
                    f"→ {CHANNEL_IPS[nxt]})."
                )
            elif auto["enabled"]:
                state["message"] = "Auto mode ON — plug in the next radar..."
            else:
                state["message"] = "Plug in the next radar..."


async def _poll_loop():
    loop = asyncio.get_event_loop()
    while True:
        active = await loop.run_in_executor(None, first_reachable_host)
        state["net_warning"] = await loop.run_in_executor(None, _net_warning)
        await _poll_step(active)
        await asyncio.sleep(POLL_INTERVAL_SEC)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(str(BASE_DIR / "static" / "index.html"))


@app.get("/api/state")
async def get_state():
    return _public_state()


@app.post("/api/settings")
async def update_settings(body: SettingsBody):
    for key, value in body.dict(exclude_unset=True).items():
        if value is None:
            continue
        if key == "hosts":
            cleaned = [h.strip() for h in value if h and h.strip()]
            if cleaned:
                cfg["hosts"] = cleaned
        else:
            cfg[key] = value
    if state["phase"] == "waiting":
        state["message"] = f"Waiting for a radar at {_hosts_str()}..."
    return _public_state()


@app.post("/api/configure")
async def configure(body: ConfigureBody):
    if state["phase"] != "detected":
        return {"error": "No radar is currently detected to configure."}
    if state["busy"]:
        return {"error": "A configuration is already in progress."}

    ip, channel = _resolve_target(body.channel, body.ip)
    if not ip:
        return {"error": "Provide a channel (0-3) or a manual IP."}

    host = state["active_host"] or (cfg["hosts"][0] if cfg["hosts"] else None)
    if not host:
        return {"error": "No factory host configured."}

    await _run_configuration(channel, ip, host)
    return _public_state()


@app.post("/api/auto")
async def set_auto(body: AutoBody):
    """Arm/disarm auto mode: configure each detected radar to the armed target."""
    target_ip, channel = _resolve_target(body.channel, body.ip)
    if body.enabled and not target_ip:
        return {"error": "Pick a channel or enter an IP before turning on auto mode."}

    state["auto"] = {"enabled": body.enabled, "channel": body.channel, "ip": body.ip}
    if body.enabled:
        state["cycle"]["enabled"] = False   # auto + cycle are mutually exclusive
        state["message"] = f"Auto mode ON — the next radar will be configured as {target_ip}."
    elif state["phase"] not in ("configuring",):
        state["message"] = (
            f"Auto mode off. Radar detected at {state['active_host']}."
            if state["detected"] else f"Waiting for a radar at {_hosts_str()}..."
        )
    return _public_state()


@app.post("/api/cycle")
async def set_cycle(body: CycleBody):
    """Arm/disarm cycle mode: each detected radar takes the next channel 0->3."""
    if body.enabled:
        # Starting a cycle: begin at channel 0, and turn off plain auto mode.
        state["cycle"] = {"enabled": True, "index": 0, "count": 0}
        state["auto"] = {"enabled": False, "channel": None, "ip": None}
        first = CYCLE_CHANNELS[0]
        state["message"] = (
            f"Cycle started — plug in radars one by one. First → channel {first} "
            f"({CHANNEL_IPS[first]})."
        )
    else:
        state["cycle"]["enabled"] = False
        if state["phase"] not in ("configuring",):
            state["message"] = (
                f"Cycle stopped. Radar detected at {state['active_host']}."
                if state["detected"] else f"Waiting for a radar at {_hosts_str()}..."
            )
    return _public_state()


@app.post("/api/dismiss")
async def dismiss():
    """Clear an error / force back to detection (e.g. after fixing settings)."""
    state["last_result"] = None
    state["phase"] = "detected" if state["detected"] else "waiting"
    state["message"] = (
        f"Radar detected at {state['active_host']}. Pick a channel and configure it."
        if state["detected"]
        else f"Waiting for a radar at {_hosts_str()}..."
    )
    return _public_state()


# ── WebSocket (push state every second, like deploy-tracker) ──────────────────

@app.websocket("/ws/state")
async def ws_state(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(_public_state())
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Use 127.0.0.1 (not "localhost") to avoid browsers auto-upgrading to HTTPS
    # via HSTS — this is an HTTP-only server, and HTTPS would return 400.
    url = "http://127.0.0.1:8001"
    print(f"Starting Magos Configurator at {url}")
    print("  (open it as http://, NOT https:// — this is a plain-HTTP local server)")
    print(f"  factory IPs  : {_hosts_str()}  (your laptop must be on that subnet)")
    print(f"  login        : {cfg['username']}:{'*' * len(cfg['password'])}")
    print(f"  ntp/tz       : {cfg['ntp']} / {cfg['timezone']}")
    print(f"  gw/dns       : {cfg['gateway']} / {cfg['dns']}")
    with contextlib.suppress(Exception):
        webbrowser.open(url)
    uvicorn.run(app, host="127.0.0.1", port=8001, log_level="warning")
