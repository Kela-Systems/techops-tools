#!/usr/bin/env python3
"""Provision-ISR IP speaker configurator — bench Web UI (FastAPI).

Workflow (one speaker at a time):
  1. Connect a speaker to the bench network. It arrives on DHCP (admin/123456),
     so there is no fixed factory IP — the UI sweeps the bench subnets
     (192.168.1/2/88.0/24) for a host serving the speaker's landing page.
  2. The UI shows the detected address + MAC; press Configure.
  3. Full pipeline: set password -> NTP -> upload the media file -> static IP
     (192.168.88.70, LAST — the connection moves) -> verify on the new address.
  4. Unplug it and connect the next speaker.

The common bench-UI shell (run machinery, routes, WebSocket, step logging)
lives in bench_core.bench_ui; this file adds only the speaker specifics: the
DHCP subnet scan, the fixed-target configure route, and the pipeline call.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from bench_core.bench_ui import TERMINAL_PHASES, BenchConfigurator, read_device_mac
from bench_core.run_record import build_run_entry

from speaker_client import (
    DEFAULT_GATEWAY,
    DEFAULT_NETMASK,
    DEFAULT_NTP_SERVER,
    DEFAULT_SCHEME,
    DEFAULT_STATIC_IP,
    DEFAULT_USERNAME,
    SpeakerClient,
    SpeakerError,
)
from speaker_configure import (
    DEFAULT_HTTP_PORT,
    DEFAULT_SCAN_SUBNETS,
    configure_speaker,
    device_name,
    resolve_media,
    scan_for_speaker,
    verify_speaker,
)

BASE_DIR = Path(__file__).resolve().parent


class SpeakerConfigurator(BenchConfigurator):
    title = "Provision-ISR Speaker Configurator"
    port = 8006
    html_file = "speaker.html"
    config_filename = "config/speaker.config.json"
    log_filename = "speaker-config.log"
    logger_name = "speaker"
    tailscale_label = "speaker"
    history_limit = 30
    verify_supported = True    # mutation-free re-check of a finished speaker (TEC-348)
    record_tool = "speaker"

    # ── config helpers ────────────────────────────────────────────────────────

    def _static(self) -> dict:
        return self.cfg.get("static", {}) or {}

    def _target_ip(self) -> str:
        return self._static().get("ip", DEFAULT_STATIC_IP)

    def _scan_subnets(self) -> list[str]:
        return self.cfg.get("scan_subnets", DEFAULT_SCAN_SUBNETS)

    def _media_status(self) -> tuple[str, bool]:
        """(display name, found) for the configured media file."""
        rel = (self.cfg.get("media_file") or "").strip()
        if not rel:
            return "(none configured)", False
        try:
            path = resolve_media(self.cfg)
        except SpeakerError:
            return f"{rel} (MISSING)", False
        return path.name, True

    # ── state ─────────────────────────────────────────────────────────────────

    def initial_state(self) -> dict:
        return {
            "phase": "waiting",        # waiting|detected|configuring|configured|
                                       # verifying|verified|error
            "detected": False,
            "active_host": None,
            "active_mac": None,
            "busy": False,
            "message": "Connect the first speaker (it arrives on DHCP — scanning "
                       + ", ".join(self._scan_subnets()) + ")…",
            "last_result": None,
            "history": [],
        }

    def extra_public_state(self) -> dict:
        media_name, media_ok = self._media_status()
        static = self._static()
        ntp = self.cfg.get("ntp", {}) or {}
        return {
            "scan_subnets": self._scan_subnets(),
            "target_ip": self._target_ip(),
            "gateway": static.get("gateway", DEFAULT_GATEWAY),
            "netmask": static.get("netmask", DEFAULT_NETMASK),
            "ntp_server": ntp.get("server", DEFAULT_NTP_SERVER),
            "media_file": media_name,
            "media_found": media_ok,
        }

    def reload(self) -> str:
        msg = super().reload()
        media_name, media_ok = self._media_status()
        return f"{msg} Media file: {media_name}{'' if media_ok else ' — fix it before configuring.'}"

    # ── pipeline hooks ────────────────────────────────────────────────────────

    def hostname_for(self, inputs: dict) -> str:
        return device_name(self._target_ip())

    def client_host(self, inputs: dict) -> str:
        return inputs.get("host") or self.cfg.get("host", "")

    def build_client(self, run_cfg: dict, host: str):
        return SpeakerClient(host=host,
                             username=run_cfg.get("username", DEFAULT_USERNAME),
                             scheme=run_cfg.get("scheme", DEFAULT_SCHEME))

    def run_pipeline(self, client, run_cfg: dict, inputs: dict) -> dict:
        return configure_speaker(client, settings=run_cfg,
                                 media_path=inputs.get("media_path") or None)

    def verify_pipeline(self, client, run_cfg: dict, inputs: dict) -> dict:
        """Mutation-free re-check of a finished speaker (TEC-348). No resolver:
        every expectation a speaker has is station-wide, so the config is the
        expectation and there is no per-unit record to consult."""
        return verify_speaker(client, settings=run_cfg,
                              media_path=inputs.get("media_path") or None)

    def verify_inputs(self, body) -> dict:
        """The media file the pipeline should expect in the slot. A missing or
        unconfigured file leaves the row out rather than failing it — the file
        not being on THIS bench PC says nothing about the speaker."""
        inputs = super().verify_inputs(body)
        try:
            media = resolve_media(self.cfg)
        except SpeakerError:
            media = None
        inputs["media_path"] = str(media) if media else ""
        return inputs

    def verify_label(self, inputs: dict) -> str:
        return f"the speaker on {inputs.get('host') or 'the bench network'}"

    def build_entry(self, result: dict, inputs: dict, duration: int) -> dict:
        ident = result["identity"]
        return build_run_entry(
            tool="speaker",
            ok=result["ok"],
            error=result["error"],
            serial=ident.get("serial", "unknown"),
            mac=ident.get("mac") or inputs.get("mac") or "unknown",
            model=ident.get("model", "Provision-ISR speaker"),
            firmware=ident.get("firmware", "unknown"),
            duration_s=duration,
            verification=result.get("verification", []),
            warnings=result.get("warnings", []),
            steps=result["steps"],
            log=result["log"],
            device={
                "hostname": result["hostname"],
                "ip": self._target_ip(),
                "from_host": inputs.get("host", ""),
            },
        )

    def success_message(self, result: dict, entry: dict, took: str) -> str:
        return (f"Configured speaker at {entry['device']['ip']} (SN {entry['serial']}) "
                f"in {took}. Unplug it and connect the next one.")

    def verify_message(self, result: dict, entry: dict, took: str) -> str:
        return (f"Speaker {entry['serial']} PASSED verification in {took} — nothing "
                "was changed. Unplug it and connect the next one.")

    def dismiss_message(self) -> str:
        return "Connect the next speaker…"

    # ── detection loop (DHCP — subnet sweep) ──────────────────────────────────

    def _find_speaker(self) -> Optional[str]:
        """The scan runs in an executor thread; check the last-seen address (or
        the target static IP, for re-runs) first so re-detection is instant."""
        guess = self.state.get("active_host") or self._target_ip()
        return scan_for_speaker(self._scan_subnets(),
                                int(self.cfg.get("http_port", DEFAULT_HTTP_PORT)),
                                self.cfg.get("scheme", DEFAULT_SCHEME),
                                first_guess=guess)

    async def poll_once(self, loop) -> None:
        if self.state["busy"]:
            return  # mid-run the speaker changes IP — leave detection alone

        host = await loop.run_in_executor(None, self._find_speaker)
        self.state["detected"] = bool(host)

        if host:
            self.state["active_host"] = host
            mac = await loop.run_in_executor(None, read_device_mac, host.split(":")[0])
            self.state["active_mac"] = mac
            if self.state["phase"] in TERMINAL_PHASES:
                return  # a run finished but a speaker is still visible
            self.state["phase"] = "detected"
            self.state["message"] = (f"Speaker detected on {host} (MAC {mac or 'unknown'}). "
                                     "Press Configure.")
        else:
            self.state["active_host"] = None
            self.state["active_mac"] = None
            if self.state["phase"] in ("detected", *TERMINAL_PHASES):
                self.state["phase"] = "waiting"
                self.state["message"] = "Connect the next speaker…"

    # ── routes ────────────────────────────────────────────────────────────────

    def register_routes(self, app) -> None:
        @app.post("/api/configure")
        async def configure():
            if not self.state["detected"] or not self.state["active_host"]:
                return {"error": "No speaker is currently detected."}
            try:
                media = resolve_media(self.cfg)
            except SpeakerError as e:
                return {"error": str(e)}

            target_ip = self._target_ip()
            inputs = {
                "host": self.state["active_host"],
                "mac": self.state["active_mac"],
                "media_path": str(media) if media else "",
            }
            label = f"{device_name(target_ip)} ({inputs['host']} -> {target_ip})"
            if not await self.execute_run(inputs, label):
                return {"error": "A configuration is already in progress."}
            return self.public_state()

    # ── CLI banner ────────────────────────────────────────────────────────────

    def print_banner(self) -> None:
        static = self._static()
        media_name, media_ok = self._media_status()
        print(f"  detection    : DHCP scan of {', '.join(self._scan_subnets())}")
        print(f"  static IP    : {self._target_ip()} "
              f"(gw {static.get('gateway', DEFAULT_GATEWAY)} / "
              f"{static.get('netmask', DEFAULT_NETMASK)})")
        print(f"  NTP          : {(self.cfg.get('ntp', {}) or {}).get('server', DEFAULT_NTP_SERVER)}")
        print(f"  media file   : {media_name}{'' if media_ok else '  <-- fix before configuring'}")
        print(f"  config       : "
              f"{'config/speaker.config.json' if self.cfg else 'MISSING — copy the example'}")


configurator = SpeakerConfigurator(BASE_DIR)
app = configurator.build_app()


if __name__ == "__main__":
    configurator.run()
