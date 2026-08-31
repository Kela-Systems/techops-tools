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
from typing import Optional, Union

from pydantic import BaseModel

from bench_core.bench_ui import TERMINAL_PHASES, BenchConfigurator, read_device_mac
from bench_core.ip_mode import (
    MODE_CYCLE,
    MODE_DHCP,
    MODE_FIXED,
    MODE_MANUAL,
    IpModeError,
    IpModePolicy,
    split_ip,
)
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

# The pair of octets the speaker cycles between (TEC-848). Two, not a range: a
# site takes at most two speakers, so this is the whole of it. Overridable per
# station via `static.cycle_min` / `static.cycle_max`.
DEFAULT_CYCLE_MIN = 70
DEFAULT_CYCLE_MAX = 71


class ConfigureBody(BaseModel):
    ip_mode: str = ""              # blank = use the mode the operator selected
    # Required for manual mode; "71", 71 or the full "192.168.88.71".
    octet: Union[int, str] = ""


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
    ip_modes_enabled = True    # fixed / cycle / manual / DHCP (TEC-848)
    record_tool = "speaker"

    # ── config helpers ────────────────────────────────────────────────────────

    def _static(self) -> dict:
        return self.cfg.get("static", {}) or {}

    def _target_ip(self) -> str:
        """The address the NEXT speaker will get, or `""` under DHCP.

        Used for the detection sweep's first guess and the page's preview. Until
        TEC-848 this was a constant off the config; it is now whatever the
        selected mode says, which is the whole point of the change.
        """
        return self.ip_modes.next_ip() if self.ip_modes else self._config_ip()

    def _config_ip(self) -> str:
        """The station's default address — still the config's `static.ip`, which
        is what a bench that never touches the mode picker keeps getting."""
        return self._static().get("ip", DEFAULT_STATIC_IP)

    def _scan_subnets(self) -> list[str]:
        return self.cfg.get("scan_subnets", DEFAULT_SCAN_SUBNETS)

    def ip_mode_policy(self) -> IpModePolicy:
        """All four modes (TEC-848). The default stays what it has always been —
        every speaker to the config's `static.ip` — so a bench that ignores the
        picker behaves exactly as before.

        Manual entry is allowed anywhere on the subnet; only the cycle is
        narrowed, to the .70/.71 pair a site actually takes.
        """
        static = self._static()
        prefix, octet = split_ip(self._config_ip())
        return IpModePolicy(
            modes=(MODE_FIXED, MODE_CYCLE, MODE_MANUAL, MODE_DHCP),
            prefix=prefix or "192.168.88",
            octet_min=int(static.get("octet_min", 2)),
            octet_max=int(static.get("octet_max", 254)),
            cycle_min=int(static.get("cycle_min", DEFAULT_CYCLE_MIN)),
            cycle_max=int(static.get("cycle_max", DEFAULT_CYCLE_MAX)),
            default_mode=static.get("default_mode", MODE_FIXED),
            default_fixed_octet=octet or 0,
        )

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
        # The addressing block (mode, range, next address) is contributed by the
        # shared IpModeStore — see BenchConfigurator.public_state.
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
        """The name this run is about. A verify run doesn't know which address
        the unit was given until the pipeline has logged in and read its record,
        so fall back to where detection found it — the base only needs this for
        the "Verifying …" line."""
        if inputs.get("verifying"):
            return f"the speaker at {inputs.get('host') or 'the bench network'}"
        return device_name(inputs.get("target_ip") or "")

    def client_host(self, inputs: dict) -> str:
        return inputs.get("host") or self.cfg.get("host", "")

    def build_client(self, run_cfg: dict, host: str):
        return SpeakerClient(host=host,
                             username=run_cfg.get("username", DEFAULT_USERNAME),
                             scheme=run_cfg.get("scheme", DEFAULT_SCHEME))

    def run_pipeline(self, client, run_cfg: dict, inputs: dict) -> dict:
        return configure_speaker(client, settings=run_cfg,
                                 media_path=inputs.get("media_path") or None,
                                 target_ip=inputs.get("target_ip"),
                                 ip_mode=inputs.get("ip_mode") or "static",
                                 mac=inputs.get("mac") or "")

    def verify_pipeline(self, client, run_cfg: dict, inputs: dict) -> dict:
        """Mutation-free re-check of a finished speaker (TEC-348).

        Given a resolver since TEC-848: the address is no longer station-wide
        (the operator picks a mode per batch), so which one THIS speaker was
        supposed to get comes off its own configure record. The resolver also
        answers "was it ever provisioned by us", which nothing else here can.
        """
        return verify_speaker(client, settings=run_cfg,
                              media_path=inputs.get("media_path") or None,
                              resolve=self.verify_resolver(inputs),
                              target_ip=inputs.get("target_ip"),
                              ip_mode=inputs.get("ip_mode") or "")

    def verify_inputs(self, body) -> dict:
        """The media file the pipeline should expect in the slot, plus the
        address expectation an operator may state for a unit whose record is
        missing or wrong.

        A missing or unconfigured media file leaves the row out rather than
        failing it — the file not being on THIS bench PC says nothing about the
        speaker.
        """
        inputs = super().verify_inputs(body)
        inputs["verifying"] = True
        try:
            media = resolve_media(self.cfg)
        except SpeakerError:
            media = None
        inputs["media_path"] = str(media) if media else ""

        overrides = inputs["expected_overrides"]
        # "This one was left on DHCP" is a claim about the unit, so it wins over
        # any address also typed; blank means "use the record".
        if overrides.get("ip_mode") == MODE_DHCP:
            inputs["ip_mode"], inputs["target_ip"] = MODE_DHCP, ""
            return inputs
        inputs["ip_mode"] = ""
        inputs["target_ip"] = None
        stated = str(overrides.get("ip") or "").strip()
        if stated:
            # Validated rather than trusted: an address outside this bench's
            # subnet would fail every speaker it was typed against.
            try:
                octet = self.ip_modes.policy.parse_octet(stated)
                inputs["target_ip"] = self.ip_modes.policy.ip_for(octet)
            except IpModeError as e:
                self.logger.warning("Ignoring the stated address %r — %s", stated, e)
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
                # Both pipelines report these authoritatively: the address
                # configure ASSIGNED, or the one verify CHECKED AGAINST
                # (recovered from the configure record). Empty on a DHCP run —
                # the lease belongs to the site's DHCP server, so recording it
                # as ours would be a claim the QA label (TEC-352) would print.
                "hostname": result["hostname"],
                "ip": result.get("ip", ""),
                "ip_mode": result.get("ip_mode", "static"),
                # Where the speaker actually answered when the run finished.
                # Under DHCP this is the only address anyone has, and it is a
                # finding rather than an assignment — hence its own field.
                "reached_at": result.get("reached_at", ""),
                "from_host": inputs.get("host", ""),
            },
        )

    def success_message(self, result: dict, entry: dict, took: str) -> str:
        dev = entry["device"]
        where = (f"at {dev['ip']}" if dev.get("ip")
                 else f"on DHCP (currently {dev.get('reached_at') or 'unknown'})")
        return (f"Configured speaker {where} (SN {entry['serial']}) "
                f"in {took}. Unplug it and connect the next one.")

    def verify_label(self, inputs: dict) -> str:
        return f"the speaker on {inputs.get('host') or 'the bench network'}"

    def verify_message(self, result: dict, entry: dict, took: str) -> str:
        return (f"Speaker {entry['serial']} PASSED verification in {took} — nothing "
                "was changed. Unplug it and connect the next one.")

    def dismiss_message(self) -> str:
        return "Connect the next speaker…"

    # ── detection loop (DHCP — subnet sweep) ──────────────────────────────────

    def _find_speaker(self) -> Optional[str]:
        """The scan runs in an executor thread; check the last-seen address (or
        the target static IP, for re-runs) first so re-detection is instant."""
        # Under DHCP the next speaker has no address to guess, so fall back to
        # the station default — a re-run of a previously provisioned unit is
        # still the likeliest place to find one.
        guess = (self.state.get("active_host") or self._target_ip()
                 or self._config_ip())
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
        # /api/ip-mode is the shared route (TEC-848) — see BenchConfigurator.

        @app.post("/api/configure")
        async def configure(body: ConfigureBody):
            if not self.state["detected"] or not self.state["active_host"]:
                return {"error": "No speaker is currently detected."}
            try:
                media = resolve_media(self.cfg)
            except SpeakerError as e:
                return {"error": str(e)}

            try:
                assign = self.resolve_ip_mode(body.ip_mode, body.octet)
            except IpModeError as e:
                return {"error": str(e)}
            if assign.dhcp and not self.state["active_mac"]:
                # Left on DHCP the speaker may move, and the only way back to it
                # is its MAC — so a run that has never read one cannot confirm
                # anything afterwards. Refused here rather than discovered at
                # the last step, with the speaker already changed.
                return {"error": "Could not read this speaker's MAC over ARP, so "
                                 "it could not be found again after being left on "
                                 "DHCP. Check the cabling/adapter subnet, or "
                                 "assign a static address instead."}

            inputs = {
                "host": self.state["active_host"],
                "mac": self.state["active_mac"],
                "media_path": str(media) if media else "",
                "target_ip": assign.ip,
                "ip_mode": assign.mode,
                "advance_cycle": assign.advance_cycle,
            }
            label = (f"{device_name(assign.ip)} ({inputs['host']} -> "
                     f"{assign.ip or 'DHCP'})")
            if not await self.execute_run(inputs, label):
                return {"error": "A configuration is already in progress."}
            return self.public_state()

    # ── CLI banner ────────────────────────────────────────────────────────────

    def print_banner(self) -> None:
        static = self._static()
        media_name, media_ok = self._media_status()
        print(f"  detection    : DHCP scan of {', '.join(self._scan_subnets())}")
        print(f"  address mode : {self.ip_modes.describe()}")
        print(f"  next speaker : {self._target_ip() or 'left on DHCP'} "
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
