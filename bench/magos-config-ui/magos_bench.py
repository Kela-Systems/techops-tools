#!/usr/bin/env python3
"""Shared bench engine for the Magos configurators (radar + APU).

The radar UI (app.py) and the APU UI (apu_app.py) are the same iterative
plug-in → detect → configure → unplug workflow with the same auto / cycle modes,
the same settings panel, and the same per-unit logging — they differ only in the
device they talk to and a couple of fields (the APU also sets its controlled
radars). All of that common machinery lives here in `MagosBench`; each app is a
thin subclass that fills in the device-specific hooks.

This reuses the small generic bits from the shared `bench_core` package
(`OperatorStore`, `save_run_record`, ...) so the Magos tools and the Teltonika
tools share one infrastructure layer, including the single per-run JSON writer
(TEC-572). The device talking still goes through the Magos clients
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# Generic bench-UI helpers shared with the Teltonika tools.
from bench_core.bench_ui import (
    OPERATOR_FILENAME,
    OperatorBody,
    OperatorStore,
    VerifyBody,
    bench_version,
    install_loop_exception_handler,
    prune_json_logs,
    run_stamp,
    save_run_record,
    station_id,
)
from bench_core.central import start_central_uploader
from bench_core.config_check import config_fingerprint
from bench_core.history import resolve_expected
from bench_core.label_printer import make_label_printer
from bench_core.run_record import KIND_CONFIGURE, KIND_VERIFY

# The Magos device clients log through the "magos" logger; reuse its line format
# so step lines render the same in every tool.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from magos_configure import (  # noqa: E402
    FACTORY_CONFIG_PATH,
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
    uses_radar_ip: bool = False           # APU also targets controlled radars
    config_section: str = ""              # this tool's section of the config file
                                          # ("ar300" / "apu"); "" = no persistence
    record_tool: str = ""                 # this tool's name in the run records
                                          # ("magos-radar" / "magos-apu"), which is
                                          # also how a verify pass finds the unit's
                                          # own configure run
    # Opt in to the mutation-free Verify pass (TEC-851). Off by default so a
    # subclass without `do_verify` gets no route and no button rather than a
    # button that errors.
    verify_supported: bool = False

    def __init__(self, base_dir: Path, default_cfg: dict) -> None:
        self.base_dir = base_dir
        self.log_dir = base_dir / "logs"
        self.log_dir.mkdir(exist_ok=True)
        self.log = self._setup_logging()
        prune_json_logs(self.log_dir)  # trim any backlog left by earlier sessions
        # Ships queued run records to the collector when the station has
        # BENCH_CENTRAL_URL set (TEC-573); None (fully off) otherwise.
        self.central_uploader = start_central_uploader(self.log_dir, self.log)
        self.bench_version = bench_version(base_dir)
        self.station_id = station_id()
        self.operator_store = OperatorStore(base_dir.parent / OPERATOR_FILENAME)
        # Prints the QA label on a verified-OK run (TEC-352). Station-level,
        # like the operator name: one printer serves all seven tools.
        self.label_printer = make_label_printer(base_dir.parent, self.log)
        self.log.info("%s starting — bench version %s, station %s",
                      self.title, self.bench_version, self.station_id)
        self.cfg: dict = dict(default_cfg)
        # The defaults were seeded from config/magos.config.json (via the apps'
        # DEFAULT_CFG); UI edits are written back into this file so it stays
        # the single source of truth across restarts. An attribute (not the
        # module constant) so tests can point persistence at a scratch file.
        self.config_path: Path = FACTORY_CONFIG_PATH
        # The hash pins the settings each run was provisioned under (TEC-356).
        self.config_hash: str = self._config_fingerprint()
        self._misses = 0
        # Monotonic clock + last-activity stamp drive the hands-free idle
        # timeout. `_now` is an attribute so tests can inject a fake clock.
        self._now = time.monotonic
        self._last_activity = self._now()
        # The running uvicorn server handle, set once in run(). Closing the
        # dashboard does NOT stop the tool — only the hands-free auto/cycle mode
        # disarms itself after AUTO_IDLE_TIMEOUT_SEC of inactivity.
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
            auto["radar_ips"] = None
        return auto

    def _initial_state(self) -> dict:
        return {
            # waiting|detected|configuring|configured|verifying|verified|error
            "phase": "waiting",
            "detected": False,
            "active_host": None,         # the address the current unit answered on
            # Whether that address is a FACTORY one. A unit found on its
            # permanent address is a finished unit: it can be verified, and it
            # must never be configured — least of all by a hands-free mode
            # (TEC-851).
            "on_factory_ip": False,
            # The permanent address of the unit we just configured. It answers
            # there for as long as it stays plugged in, and re-detecting it would
            # mean the "plug in the next one" prompt never came back.
            "settled_host": None,
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

    @property
    def device_word_cap(self) -> str:
        """`device_word` at the start of a sentence. Not `str.capitalize()`,
        which lowercases the rest and renders the APU tool's word as "Apu"."""
        return self.device_word[:1].upper() + self.device_word[1:]

    def _hosts_str(self) -> str:
        return " / ".join(self.cfg.get("hosts", [])) or "(no hosts set)"

    def _verify_hosts_str(self) -> str:
        return " / ".join(self.verify_hosts()) or "(none)"

    @property
    def cycle_channels(self) -> list:
        return list(self.channel_ips)

    # ── device hooks (override in the subclass) ───────────────────────────────

    def resolve_target(self, channel: Optional[str], ip: Optional[str],
                       radar_ips: Optional[str] = None) -> Optional[dict]:
        """Map a (channel, ip[, radar_ips]) choice to a target dict
        ({"channel","ip",[ "radars"]}) or None when nothing is selected."""
        raise NotImplementedError

    def do_configure(self, target: dict, host: str,
                     avoid_serial: Optional[str]) -> dict:
        """Run the device pipeline in a worker thread; never raises. Returns a
        result dict with: ok, skipped, ip (address shown in history), identity,
        raw, steps, log, error, verified, verify_detail."""
        raise NotImplementedError

    def do_verify(self, host: str, resolve) -> dict:
        """Re-check a finished unit in a worker thread, changing nothing
        (TEC-851); never raises. Same result contract as `do_configure`, plus
        `verification` — the rows the shared table renders.

        `resolve` is `verify_resolver()`: call it once, with the identity, as
        soon as the unit says what it is (see there for why the timing matters).
        """
        raise NotImplementedError

    def build_entry(self, target: dict, host: str, result: dict,
                    duration: int) -> dict:
        """Build the per-unit history entry from a configure result, via
        bench_core.run_record.build_run_entry (the canonical schema, TEC-346)
        — per-family fields go in `device`."""
        raise NotImplementedError

    def build_verify_entry(self, host: str, result: dict, duration: int) -> dict:
        """`build_entry` for a verify run, which has no target. `kind` is stamped
        by `run_verification` afterwards."""
        raise NotImplementedError

    def success_message(self, ident: dict, result: dict, target: dict) -> str:
        raise NotImplementedError

    @staticmethod
    def verified_note(result: dict) -> str:
        """The verification half of a configure run's success line.

        Three outcomes, not two: rows that could not be read collapse to None,
        and "I could not check" must not be worded like a pass — "configured but
        NOT verified" is a real end state on these tools and the operator has to
        be able to see it.
        """
        verified = result.get("verified")
        if verified:
            return " Verified at the new address — all checks passed."
        if verified is None:
            return " NOT verified — the checks could not be run."
        return f" NOT verified — {result.get('verify_detail') or 'checks failed'}."

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

    def verify_hosts(self) -> list[str]:
        """The permanent addresses a FINISHED unit lives on — the same
        channel→IP map the tool provisions from, so an operator who changed a
        channel's address in settings gets it swept without saying so twice.

        Detection covers these as well as the factory hosts (TEC-851): a
        provisioned radar or APU has long since left 192.168.40.x, so without
        this there is nothing to press Verify on.
        """
        return [str(ip).split("/")[0] for ip in self.channel_ips.values()]

    def _answering(self, hosts: list[str]) -> set[str]:
        """Which of `hosts` a dashboard answers on, probed in parallel.

        In parallel because the list is now the factory hosts plus every
        permanent address: probed one after another at DETECT_TIMEOUT_SEC each,
        a bench with nothing plugged in would take longer to sweep than the poll
        interval it is being swept from.
        """
        if not hosts:
            return set()
        scheme, verify = self.cfg["scheme"], not self.cfg["insecure"]
        with ThreadPoolExecutor(max_workers=min(16, len(hosts))) as pool:
            futures = {pool.submit(probe_http, host, scheme, DETECT_TIMEOUT_SEC,
                                   verify): host for host in hosts}
            return {futures[f] for f in as_completed(futures) if f.result()}

    def detect(self) -> tuple[Optional[str], bool]:
        """Where a unit is answering and whether that is a factory address.

        Factory hosts win: a fresh unit is the common case and the one with work
        to do. A hit on a permanent address is a finished unit — except when it
        is the one we just configured, which is still sitting on the bench and
        must keep reading as "done, unplug it" rather than as a new arrival.
        """
        factory = list(self.cfg.get("hosts", []))
        permanent = [h for h in self.verify_hosts() if h not in factory]
        answering = self._answering(factory + permanent)

        settled = self.state.get("settled_host")
        if settled and settled not in answering:
            # Unplugged at last. A unit that answers there later is a different
            # one (or the same one brought back for a QA sweep), so let it be
            # detected again.
            self.state["settled_host"] = None
            settled = None

        for candidate in factory:
            if candidate in answering:
                return candidate, True
        for candidate in permanent:
            if candidate in answering and candidate != settled:
                return candidate, False
        return None, False

    def net_warning(self) -> Optional[str]:
        """Warn when no local adapter can reach either subnet — without this, a
        wrong NIC setup is indistinguishable from 'nothing plugged in'.

        Either subnet, because the two things an operator does here need
        different ones: provisioning happens on the factory 192.168.40.x, an
        end-of-batch Verify sweep on the permanent 192.168.88.x.
        """
        if not self.cfg.get("hosts"):
            return "No factory hosts configured."
        for candidate in list(self.cfg["hosts"]) + self.verify_hosts():
            host, _ = self._split_host_port(candidate)
            if is_on_link(host):
                return None
        return (f"This PC has no network adapter on the factory subnet "
                f"({self._hosts_str()}) or on the subnet finished units live on "
                f"({self._verify_hosts_str()}) — {self.device_word}s cannot be "
                "detected. Set the adapter to a static 192.168.40.x address to "
                "configure, or 192.168.88.x to verify a finished unit.")

    # ── per-unit logging ──────────────────────────────────────────────────────

    def _save_log(self, entry: dict, raw: dict) -> Optional[str]:
        # Verify records lead with `verify_` so the two kinds of run are tellable
        # apart in logs/ without opening anything — an operator asked to send
        # "the log for that unit" picks the right file.
        prefix = ("verify_" if entry.get("kind") == KIND_VERIFY else "") + self.log_prefix
        return save_run_record(self.log_dir, entry,
                               name_stem=entry.get("serial"),
                               prefix=prefix,
                               extra={"raw_identity_payloads": raw},
                               logger=self.log)

    # ── public state ──────────────────────────────────────────────────────────

    def _config_fingerprint(self) -> str:
        """Hash of the current (redacted) settings — recomputed when settings
        change and stamped into every run record (TEC-356)."""
        return config_fingerprint({k: v for k, v in self.cfg.items()
                                   if k != "password"})

    def run_stamp(self) -> dict:
        """Provenance fields (operator / station_id / bench_version /
        config_hash) added to every history entry and per-unit JSON."""
        return run_stamp(self.operator_store.get(), self.station_id,
                         self.bench_version, self.config_hash)

    def counts(self) -> dict:
        """Session tallies, with verify runs counted separately: a QA sweep
        re-checks units that are already in the "done" pile, so folding the two
        together would double-count the batch."""
        configures = [h for h in self.state["history"]
                      if h.get("kind", KIND_CONFIGURE) == KIND_CONFIGURE]
        verifies = [h for h in self.state["history"]
                    if h.get("kind") == KIND_VERIFY]
        done = sum(1 for h in configures if h["status"] == "ok")
        passed = sum(1 for h in verifies if h["status"] == "ok")
        return {"done": done, "error": len(configures) - done,
                "verified": passed, "verify_failed": len(verifies) - passed}

    def public_state(self) -> dict:
        return {
            **self.state,
            "bench_version": self.bench_version,
            "station_id": self.station_id,
            "operator": self.operator_store.get(),
            "config": {k: v for k, v in self.cfg.items() if k != "password"},
            "config_hash": self.config_hash,
            "config_warnings": [],  # the config file holds no tokens/placeholders
                                    # worth self-checking (unlike the RUTM/OTD tools)
            "password_set": bool(self.cfg.get("password")),
            "printer": self.label_printer.status(),
            "channel_ips": self.channel_ips,
            "verify_hosts": self.verify_hosts(),
            "verify_supported": self.verify_supported,
            "counts": self.counts(),
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
        run_t0 = time.monotonic()
        result = await loop.run_in_executor(None, self.do_configure, target, host, avoid)
        duration = int(time.monotonic() - run_t0)
        ident = result["identity"]

        if result["skipped"]:
            self.state["busy"] = False
            self.state["phase"] = "configured"
            self.state["message"] = (
                f"{self.device_word_cap} SN {avoid} was already configured "
                f"and is still answering on {host} — unplug it and plug in the next one.")
            return None

        entry = self.build_entry(target, host, result, duration)
        entry.update(self.run_stamp())  # who / where / which code (TEC-345)
        # Before _save_log, which also spools to central, and in the executor
        # because printing blocks — see the same call in
        # BenchConfigurator.execute_run (TEC-352). Mutates `entry` in place.
        await loop.run_in_executor(None, self.label_printer.print_run, entry)
        entry["log_file"] = self._save_log(entry, result["raw"])

        self.state["history"].insert(0, entry)
        del self.state["history"][HISTORY_MAX:]
        self.state["last_result"] = entry
        self.state["busy"] = False

        if result["ok"]:
            if ident["serial"] not in (None, "", "unknown"):
                self.state["last_ok_serial"] = ident["serial"]
            # It now answers on its permanent address, which detection also
            # sweeps. Remember it so the unit still on the bench reads as "done,
            # unplug it" instead of as a finished unit that just turned up.
            self.state["settled_host"] = str(result["ip"]).split("/")[0]
            self.state["phase"] = "configured"
            self.state["message"] = self.success_message(ident, result, target)
        else:
            self.state["phase"] = "error"
            self.state["message"] = f"Configuration failed: {result['error']}"
        return entry

    # ── the verify pass (shared orchestration, TEC-851) ───────────────────────

    def verify_resolver(self, overrides: Optional[dict] = None):
        """A callable `(identity) -> (expected, prior_run_row)` for a verify pass
        to invoke once it knows what unit it is talking to.

        A callable rather than pre-computed values because the lookup key is the
        serial, and the serial comes off the device — so it cannot be resolved
        before the pass has logged in.

        `prior_run_row` is a FAILING row when no configure record exists
        anywhere. A pass must include it, not drop it: without it a unit nobody
        ever provisioned verifies green, and TEC-352 prints it a label.
        """
        def resolve(identity: dict) -> tuple[dict, Optional[dict]]:
            expected, prior_row, source = resolve_expected(
                self.log_dir,
                serial=(identity or {}).get("serial", "") or "",
                mac=(identity or {}).get("mac", "") or "",
                tool=self.record_tool,
                overrides=overrides or {})
            self.log.info("Verifying against the %s configure record.", source)
            return expected, prior_row

        return resolve

    async def run_verification(self, host: str,
                               overrides: Optional[dict] = None) -> dict:
        """Re-check one finished unit and record it as a `kind: "verify"` run.

        Deliberately the same shape as `run_configuration` — a verify pass is a
        first-class run with its own record, not a configure run with its steps
        skipped, because "verified OK" is what an end-of-batch sweep counts and
        what earns a QA label.
        """
        self.state["busy"] = True
        self.state["phase"] = "verifying"
        self.state["message"] = (f"Verifying the {self.device_word} at {host} — "
                                 "nothing will be changed...")

        loop = asyncio.get_event_loop()
        run_t0 = time.monotonic()
        resolve = self.verify_resolver(overrides)
        result = await loop.run_in_executor(None, self.do_verify, host, resolve)
        duration = int(time.monotonic() - run_t0)
        ident = result["identity"]

        entry = self.build_verify_entry(host, result, duration)
        entry["kind"] = KIND_VERIFY
        entry.update(self.run_stamp())  # who / where / which code (TEC-345)
        # Before _save_log, in the executor — as in run_configuration (TEC-352).
        await loop.run_in_executor(None, self.label_printer.print_run, entry)
        entry["log_file"] = self._save_log(entry, result["raw"])

        self.state["history"].insert(0, entry)
        del self.state["history"][HISTORY_MAX:]
        self.state["last_result"] = entry
        self.state["busy"] = False

        if result["ok"]:
            self.state["phase"] = "verified"
            self.state["message"] = (
                f"{self.device_word_cap} SN {ident['serial']} PASSED "
                "verification — nothing was changed. Unplug it and plug in the "
                "next one.")
        else:
            self.state["phase"] = "error"
            self.state["message"] = (
                f"{self.device_word_cap} SN {ident['serial']} FAILED "
                f"verification: {result['error']}")
        return entry

    async def verify_request(self, overrides: Optional[dict] = None) -> dict:
        host = self.state["active_host"]
        if not self.state["detected"] or not host:
            return {"error": f"No {self.device_word} is currently detected to verify."}
        if self.state["busy"]:
            return {"error": "A run is already in progress."}
        await self.run_verification(host, overrides)
        return self.public_state()

    # ── detection loop ────────────────────────────────────────────────────────

    async def poll_step(self, active: Optional[str],
                        on_factory_ip: bool = True) -> None:
        """One detection-loop iteration given the reachable host (or None) and
        whether it is a factory address. Split out from the loop so the
        auto/cycle decision logic is testable."""
        # A configuration is running — mid-run the unit changes IP and briefly
        # drops, so leave detection state untouched until it finishes (matches
        # the Teltonika tools). Done first so a run isn't disturbed by a blip.
        if self.state["busy"]:
            return

        reachable = active is not None
        self._misses = 0 if reachable else self._misses + 1
        self.state["detected"] = reachable
        self.state["active_host"] = active
        self.state["on_factory_ip"] = reachable and on_factory_ip

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
        if (not reachable
                and (auto["enabled"] or cycle["enabled"])
                and self._now() - self._last_activity >= AUTO_IDLE_TIMEOUT_SEC):
            self._disarm_idle()
            return

        if reachable:
            if not on_factory_ip:
                # A finished unit, answering on the address it was provisioned
                # to. There is nothing to configure and everything to check, so
                # it is Verify-only — and deliberately invisible to auto and
                # cycle mode, which exist to provision fresh units and would
                # otherwise re-provision a unit somebody brought back for QA.
                if phase in ("waiting", "detected"):
                    self.state["phase"] = "detected"
                    # Rebuilt every poll, not just on the way in: swapping a
                    # fresh unit for a finished one inside the miss window leaves
                    # the phase at "detected", and the banner would otherwise
                    # still be telling the operator to pick a channel for it.
                    self.state["message"] = self._detected_message()
            elif phase in ("waiting", "detected") and cycle["enabled"]:
                channel = self.cycle_channels[cycle["index"]]
                target = self.resolve_target(channel, None, None)
                entry = await self.run_configuration(target, active, guard_repeat=True)
                if entry and entry["status"] == "ok":
                    cycle["count"] += 1
                    cycle["index"] = (cycle["index"] + 1) % len(self.cycle_channels)
            elif phase in ("waiting", "detected") and auto["enabled"]:
                target = self.resolve_target(auto["channel"], auto["ip"],
                                             auto.get("radar_ips"))
                if target:
                    await self.run_configuration(target, active, guard_repeat=True)
                elif phase != "detected":
                    self.state["phase"] = "detected"
                    self.state["message"] = (f"{self.device_word_cap} detected at "
                                             f"{active} (auto armed, but no "
                                             "target set).")
            elif phase in ("waiting", "detected"):
                self.state["phase"] = "detected"
                self.state["message"] = self._detected_message()   # see above
            # phase == configured/verified/error while still plugged in: wait for
            # unplug.
        else:  # nothing reachable for MISS_THRESHOLD polls in a row
            if (phase in ("detected", "configured", "verified", "error")
                    and self._misses >= MISS_THRESHOLD):
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
                active, on_factory = await loop.run_in_executor(None, self.detect)
                self.state["net_warning"] = await loop.run_in_executor(None, self.net_warning)
                await self.poll_step(active, on_factory)
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
        self.config_hash = self._config_fingerprint()
        self._persist_settings()
        if self.state["phase"] == "waiting":
            self.state["message"] = f"Waiting for a {self.device_word} at {self._hosts_str()}..."
        return self.public_state()

    def _persist_settings(self) -> None:
        """Write the current settings back into this tool's section of the
        factory-defaults config file, so a UI edit survives a restart and the
        file stays the single source of truth (editing the FILE by hand still
        needs a restart — it is only read at startup).

        Only the keys in self.cfg are (over)written: `_comment` keys, `host`
        (the CLI default), `channel_ips` and the OTHER tool's section are
        preserved, so the radar and APU apps can share the file. The write is
        atomic (tmp + replace) and best-effort — a disk problem is logged but
        never blocks the in-memory settings change."""
        if not self.config_section:
            return
        try:
            raw: dict = {}
            if self.config_path.exists():
                raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            raw.setdefault(self.config_section, {}).update(self.cfg)
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.config_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
            os.replace(tmp, self.config_path)
            self.log.info("Settings saved to %s (%s section).",
                          self.config_path.name, self.config_section)
        except (OSError, ValueError) as e:
            self.log.warning("Settings applied for this session but could NOT be "
                             "saved to %s: %s", self.config_path, e)

    async def configure_request(self, channel: Optional[str], ip: Optional[str],
                                radar_ips: Optional[str] = None) -> dict:
        if self.state["phase"] != "detected":
            return {"error": f"No {self.device_word} is currently detected to configure."}
        if self.state["busy"]:
            return {"error": "A configuration is already in progress."}
        if not self.state["on_factory_ip"]:
            # A finished unit answering on its permanent address. Re-provisioning
            # it would be a mistake nobody asked for, so this is a refusal rather
            # than a warning.
            return {"error": (f"The {self.device_word} at {self.state['active_host']} "
                              "is already provisioned — press Verify to check it. "
                              f"To configure a fresh {self.device_word}, plug one in "
                              "on the factory subnet.")}
        target = self.resolve_target(channel, ip, radar_ips)
        if not target:
            return {"error": f"Provide a channel ({'/'.join(self.channel_ips)}) "
                             "or a manual IP."}
        host = self.state["active_host"] or (self.cfg["hosts"][0] if self.cfg.get("hosts") else None)
        if not host:
            return {"error": "No factory host configured."}
        await self.run_configuration(target, host)
        return self.public_state()

    def set_auto(self, enabled: bool, channel: Optional[str], ip: Optional[str],
                 radar_ips: Optional[str] = None) -> dict:
        target = self.resolve_target(channel, ip, radar_ips)
        if enabled and not target:
            return {"error": "Pick a channel or enter an IP before turning on auto mode."}
        auto = {"enabled": enabled, "channel": channel, "ip": ip}
        if self.uses_radar_ip:
            auto["radar_ips"] = radar_ips
        self.state["auto"] = auto
        if enabled:
            self.state["cycle"]["enabled"] = False  # auto + cycle are mutually exclusive
            extra = ""
            if self.uses_radar_ip and target.get("radars"):
                extra = " (radars " + ", ".join(
                    f"{r['radar_id']}={r['ip']}" for r in target["radars"]) + ")"
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

    def _detected_message(self) -> str:
        """What the banner says about the unit on the bench. A unit found on its
        permanent address is finished: telling the operator to pick a channel for
        it would be inviting them to re-provision a unit that is already done."""
        host = self.state["active_host"]
        if not self.state["on_factory_ip"]:
            return (f"Finished {self.device_word} detected at {host}. Press Verify "
                    "to check it — nothing will be changed.")
        return (f"{self.device_word_cap} detected at {host}. "
                "Pick a channel and configure it.")

    def dismiss(self) -> dict:
        self.state["last_result"] = None
        self.state["phase"] = "detected" if self.state["detected"] else "waiting"
        if self.state["detected"]:
            self.state["message"] = self._detected_message()
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
            return f"{prefix} {self._detected_message()}"
        return f"Waiting for a {self.device_word} at {self._hosts_str()}..."

    def _cycle_start_message(self, first_channel: str) -> str:
        return (f"Cycle started — plug in {self.device_word}s one by one. First → "
                f"channel {first_channel} ({self.channel_ips[first_channel]}).")

    # ── FastAPI app ────────────────────────────────────────────────────────────

    def register_routes(self, app: FastAPI) -> None:
        """Subclass adds /api/settings, /api/configure, /api/auto, /api/cycle
        with its own request bodies, delegating to the handlers above."""

    def build_app(self) -> FastAPI:
        @contextlib.asynccontextmanager
        async def lifespan(_app: FastAPI):
            # Drop the harmless WinError 10054 tracebacks a dropped browser
            # connection triggers on Windows (see bench_ui for the full story).
            install_loop_exception_handler()
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
        # Shared CSS/JS live in the bench_core package so every tool serves one
        # copy (see bench_core.bench_ui.SHARED_STATIC_DIR).
        from bench_core.bench_ui import SHARED_STATIC_DIR
        app.mount("/shared", StaticFiles(directory=str(SHARED_STATIC_DIR)),
                  name="shared")

        @app.get("/")
        async def index():
            return FileResponse(str(self.base_dir / "static" / self.html_file))

        @app.get("/api/state")
        async def get_state():
            return self.public_state()

        @app.post("/api/dismiss")
        async def dismiss():
            return self.dismiss()

        @app.post("/api/operator")
        async def set_operator(body: OperatorBody):
            name = self.operator_store.set(body.operator)
            self.log.info("Operator set to '%s'.", name or "(cleared)")
            return self.public_state()

        # The Verify route is the shared one (same path, same body, same
        # "nothing typed" contract as the five BenchConfigurator tools), so the
        # shared button in bench.js works here untouched.
        if self.verify_supported:
            @app.post("/api/verify")
            async def verify(body: VerifyBody):
                return await self.verify_request(dict(body.expected or {}))

        @app.websocket("/ws/state")
        async def ws_state(websocket: WebSocket):
            await websocket.accept()
            try:
                while True:
                    await websocket.send_json(self.public_state())
                    await asyncio.sleep(1)
            except WebSocketDisconnect:
                pass  # client closed the tab — normal
            except Exception:
                # Don't swallow real bugs silently; log so they're diagnosable.
                self.log.exception("WebSocket state push failed.")

        self.register_routes(app)
        return app

    # ── CLI entrypoint ──────────────────────────────────────────────────────────

    def run(self) -> None:
        # 127.0.0.1 (not "localhost") avoids browsers auto-upgrading to HTTPS via
        # HSTS — this is an HTTP-only server, and HTTPS would return 400.
        url = f"http://127.0.0.1:{self.port}"
        print(f"Starting {self.title} at {url}")
        print(f"  version      : {self.bench_version}")
        print(f"  station      : {self.station_id}")
        print(f"  operator     : {self.operator_store.get() or '(not set — enter it on the page)'}")
        print("  (open it as http://, NOT https:// — this is a plain-HTTP local server)")
        self.print_banner()
        app = self.build_app()
        # Tools are launched by the bench dashboard, which opens the one browser
        # tab itself — so a tool never auto-opens a tab unless explicitly asked
        # with BENCH_OPEN_BROWSER=1 (e.g. when running this one tool on its own).
        # (Opt-in, not opt-out: relying on the launcher to unset a var was
        # fragile on Windows and left every tool opening an about:blank tab.)
        if os.environ.get("BENCH_OPEN_BROWSER"):
            with contextlib.suppress(Exception):
                webbrowser.open(url)
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                log_level="warning")
        self._server = uvicorn.Server(config)
        self._server.run()
