"""S0.6 — who is in this unit: serial, model, firmware and MAC of every part.

Replaces a per-unit site.yaml. Everything is read off the devices themselves
(through the session's SOCKS forward, or SSH tunnelled through it) and written
into the run record and the PDF cover, so the report documents which serials
shipped in this unit. The radar/APU systemStatus payloads are kept as the
baseline S6.12 compares link counters against.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from .access import socks_http
from .context import Context
from .devices import (DeviceError, Magos, PlanetWeb, Raythink, Speaker,
                      magos_identity, teltonika_identity)


@dataclass
class Part:
    role: str            # radar_0, apu_.60, camera, speaker, router, modem, switch, poe_switch
    kind: str
    ip: str
    serial: str = "unknown"
    model: str = "unknown"
    firmware: str = "unknown"
    mac: str = "unknown"
    error: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        last = self.ip.rsplit(".", 1)[1]
        return f"{self.role} .{last}" if self.ip.startswith("192.168.88.") else f"{self.role} {self.ip}"

    @property
    def missing(self) -> list[str]:
        return [k for k in ("serial", "model", "firmware")
                if getattr(self, k) in ("", "unknown", None)]

    @property
    def identified(self) -> bool:
        return not self.error and all(v not in ("", "unknown", None)
                                      for v in (self.serial, self.model, self.firmware))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["identified"] = self.identified
        return d


def friendly_error(e: BaseException) -> str:
    """What a probe failure means, in words — not the transport library's."""
    text = f"{type(e).__name__}: {e}"
    low = text.lower()
    if "malformed reply" in low or "proxyerror" in low or "socks" in low:
        return "no answer on its web interface"
    if "timeout" in low or "timed out" in low:
        return "timed out on its web interface"
    if "protocol banner" in low or "no existing session" in low or "connection closed" in low \
            or "eof" in low or "unable to connect" in low or "no answer on ssh" in low:
        return "no answer on SSH (port 22)"
    if "login refused" in low or "authentication" in low:
        return str(e)
    return text


def _plan_parts(ctx: Context) -> list[Part]:
    plan = ctx.release.plan
    parts = [Part(rid, "radar", ip) for rid, ip in plan.radars.items()]
    parts += [Part(f"apu_{ip.rsplit('.', 1)[1]}", "apu", ip) for ip in plan.apus]
    parts += [Part("camera", "camera", plan.camera), Part("speaker", "speaker", plan.speaker),
              Part("router", "router", plan.router), Part("modem", "modem", plan.modem),
              Part("switch", "switch", plan.switch), Part("poe_switch", "poe_switch", plan.poe_switch)]
    return parts


def _probe(ctx: Context, part: Part) -> Part:
    socks = ctx.session.socks_url
    try:
        if part.kind in ("radar", "apu"):
            with socks_http.client(socks) as http:
                m = Magos(http, part.ip, ctx.creds.device("apu" if part.kind == "apu" else "magos"))
                m.login()
                status = m.system_status()
            ident = magos_identity(status)
            part.extra = {"hostname": ident["hostname"], "uptime": ident["uptime"],
                          "counters": ident["counters"]}
            ctx.facts.setdefault("magos_status", {})[part.ip] = status
        elif part.kind == "camera":
            with socks_http.client(socks) as http:
                cam = Raythink(http, part.ip, ctx.creds.device("camera"), ctx.redact.add)
                cam.login()
                ident = cam.identity()
        elif part.kind == "speaker":
            with socks_http.client(socks) as http:
                spk = Speaker(http, part.ip, ctx.creds.device("speaker"))
                spk.login()
                ident = spk.identity()
            part.extra = {"netip": ident.pop("netip", None)}
        elif part.kind in ("router", "modem", "switch"):
            ident = teltonika_identity(ctx.device_ssh(part.ip, "teltonika"))
            part.extra = {"hostname": ident.pop("hostname", None)}
        elif part.kind == "poe_switch":
            with socks_http.client(socks) as http:
                web = PlanetWeb(http, part.ip, ctx.creds.device("planet"))
                web.login()
                ident = web.identity()
            part.extra = {"power_inputs": ident.pop("power_inputs")}
            ctx.facts["poe_power_inputs"] = part.extra["power_inputs"]
        else:
            raise DeviceError(f"no identity probe for {part.kind}")
    except Exception as e:  # noqa: BLE001 — one bad device must not sink the others
        part.error = ctx.redact(friendly_error(e))
        part.extra["raw_error"] = ctx.redact(f"{type(e).__name__}: {e}")[:300]
        return part
    for key in ("serial", "model", "firmware", "mac"):
        setattr(part, key, str(ident.get(key) or "unknown"))
    return part


def discover(ctx: Context, progress: Optional[Callable[[Part], None]] = None) -> list[Part]:
    parts = _plan_parts(ctx)
    with ThreadPoolExecutor(max_workers=6) as pool:
        done = list(pool.map(lambda p: _probe(ctx, p), parts))
    for p in done:
        if progress:
            progress(p)
    ctx.facts["inventory"] = [p.to_dict() for p in done]
    ctx.facts["mac_to_role"] = {p.mac: p.role for p in done if p.mac not in ("", "unknown")}
    return done
