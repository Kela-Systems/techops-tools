#!/usr/bin/env python3
"""Magos APU Configurator – iterative APU-provisioning Web UI (FastAPI).

Same iterative workflow as the radar configurator (app.py), but for the AR
Processing Unit (APU): detect a unit on its factory IP (192.168.40.60), pick a
channel, and it sets NTP + timezone, the controlled-radar IP, and the APU's own
static IP — then loops for the next one. Runs independently on its own port.

Device talking is delegated to APUClient in apu_configure.py.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))

from apu_configure import (  # noqa: E402
    APUClient,
    APU_CHANNEL_IPS,
    CONTROLLED_RADAR_IPS,
    DEFAULT_DNS,
    DEFAULT_GATEWAY,
    DEFAULT_HOST,
    DEFAULT_IFACE,
    DEFAULT_NETMASK,
    DEFAULT_NTP,
    DEFAULT_PASSWORD,
    DEFAULT_TIMEZONE,
    DEFAULT_USERNAME,
)
from magos_configure import (  # noqa: E402
    LOG_LINE_FORMAT,
    is_on_link,
    log as device_logger,
    probe_http,
    set_log_serial,
    verify_device_at,
)

BASE_DIR = Path(__file__).resolve().parent
POLL_INTERVAL_SEC = 2.0
DETECT_TIMEOUT_SEC = 1.0
PORT = 8002
# A device must miss this many consecutive polls before we treat it as
# unplugged — a single blip during its IP change must not restart the cycle.
MISS_THRESHOLD = 3
HISTORY_MAX = 200

# ── Logging: rolling APU log + structured per-unit JSON, like the radar app.
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

device_logger.setLevel(logging.INFO)
device_logger.propagate = False
if not any(isinstance(h, logging.FileHandler) for h in device_logger.handlers):
    _rolling = logging.handlers.RotatingFileHandler(
        LOG_DIR / "apu-config.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    _rolling.setFormatter(logging.Formatter("%(asctime)s " + LOG_LINE_FORMAT))
    device_logger.addHandler(_rolling)
    _console = logging.StreamHandler()
    _console.setFormatter(logging.Formatter(LOG_LINE_FORMAT))
    device_logger.addHandler(_console)


class _StepCollector(logging.Handler):
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


app = FastAPI(title="Magos APU Configurator", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


cfg: dict = {
    "hosts": [DEFAULT_HOST],         # APU factory IP(s) to watch
    "scheme": "http",
    "insecure": False,
    "username": DEFAULT_USERNAME,
    "password": DEFAULT_PASSWORD,
    "ntp": DEFAULT_NTP,
    "timezone": DEFAULT_TIMEZONE,
    "gateway": DEFAULT_GATEWAY,
    "dns": DEFAULT_DNS,
    "netmask": DEFAULT_NETMASK,
    "iface": DEFAULT_IFACE,
}


def _hosts_str() -> str:
    return " / ".join(cfg["hosts"]) or "(no hosts set)"


state: dict = {
    "phase": "waiting",
    "detected": False,
    "active_host": None,
    "busy": False,
    "message": f"Waiting for an APU at {_hosts_str()}...",
    "last_result": None,
    "history": [],
    "auto": {"enabled": False, "channel": None, "ip": None, "radar_ip": None},
    # Cycle mode: APUs are provisioned in groups of 4. Each detected APU takes
    # the next channel (0->1->2->3, wrapping for the next group). `index` is the
    # channel the NEXT detected APU will get; `count` is total configured.
    "cycle": {"enabled": False, "index": 0, "count": 0},
    # Serial of the last successfully configured APU — auto/cycle mode refuses
    # to reconfigure the same unit if it briefly reappears on the factory IP.
    "last_ok_serial": None,
    # Set when no local adapter can reach the factory subnet (shown in the UI).
    "net_warning": None,
}

CYCLE_CHANNELS = list(APU_CHANNEL_IPS)  # ["0","1","2","3"]


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
    iface: Optional[str] = None


class ConfigureBody(BaseModel):
    channel: Optional[str] = None
    ip: Optional[str] = None
    radar_ip: Optional[str] = None


class AutoBody(BaseModel):
    enabled: bool
    channel: Optional[str] = None
    ip: Optional[str] = None
    radar_ip: Optional[str] = None


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
    """First factory host where a dashboard answers HTTP (not just an open port)."""
    for candidate in cfg["hosts"]:
        if probe_http(candidate, scheme=cfg["scheme"], timeout=DETECT_TIMEOUT_SEC,
                      verify=not cfg["insecure"]):
            return candidate
    return None


def _net_warning() -> Optional[str]:
    """Warn when no local adapter sits on the factory subnet — without this,
    a wrong NIC setup is indistinguishable from 'no APU plugged in'."""
    if not cfg["hosts"]:
        return "No factory hosts configured."
    for candidate in cfg["hosts"]:
        host, _ = _split_host_port(candidate, cfg["scheme"])
        if is_on_link(host):
            return None
    return (f"This PC has no network adapter on the factory subnet ({_hosts_str()}) — "
            "APUs cannot be detected. Set the adapter to a static 192.168.40.x address.")


def _resolve_target(channel: Optional[str], ip: Optional[str], radar_ip: Optional[str]):
    """Return (apu_ip, radar_ip, channel_label) or (None, None, None).

    For a channel, the APU IP is .6N and the controlled radar is auto-set to .5N.
    For a manual IP, the radar IP is whatever (optionally) was supplied.
    """
    ch = (channel or "").strip().lower()
    if ch in APU_CHANNEL_IPS:
        return APU_CHANNEL_IPS[ch], CONTROLLED_RADAR_IPS[ch], ch
    if ip and ip.strip():
        return ip.strip(), (radar_ip.strip() if radar_ip else None), (ch or "other")
    return None, None, None


def do_configure(ip: str, host: str, radar_ip: Optional[str],
                 avoid_serial: Optional[str] = None) -> dict:
    """Run login -> identity -> NTP/TZ -> controlled radar -> networking -> verify.
    Never raises.

    If `avoid_serial` matches the device's serial, the run is skipped — auto and
    cycle mode pass the last configured serial so a unit that briefly reappears
    on the factory IP (slow to apply its new address) isn't configured twice.
    """
    collector = _StepCollector()
    device_logger.addHandler(collector)
    set_log_serial(None)

    identity = {"serial": "unknown", "mac": "unknown", "model": "unknown"}
    raw: dict = {}
    error: Optional[str] = None
    ok = False
    skipped = False
    verified = False
    verify_detail: Optional[str] = None
    try:
        device_logger.info("Detected APU at %s — starting configuration.", host)
        client = APUClient(host, scheme=cfg["scheme"], verify=not cfg["insecure"])
        client.login(cfg["username"], cfg["password"])
        ident = client.get_identity()
        raw = ident.pop("raw", {})
        identity = ident
        if avoid_serial and identity.get("serial") not in (None, "", "unknown") \
                and identity["serial"] == avoid_serial:
            skipped = True
            device_logger.warning(
                "Same APU as the previous run (SN %s) is still answering on the "
                "factory IP — skipping. Unplug it before the next one.", avoid_serial)
        else:
            client.set_ntp_tz(cfg["ntp"], cfg["timezone"])
            if radar_ip:
                client.set_controlled_radar(radar_ip)
            client.set_network(cfg["iface"], ip, cfg["netmask"], cfg["gateway"], cfg["dns"])
            vres = verify_device_at(ip, scheme=cfg["scheme"],
                                    username=cfg["username"], password=cfg["password"],
                                    expect_substring=ip, verify_tls=not cfg["insecure"])
            verified = vres["verified"]
            verify_detail = vres["detail"]
            device_logger.info("Configuration complete — APU should now be at %s.", ip)
            ok = True
    except Exception as e:  # MagosError + any requests/network error
        error = str(e)
        device_logger.error("Configuration FAILED: %s", e)
    finally:
        device_logger.removeHandler(collector)

    steps = collector.steps
    log_text = "\n".join(f"[{s['level']}] [{s['sn']}] {s['msg']}" for s in steps)
    return {
        "ok": ok, "skipped": skipped, "ip": ip, "identity": identity, "raw": raw,
        "steps": steps, "log": log_text, "error": error,
        "verified": verified, "verify_detail": verify_detail,
    }


def _save_apu_log(entry: dict, raw: dict) -> Optional[str]:
    ts = datetime.now(timezone.utc)
    name = f"apu_{ts.strftime('%Y%m%d-%H%M%S')}_{_slug(entry.get('serial') or 'unknown')}_{entry.get('status')}.json"
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
        "channel_ips": APU_CHANNEL_IPS,
        "controlled_radar_ips": CONTROLLED_RADAR_IPS,
    }


async def _run_configuration(channel: str, ip: str, host: str, radar_ip: Optional[str],
                             guard_repeat: bool = False) -> Optional[dict]:
    state["busy"] = True
    state["phase"] = "configuring"
    state["message"] = f"Configuring APU ({host}) as {ip}..."
    avoid = state["last_ok_serial"] if guard_repeat else None

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, do_configure, ip, host, radar_ip, avoid)
    ident = result["identity"]

    if result["skipped"]:
        state["busy"] = False
        state["phase"] = "configured"
        state["message"] = (
            f"APU SN {avoid} was already configured and is still answering on {host} — "
            "unplug it and plug in the next one."
        )
        return None

    entry = {
        "channel": channel,
        "ip": result["ip"],
        "radar_ip": radar_ip or "—",
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
    entry["log_file"] = _save_apu_log(entry, result["raw"])

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
            f"Configured {ident['model']} (SN {ident['serial']}) as {result['ip']}"
            + (f", controlling radar {radar_ip}" if radar_ip else "")
            + "." + verified_note + " Unplug it and plug in the next one."
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
        pass
    elif reachable:
        if phase in ("waiting", "detected") and cycle["enabled"]:
            channel = CYCLE_CHANNELS[cycle["index"]]
            apu_ip, radar_ip, _ = _resolve_target(channel, None, None)
            entry = await _run_configuration(channel, apu_ip, active, radar_ip,
                                             guard_repeat=True)
            if entry and entry["status"] == "ok":
                cycle["count"] += 1
                cycle["index"] = (cycle["index"] + 1) % len(CYCLE_CHANNELS)
        elif phase in ("waiting", "detected") and auto["enabled"]:
            apu_ip, radar_ip, channel = _resolve_target(auto["channel"], auto["ip"], auto["radar_ip"])
            if apu_ip:
                await _run_configuration(channel, apu_ip, active, radar_ip,
                                         guard_repeat=True)
            elif phase != "detected":
                state["phase"] = "detected"
                state["message"] = f"APU detected at {active} (auto armed, but no target set)."
        elif phase == "waiting":
            state["phase"] = "detected"
            state["message"] = f"APU detected at {active}. Pick a channel and configure it."
    else:
        if phase in ("detected", "configured", "error") and _misses >= MISS_THRESHOLD:
            state["phase"] = "waiting"
            if cycle["enabled"]:
                nxt = CYCLE_CHANNELS[cycle["index"]]
                state["message"] = (
                    f"Cycle mode ON — plug in the next APU (it will be channel {nxt} "
                    f"→ {APU_CHANNEL_IPS[nxt]})."
                )
            elif auto["enabled"]:
                state["message"] = "Auto mode ON — plug in the next APU..."
            else:
                state["message"] = "Plug in the next APU..."


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
    return FileResponse(str(BASE_DIR / "static" / "apu.html"))


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
        state["message"] = f"Waiting for an APU at {_hosts_str()}..."
    return _public_state()


@app.post("/api/configure")
async def configure(body: ConfigureBody):
    if state["phase"] != "detected":
        return {"error": "No APU is currently detected to configure."}
    if state["busy"]:
        return {"error": "A configuration is already in progress."}

    apu_ip, radar_ip, channel = _resolve_target(body.channel, body.ip, body.radar_ip)
    if not apu_ip:
        return {"error": "Provide a channel (0-3) or a manual IP."}

    host = state["active_host"] or (cfg["hosts"][0] if cfg["hosts"] else None)
    if not host:
        return {"error": "No factory host configured."}

    await _run_configuration(channel, apu_ip, host, radar_ip)
    return _public_state()


@app.post("/api/auto")
async def set_auto(body: AutoBody):
    apu_ip, radar_ip, channel = _resolve_target(body.channel, body.ip, body.radar_ip)
    if body.enabled and not apu_ip:
        return {"error": "Pick a channel or enter an IP before turning on auto mode."}

    state["auto"] = {"enabled": body.enabled, "channel": body.channel,
                     "ip": body.ip, "radar_ip": body.radar_ip}
    if body.enabled:
        state["cycle"]["enabled"] = False   # auto + cycle are mutually exclusive
        state["message"] = (
            f"Auto mode ON — the next APU will be configured as {apu_ip}"
            + (f" (radar {radar_ip})" if radar_ip else "") + "."
        )
    elif state["phase"] not in ("configuring",):
        state["message"] = (
            f"Auto mode off. APU detected at {state['active_host']}."
            if state["detected"] else f"Waiting for an APU at {_hosts_str()}..."
        )
    return _public_state()


@app.post("/api/cycle")
async def set_cycle(body: CycleBody):
    if body.enabled:
        # Starting a cycle: begin at channel 0, and turn off plain auto mode.
        state["cycle"] = {"enabled": True, "index": 0, "count": 0}
        state["auto"] = {"enabled": False, "channel": None, "ip": None, "radar_ip": None}
        first = CYCLE_CHANNELS[0]
        state["message"] = (
            f"Cycle started — plug in APUs one by one. First → channel {first} "
            f"(APU {APU_CHANNEL_IPS[first]} / radar {CONTROLLED_RADAR_IPS[first]})."
        )
    else:
        state["cycle"]["enabled"] = False
        if state["phase"] not in ("configuring",):
            state["message"] = (
                f"Cycle stopped. APU detected at {state['active_host']}."
                if state["detected"] else f"Waiting for an APU at {_hosts_str()}..."
            )
    return _public_state()


@app.post("/api/dismiss")
async def dismiss():
    state["last_result"] = None
    state["phase"] = "detected" if state["detected"] else "waiting"
    state["message"] = (
        f"APU detected at {state['active_host']}. Pick a channel and configure it."
        if state["detected"]
        else f"Waiting for an APU at {_hosts_str()}..."
    )
    return _public_state()


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
    url = f"http://127.0.0.1:{PORT}"
    print(f"Starting Magos APU Configurator at {url}")
    print("  (open it as http://, NOT https:// — this is a plain-HTTP local server)")
    print(f"  factory IP   : {_hosts_str()}  (your laptop must be on that subnet)")
    print(f"  login        : {cfg['username']}:{'*' * len(cfg['password'])}")
    print(f"  ntp/tz       : {cfg['ntp']} / {cfg['timezone']}")
    print(f"  gw/dns/mask  : {cfg['gateway']} / {cfg['dns']} / {cfg['netmask']}")
    with contextlib.suppress(Exception):
        webbrowser.open(url)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
