#!/usr/bin/env python3
"""Magos APU Configurator — iterative APU-provisioning Web UI (FastAPI).

Same iterative workflow as the radar configurator (app.py), but for the AR
Processing Unit (APU): detect a unit on its factory IP (192.168.40.60), pick
which APU it is (0 or 1), and it sets NTP + timezone, the two controlled
radars, and the APU's own static IP — then loops for the next one. Runs
independently on its own port.

Requires APU firmware 3.1.2 (rc builds accepted) — the multi-radar firmware
where one APU controls two radars, so a full system is 4 radars + 2 APUs.
Older units are refused with a message asking the operator to upgrade first.

The shared bench machinery lives in `magos_bench.MagosBench`; this file adds only
the APU specifics. Device talking is delegated to APUClient in apu_configure.py.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel

from bench_core.bench_ui import StepCollector
from bench_core.run_record import build_run_entry

from apu_configure import (
    APUClient,
    APU_CHANNEL_IPS,
    APU_RADAR_ASSIGNMENTS,
    DEFAULT_DNS,
    DEFAULT_GATEWAY,
    DEFAULT_HOST,
    DEFAULT_IFACE,
    DEFAULT_NETMASK,
    DEFAULT_NTP,
    DEFAULT_PASSWORD,
    DEFAULT_TIMEZONE,
    DEFAULT_USERNAME,
    firmware_error,
    firmware_ok,
    firmware_version,
    radars_from_ips,
)
from magos_bench import MagosBench
from magos_configure import (
    MagosError,
    load_factory_defaults,
    set_log_serial,
    verify_device_at,
)

BASE_DIR = Path(__file__).resolve().parent

# From the config file's "hosts" when set (the UI's settings editor persists
# there); a fresh file falls back to the APU factory IP.
_cfg = load_factory_defaults("apu")

DEFAULT_CFG = {
    "hosts": list(_cfg.get("hosts") or [DEFAULT_HOST]),   # APU factory IP(s) to watch
    "scheme": _cfg.get("scheme", "http"),
    "insecure": bool(_cfg.get("insecure", False)),
    "username": DEFAULT_USERNAME,
    "password": DEFAULT_PASSWORD,
    "ntp": DEFAULT_NTP,
    "timezone": DEFAULT_TIMEZONE,
    "gateway": DEFAULT_GATEWAY,
    "dns": DEFAULT_DNS,
    "netmask": DEFAULT_NETMASK,
    "iface": DEFAULT_IFACE,
}
del _cfg


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
    radar_ips: Optional[str] = None      # comma-separated, manual targets only


class AutoBody(BaseModel):
    enabled: bool
    channel: Optional[str] = None
    ip: Optional[str] = None
    radar_ips: Optional[str] = None


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
    config_section = "apu"     # UI settings edits persist into this file section

    def extra_public_state(self) -> dict:
        return {"apu_radars": APU_RADAR_ASSIGNMENTS}

    @staticmethod
    def _radars_summary(radars: list) -> Optional[str]:
        """One human-readable line, e.g. 'radar_0=192.168.88.50, radar_1=...'
        — stored as device.radar_ip so history/central columns stay simple."""
        if not radars:
            return None
        return ", ".join(f"{r['radar_id']}={r['ip']}" for r in radars)

    def resolve_target(self, channel: Optional[str], ip: Optional[str],
                       radar_ips: Optional[str] = None) -> Optional[dict]:
        """APU 0/1 maps to a fixed IP + its two assigned radars. For a manual
        IP, the radars are whatever (optionally) was supplied, comma-separated,
        with IDs assigned radar_0, radar_1, ... in order."""
        ch = (channel or "").strip().lower()
        if ch in APU_CHANNEL_IPS:
            return {"channel": ch, "ip": APU_CHANNEL_IPS[ch],
                    "radars": [dict(r) for r in APU_RADAR_ASSIGNMENTS[ch]]}
        if ip and ip.strip():
            ips = [s.strip() for s in (radar_ips or "").split(",") if s.strip()]
            return {"channel": ch or "other", "ip": ip.strip(),
                    "radars": radars_from_ips(ips)}
        return None

    def do_configure(self, target: dict, host: str,
                     avoid_serial: Optional[str]) -> dict:
        """login → identity → firmware gate → NTP/TZ → controlled radars →
        networking → verify. Never raises."""
        ip = target["ip"]
        radars = target.get("radars") or []
        collector = StepCollector()
        self.log.addHandler(collector)
        set_log_serial(None)

        identity = {"serial": "unknown", "mac": "unknown", "model": "unknown"}
        raw: dict = {}
        firmware: Optional[str] = None
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
            firmware = firmware_version(raw)
            if avoid_serial and identity.get("serial") not in (None, "", "unknown") \
                    and identity["serial"] == avoid_serial:
                skipped = True
                self.log.warning(
                    "Same APU as the previous run (SN %s) is still answering on the "
                    "factory IP — skipping. Unplug it before the next one.", avoid_serial)
            else:
                # Refuse pre-3.1.2 units BEFORE changing anything — the
                # multi-radar assignment below only exists on 3.1.2+.
                if not firmware_ok(firmware):
                    raise MagosError(firmware_error(firmware))
                self.log.info("Firmware %s — OK.", firmware)
                client.set_ntp_tz(self.cfg["ntp"], self.cfg["timezone"])
                if radars:
                    client.set_radars(radars)
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
            "firmware": firmware, "steps": steps, "log": log_text, "error": error,
            "verified": verified, "verify_detail": verify_detail,
        }

    def build_entry(self, target: dict, host: str, result: dict,
                    duration: int) -> dict:
        ident = result["identity"]
        radars = target.get("radars") or []
        return build_run_entry(
            tool="magos-apu",
            ok=result["ok"],
            error=result["error"],
            serial=ident["serial"],
            mac=ident["mac"],
            model=ident["model"],
            firmware=result.get("firmware"),
            duration_s=duration,
            verified=result["verified"],
            verify_detail=result["verify_detail"],
            steps=result["steps"],
            log=result["log"],
            device={
                "channel": target["channel"],
                "ip": result["ip"],
                "radars": radars,
                "radar_ip": self._radars_summary(radars) or "—",
                "from_host": host,
                "ntp": self.cfg["ntp"],
                "timezone": self.cfg["timezone"],
            },
        )

    def success_message(self, ident: dict, result: dict, target: dict) -> str:
        radars = self._radars_summary(target.get("radars") or [])
        verified_note = (" Verified at the new IP." if result["verified"]
                         else f" NOT verified: {result['verify_detail']}.")
        return (f"Configured {ident['model']} (SN {ident['serial']}) as {result['ip']}"
                + (f", controlling {radars}" if radars else "")
                + "." + verified_note + " Unplug it and plug in the next one.")

    def register_routes(self, app: FastAPI) -> None:
        @app.post("/api/settings")
        async def update_settings(body: SettingsBody):
            return self.apply_settings(body.model_dump(exclude_unset=True))

        @app.post("/api/configure")
        async def configure(body: ConfigureBody):
            return await self.configure_request(body.channel, body.ip, body.radar_ips)

        @app.post("/api/auto")
        async def set_auto(body: AutoBody):
            return self.set_auto(body.enabled, body.channel, body.ip, body.radar_ips)

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
