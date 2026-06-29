#!/usr/bin/env python3
"""Magos APU Configurator — iterative APU-provisioning Web UI (FastAPI).

Same iterative workflow as the radar configurator (app.py), but for the AR
Processing Unit (APU): detect a unit on its factory IP (192.168.40.60), pick a
channel, and it sets NTP + timezone, the controlled-radar IP, and the APU's own
static IP — then loops for the next one. Runs independently on its own port.

The shared bench machinery lives in `magos_bench.MagosBench`; this file adds only
the APU specifics. Device talking is delegated to APUClient in apu_configure.py.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel

from bench_core.bench_ui import StepCollector

from apu_configure import (
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
from magos_bench import MagosBench
from magos_configure import set_log_serial, verify_device_at

BASE_DIR = Path(__file__).resolve().parent

DEFAULT_CFG = {
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
    start_channel: Optional[str] = None


class ApuBench(MagosBench):
    title = "Magos APU Configurator"
    port = 8002
    html_file = "apu.html"
    log_file = "apu-config.log"
    log_prefix = "apu_"
    logger_name = "magos"
    device_word = "APU"
    channel_ips = APU_CHANNEL_IPS
    uses_radar_ip = True

    def extra_public_state(self) -> dict:
        return {"controlled_radar_ips": CONTROLLED_RADAR_IPS}

    def resolve_target(self, channel: Optional[str], ip: Optional[str],
                       radar_ip: Optional[str] = None) -> Optional[dict]:
        """For a channel, the APU IP is .6N and the controlled radar is .5N. For
        a manual IP, the radar IP is whatever (optionally) was supplied."""
        ch = (channel or "").strip().lower()
        if ch in APU_CHANNEL_IPS:
            return {"channel": ch, "ip": APU_CHANNEL_IPS[ch],
                    "radar_ip": CONTROLLED_RADAR_IPS[ch]}
        if ip and ip.strip():
            return {"channel": ch or "other", "ip": ip.strip(),
                    "radar_ip": radar_ip.strip() if radar_ip else None}
        return None

    def do_configure(self, target: dict, host: str,
                     avoid_serial: Optional[str]) -> dict:
        """login → identity → NTP/TZ → controlled radar → networking → verify.
        Never raises."""
        ip = target["ip"]
        radar_ip = target.get("radar_ip")
        collector = StepCollector()
        self.log.addHandler(collector)
        set_log_serial(None)

        identity = {"serial": "unknown", "mac": "unknown", "model": "unknown"}
        raw: dict = {}
        error: Optional[str] = None
        ok = skipped = verified = False
        verify_detail: Optional[str] = None
        try:
            self.log.info("Detected APU at %s — starting configuration.", host)
            client = APUClient(host, scheme=self.cfg["scheme"], verify=not self.cfg["insecure"])
            client.login(self.cfg["username"], self.cfg["password"])
            ident = client.get_identity()
            raw = ident.pop("raw", {})
            identity = ident
            if avoid_serial and identity.get("serial") not in (None, "", "unknown") \
                    and identity["serial"] == avoid_serial:
                skipped = True
                self.log.warning(
                    "Same APU as the previous run (SN %s) is still answering on the "
                    "factory IP — skipping. Unplug it before the next one.", avoid_serial)
            else:
                client.set_ntp_tz(self.cfg["ntp"], self.cfg["timezone"])
                if radar_ip:
                    client.set_controlled_radar(radar_ip)
                client.set_network(self.cfg["iface"], ip, self.cfg["netmask"],
                                   self.cfg["gateway"], self.cfg["dns"])
                vres = verify_device_at(ip, scheme=self.cfg["scheme"],
                                        username=self.cfg["username"], password=self.cfg["password"],
                                        expect_substring=ip, verify_tls=not self.cfg["insecure"])
                verified = vres["verified"]
                verify_detail = vres["detail"]
                self.log.info("Configuration complete — APU should now be at %s.", ip)
                ok = True
        except Exception as e:  # MagosError + any requests/network error
            error = str(e)
            self.log.error("Configuration FAILED: %s", e)
        finally:
            self.log.removeHandler(collector)

        steps = collector.steps
        log_text = "\n".join(f"[{s['level']}] [{s['sn']}] {s['msg']}" for s in steps)
        return {
            "ok": ok, "skipped": skipped, "ip": ip, "identity": identity, "raw": raw,
            "steps": steps, "log": log_text, "error": error,
            "verified": verified, "verify_detail": verify_detail,
        }

    def build_entry(self, target: dict, host: str, result: dict) -> dict:
        ident = result["identity"]
        return {
            "channel": target["channel"],
            "ip": result["ip"],
            "radar_ip": target.get("radar_ip") or "—",
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
        radar_ip = target.get("radar_ip")
        verified_note = (" Verified at the new IP." if result["verified"]
                         else f" NOT verified: {result['verify_detail']}.")
        return (f"Configured {ident['model']} (SN {ident['serial']}) as {result['ip']}"
                + (f", controlling radar {radar_ip}" if radar_ip else "")
                + "." + verified_note + " Unplug it and plug in the next one.")

    def register_routes(self, app: FastAPI) -> None:
        @app.post("/api/settings")
        async def update_settings(body: SettingsBody):
            return self.apply_settings(body.model_dump(exclude_unset=True))

        @app.post("/api/configure")
        async def configure(body: ConfigureBody):
            return await self.configure_request(body.channel, body.ip, body.radar_ip)

        @app.post("/api/auto")
        async def set_auto(body: AutoBody):
            return self.set_auto(body.enabled, body.channel, body.ip, body.radar_ip)

        @app.post("/api/cycle")
        async def set_cycle(body: CycleBody):
            return self.set_cycle(body.enabled, body.start_channel)

    def print_banner(self) -> None:
        print(f"  factory IP   : {self._hosts_str()}  (your laptop must be on that subnet)")
        print(f"  login        : {self.cfg['username']}:{'*' * len(self.cfg['password'])}")
        print(f"  ntp/tz       : {self.cfg['ntp']} / {self.cfg['timezone']}")
        print(f"  gw/dns/mask  : {self.cfg['gateway']} / {self.cfg['dns']} / {self.cfg['netmask']}")


configurator = ApuBench(BASE_DIR, DEFAULT_CFG)
app = configurator.build_app()


if __name__ == "__main__":
    configurator.run()
