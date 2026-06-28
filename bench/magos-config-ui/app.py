#!/usr/bin/env python3
"""Magos radar Configurator — iterative radar-provisioning Web UI (FastAPI).

Plug a fresh radar into the laptop (it boots on a factory IP such as
192.168.40.50/.60 with admin:password); the UI detects it, you pick the channel
(0-3) or a manual IP, and it sets NTP + the static IP, then loops for the next
one. Auto / Cycle modes provision hands-free.

All the shared bench machinery (detection loop, auto/cycle state machine,
settings, logging, routes) lives in `magos_bench.MagosBench`; this file adds only
the radar specifics. Device talking is delegated to MagosClient in
magos_configure.py, so the CLI and the UI share one code path.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel

from bench_core.bench_ui import StepCollector

from magos_bench import MagosBench
from magos_configure import (
    CHANNEL_IPS,
    DEFAULT_DNS,
    DEFAULT_GATEWAY,
    DEFAULT_HOST,
    DEFAULT_NETMASK,
    DEFAULT_NTP,
    DEFAULT_PASSWORD,
    DEFAULT_TIMEZONE,
    DEFAULT_USERNAME,
    MagosClient,
    set_log_serial,
    to_cidr,
    verify_device_at,
)

BASE_DIR = Path(__file__).resolve().parent

# Factory IPs a fresh radar may boot on — detection checks each in order.
DEFAULT_HOSTS = [DEFAULT_HOST, "192.168.40.60"]

DEFAULT_CFG = {
    "hosts": list(DEFAULT_HOSTS),
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
    start_channel: Optional[str] = None


class RadarBench(MagosBench):
    title = "Magos Configurator"
    port = 8001
    html_file = "index.html"
    log_file = "magos-config.log"
    log_prefix = ""
    logger_name = "magos"
    device_word = "radar"
    channel_ips = CHANNEL_IPS
    uses_radar_ip = False

    def resolve_target(self, channel: Optional[str], ip: Optional[str],
                       radar_ip: Optional[str] = None) -> Optional[dict]:
        ch = (channel or "").strip().lower()
        if ch in CHANNEL_IPS:
            return {"channel": ch, "ip": CHANNEL_IPS[ch]}
        if ip and ip.strip():
            return {"channel": ch or "other", "ip": ip.strip()}
        return None

    def do_configure(self, target: dict, host: str,
                     avoid_serial: Optional[str]) -> dict:
        """login → identity → NTP → RF channel → networking → verify. Never raises.

        When `channel` is a channel number, the radar's RF channel is set to the
        matching variant before the IP change (firmware >= 3.x; older radars are
        left untouched). Manual-IP runs don't touch the channel.
        """
        ip = target["ip"]
        channel = target["channel"]
        ip_cidr = to_cidr(ip, self.cfg["netmask"])
        collector = StepCollector()
        self.log.addHandler(collector)
        set_log_serial(None)  # reset; get_identity() will set the real serial

        identity = {"serial": "unknown", "mac": "unknown", "model": "unknown"}
        raw: dict = {}
        error: Optional[str] = None
        ok = skipped = verified = False
        verify_detail: Optional[str] = None
        try:
            self.log.info("Detected radar at %s — starting configuration.", host)
            client = MagosClient(host, scheme=self.cfg["scheme"], verify=not self.cfg["insecure"])
            client.login(self.cfg["username"], self.cfg["password"])
            ident = client.get_identity()
            raw = ident.pop("raw", {})
            identity = ident
            if avoid_serial and identity.get("serial") not in (None, "", "unknown") \
                    and identity["serial"] == avoid_serial:
                skipped = True
                self.log.warning(
                    "Same radar as the previous run (SN %s) is still answering on the "
                    "factory IP — skipping. Unplug it before the next one.", avoid_serial)
            else:
                if self.cfg["ntp"]:
                    client.set_ntp(self.cfg["ntp"], self.cfg["timezone"])
                # Set the RF channel before networking — the IP change drops the link.
                if channel in CHANNEL_IPS:
                    client.set_channel(channel)
                client.set_network(ip_cidr, self.cfg["gateway"], self.cfg["dns"])
                vres = verify_device_at(ip_cidr, scheme=self.cfg["scheme"],
                                        username=self.cfg["username"], password=self.cfg["password"],
                                        expect_substring=ip.split("/")[0],
                                        verify_tls=not self.cfg["insecure"])
                verified = vres["verified"]
                verify_detail = vres["detail"]
                self.log.info("Configuration complete — radar should now be at %s.", ip_cidr)
                ok = True
        except Exception as e:  # MagosError + any requests/network error
            error = str(e)
            self.log.error("Configuration FAILED: %s", e)
        finally:
            self.log.removeHandler(collector)

        steps = collector.steps
        log_text = "\n".join(f"[{s['level']}] [{s['sn']}] {s['msg']}" for s in steps)
        return {
            "ok": ok, "skipped": skipped, "ip": ip_cidr, "identity": identity,
            "raw": raw, "steps": steps, "log": log_text, "error": error,
            "verified": verified, "verify_detail": verify_detail,
        }

    def build_entry(self, target: dict, host: str, result: dict) -> dict:
        ident = result["identity"]
        return {
            "channel": target["channel"],
            "ip": result["ip"],
            "from_host": host,
            "ntp": self.cfg["ntp"],
            "timezone": self.cfg["timezone"],
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

    def success_message(self, ident: dict, result: dict, target: dict) -> str:
        verified_note = (" Verified at the new IP." if result["verified"]
                         else f" NOT verified: {result['verify_detail']}.")
        return (f"Configured {ident['model']} (SN {ident['serial']}) as {result['ip']}."
                + verified_note + " Unplug it and plug in the next one.")

    def register_routes(self, app: FastAPI) -> None:
        @app.post("/api/settings")
        async def update_settings(body: SettingsBody):
            return self.apply_settings(body.dict(exclude_unset=True))

        @app.post("/api/configure")
        async def configure(body: ConfigureBody):
            return await self.configure_request(body.channel, body.ip)

        @app.post("/api/auto")
        async def set_auto(body: AutoBody):
            return self.set_auto(body.enabled, body.channel, body.ip)

        @app.post("/api/cycle")
        async def set_cycle(body: CycleBody):
            return self.set_cycle(body.enabled, body.start_channel)

    def print_banner(self) -> None:
        print(f"  factory IPs  : {self._hosts_str()}  (your laptop must be on that subnet)")
        print(f"  login        : {self.cfg['username']}:{'*' * len(self.cfg['password'])}")
        print(f"  ntp/tz       : {self.cfg['ntp']} / {self.cfg['timezone']}")
        print(f"  gw/dns       : {self.cfg['gateway']} / {self.cfg['dns']}")


configurator = RadarBench(BASE_DIR, DEFAULT_CFG)
app = configurator.build_app()


if __name__ == "__main__":
    configurator.run()
