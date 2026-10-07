#!/usr/bin/env python3
"""RUTM08 configurator — bench Web UI (FastAPI).

Workflow (one router at a time, no manifest):
  1. Pick the role: Server (main router of a server box) or Edge (the router
     inside a Gotcha edge box). It persists, so a batch is one choice.
  2. Plug a RUTM08 into the laptop (it boots at 192.168.1.1).
  3. The UI detects it, reads its LAN MAC over ARP, and asks for the site name
     and the factory label password.
  4. Submit -> full pipeline: set password -> hostname rut-<site> (rut-edge-<site>)
     -> timezone -> firmware -> RMS -> Tailscale -> [edge: port forwards, WAN
     access, DHCP pool, static WAN 192.168.88.20] -> verify -> move LAN to
     192.168.88.1 (edge: 192.168.89.1).
  5. Unplug it and plug in the next one.

The common bench-UI shell (run machinery, routes, WebSocket) lives in
bench_core.bench_ui; this file adds only the RUTM specifics: the role toggle,
the three-address detection (the factory address AND each role's final LAN, so
an already-moved router can be re-run and its role told from where it answers)
and the configure_rutm pipeline call.

The role is station state, not config: it lives in `role-state.json` beside the
tool (the `ip-state.json` idea), so flipping it never changes `config_hash`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from bench_core import DEFAULT_HOST, DEFAULT_USERNAME, device_name
from bench_core.bench_ui import (
    DETECT_TIMEOUT_SEC,
    TERMINAL_PHASES,
    BenchConfigurator,
    read_device_mac,
)
from bench_core.run_record import build_run_entry

from rutm_configure import (
    DEFAULT_RUTM_LAN_IP,
    DEFAULT_RUTM_PREFIX,
    ROLE_EDGE,
    ROLE_SERVER,
    ROLES,
    RutmClient,
    configure_rutm,
    effective_settings,
    verify_rutm,
)

BASE_DIR = Path(__file__).resolve().parent

ROLE_LABELS = {ROLE_SERVER: "Server", ROLE_EDGE: "Edge"}


class ConfigureBody(BaseModel):
    site_name: str
    initial_password: str = ""


class RoleBody(BaseModel):
    role: str


class RoleStore:
    """The role the next router is provisioned as, persisted per station.

    The same idea as `IpModeStore`: an operator picks it once and works through
    a batch, so it survives the next unit and a restart. Kept out of the config,
    which is the station's checked-in intent — and whose fingerprint would
    otherwise change every time the toggle did.
    """

    FILENAME = "role-state.json"

    def __init__(self, path: Path, logger: Optional[logging.Logger] = None) -> None:
        self.path = Path(path)
        self.log = logger or logging.getLogger(__name__)
        self.role = ROLE_SERVER
        self.load()

    def load(self) -> None:
        """Read the saved role; anything missing, unreadable or unknown is the
        server role, which is what the tool did before roles existed."""
        data: dict = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self.log.warning("Could not read %s — starting on the server role.",
                             self.path.name)
        role = data.get("role") if isinstance(data, dict) else None
        self.role = role if role in ROLES else ROLE_SERVER

    def set(self, role: str) -> None:
        role = (role or "").strip().lower()
        if role not in ROLES:
            raise ValueError(f"'{role}' is not a RUTM08 role "
                             f"({', '.join(ROLES)}).")
        self.role = role
        self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps({"role": self.role}), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            self.log.warning("Could not persist the role to %s — the selection "
                             "will not survive a restart.", self.path)


class RutmConfigurator(BenchConfigurator):
    title = "RUTM08 Configurator"
    port = 8004
    html_file = "rutm.html"
    config_filename = "config/rutm.config.json"
    log_filename = "rutm-config.log"
    tailscale_label = "rutm"
    history_limit = 30  # full step logs per entry — the JSON files are the archive
    label_scan_enabled = True  # read the factory password off the QR label (TEC-349)
    retain_label_password = True  # and keep it on bench-central (TEC-845)
    verify_supported = True    # mutation-free re-check of a finished router (TEC-348)
    record_tool = "rutm"

    def __init__(self, base_dir: Path) -> None:
        self.roles = RoleStore(base_dir / RoleStore.FILENAME)
        super().__init__(base_dir)
        self.roles.log = self.logger

    # ── state ────────────────────────────────────────────────────────────────

    def initial_state(self) -> dict:
        return {
            "phase": "waiting",        # waiting|detected|configuring|configured|
                                       # verifying|verified|error
            "detected": False,
            "active_host": None,       # the address the router answered on
            "active_mac": None,
            "at_final_lan": False,     # on the selected role's LAN -> probably provisioned
            "detected_role": None,     # the role whose final LAN it answered on
            "busy": False,
            "message": "Plug in the first RUTM08…",
            "last_result": None,
            "history": [],
        }

    @property
    def role(self) -> str:
        return self.roles.role

    def effective(self, role: Optional[str] = None) -> dict:
        """The settings `role` (default: the selected one) would run with."""
        return effective_settings(self.cfg, role or self.role)

    def role_lans(self) -> dict:
        """{role: the final LAN address it moves a router to}."""
        return {r: (self.effective(r).get("lan_ip") or "").strip() for r in ROLES}

    def extra_public_state(self) -> dict:
        eff = self.effective()
        wan = eff.get("wan", {}) or {}
        dhcp = eff.get("dhcp", {}) or {}
        if wan.get("enabled"):
            wan_text = (f"{wan.get('ipaddr', '?')}/{wan.get('netmask', '?')} via "
                        f"{wan.get('gateway') or 'no gateway'}")
        else:
            wan_text = "DHCP from the uplink"
        if dhcp.get("enabled"):
            start, limit = int(dhcp.get("start", 0)), int(dhcp.get("limit", 0))
            pool = f".{start}-.{start + limit - 1}"
        else:
            pool = "stock"
        return {"name_prefix": eff.get("name_prefix", DEFAULT_RUTM_PREFIX),
                "role": self.role,
                "roles": list(ROLES),
                "final_lan_ip": eff.get("lan_ip", ""),
                "wan_summary": wan_text,
                "dhcp_pool": pool,
                "port_forward_count": (len(eff.get("port_forwards") or [])
                                       if self.role == ROLE_EDGE else 0)}

    def role_mismatch(self) -> Optional[str]:
        """The other role, when the detected router answers on ITS final LAN."""
        detected = self.state.get("detected_role")
        return detected if detected and detected != self.role else None

    def role_message(self) -> str:
        label = ROLE_LABELS[self.role]
        return (f"Role: {label} — new routers are named "
                f"{self.effective().get('name_prefix', DEFAULT_RUTM_PREFIX)}<site> "
                f"and end on {self.effective().get('lan_ip', '')}.")

    # ── pipeline hooks ─────────────────────────────────────────────────────────

    def run_config(self) -> dict:
        """The selected role's settings. A verify pass ignores these and starts
        from the base config — see `verify_pipeline`."""
        return self.effective()

    def hostname_for(self, inputs: dict) -> str:
        """The name this run is about. On a verify run there may be no site name
        — it is recovered from the unit's configure record, which needs the
        serial and so can't happen until the pipeline has logged in. The base
        only needs this for the "Verifying …" line, so fall back to the address.
        """
        site = inputs.get("site_name") or ""
        if not site:
            return inputs.get("host") or "the connected router"
        prefix = self.effective(inputs.get("role")).get("name_prefix", DEFAULT_RUTM_PREFIX)
        return device_name(site, prefix)

    def client_host(self, inputs: dict) -> str:
        return inputs["host"]

    def build_client(self, run_cfg: dict, host: str):
        return RutmClient(
            host=host,
            username=run_cfg.get("username", DEFAULT_USERNAME),
            scheme=run_cfg.get("scheme", "https"),
            verify=not run_cfg.get("insecure", True),
        )

    def run_pipeline(self, client, run_cfg: dict, inputs: dict) -> dict:
        return configure_rutm(client, site_name=inputs["site_name"],
                              initial_password=inputs["initial_password"], settings=run_cfg,
                              role=inputs.get("role") or self.role)

    def verify_pipeline(self, client, run_cfg: dict, inputs: dict) -> dict:
        """Mutation-free re-check of a finished router (TEC-348).

        Handed the BASE config, not `run_cfg`: that one is merged for the
        toggle, and the role a unit is checked as comes from its own record
        first. `verify_rutm` merges once it knows."""
        return verify_rutm(client, settings=json.loads(json.dumps(self.cfg)),
                           resolve=self.verify_resolver(inputs),
                           site_name=inputs.get("site_name") or "",
                           fallback_role=inputs.get("fallback_role") or self.role)

    def verify_inputs(self, body) -> dict:
        """The site name is the one per-unit expectation a RUTM08 has, and the
        operator may know it when the record doesn't. Routed through the shared
        `expected` overrides so the lookup treats it as the deliberate override
        it is (and blanks stay blanks). A `role` in the overrides is the same
        kind of statement and lands there too.

        With no record at all, the unit is checked as the role whose LAN it
        answered on, then as the toggle says."""
        inputs = super().verify_inputs(body)
        inputs["site_name"] = (body.expected or {}).get("site_name", "")
        inputs["fallback_role"] = self.state.get("detected_role") or self.role
        return inputs

    def build_entry(self, result: dict, inputs: dict, duration: int) -> dict:
        ident = result["identity"]
        role = (result.get("role") or inputs.get("role")
                or inputs.get("fallback_role") or self.role)
        eff = self.effective(role if role in ROLES else ROLE_SERVER)
        device = {
            # Both pipelines report the name authoritatively as `name`: the
            # one configure WROTE, or the one verify CHECKED AGAINST
            # (recovered from the configure record). Empty on a verify run
            # that found no recorded name — better an empty field than the
            # run label, which is the device's address.
            "hostname": result["name"] if "name" in result else result["hostname"],
            "site_name": inputs.get("site_name")
                         or (inputs.get("expected") or {}).get("site_name", ""),
            "password_source": self.password_source(inputs, "initial_password"),
            # What a verify pass reads back to know how to check this unit.
            "role": role,
            "ip": result.get("lan_ip") or eff.get("lan_ip", DEFAULT_RUTM_LAN_IP),
        }
        if role == ROLE_EDGE:
            device["wan_ip"] = (result.get("wan_ip")
                                or (eff.get("wan", {}) or {}).get("ipaddr", ""))
        return build_run_entry(
            tool="rutm",
            ok=result["ok"],
            error=result["error"],
            serial=ident.get("serial", "unknown"),
            mac=ident.get("mac", inputs.get("mac") or "unknown"),
            model=ident.get("model", "RUTM08"),
            firmware=ident.get("firmware", "unknown"),
            duration_s=duration,
            verification=result.get("verification", []),
            warnings=result.get("warnings", []),
            steps=result["steps"],
            log=result["log"],
            device=device,
        )

    def dismiss_message(self) -> str:
        return "Plug in the next RUTM08…"

    # ── detection loop ──────────────────────────────────────────────────────────

    def _detect_host(self) -> Optional[str]:
        """First address a router answers on: the factory IP, then each role's
        final LAN (an already-provisioned unit plugged back in). Returns None if
        none answers."""
        port = 443 if self.cfg.get("scheme", "https") == "https" else 80
        hosts = [self.cfg.get("host", DEFAULT_HOST)]
        for lan_ip in self.role_lans().values():
            if lan_ip and lan_ip not in hosts:
                hosts.append(lan_ip)
        for host in hosts:
            try:
                with socket.create_connection((host, port), timeout=DETECT_TIMEOUT_SEC):
                    return host
            except OSError:
                continue
        return None

    def _classify(self, host: str) -> None:
        """Which role's final LAN `host` is, if any, and whether that is the
        selected role's. The factory address is neither."""
        factory = self.cfg.get("host", DEFAULT_HOST)
        lans = self.role_lans()
        self.state["detected_role"] = (None if host == factory else
                                       next((r for r, ip in lans.items() if ip == host),
                                            None))
        self.state["at_final_lan"] = host == lans[self.role] and host != factory

    async def poll_once(self, loop) -> None:
        if self.state["busy"]:
            return  # mid-run the device reboots/moves IP — leave detection alone

        host = await loop.run_in_executor(None, self._detect_host)
        self.state["detected"] = bool(host)

        if host:
            self.state["active_host"] = host
            self._classify(host)
            mac = await loop.run_in_executor(None, read_device_mac, host)
            self.state["active_mac"] = mac

            if self.state["phase"] in TERMINAL_PHASES:
                pass  # still plugged in after a run; wait for unplug
            else:
                self.state["phase"] = "detected"
                other = self.role_mismatch()
                if other:
                    self.state["message"] = (
                        f"Router detected on {host} (MAC {mac or 'unknown'}) — that is "
                        f"the {ROLE_LABELS[other]} LAN, so it looks like a unit "
                        f"provisioned as {ROLE_LABELS[other]}. Switch the role to "
                        f"{ROLE_LABELS[other]} to re-run it; Verify works as it is.")
                elif self.state["at_final_lan"]:
                    self.state["message"] = (f"Router detected on {host} (MAC {mac or 'unknown'}) "
                                             "— already moved to the final LAN, so it was likely "
                                             "provisioned before. To re-run it, leave the label "
                                             "password empty.")
                else:
                    self.state["message"] = (f"New router detected on {host} "
                                             f"(MAC {mac or 'unknown'}). Enter the site name and "
                                             "the label password, then Configure.")
        else:
            self.state["active_host"] = None
            self.state["active_mac"] = None
            self.state["at_final_lan"] = False
            self.state["detected_role"] = None
            if self.state["phase"] in ("detected", *TERMINAL_PHASES):
                self.state["phase"] = "waiting"
                self.state["message"] = "Plug in the next RUTM08…"

    # ── routes ────────────────────────────────────────────────────────────────

    def register_routes(self, app) -> None:
        @app.post("/api/configure")
        async def configure(body: ConfigureBody):
            site_name = body.site_name.strip()
            if not site_name:
                return {"error": "Enter a site name."}
            if not re.search(r"[A-Za-z0-9]", site_name):
                return {"error": "The site name needs at least one letter or digit."}
            if not self.state["detected"]:
                return {"error": "No router is currently detected."}
            other = self.role_mismatch()
            if other:
                return {"error": (f"This router answers on the {ROLE_LABELS[other]} LAN. "
                                  f"Switch the role to {ROLE_LABELS[other]} to re-run it, "
                                  "or Verify it as it is.")}
            host = self.state["active_host"] or self.cfg.get("host", DEFAULT_HOST)
            inputs = {"site_name": site_name, "role": self.role,
                      "host": host, "mac": self.state["active_mac"],
                      **self.label_password_inputs(body.initial_password,
                                                   key="initial_password")}
            label = f"{self.hostname_for(inputs)} ({host})"
            if not await self.execute_run(inputs, label):
                return {"error": "A configuration is already in progress."}
            return self.public_state()

        @app.post("/api/role")
        async def set_role(body: RoleBody):
            """Select the role the next router is provisioned as. Refused mid-run:
            the run already took its settings, and its record would then say a
            role it was not given."""
            if self.state["busy"]:
                return {"error": "A run is in progress — switch the role once it "
                                 "has finished."}
            try:
                self.roles.set(body.role)
            except ValueError as e:
                return {"error": str(e)}
            if self.state.get("active_host"):
                self._classify(self.state["active_host"])
            self.state["message"] = self.role_message()
            self.logger.info("%s", self.role_message())
            return self.public_state()

    # ── CLI banner ──────────────────────────────────────────────────────────────

    def print_banner(self) -> None:
        print(f"  role         : {ROLE_LABELS[self.role]} "
              f"(names {self.effective().get('name_prefix', DEFAULT_RUTM_PREFIX)}<site>; "
              "switch it in the UI)")
        print(f"  device       : {self.cfg.get('host', DEFAULT_HOST)} "
              "(laptop must be on 192.168.1.x)")
        print(f"  final LAN    : {self.effective().get('lan_ip', DEFAULT_RUTM_LAN_IP)} "
              "(applied as the last step)")
        print(f"  config       : "
              f"{'config/rutm.config.json' if self.cfg else 'MISSING — copy the example'}")
        fw_cfg = self.cfg.get("firmware", {}) or {}
        if fw_cfg.get("mode") == "local":
            fw_bin = self.base_dir / (fw_cfg.get("bin_path") or "")
            fw_state = "found" if fw_bin.is_file() else "MISSING — download it before configuring"
            print(f"  firmware     : local image {fw_bin.name} ({fw_state})")


configurator = RutmConfigurator(BASE_DIR)
app = configurator.build_app()


if __name__ == "__main__":
    configurator.run()
