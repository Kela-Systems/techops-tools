#!/usr/bin/env python3
"""
Fleet rollout of TEC-359 (SIM failover + per-operator data limits) to OTD500s
that are already in the field.

The bench configurator does this to a device sitting on your desk. This does the
same two steps — `configure_sim_switch` then `install_quota_sync`, from the same
bench_core code, so there is no second implementation to keep in step — over the
tailnet to devices that are already deployed.

WHY THIS IS NOT JUST A FOR-LOOP
The limits are enforced, not advisory. `quota_limit` with `enabled=1` and a limit
below what the SIM has already spent this period cuts the data as soon as the
script writes it, and `sim_switch` treats `data_limit` as a failover trigger — so
such a device fails over on the spot and, because failover is sticky
(`enable_back=0`), stays there. A device whose both SIMs are over is left with no
uplink and will not recover on its own. That is the whole reason this tool
defaults to reading, refuses by default anything it cannot prove is safe, and
never half-configures a device.

WHAT IT DOES DIFFERENTLY FROM THE PRE-FLIGHT
`quota_preflight.py` predicts from Tobee: RMS's idea of which ICCID is in which
slot, and Droam's carrier-side usage. This connects to the device and reads the
ground truth instead — the ICCIDs UCI actually reports, the `quota_limit` section
that is actually there, and (when the device will tell us) its own usage counter,
which is the number `quota_limit` actually enforces against. So --check is worth
running even where the pre-flight said everything was fine, and it is the only
way to clear the devices whose SIMs Droam has no usage figure for.

THE RUNBOOK

  0. Refresh the pre-flight so the device list and the tailnet addresses are
     current. This tool reads its target list from that report.

         python3 quota_preflight.py

  1. Look at what the fleet would get, without connecting to anything:

         python3 quota_rollout.py --plan-only

  2. Connect read-only to the ready group and compare intent against reality.
     Nothing is written; this is safe to run at any time, on any group.

         python3 quota_rollout.py --check

  3. Apply to ONE named device, then look at it:

         python3 quota_rollout.py --apply --yes --device otd-kela-fob-03

     Confirm on the device (or with --check again) that both slots carry the
     limit you expect and that the SIM in use did not change:

         ssh root@<tailnet-ip> 'uci show quota_limit; logread -e kela-quota-sync | tail'

  4. Then the next few, still named explicitly. There is NO group-wide apply:
     --apply refuses to run without --device, and applies sequentially in the
     order given, so a surprise stops after one device.

         python3 quota_rollout.py --apply --yes \
             --device otd-kela-fob-07 --device otd-kela-fob-09,otd-kela-fob-10

     A device already applied is skipped on re-runs (state file) unless --redo,
     so repeating a command is safe.

  5. Anything the gates refused is listed in the run summary with its reason.
     Work through those individually; --allow-unknown-usage and --force exist for
     when you have checked a device by hand and decided.

DEVICES WITH NO TAILNET ADDRESS (--via rms)
Roughly a third of the fleet is online in RMS but has no tailnet address, so
there is nothing to SSH to. For those, `--via rms` sends the same commands
through the RMS command relay instead, which addresses a device by its RMS id
and needs no route to it at all:

    export RMS_API_TOKEN=...           # or put it in scripts/.env
    python3 quota_rollout.py --check --via rms --device otd-kela-afb25-1
    python3 quota_rollout.py --apply --yes --via rms --device otd-kela-afb25-1

Nothing about WHAT gets written changes — `configure_sim_switch`,
`install_quota_sync` and the verification all run unmodified, because only the
shell underneath them is swapped (see RmsClient). What does change:

  * It is slow. Every command is an HTTP POST plus a polled channel read, so a
    device takes minutes rather than seconds. One device at a time.
  * The relay returns output but no exit status, so each command is wrapped to
    have the device report its own `$?`. A reply that comes back without that
    marker is treated as a failure, never as success.
  * The relay also reports "Timeout." for commands that DID run on the device,
    so every command sent has to be idempotent and is retried once. That is why
    a file goes up as ONE base64 heredoc that rebuilds and md5-checks it, rather
    than as a series of appends that a retry would double.
  * Firmware cannot be read over REST, so the version from the pre-flight
    report (RMS/Tobee, not read live) is what the firmware gate sees.
  * It only works while RMS says the device is online.

THE SAFETY GATES, AND WHAT EVIDENCE THEY USE
A device is refused (reported, not written to) when any of these hold:
  * its configuration could not be read at all;
  * it is not on the verified firmware (--allow-unverified-fw);
  * a slot would get an ENFORCED cap and no per-SIM usage figure is available
    (--allow-unknown-usage);
  * a slot has already spent its cap, i.e. writing it would cut that SIM now
    (--force, and only ever after looking at that device).
The per-SIM figure is Droam's, taken from the pre-flight report and matched on the
ICCID the DEVICE reports — so a SIM that has moved since RMS last looked
contributes nothing rather than the wrong SIM's usage. The device's own mdcollect
counter is read and displayed, but does not satisfy the gate by default; see
read_usage() for why not.

WHAT GETS WRITTEN, PER DEVICE
  * `sim_switch` UCI rules for slots 1 and 2 (+ the eSIM section left disabled),
    then a `sim_switch` service restart. Harmless on its own: it changes when the
    device fails over, not whether it has data.
  * `/usr/local/bin/kela-quota-sync` + its boot hook + a 10-minute cron entry +
    the /etc/sysupgrade.conf keep list, and then one immediate run of the script,
    which is what writes `quota_limit`.
Nothing else. No password change, no hostname, no firmware, no RMS, no Tailscale
— this is deliberately not the factory pipeline.

REQUIREMENTS
  * For the default SSH transport: on the tailnet (devices are reached at their
    100.x address). For --via rms: $RMS_API_TOKEN, and no route to the device.
  * The device's admin password, for SSH only — the relay already runs as root.
    Taken from --password, then $OTD_PASSWORD, then
    `new_password` in site.config.json. Devices that never went through the bench
    with that password will fail to log in and are reported as such, not retried
    forever.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as futures
import difflib
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "bench" / "bench-core" / "src"))

from bench_core import (  # noqa: E402
    DEFAULT_MODEM_ID,
    ESIM_SLOT,
    QUOTA_SYNC_PATH,
    SIM_SLOTS,
    TeltonikaClient,
    VERIFIED_SIM_SWITCH_FW,
    assert_device_model,
    fw_carries_version,
    validate_sim_switch_config,
)

# The operator table and its prefix-matching order live in the pre-flight, which
# in turn takes them from bench_core. Importing it keeps all three agreeing.
from quota_preflight import _gb, load_table, match_operator  # noqa: E402

DEFAULT_CONFIG = REPO / "bench" / "otd-config-ui" / "config" / "site.config.json"
DEFAULT_REPORT = HERE / "quota_preflight.json"
STATE_PATH = HERE / "quota_rollout_state.json"
MODEL_PREFIX = "OTD"
# quota_limit's section for a slot, and the option names the on-device script
# writes. Both must match render_quota_sync_script(), or the diff below would
# compare against the wrong thing.
SECTION_FOR = {slot: f"mob1s{slot}a1" for slot in SIM_SLOTS + (ESIM_SLOT,)}
MANAGED_OPTIONS = ("enabled", "data_limit", "period", "reset_day")

_UCI_LINE = re.compile(r"^([\w.@\[\]-]+)\.([\w-]+)=(?:'(.*)'|(.*))$")
# md5sum prints "<hash>  <path>", but the reply may carry other lines with it, so
# the hash is picked out by shape rather than by position.
_MD5_RE = re.compile(r"[0-9a-f]{32}")


# --- reading the device ------------------------------------------------------

def _uci_map(text: str, package: str) -> dict[str, dict[str, str]]:
    """`uci show <pkg>` output -> {section: {option: value}}."""
    out: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        m = _UCI_LINE.match(line.strip())
        if not m:
            continue
        path, option, quoted, bare = m.groups()
        if not path.startswith(f"{package}."):
            continue
        section = path[len(package) + 1:]
        out.setdefault(section, {})[option] = quoted if quoted is not None else bare
    return out


def _slot_iccids(simcard: dict[str, dict[str, str]]) -> dict[int, dict[str, str]]:
    """{slot: {iccid, modem}} from `uci show simcard`.

    Matched on the `position` option rather than section order, and falling back
    to index = slot - 1 — exactly what index_for() does on the device, so this
    reads the same SIM the device will read.
    """
    by_position = {opts.get("position"): opts for opts in simcard.values()}
    out: dict[int, dict[str, str]] = {}
    for slot in SIM_SLOTS:
        opts = by_position.get(str(slot)) or simcard.get(f"@sim[{slot - 1}]") or {}
        out[slot] = {"iccid": (opts.get("iccid") or "").strip(),
                     "modem": (opts.get("modem") or "").strip()}
    return out


def read_device(client: TeltonikaClient, identity: dict,
                fallback_fw: str = "") -> dict:
    """Everything needed to decide about this device.

    One command per fact, rather than one chained payload with markers: the
    chained version came back empty against the bench OTD500 even though the
    same commands answer fine on their own, and a read that silently returns
    nothing is the worst possible input to a gate that decides whether to cut a
    SIM's data. They share one SSH connection, so the extra round trips cost
    almost nothing.

    Firmware comes from the REST identity first — /etc/version is empty on this
    build, and an empty version string looks exactly like an unverified firmware.
    """
    def sh(command: str) -> str:
        return client.ssh_exec(command, check=False).strip()

    # A positive signal that commands run at all, so "nothing is configured" can
    # never be confused with "the device did not answer".
    read_ok = sh("echo ok") == "ok"
    simcard = _uci_map(sh("uci show simcard 2>/dev/null"), "simcard")
    rest_fw = (identity.get("firmware") or "").strip()
    return {
        "read_ok": read_ok,
        "firmware": (rest_fw if rest_fw and rest_fw != "unknown"
                     else sh("cat /etc/version 2>/dev/null") or fallback_fw),
        "simcard_sections": len(simcard),
        "slots": _slot_iccids(simcard),
        "quota": _uci_map(sh("uci show quota_limit 2>/dev/null"), "quota_limit"),
        "sim_switch_sections": len(_uci_map(sh("uci show sim_switch 2>/dev/null"),
                                            "sim_switch")),
        "quota_sync_installed": sh(f"[ -x {QUOTA_SYNC_PATH} ] && echo yes") == "yes",
        "modem": next((o.get("modem") for o in simcard.values() if o.get("modem")),
                      DEFAULT_MODEM_ID),
    }


def read_usage(client: TeltonikaClient, facts: dict) -> dict[int, dict]:
    """The device's own data counter per slot, ADVISORY ONLY.

    `quota_limit` enforces against mdcollect, so this ought to be the best
    evidence there is. Two things stop it from being the gate:

      * The call is not in Teltonika's public API. On 07.22.3 it answers
        `{"tx":941741055,"rx":4730135753}` — bytes, so tx+rx is the total.
      * On the bench unit BOTH slots returned the identical figure, which means
        the interface/sim/modem arguments were not honoured (or the counter is
        modem-wide). A device-wide number compared against a per-slot cap is not
        a safe basis for a decision either way.
      * The window is unknown. `get_raw_total` reads like a lifetime counter, and
        the caps are per billing period, so even "well under the cap" proves
        nothing unless the window is known to cover the period.

    So it is reported, not trusted: the gate uses Droam's per-ICCID figure unless
    --trust-device-counter says otherwise. If you find the two slots reporting
    DIFFERENT numbers on a dual-SIM device, the arguments are being honoured
    after all and this can be promoted.
    """
    out: dict[int, dict] = {}
    for slot in SIM_SLOTS:
        args = json.dumps({"interface": SECTION_FOR[slot], "sim": str(slot),
                           "modem": facts["modem"]})
        raw = client.ssh_exec(
            f"ubus -S call mdcollect get_raw_total {json_arg(args)} 2>&1",
            check=False).strip()
        total_mib, note = None, raw[:200]
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            counters = {k: v for k, v in payload.items()
                        if isinstance(v, (int, float)) and k in ("tx", "rx", "total",
                                                                 "raw_total", "bytes")}
            if counters:
                total_mib = sum(counters.values()) / (1024 * 1024)
                note = " + ".join(f"{k}={v}" for k, v in sorted(counters.items()))
        out[slot] = {"counter_mib": total_mib, "raw": note}
    return out


def json_arg(text: str) -> str:
    """Quote a JSON blob for a POSIX shell without mangling its double quotes."""
    return "'" + text.replace("'", "'\\''") + "'"


# --- deciding ---------------------------------------------------------------

def plan_device(facts: dict, usage: dict, predicted: dict, operators: list[dict],
                unknown: dict, *, trust_counter: bool = False) -> list[dict]:
    """One row per managed slot: what it has now, what it would get, and whether
    that would cut its data."""
    rows = []
    for slot in SIM_SLOTS:
        iccid = facts["slots"][slot]["iccid"]
        name, row = match_operator(iccid, operators, unknown) if iccid else \
            ("unknown (empty slot)", unknown)
        current = facts["quota"].get(SECTION_FOR[slot], {})
        wanted = {"enabled": "1" if row["enabled"] else "0",
                  "data_limit": str(row["data_limit_mb"]),
                  "reset_day": str(row["reset_day"])}
        counter = usage.get(slot, {}).get("counter_mib")
        # Droam's figure for the SAME ICCID, from the pre-flight. Matched on the
        # ICCID the DEVICE reports, so a stale RMS record (the SIM having moved)
        # contributes nothing instead of describing the wrong SIM.
        droam = next((s.get("used_mib") for s in predicted.get("slots", [])
                      if s.get("slot") == slot and s.get("iccid") == iccid), None)
        known = [v for v in (droam, counter if trust_counter else None)
                 if v is not None]
        rows.append({
            "slot": slot,
            "iccid": iccid,
            "operator": name,
            "empty": not iccid,
            "limit_mib": row["data_limit_mb"],
            "reset_day": row["reset_day"],
            "enforced": bool(row["enabled"]),
            "current": {k: current.get(k, "") for k in MANAGED_OPTIONS},
            "section_exists": bool(current),
            "changes": {k: v for k, v in wanted.items() if current.get(k) != v},
            "counter_mib": counter,
            "used_mib_droam": droam,
            # Conservative: if two figures disagree, believe the higher one. Being
            # wrong in this direction skips a safe device; the other way cuts a
            # live one.
            "used_mib": max(known) if known else None,
            "usage_note": usage.get(slot, {}).get("raw", ""),
        })
    return rows


def gate(facts: dict, rows: list[dict], *, allow_unverified_fw: bool,
         allow_unknown_usage: bool) -> list[str]:
    """Every reason not to write to this device. Empty means go."""
    stop = []
    if not facts.get("read_ok"):
        stop.append("commands do not run over SSH on this device, so nothing read "
                    "from it can be trusted")
    elif not facts.get("simcard_sections"):
        stop.append("the device reported no `simcard` sections, so which SIM is in "
                    "which slot could not be established")
    if not fw_carries_version(facts["firmware"], VERIFIED_SIM_SWITCH_FW):
        if not allow_unverified_fw:
            stop.append(f"firmware {facts['firmware'] or 'unknown'} is not the "
                        f"verified {VERIFIED_SIM_SWITCH_FW} (--allow-unverified-fw "
                        f"to override once you have checked `uci export sim_switch`)")
    for row in rows:
        if row["empty"] or not row["enforced"]:
            continue
        if row["used_mib"] is None:
            if not allow_unknown_usage:
                stop.append(f"slot {row['slot']} ({row['operator']}) would get an "
                            f"enforced {_gb(row['limit_mib'])} cap and no usage "
                            f"figure could be read (--allow-unknown-usage to accept)")
        elif row["used_mib"] >= row["limit_mib"]:
            stop.append(f"slot {row['slot']} ({row['operator']}) has spent "
                        f"{_gb(row['used_mib'])} of the {_gb(row['limit_mib'])} cap "
                        f"it would get — writing it would cut this SIM's data now")
    return stop


# --- addressing -------------------------------------------------------------

def tailnet_nodes() -> dict[str, str]:
    """{hostname_lower: 100.x} from the local tailscale CLI, for devices Tobee
    did not manage to pair. Empty (not fatal) when the CLI is not around."""
    for cli in ("tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
        try:
            proc = subprocess.run([cli, "status", "--json"], capture_output=True,
                                  text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            continue
        nodes = {}
        for peer in list((data.get("Peer") or {}).values()) + [data.get("Self") or {}]:
            name = (peer.get("HostName") or "").lower()
            ips = [ip for ip in (peer.get("TailscaleIPs") or []) if ":" not in ip]
            if name and ips:
                nodes[name] = ips[0]
        return nodes
    return {}


def resolve_host(device: dict, nodes: dict[str, str]) -> tuple[str, str]:
    """(address, note) to connect to.

    The LIVE tailnet comes first, matched on the device name exactly, and the
    report's address second. That order is the opposite of the obvious one and
    deliberate: a device removed from the tailnet and rejoined gets a NEW
    address, leaving the reported one stale — and a released Tailscale address
    can later belong to a different node. Reaching the wrong device is far worse
    than not reaching this one, so the cached value is only a fallback, and the
    serial is checked after connecting (see identity_mismatch) either way.

    Only an exact name match counts. If the tailnet node is named anything else
    (Tailscale renames a rejoined node to <name>-1 when the name is still taken),
    it is not found here and --host is the way in.
    """
    live = nodes.get(device["name"].lower(), "")
    if live:
        return live, ""
    cached = device.get("tailscale_ip") or ""
    if cached:
        return cached, ("from the pre-flight report, not confirmed against the live "
                        "tailnet — verify the serial in the output")
    return "", ""


def identity_mismatch(device: dict, identity: dict) -> str:
    """A message when the device that answered is not the one we meant, else "".

    The report records each device's serial, and the serial is read off the
    connected device's own manufacturer block, so comparing them catches a stale
    or reused address before anything is written. Only checked when both are
    known: refusing a device because the report has no serial for it would be
    noise, not safety.
    """
    expected = str(device.get("serial") or "").strip()
    actual = str(identity.get("serial") or "").strip()
    if not expected or not actual:
        return ""
    if actual.lower() == "unknown":
        return (f"could not read a serial from the device that answered, so there is no "
                f"way to confirm it is {device['name']} (serial {expected}).")
    if actual.lower() == expected.lower():
        return ""
    return (f"the device that answered reports serial {actual}, but the report says "
            f"{device['name']} is serial {expected} — this is a DIFFERENT device, "
            f"most likely a stale address. Nothing was read further or written.")


# --- the RMS relay transport -------------------------------------------------

class RmsClient(TeltonikaClient):
    """A TeltonikaClient whose shell runs through the RMS command relay, for the
    devices that have no tailnet address.

    Everything bench_core does to a device is a shell command (`ssh_exec`) or a
    file built out of them, so replacing only the shell gets sim_switch and
    quota-sync onto an RMS-only device without a second implementation of WHAT to
    write — the part that must not be allowed to diverge. `configure_sim_switch`,
    `install_quota_sync` and `_verify_sim_switch` run unmodified.

    What the relay does not give us, and what is done about it:

      * No exit status — it returns output only. Every command is therefore
        wrapped so the device echoes its own `$?` on a marker line, and a reply
        without that marker is treated as a failure. Guessing "it worked" here
        could mean a half-written UCI section on a live device.
      * No stdin, and an undocumented length limit, so bench_core's 5 KB heredoc
        file write is a bad bet. `_put_file` is overridden to send base64 in
        chunks and verify with md5 before anything is moved into place.
      * Latency: each command is a POST plus a polled channel read, so a full
        device takes minutes rather than seconds. That is why applying is
        sequential and per-device anyway.

    The relay must accept multi-line commands: bench_core writes the
    /etc/sysupgrade.conf block with a heredoc, and the `$?` wrapper is a second
    line. If that turns out not to be true, this will fail loudly on the marker
    check rather than half-apply.
    """

    RC_MARK = "___KELA_RC="
    # A file goes up in ONE command, so the ceiling has to hold the whole base64
    # payload. 12 KB commands were accepted by the relay in testing against an
    # OTD500; this leaves room under that and is still 3x the biggest file we
    # send (the ~3.5 KB quota script, 4.7 KB once base64'd). Exceeding it is an
    # error rather than a silent split — see _put_file.
    MAX_COMMAND = 10000
    # Base64 wrapped the way base64(1) itself wraps it, rather than one endless
    # line, so nothing downstream has to cope with a 5 KB line.
    B64_COLS = 76
    # Reserved TLD (RFC 2606): if anything ever tries the REST API on this
    # client it fails immediately instead of reaching some real host.
    HOST = "rms-relay.invalid"

    def __init__(self, device_id: int, name: str = "", relay_timeout: float = 90.0):
        super().__init__(host=self.HOST)
        self.device_id = int(device_id)
        self.name = name or f"RMS #{device_id}"
        self.relay_timeout = relay_timeout
        self.relay_calls = 0

    @staticmethod
    def _retryable(status: str, value: str) -> bool:
        """A reply worth sending again: our own poll deadline, or RMS reporting a
        timeout of its own. Anything else is the device's answer, not transport
        noise, and must be reported rather than papered over by a second try."""
        return status == "timeout" or (status == "error"
                                       and "timeout" in (value or "").lower())

    def login(self, password: str) -> None:
        """No REST login over the relay — it already runs as root on the device.
        The password is only recorded in case something downstream reads it."""
        self.password = password

    def close(self) -> None:
        pass  # nothing is held open

    def ssh_exec(self, command: str, check: bool = True,
                 exec_timeout=None) -> str:
        from sim_audit import rms_run_command  # imported here: the SSH path never needs it

        wrapped = f"{command}\nprintf '{self.RC_MARK}%s\\n' \"$?\""
        timeout = float(exec_timeout or self.relay_timeout)
        status, value = rms_run_command(self.device_id, wrapped, timeout=timeout)
        if self._retryable(status, value):
            # Two different timeouts land here: ours (we stopped polling) and
            # RMS's own ("error"/"Timeout."), which it reports even for commands
            # that DID run on the device. Retrying is only safe because every
            # command this client sends is idempotent — which is exactly why
            # _put_file sends a file as one rebuild-and-verify command rather
            # than as appends that would double up on a retry.
            status, value = rms_run_command(self.device_id, wrapped, timeout=timeout)
            self.relay_calls += 1
        self.relay_calls += 1
        if status != "completed":
            raise SystemExit(f"RMS relay {status} for {self.name}: {value or command}")

        lines = value.splitlines()
        rc = None
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].startswith(self.RC_MARK):
                rc = lines[i][len(self.RC_MARK):].strip()
                del lines[i]
                break
        output = "\n".join(lines).strip()
        if rc is None:
            raise SystemExit(
                f"RMS relay returned no exit status for {self.name} — the command may "
                f"have been truncated, so whether it ran cannot be established.\n"
                f"  command: {command[:120]}\n  reply: {output[:200]}")
        if check and rc != "0":
            raise SystemExit(f"Command failed (rc={rc}) on {self.name}: {command}\n{output}")
        return output

    def _put_file(self, path: str, content: str, *, mode: str = "644") -> None:
        """Send a file in ONE relay command, verified before it lands.

        The whole base64 payload travels in a single heredoc that rebuilds the
        temp file from scratch, decodes it, and prints its md5 — so the command
        is idempotent and self-verifying, and only a matching md5 moves it into
        place. A half-written kela-quota-sync would be executed by cron every 10
        minutes, so "probably arrived" is not good enough.

        It used to append base64 in 1 KB chunks, one command each. The appends
        work, but they turn one write into a dozen round trips, and the relay
        reports "Timeout." for commands that have in fact run on the device
        (observed mid-sequence on a healthy OTD500) — which makes a failed append
        unretryable, since a retry might double it. The observed failure was a
        file that assembled EMPTY while every command reported success. One
        command has one chance to go wrong, and retrying it is always safe.
        """
        payload = base64.b64encode(content.encode()).decode()
        wrapped = "\n".join(payload[i:i + self.B64_COLS]
                            for i in range(0, len(payload), self.B64_COLS))
        tmp = f"/tmp/.kela-put.{self.device_id}"
        want = hashlib.md5(content.encode()).hexdigest()
        # busybox base64 first, openssl as the fallback for a build without it.
        command = (f"rm -f {tmp} {tmp}.b64\n"
                   f"cat > {tmp}.b64 <<'{self.PUT_FILE_EOF}'\n"
                   f"{wrapped}\n{self.PUT_FILE_EOF}\n"
                   f"base64 -d {tmp}.b64 > {tmp} 2>/dev/null || "
                   f"openssl base64 -d -in {tmp}.b64 -out {tmp}\n"
                   f"md5sum {tmp}")
        if len(command) > self.MAX_COMMAND:
            raise SystemExit(
                f"{path} is {len(content)} bytes, which does not fit in one RMS relay "
                f"command ({len(command)} > {self.MAX_COMMAND} chars). Splitting it "
                f"across commands is what this deliberately does not do; send this "
                f"file over SSH instead.")
        log_progress(f"    {path}: {len(content)} bytes in one relay command")
        out = self.ssh_exec(command, check=False)
        got = next((tok for tok in out.split() if _MD5_RE.fullmatch(tok)), "")
        if got != want:
            self.ssh_exec(f"rm -f {tmp} {tmp}.b64", check=False)
            raise SystemExit(
                f"{path} did not arrive intact over the RMS relay (md5 "
                f"{got or 'unreadable'} != {want}). Nothing was moved into place."
                + (f"\n  the device said: {out.strip()[:200]}" if not got else ""))
        self.ssh_exec(f"mkdir -p {posixpath.dirname(path)} && chmod {mode} {tmp} && "
                      f"mv {tmp} {path} && rm -f {tmp}.b64")


def rms_index() -> dict[str, dict]:
    """{device name lower: RMS device record} for the whole RMS inventory.

    RMS addresses devices by its own numeric id, so a name has to be resolved
    through the inventory before anything can be sent to it.
    """
    if not os.environ.get("RMS_API_TOKEN"):
        raise SystemExit(
            "--via rms needs an RMS API token: put RMS_API_TOKEN=... in "
            f"{HERE / '.env'} (see .env.example) or export it.")
    from sim_audit import fetch_rms_devices

    try:
        devices = fetch_rms_devices()
    except Exception as exc:  # noqa: BLE001 - surface the reason, not a traceback
        raise SystemExit(f"Could not read the RMS inventory: {type(exc).__name__}: {exc}")
    return {str(d.get("name", "")).lower(): d for d in devices if d.get("id")}


def log_progress(text: str) -> None:
    print(text, flush=True)


# --- the run ----------------------------------------------------------------

def suggest(missing: list[str], by_name: dict[str, dict]) -> str:
    """Near misses for mistyped device names, so a typo doesn't send someone back
    to the report to hunt for the exact spelling."""
    lines = []
    for name in missing:
        close = difflib.get_close_matches(name.lower(), list(by_name), n=3, cutoff=0.6)
        if close:
            lines.append(f"  did you mean: {', '.join(by_name[c]['name'] for c in close)}")
    return "\n".join(lines)


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except ValueError:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def connect(host: str, passwords: list[str], username: str) -> TeltonikaClient:
    """A logged-in client, trying each candidate password. login() raises
    SystemExit on a bad password, which would otherwise kill the whole run."""
    last = ""
    for password in passwords:
        client = TeltonikaClient(host=host, username=username)
        try:
            client.login(password)
            return client
        except SystemExit as exc:
            last = str(exc)
            client.close()
    raise RuntimeError(f"could not log in ({last or 'no password accepted'})")


def open_client(device: dict, opts: argparse.Namespace, passwords: list[str],
                nodes: dict[str, str], rms: dict[str, dict]) -> tuple[object, str]:
    """(client, address) for this device on the chosen transport, or (None, reason)
    when it cannot be reached at all."""
    if opts.via == "rms":
        entry = rms.get(device["name"].lower())
        if not entry:
            return None, "not in the RMS inventory under this name"
        if entry.get("status") != 1:
            return None, "RMS reports it offline, so the relay cannot reach it"
        client = RmsClient(entry["id"], device["name"], relay_timeout=opts.relay_timeout)
        client.login(passwords[0] if passwords else "")
        return client, f"RMS #{entry['id']}"
    host, note = resolve_host(device, nodes)
    if not host:
        return None, (note or "no tailnet address: not on the live tailnet under this "
                              "name and the report has none — try --via rms")
    return connect(host, passwords, opts.username), (f"{host} ({note})" if note else host)


def handle(device: dict, cfg: dict, opts: argparse.Namespace, operators: list[dict],
           unknown: dict, passwords: list[str], nodes: dict[str, str],
           rms: dict[str, dict]) -> dict:
    """Check (and optionally apply) one device. Never raises: a device that goes
    wrong is a row in the report, not the end of the run."""
    result = {"device": device["name"], "site": device.get("site", ""),
              "group": device.get("group", ""), "host": "", "status": "",
              "transport": opts.via, "reasons": [], "slots": [], "verify": []}
    client = None
    try:
        client, where = open_client(device, opts, passwords, nodes, rms)
        if client is None:
            result["status"] = "unreachable"
            result["reasons"] = [where]
            return result
        result["host"] = where
        identity = client.get_identity()
        result["serial"] = identity.get("serial", "")
        result["model"] = identity.get("model", "")
        # Refuses anything that is not an OTD, including a device whose model
        # could not be read at all.
        assert_device_model(identity, MODEL_PREFIX, "OTD500 quota rollout")
        # Then: is it the RIGHT OTD? A stale address reaches the wrong box, and
        # --force must not be able to talk anyone past this one.
        wrong = identity_mismatch(device, identity)
        if wrong:
            result["status"] = "wrong-device"
            result["reasons"] = [wrong]
            return result
        # The relay gives no REST answer for firmware, so the inventory's value
        # (RMS/Tobee, not read live) is the last resort before refusing.
        facts = read_device(client, identity, fallback_fw=device.get("firmware", ""))
        result["firmware"] = facts["firmware"]
        result["quota_sync_installed"] = facts["quota_sync_installed"]
        usage = read_usage(client, facts)
        rows = plan_device(facts, usage, device, operators, unknown,
                           trust_counter=opts.trust_device_counter)
        result["slots"] = rows
        result["reasons"] = gate(facts, rows,
                                 allow_unverified_fw=opts.allow_unverified_fw,
                                 allow_unknown_usage=opts.allow_unknown_usage)
        pending = any(row["changes"] for row in rows) or not facts["quota_sync_installed"]
        if not opts.apply:
            result["status"] = "would-change" if pending else "already-current"
            return result
        if result["reasons"] and not opts.force:
            result["status"] = "refused"
            return result
        if result["reasons"]:
            result["forced"] = True
        client.configure_sim_switch(cfg)
        client.install_quota_sync(cfg)
        # The private verifier is the exact read-back for these two steps;
        # verify_configuration() would demand the whole factory pipeline's inputs.
        result["verify"] = client._verify_sim_switch(cfg)
        failed = [r["item"] for r in result["verify"] if r["ok"] is False]
        result["status"] = "applied" if not failed else "applied-with-warnings"
        result["reasons"] += [f"verification failed: {i}" for i in failed]
    except (RuntimeError, SystemExit, OSError) as exc:
        result["status"] = "error"
        result["reasons"] = [f"{type(exc).__name__}: {exc}"]
    except Exception as exc:  # noqa: BLE001 - one bad device must not stop the run
        result["status"] = "error"
        result["reasons"] = [f"unexpected {type(exc).__name__}: {exc}"]
    finally:
        if client is not None:
            client.close()
    return result


def describe(result: dict) -> str:
    bits = []
    for row in result.get("slots", []):
        change = ("no change" if not row["changes"]
                  else "set " + ",".join(f"{k}={v}" for k, v in row["changes"].items()))
        if row["empty"]:
            # No SIM, so no usage to report — but the fallback cap IS written to
            # this slot, and saying only "empty" hid that from the one line an
            # operator actually reads before applying.
            bits.append(f"s{row['slot']} empty -> {_gb(row['limit_mib'])} fallback"
                        f"{' ENFORCED' if row['enforced'] else ''} {change}")
            continue
        used = _gb(row["used_mib"]) if row["used_mib"] is not None else "usage ?"
        counter = ("" if row["counter_mib"] is None
                   else f" [dev {_gb(row['counter_mib'])}]")
        bits.append(f"s{row['slot']} {row['operator']} {used}/"
                    f"{_gb(row['limit_mib'])}{counter} {change}")
    return " | ".join(bits)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="connect read-only and report (the default)")
    mode.add_argument("--apply", action="store_true",
                      help="actually write, to named devices only")
    ap.add_argument("--yes", action="store_true", help="required together with --apply")
    ap.add_argument("--plan-only", action="store_true",
                    help="print the operator table and the target list, connect to nothing")
    ap.add_argument("--report", type=Path, default=DEFAULT_REPORT,
                    help="quota_preflight.json to take the device list from")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--group", default="ready",
                    choices=("ready", "firmware", "checks", "offline", "all"),
                    help="which pre-flight group to SCAN with --check (default: ready). "
                         "Ignored by --apply, which only ever takes named devices.")
    ap.add_argument("--device", action="append", default=[], metavar="NAME",
                    help="a device to work on, by its exact name. Repeatable, and "
                         "accepts a comma-separated list. REQUIRED for --apply: there "
                         "is deliberately no way to write to a whole group at once.")
    ap.add_argument("--host", default="",
                    help="check/apply one device at this address instead of reading "
                         "the report — for the bench unit (192.168.1.1) or a device "
                         "the tailnet pairing missed")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop a --check scan after N devices")
    ap.add_argument("--workers", type=int, default=0,
                    help="parallel devices (default: 8 checking, 1 applying)")
    ap.add_argument("--via", default="ssh", choices=("ssh", "rms"),
                    help="transport: ssh over the tailnet (default), or the RMS "
                         "command relay for devices with no tailnet address. RMS "
                         "needs $RMS_API_TOKEN and is minutes-per-device slow.")
    ap.add_argument("--relay-timeout", type=float, default=90.0,
                    help="seconds to wait for one RMS relay command (default 90)")
    ap.add_argument("--username", default="admin", help="REST/WebUI user")
    ap.add_argument("--password", default="", help="device password")
    ap.add_argument("--redo", action="store_true",
                    help="re-apply devices the state file says are done")
    ap.add_argument("--allow-unverified-fw", action="store_true",
                    help="apply to devices not on the verified firmware")
    ap.add_argument("--allow-unknown-usage", action="store_true",
                    help="apply even when no usage figure could be read")
    ap.add_argument("--trust-device-counter", action="store_true",
                    help="let the on-device mdcollect counter satisfy the usage gate "
                         "(only once you have confirmed it reports per slot — see "
                         "read_usage)")
    ap.add_argument("--force", action="store_true",
                    help="apply despite the gates (implies you checked by hand)")
    ap.add_argument("--output-dir", type=Path, default=HERE)
    opts = ap.parse_args()

    named = [name.strip() for spec in opts.device for name in spec.split(",")
             if name.strip()]
    if opts.apply and not opts.yes:
        raise SystemExit("--apply writes to live devices; pass --yes as well.")
    if opts.apply and not (named or opts.host):
        raise SystemExit(
            "--apply needs the devices named explicitly: --device NAME [--device NAME] "
            "(or --host ADDRESS for one off-report device).\nThere is no group-wide "
            "apply on purpose — writing an enforced data limit is not something to do "
            "to a whole group in one command.\nRun --check --group "
            f"{opts.group} first to see the candidates.")
    if not opts.report.exists() and not opts.host:
        raise SystemExit(f"{opts.report} not found — run quota_preflight.py first.")

    cfg = json.loads(opts.config.read_text()).get("sim_switch") or {}
    if not cfg.get("enabled"):
        raise SystemExit(f"{opts.config}: sim_switch is not enabled — nothing to roll out.")
    validate_sim_switch_config(cfg)
    operators, unknown = load_table(opts.config)
    passwords = [p for p in (opts.password, os.environ.get("OTD_PASSWORD", ""),
                             json.loads(opts.config.read_text()).get("new_password", ""))
                 if p]
    # The relay runs as root on the device, so it needs no password of ours.
    if not passwords and opts.via == "ssh":
        raise SystemExit("No device password: pass --password, set $OTD_PASSWORD, or "
                         "put new_password in the site config.")

    if opts.host:
        # No pre-flight row for this one, so there is no Droam figure to compare
        # against — the device's own counter is all the evidence there is.
        devices = [{"name": opts.host, "tailscale_ip": opts.host, "site": "(--host)",
                    "group": "(direct)", "slots": []}]
    elif named:
        # Named devices are taken from the whole report, not from a group: the
        # group is a triage aid, not a permission boundary, and refusing a device
        # because it landed in a different bucket would just be confusing.
        by_name = {d["name"].lower(): d for d in
                   json.loads(opts.report.read_text())["devices"]}
        missing = [n for n in named if n.lower() not in by_name]
        if missing:
            raise SystemExit(
                "Not in the report: " + ", ".join(missing) + "\n" + suggest(missing, by_name)
                + f"\nNames must match exactly. See them with: {Path(__file__).name} "
                  "--plan-only --group all")
        devices = [by_name[n.lower()] for n in named]
    else:
        report = json.loads(opts.report.read_text())
        devices = [d for d in report["devices"]
                   if opts.group == "all" or d.get("group") == opts.group]
    state = load_state()
    if opts.apply and not opts.redo:
        done = [d["name"] for d in devices
                if state.get(d["name"], {}).get("status") == "applied"]
        for name in done:
            print(f"{name}: already applied at {state[name]['at']} — skipping "
                  f"(--redo to write it again).")
        devices = [d for d in devices if d["name"] not in done]
    if opts.limit and not named:
        devices = devices[:opts.limit]

    print(f"Operator table from {opts.config}:")
    for op in operators + [{"name": "unknown (fallback)", "iccid_prefixes": [], **unknown}]:
        print(f"  {op['name']:<20} {','.join(op['iccid_prefixes']) or '(no match)':<18}"
              f"{_gb(op['data_limit_mb']):>11}  day {op['reset_day']:<3}"
              f"{'ENFORCED' if op['enabled'] else 'not enforced'}")
    print(f"  slot {ESIM_SLOT} (eSIM) is forced off.\n")
    if opts.host:
        scope = f"one device at {opts.host}"
    elif named:
        scope = f"{len(devices)} named device(s)"
    else:
        scope = f"{len(devices)} device(s) in group '{opts.group}' of {opts.report.name}"
    print(f"Scope: {scope}.")
    nodes = {} if opts.via == "rms" else tailnet_nodes()
    if opts.plan_only or not devices:
        for dev in devices:
            if opts.via == "rms":
                where = "(via the RMS relay)"
            else:
                # What it would actually connect to, resolved live — not the
                # report's cached address, which is the value that goes stale.
                host, note = resolve_host(dev, nodes)
                where = (host or "(no address)") + (f"  [{note}]" if note else "")
            print(f"  {dev['name']:<34} {where}")
        return

    rms = rms_index() if opts.via == "rms" else {}
    transport = ("the RMS command relay (minutes per device)" if opts.via == "rms"
                 else "SSH over the tailnet")
    if opts.apply:
        print(f"APPLYING over {transport}, in order:")
        for dev in devices:
            if opts.via == "rms":
                entry = rms.get(dev["name"].lower()) or {}
                where = (f"RMS #{entry['id']}" if entry.get("status") == 1
                         else "(not reachable in RMS)")
            else:
                host, note = resolve_host(dev, nodes)
                where = host or "(no address)"
                if note:
                    where += f"  [{note}]"
            print(f"  {dev['name']:<34} {where}")
        print()
    else:
        print(f"Checking {len(devices)} device(s) over {transport}, read-only.\n")
    # One device at a time on the relay: it is a shared, rate-limited channel and
    # a wall of interleaved slow commands is not worth the wall-clock saving.
    workers = opts.workers or (1 if opts.apply or opts.via == "rms" else 8)
    results: list[dict] = []
    with futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        jobs = {pool.submit(handle, d, cfg, opts, operators, unknown, passwords,
                            nodes, rms): d for d in devices}
        for done_job in futures.as_completed(jobs):
            result = done_job.result()
            results.append(result)
            print(f"[{result['status']:<21}] {result['device']:<34} "
                  f"{result['host'] or '-':<16} {describe(result)}")
            for reason in result["reasons"]:
                print(f"{'':<24}  - {reason}")
            if result["status"] in ("applied", "applied-with-warnings"):
                state[result["device"]] = {
                    "status": "applied",
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "host": result["host"], "firmware": result.get("firmware", ""),
                    "warnings": result["reasons"],
                }
                save_state(state)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log = opts.output_dir / f"quota_rollout_{stamp}.json"
    log.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "apply" if opts.apply else "check",
        "group": opts.group, "config": str(opts.config), "report": str(opts.report),
        "results": results,
    }, indent=2) + "\n")

    counts: dict[str, int] = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    print("\n" + ", ".join(f"{n} {status}" for status, n in sorted(counts.items())))
    print(f"Wrote {log}")
    if not opts.apply and counts.get("would-change"):
        ready = [r["device"] for r in results
                 if r["status"] == "would-change" and not r["reasons"]]
        print(f"{counts['would-change']} device(s) have pending changes"
              + (f", {len(ready)} of them with nothing blocking." if ready else "."))
        if ready:
            print(f"Apply to one of them with:\n  {Path(__file__).name} --apply --yes "
                  f"--device {ready[0]}")


if __name__ == "__main__":
    main()
