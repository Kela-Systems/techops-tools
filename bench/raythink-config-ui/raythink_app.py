#!/usr/bin/env python3
"""Raythink camera configurator — bench Web UI (FastAPI).

Workflow (one camera at a time, no manifest):
  1. Connect a Raythink camera (it ships on the static IP 192.168.1.123,
     admin/admin) to the bench network.
  2. The UI detects it, reads its LAN MAC over ARP, and asks for the config
     profile (LAN or Cellular) and how to address it: a static-IP octet
     (manual, or auto-cycled 30 -> 31 -> ... -> 50 -> 30), or DHCP.
  3. Submit -> full pipeline: set password -> import profile -> NTP -> address
     the camera (static 192.168.88.XX, or DHCP) -> verify.
  4. The camera moves off 192.168.1.123; connect the next one.

The common bench-UI shell (run machinery, routes, WebSocket, step logging) lives
in bench_core.bench_ui; this file adds only the camera specifics: detection on
the factory IP, the profile/IP-mode form, the persisted cycle counter, and the
configure_camera pipeline call.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from pydantic import BaseModel

from bench_core import tcp_port_open
from bench_core.bench_ui import (
    DETECT_TIMEOUT_SEC,
    TERMINAL_PHASES,
    BenchConfigurator,
    read_device_mac,
)
from bench_core.ip_mode import (
    ALL_MODES,
    MODE_DHCP,
    IpModeError,
    IpModePolicy,
)
from bench_core.run_record import build_run_entry

from raythink_base import DEFAULT_HOST, GENERATION_LABELS, GEN_RPC2
from raythink_configure import (
    DEFAULT_LEASE_TIMEOUT,
    DEFAULT_OCTET_MAX,
    DEFAULT_OCTET_MIN,
    DEFAULT_SCAN_SUBNETS,
    camera_hosts,
    configure_camera,
    device_name,
    find_camera,
    profile_generations,
    resolve_profile,
    verify_camera,
)

BASE_DIR = Path(__file__).resolve().parent


class ConfigureBody(BaseModel):
    profile: str = ""              # profile key, e.g. "lan" | "cellular"
    ip_mode: str = ""              # blank = use the mode the operator selected
    # Required for manual mode; "30", 30 or the full "192.168.88.30".
    octet: Union[int, str] = ""


class RaythinkConfigurator(BenchConfigurator):
    title = "Raythink Camera Configurator"
    port = 8005
    html_file = "raythink.html"
    config_filename = "config/raythink.config.json"
    log_filename = "raythink-config.log"
    logger_name = "raythink"
    tailscale_label = "raythink"
    history_limit = 30
    verify_supported = True    # mutation-free re-check of a finished camera (TEC-348)
    ip_modes_enabled = True    # fixed / cycle / manual / DHCP (TEC-848)
    # The name this tool's state file has always had. Kept so a bench upgrading
    # in place keeps its cycle counter instead of silently restarting the range
    # (bench_core.ip_mode.IpModeStore also reads the old key names).
    ip_mode_filename = "ip_state.json"
    record_tool = "raythink"

    # ── helpers: octet range + address modes ─────────────────────────────────

    def _octet_range(self) -> tuple[int, int]:
        static = self.cfg.get("static", {}) or {}
        return (int(static.get("octet_min", DEFAULT_OCTET_MIN)),
                int(static.get("octet_max", DEFAULT_OCTET_MAX)))

    def _dhcp_subnets(self) -> list[str]:
        """Subnets swept for a camera's MAC after it is switched to DHCP."""
        return (self.cfg.get("dhcp", {}) or {}).get("scan_subnets", DEFAULT_SCAN_SUBNETS)

    def ip_mode_policy(self) -> IpModePolicy:
        """All four modes. The camera is the one device on this bench that wants
        every one of them: several to a site (cycle), one to a site (fixed), a
        one-off (manual), or the site's own DHCP server (dhcp).

        One range bounds all three static modes, as it always has here — the
        cameras' block on the operational subnet.
        """
        static = self.cfg.get("static", {}) or {}
        lo, hi = self._octet_range()
        return IpModePolicy(
            modes=ALL_MODES,
            prefix=static.get("subnet_prefix", "192.168.88"),
            octet_min=lo, octet_max=hi,
            default_mode=static.get("default_mode", "manual"),
            default_fixed_octet=int(static.get("fixed_octet", 0) or 0),
        )

    # ── state ────────────────────────────────────────────────────────────────

    def initial_state(self) -> dict:
        return {
            "phase": "waiting",        # waiting|detected|configuring|configured|
                                       # verifying|verified|error
            "detected": False,
            "active_host": None,
            "active_mac": None,
            # Which API the detected camera speaks (see raythink_client). None
            # also means "nothing detected". Held in state rather than probed
            # per use because detection has already paid for the answer.
            "generation": None,
            "generation_label": "",
            "on_factory_ip": False,    # False also means "nothing detected"
            "busy": False,
            "message": "Connect the first camera (it ships on 192.168.1.123)…",
            "last_result": None,
            "history": [],
        }

    def extra_public_state(self) -> dict:
        # The addressing block (mode, range, next address) is contributed by the
        # shared IpModeStore — see BenchConfigurator.public_state.
        static = self.cfg.get("static", {}) or {}
        profiles = list((self.cfg.get("profiles", {}) or {}).keys())
        return {
            "profiles": profiles,
            # Which generations each profile has a file for, so the page can mark
            # one that cannot serve the camera currently on the bench.
            "profile_generations": {p: profile_generations(self.cfg, p)
                                    for p in profiles},
            "ntp_server": self.cfg.get("ntp_server", ""),
            "gateway": static.get("gateway", ""),
            "netmask": static.get("netmask", ""),
            "dhcp_subnets": self._dhcp_subnets(),
        }

    def reload(self) -> str:
        msg = super().reload()   # also re-clamps the mode store into the new range
        n = len(self.cfg.get("profiles", {}) or {})
        return f"{msg} {n} profile(s) configured."

    # ── pipeline hooks ────────────────────────────────────────────────────────

    def hostname_for(self, inputs: dict) -> str:
        """The name this run is about. A verify run may have neither an octet nor
        a record yet (the record is keyed on the serial, which needs a login), so
        fall back to the address detection found — the base only needs this for
        the "Verifying …" line."""
        if inputs.get("verifying") and inputs.get("octet") is None:
            return f"the camera at {inputs.get('host') or 'the bench network'}"
        return device_name(inputs.get("octet"))

    def client_host(self, inputs: dict) -> str:
        return inputs.get("host") or self.cfg.get("host", DEFAULT_HOST)

    def build_client(self, run_cfg: dict, host: str):
        """The client for whichever generation is on the bench.

        The generation comes off the detection state rather than being probed
        again here: `poll_once` established it a second ago, and re-probing
        would spend a round trip re-learning something we already know. It falls
        back to a probe (inside `open_camera`) if state somehow has none.
        """
        from raythink_base import DEFAULT_SCHEME, DEFAULT_USERNAME
        from raythink_client import open_camera
        return open_camera(
            host,
            username=run_cfg.get("username", DEFAULT_USERNAME),
            scheme=run_cfg.get("scheme", DEFAULT_SCHEME),
            verify=not run_cfg.get("insecure", True),
            generation=self.state.get("generation"),
        )

    def run_pipeline(self, client, run_cfg: dict, inputs: dict) -> dict:
        return configure_camera(client, profile_name=inputs["profile"],
                                profile_path=inputs["profile_path"],
                                octet=inputs.get("octet"), settings=run_cfg,
                                mac=inputs.get("mac") or "")

    def verify_pipeline(self, client, run_cfg: dict, inputs: dict) -> dict:
        """Mutation-free re-check of a finished camera (TEC-348)."""
        return verify_camera(client, settings=run_cfg,
                             resolve=self.verify_resolver(inputs),
                             profile_name=inputs.get("profile") or "",
                             octet=inputs.get("octet"),
                             ip_mode=inputs.get("ip_mode") or "")

    def verify_inputs(self, body) -> dict:
        """A camera has two per-unit expectations — which address the bench gave
        it and which profile it got — so both can be stated by the operator when
        the record is missing or wrong. Blank means "use the record".

        `octet` is validated rather than trusted: an out-of-range one would
        silently produce an expected address no camera could ever have.
        """
        inputs = super().verify_inputs(body)
        overrides = inputs["expected_overrides"]
        inputs["verifying"] = True
        inputs["profile"] = str(overrides.get("profile") or "").strip()
        inputs["ip_mode"] = (MODE_DHCP if overrides.get("ip_mode") == MODE_DHCP
                             else "")
        octet = overrides.get("octet")
        lo, hi = self._octet_range()
        try:
            inputs["octet"] = int(octet) if str(octet or "").strip() else None
        except (TypeError, ValueError):
            inputs["octet"] = None
        if inputs["octet"] is not None and not (lo <= inputs["octet"] <= hi):
            inputs["octet"] = None
        return inputs

    def verify_label(self, inputs: dict) -> str:
        return f"the camera on {inputs.get('host') or 'the bench network'}"

    def build_entry(self, result: dict, inputs: dict, duration: int) -> dict:
        ident = result["identity"]
        return build_run_entry(
            tool="raythink",
            ok=result["ok"],
            error=result["error"],
            serial=ident.get("serial", "unknown"),
            mac=ident.get("mac") or inputs.get("mac") or "unknown",
            model=ident.get("model", "Raythink"),
            firmware=ident.get("firmware", "unknown"),
            duration_s=duration,
            verification=result.get("verification", []),
            warnings=result.get("warnings", []),
            steps=result["steps"],
            log=result["log"],
            device={
                # Both pipelines report the name authoritatively as `hostname`:
                # the one configure ASSIGNED, or the one verify CHECKED AGAINST
                # (recovered from the configure record).
                "hostname": result["hostname"],
                # A verify run's profile/address are what it checked against, so
                # they come off the result rather than the request — the request
                # may well have carried neither.
                "profile": result.get("profile", inputs.get("profile", "")),
                # The address the bench assigned — empty on a DHCP run, where
                # the lease belongs to the DHCP server (it shows in the run's
                # verification rows instead).
                "ip": result.get("ip", inputs.get("target_ip", "")),
                "ip_mode": result.get("ip_mode", "static"),
                # Which API this unit was driven over. Recorded because the two
                # generations are provisioned differently enough that a record
                # is hard to read without it, and because a fleet's mix of the
                # two is exactly what a later question will be about.
                "generation": result.get("generation", ""),
            },
        )

    def success_message(self, result: dict, entry: dict, took: str) -> str:
        dev = entry["device"]
        where = f"at {dev['ip']}" if dev.get("ip") else "on DHCP"
        return (f"Configured camera {where} (SN {entry['serial']}) via "
                f"'{dev['profile']}' in {took}. Connect the next camera.")

    def verify_message(self, result: dict, entry: dict, took: str) -> str:
        return (f"{result.get('name') or 'The camera'} PASSED verification in "
                f"{took} — nothing was changed. Connect the next camera.")

    def dismiss_message(self) -> str:
        return "Connect the next camera…"

    # ── detection loop ──────────────────────────────────────────────────────

    def _reachable(self, host: str) -> bool:
        port = 443 if self.cfg.get("scheme", "http") == "https" else 80
        return tcp_port_open(host, port, timeout=DETECT_TIMEOUT_SEC)

    def _find_camera(self) -> tuple[Optional[str], Optional[str]]:
        """`(address, generation)` of a camera answering on the factory address
        or anywhere in the assigned static range.

        The range half is what makes Verify reachable (TEC-348) — a camera this
        bench already moved to 192.168.88.31 is invisible to a tool that only
        ever probes the factory address. The last-seen address is tried alone
        first, so the common case costs one connection.
        """
        scheme = self.cfg.get("scheme", "http")
        return find_camera(camera_hosts(self.cfg),
                           443 if scheme == "https" else 80, scheme,
                           first_guess=self.state.get("active_host"))

    async def poll_once(self, loop) -> None:
        if self.state["busy"]:
            return  # mid-run the camera reboots / changes IP — leave detection alone

        host, generation = await loop.run_in_executor(None, self._find_camera)
        self.state["detected"] = bool(host)

        if host:
            self.state["active_host"] = host
            self.state["generation"] = generation
            self.state["generation_label"] = GENERATION_LABELS.get(generation, "")
            mac = await loop.run_in_executor(None, read_device_mac, host)
            self.state["active_mac"] = mac
            # A camera on the factory address is fresh; one in the assigned
            # range has been through this tool already, which is what the page
            # uses to make Verify the primary action.
            factory = self.cfg.get("host", DEFAULT_HOST)
            self.state["on_factory_ip"] = host == factory
            if self.state["phase"] in TERMINAL_PHASES:
                return  # a run finished but the camera is still answering
            self.state["phase"] = "detected"
            self.state["message"] = (
                f"Camera detected on {host} — "
                f"{GENERATION_LABELS.get(generation, 'unknown generation')}, "
                f"MAC {mac or 'unknown'}. "
                + ("Pick a profile and the IP, then Configure."
                   if host == factory
                   else "It is already on an assigned address — press Verify to "
                        "check it, or Configure to redo it."))
        else:
            self.state["active_host"] = None
            self.state["active_mac"] = None
            self.state["generation"] = None
            self.state["generation_label"] = ""
            self.state["on_factory_ip"] = False
            if self.state["phase"] in ("detected", *TERMINAL_PHASES):
                self.state["phase"] = "waiting"
                self.state["message"] = "Connect the next camera…"

    # ── routes ────────────────────────────────────────────────────────────────

    def register_routes(self, app) -> None:
        # /api/ip-mode is the shared route (TEC-848) — see BenchConfigurator.

        @app.post("/api/configure")
        async def configure(body: ConfigureBody):
            profiles = self.cfg.get("profiles", {}) or {}
            if not profiles:
                return {"error": "No config profiles set — add them to raythink.config.json."}
            profile = (body.profile or "").strip() or next(iter(profiles))
            if profile not in profiles:
                return {"error": f"Unknown profile '{profile}'. Known: {', '.join(profiles)}."}

            if not self.state["detected"]:
                return {"error": "No camera is currently detected."}

            # The profile file depends on the generation, so this can only be
            # resolved once a camera is on the bench — and it is resolved here,
            # before the run starts, so a profile that has no file for this
            # camera is refused up front instead of three steps in.
            generation = self.state.get("generation") or GEN_RPC2
            try:
                profile_path = str(resolve_profile(self.cfg, profile, generation))
            except Exception as e:  # noqa: BLE001 — surface a missing file cleanly
                return {"error": str(e)}

            try:
                assign = self.resolve_ip_mode(body.ip_mode, body.octet)
            except IpModeError as e:
                return {"error": str(e)}
            if assign.dhcp and not self.state["active_mac"]:
                # DHCP is verified by finding the camera again by MAC, so a
                # camera whose MAC we never read can't be checked afterwards.
                return {"error": "Could not read this camera's MAC over ARP, so it "
                                 "could not be found again after switching to DHCP. "
                                 "Check the cabling/adapter subnet, or assign a "
                                 "static IP instead."}

            inputs = {
                "profile": profile, "profile_path": profile_path,
                "octet": assign.octet,
                "ip_mode": assign.mode, "target_ip": assign.ip,
                "advance_cycle": assign.advance_cycle,
                "host": self.state["active_host"] or self.cfg.get("host", DEFAULT_HOST),
                "mac": self.state["active_mac"],
            }
            label = (f"{device_name(assign.octet)} -> {assign.ip or 'DHCP'} "
                     f"('{profile}')")
            if not await self.execute_run(inputs, label):
                return {"error": "A configuration is already in progress."}
            return self.public_state()

    # ── CLI banner ──────────────────────────────────────────────────────────────

    def print_banner(self) -> None:
        lo, hi = self._octet_range()
        static = self.cfg.get("static", {}) or {}
        print(f"  camera       : {self.cfg.get('host', DEFAULT_HOST)} "
              "(laptop must be on 192.168.1.x)")
        print(f"  static IP    : {static.get('subnet_prefix', '192.168.88')}.{lo}-{hi} "
              f"(gw {static.get('gateway', '?')} / {static.get('netmask', '?')})")
        print(f"  address mode : {self.ip_modes.describe()}")
        dhcp = self.cfg.get("dhcp", {}) or {}
        print(f"  or DHCP      : found again by MAC on "
              f"{', '.join(self._dhcp_subnets())} "
              f"(up to {dhcp.get('lease_timeout', DEFAULT_LEASE_TIMEOUT)}s)")
        print(f"  NTP          : {self.cfg.get('ntp_server', '?')}")
        profiles = self.cfg.get("profiles", {}) or {}
        print(f"  profiles     : {', '.join(profiles) if profiles else 'NONE — add them to the config'}")
        for key, entry in profiles.items():
            # One profile key names one file per camera generation; a plain
            # string is the older generation only. Both are listed, because a
            # profile with no file for the camera on the bench is the failure
            # this banner exists to make obvious before a run starts.
            files = entry if isinstance(entry, dict) else {GEN_RPC2: entry}
            for gen in GENERATION_LABELS:
                rel = files.get(gen)
                if not rel:
                    print(f"    - {key:<9} {gen:<4}: (none configured)")
                    continue
                ok = (BASE_DIR / "config" / rel).is_file()
                print(f"    - {key:<9} {gen:<4}: {rel} ({'found' if ok else 'MISSING'})")
        print(f"  config       : "
              f"{'config/raythink.config.json' if self.cfg else 'MISSING — copy the example'}")


configurator = RaythinkConfigurator(BASE_DIR)
app = configurator.build_app()


if __name__ == "__main__":
    configurator.run()
