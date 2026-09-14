#!/usr/bin/env python3
"""
Provision a PLANET IGS-4215-8UP2T2S industrial PoE++ switch over SSH CLI (+ HTTP).

This is the PoE switch that feeds a Magos site: four AR-300 radars, a speaker
and a camera hang off it, so its per-port power plan IS the site's power plan.
Pipeline for one switch:

  web login -> identity -> firmware floor (HTTP, dual-partition) -> SSH login
    -> password -> port descriptions -> SNTP -> timezone -> verify
    -> disable telnet -> move the management IP to 192.168.88.3 (LAST — drops
       the session; confirmed by reaching the switch on the new address)

PoE is deliberately NOT in that list: the switch negotiates power per port
itself, and the bench has set none since 2026-09-14. The plan and the code that
applies it are intact behind one config flag — see `poe_is_managed`.

Five things separate this from the Teltonika tools next door:

* **A factory switch will not talk to you at all until three things happen.**
  Out of the box it has SSH *and* telnet disabled (ports 22 and 23 closed, the
  CGI web UI the only way in), so `ensure_ssh_service` turns the CLI on over
  HTTP first. Then SSH itself refuses to open a CLI until the password is
  changed, in an interactive dialogue `login` completes. And that new password
  must be saved immediately — the switch boots from its STARTUP config, so an
  unsaved one reverts to the factory password on the next reboot and the next
  run finds a switch nobody can predict.

* **The factory password is derived, not printed.** PLANET's default is `sw` +
  the last 6 hex digits of the switch's MAC, lowercase — `a8:f7:e0:f6:c4:3a`
  gives `swf6c43a`. The bench already reads the MAC off ARP to identify the
  switch, so there is still nothing to scan or type; see
  `factory_password_for`. Login tries the shared password first (a re-run is
  the common case) and the derived one second, and stops there: three wrong
  attempts start a lockout that looks exactly like the SSH fault below.

* **There is no REST API.** PLANET ships SSHv2, telnet, SNMP and a CGI web UI;
  the CLI is the documented surface, so configuration goes over SSH. The web UI
  (`dispatcher.cgi?cmd=<N>` form posts) is used for the two jobs SSH cannot do:
  reading identity before login, and flashing firmware.

* **Firmware <= v1.305b251017 has a BROKEN SSH SERVER** — it accepts `none`
  auth, refuses password auth, then closes the CLI channel right after the
  welcome banner. Telnet does the same. A switch on that firmware cannot be
  configured over the CLI at all. The fix is the firmware upgrade, which is why
  `firmware.enabled` defaults to true and why `login()` raises `SshBroken`
  (naming the upgrade) rather than a generic auth error. Note that PLANET's
  release notes for the fixing build say "Bug Fixed: NA" — the notes do not
  record it; the behaviour does.

* **Firmware flashes to the INACTIVE partition.** The switch carries two images.
  We write the spare, mark it active and reboot, so the known-good image stays
  on the other partition and a bad flash is one `partition` flip from recovery.

Two device quirks that silently produce a wrong config if missed, both learned
from the device rather than from PLANET's Command Guide:

* `poe power-limit` takes **deci-watts**: `450` is 45.0 W. The Command Guide's
  own example (`poe power-limit 95 all`) reads as watts and is wrong. Ports are
  configured here in watts and converted once, in `poe_cmds`. (Dormant while
  PoE is left to the switch, and the trap that is waiting if it is re-enabled.)
* `clock timezone <ACRONYM> <hours>` takes an acronym of **1-4 characters**.
  A longer one is accepted silently and does nothing, leaving the switch on its
  factory +8.

CLI (single device):
  python3 planet_configure.py --password 'Kelasys123!'
  python3 planet_configure.py --verify        # check a finished switch, change nothing
"""
from __future__ import annotations

import argparse
import logging
import re
import socket
import sys
import time
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent

from bench_core import (
    DEFAULT_NEW_PASSWORD,
    DEFAULT_USERNAME,
    LOG_LINE_FORMAT,
    format_verification,
    load_settings,
    log,
    make_step_runner,
    set_log_serial,
)
from bench_core.ip_mode import MODE_DHCP

try:
    import paramiko
except ImportError:  # pragma: no cover - bench installs it via bench-core
    paramiko = None

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

# --- IGS-4215 factory + target defaults ---------------------------------------
DEFAULT_PLANET_HOST = "192.168.0.100"       # factory management address
DEFAULT_PLANET_LAN_IP = "192.168.88.3"      # .1 gateway, .2 TSW202, .3 this switch
DEFAULT_PLANET_NETMASK = "255.255.255.0"
DEFAULT_PLANET_NTP_SERVER = "192.168.88.10"
DEFAULT_PLANET_TZ_ACRONYM = "IST"
DEFAULT_PLANET_TZ_OFFSET = 3
# The build that fixes the broken SSH server. Below this, the CLI is unusable.
DEFAULT_PLANET_MIN_FIRMWARE = "1.305b260324"

EXPECTED_MODEL = "IGS-4215-8UP2T2S"

# How many passwords we may try against one switch. The web UI and the CLI both
# enforce a retry count of 3 followed by a silent time (150 s on SSH, 120 s on
# telnet) during which the switch accepts the connection, prints its banner and
# closes WITHOUT prompting — which looks exactly like the broken-SSH firmware
# fault and sends the operator down the wrong path. Deriving the factory
# password from the MAC instead of guessing is what keeps us inside this.
MAX_PASSWORD_ATTEMPTS = 2

# The switch reports PWR1/PWR2; 360 W of PoE budget needs BOTH inputs wired. On
# a single supply the usable budget is 240 W, so that is the default and the
# ceiling we warn against exceeding.
SINGLE_SUPPLY_BUDGET_W = 240

# 8UP2T2S: gi1-8 are the PoE++ ports, gi9-10 plain copper, gi11-12 SFP. The
# last four carry data only — a `poe` command naming one is rejected, and
# `show poe` never reports them, so they are named but never powered.
PSE_PORT_COUNT = 8

# Web UI command ids (dispatcher.cgi?cmd=N). Stable across the 1.305 line.
CMD_LOGIN = 1
CMD_SYSTEM_INFO = 512
CMD_SSH_SETTINGS = 537
CMD_SSH_APPLY = 538
CMD_DUAL_IMAGE = 5901
CMD_SET_ACTIVE_IMAGE = 5902
CMD_REBOOT = 5889
CMD_SAVE_CONFIG = 5895
CMD_UPLOAD_FIRMWARE = 5900

# Prompts: "IGS-4215-8UP2T2S#" in exec mode, "...(config)#" in config mode, so
# the parens must be in the class or every config-mode read times out.
PROMPT = re.compile(r"[\w.\-()]+ ?[#>] ?$")
CLI_REJECT = re.compile(r"(Invalid|Incomplete|Unknown command|% ?Error|Fail)", re.I)

# A factory switch greets the shell with this instead of a prompt, and will
# not give a CLI until the password has been changed.
FORCED_PASSWORD_CHANGE = "required to change"

# Printed with a countdown when the switch has no free CLI session slot.
SESSION_LIMIT_CLOSE = "Close after"

# An expired web session answers every request with the login page, which
# redirects to cmd=11 — the only reliable marker that we are logged out.
SESSION_EXPIRED_REDIRECT = "dispatcher.cgi?cmd=11"


class SshBroken(SystemExit):
    """SSH answered but will not give us a CLI — the pre-b260324 firmware bug.

    Its own type because the caller's response is specific and useful: upgrade
    the firmware, then try again. A plain auth error would send the operator
    looking for a wrong password instead.
    """


class SwitchUnreachable(SystemExit):
    """The switch did not answer at all.

    Distinct from a refused password because the response differs: a refusal is
    worth retrying with the next candidate, an unplugged switch is not — and
    burning the retry budget against a switch that is not there would start a
    lockout for the next person who does reach it.
    """


class PlanetClient:
    """SSH CLI client for the IGS-4215, with the HTTP side-channel it needs.

    The switch has no REST API, so `cli()` over an interactive shell is the
    workhorse. The `web_*` methods exist only for what SSH cannot do: identity
    before we have a CLI, and firmware flashing.
    """

    def __init__(self, host: str, username: str = DEFAULT_USERNAME,
                 password: str = DEFAULT_NEW_PASSWORD, timeout: int = 20):
        self.host = host
        self.username = username
        self.password = password
        self.timeout = timeout
        self._t = None      # paramiko Transport
        self._ch = None     # interactive shell channel
        self._web = None    # requests.Session

    # --- SSH ----------------------------------------------------------------

    def login(self, password: Optional[str] = None,
              new_password: Optional[str] = None) -> None:
        """Open an interactive CLI session, or explain why we cannot.

        A FACTORY switch will not hand over a CLI at all until its password has
        been changed: it answers the shell with "You are required to change and
        store a new password" and prompts for one. `new_password` is what it
        gets; without it there is no way past the dialogue, and every command
        the caller then sends would sit waiting for a prompt that never comes.

        Raises `SshBroken` for the pre-b260324 signature (password auth refused
        while `none` auth is accepted, or the channel closing on its own right
        after the banner) so the caller can offer the firmware upgrade.
        """
        if paramiko is None:
            raise SystemExit("This step needs 'paramiko'.  Install it: pip install paramiko")
        pw = password or self.password
        transport = paramiko.Transport((self.host, 22))
        try:
            transport.start_client(timeout=self.timeout)
        except Exception as e:
            transport.close()
            raise SystemExit(f"SSH did not answer on {self.host}: {e}")
        try:
            transport.auth_password(self.username, pw)
        except Exception as auth_error:
            transport.close()
            if self._accepts_credentialless_auth():
                raise SshBroken(
                    "the switch's SSH server accepts credential-less auth and "
                    "refuses the password — the known fault on firmware below "
                    f"{DEFAULT_PLANET_MIN_FIRMWARE}. Upgrading the firmware "
                    "fixes it (leave firmware.enabled on).")
            raise SystemExit(f"SSH login refused on {self.host}: {auth_error}")

        channel = transport.open_session(timeout=self.timeout)
        channel.get_pty(term="vt100", width=200, height=60)
        channel.invoke_shell()
        banner = self._drain(channel, 4)
        if channel.closed or channel.eof_received:
            transport.close()
            # The switch counts down ("Close after 2 seconds") when its
            # concurrent-session limit is full. That looks like the firmware
            # fault but is the opposite kind of problem — transient, and fixed
            # by waiting rather than by flashing — so it must not be reported
            # as a reason to reflash a switch that is already current.
            if SESSION_LIMIT_CLOSE in banner:
                raise SystemExit(
                    f"{self.host} refused the CLI session and counted down to "
                    "closing it — the switch's concurrent-session limit is "
                    "full. Give the stale sessions a minute to time out, or "
                    "clear them in the web UI (Security > Access Management > "
                    "SSH > Disconnect).")
            raise SshBroken(
                "the switch closed the CLI session immediately after its welcome "
                f"banner ({banner.strip()[-60:]!r}) — the known fault on firmware "
                f"below {DEFAULT_PLANET_MIN_FIRMWARE}. Upgrading the firmware "
                "fixes it (leave firmware.enabled on).")
        self._t, self._ch = transport, channel
        forced_change = FORCED_PASSWORD_CHANGE in banner
        if forced_change:
            self._change_password_at_login(new_password or pw)
        # Long `show` output pages with --More-- and would otherwise stall every
        # read waiting for a prompt that is not coming.
        self.cli("terminal length 0", check=False)
        if forced_change:
            # Persist it NOW rather than at the end of the run. The switch boots
            # from its STARTUP config, so a password that is only in the running
            # config reverts to the factory one on the next reboot — and a run
            # that fails anywhere later would leave a switch whose password
            # nobody can predict. (Seen on real hardware: a failed run rebooted
            # and came back on `sw`+MAC.)
            self.save_config()

    def _send_expect(self, text: str, *tokens: str,
                     timeout: int = 15) -> tuple[str, str]:
        """Send `text`, read until one of `tokens` appears, return `(token, output)`.

        Several tokens because the interesting steps have more than one
        outcome — a rejected password re-prompts rather than reaching a CLI
        prompt, and waiting only for success would burn the timeout and then
        report the wrong reason. Earlier tokens win when both are present.
        """
        self._ch.send(text)
        buf, end = "", time.time() + timeout
        while time.time() < end:
            if self._ch.recv_ready():
                buf += self._ch.recv(65535).decode(errors="replace")
                for token in tokens:
                    if token in buf:
                        return token, buf
            else:
                time.sleep(0.05)
        raise SystemExit(f"expected one of {tokens} from the switch during "
                         f"login, got {buf.strip()[-160:]!r}")

    def _change_password_at_login(self, new_password: str) -> None:
        """Complete the factory switch's forced password change.

            <Enter>      -> "(New)Password: "
            <password>   -> "Verify (New)Password: "
            <password>   -> "Success." and the CLI prompt

        The switch enforces 8-32 characters with upper case, lower case, a
        numeral and a symbol, so a shared password that breaks those rules
        fails HERE — before anything is configured — rather than leaving a
        half-provisioned switch behind.
        """
        if not new_password:
            raise SystemExit(
                "this switch demands a password change before it will open a "
                "CLI, but no new password is configured — set `new_password`")
        log.info("Factory switch: completing the forced password change.")
        self._send_expect("\n", "(New)Password")
        self._send_expect(new_password + "\n", "Verify (New)Password")
        # "Password" catches the re-prompt a refusal produces, so a weak
        # password is reported as such instead of timing out on "#".
        matched, result = self._send_expect(new_password + "\n",
                                            "Success", "Password", timeout=20)
        if matched != "Success":
            raise SystemExit(
                "the switch refused the new password — it must be 8-32 "
                "characters with upper case, lower case, a numeral and a "
                f"symbol. It said: {result.strip()[-160:]!r}")
        self.password = new_password

    def _accepts_credentialless_auth(self) -> bool:
        """True if the SSH server authenticates with no credentials at all.

        No correctly configured switch does this; the broken firmware does, so
        it separates 'wrong password' from 'this firmware is the problem'.
        """
        probe = None
        try:
            probe = paramiko.Transport((self.host, 22))
            probe.start_client(timeout=self.timeout)
            probe.auth_none(self.username)
            return probe.is_authenticated()
        except Exception:
            return False
        finally:
            if probe is not None:
                probe.close()

    @staticmethod
    def _drain(channel, seconds: float) -> str:
        buf, end = "", time.time() + seconds
        while time.time() < end:
            if channel.recv_ready():
                buf += channel.recv(65535).decode(errors="replace")
            else:
                time.sleep(0.05)
        return buf

    def cli(self, command: str, *, timeout: int = 15, check: bool = True) -> str:
        """Send one CLI command and return its output (prompt line included)."""
        if self._ch is None:
            raise SystemExit("CLI not open — call login() first.")
        self._ch.send(command + "\n")
        out, end = "", time.time() + timeout
        while time.time() < end:
            if self._ch.recv_ready():
                out += self._ch.recv(65535).decode(errors="replace")
                if "--More--" in out[-30:]:
                    self._ch.send(" ")
                    continue
                if PROMPT.search(out.rstrip()):
                    break
            else:
                time.sleep(0.05)
        if check and CLI_REJECT.search(out):
            raise SystemExit(f"switch rejected {command!r}: {out.strip()[-160:]}")
        return out

    def configure(self, commands: list[str], *, save: bool = True) -> None:
        """Run commands in global config mode, then persist them.

        Saving is not optional in practice: this switch keeps running and
        startup configs apart, and a unit was found in the field whose unsaved
        running config would have silently reverted on its next reboot.
        """
        self.cli("configure")
        try:
            for command in commands:
                self.cli(command)
        finally:
            # Return to exec mode even when a command failed. The CLI session is
            # shared by every later step, and `configure` issued from inside
            # config mode is itself rejected — so without this one bad command
            # fails every step after it, and the run reports five failures with
            # one cause.
            self.cli("end", check=False)
        if save:
            self.save_config()

    def set_password(self, new_password: str, old_password: str) -> None:
        """Change the admin password over the CLI.

        Two things PLANET's Command Guide gets wrong about this command:

        * `privilege` accepts only `admin` or `user` here. The numeric level the
          guide documents (`privilege 15`) is rejected as an unknown command —
          silently, if the caller is not checking for "Unknown command".
        * It is INTERACTIVE: the switch asks for the current password before
          accepting the new one. Fired and forgotten, the next command in the
          queue gets eaten as the answer and the change fails.
        """
        self.cli("configure")
        try:
            matched, out = self._send_expect(
                f"username {self.username} privilege admin "
                f"password {new_password}\n", "Old password", "#")
            if matched == "Old password":
                _, out = self._send_expect(old_password + "\n", "#")
            if CLI_REJECT.search(out) or "incorrect" in out.lower():
                raise SystemExit("the switch refused the password change: "
                                 f"{out.strip()[-160:]!r}")
        finally:
            self.cli("end", check=False)
        self.save_config()
        self.password = new_password

    def save_config(self) -> None:
        self.cli("copy running-config startup-config", timeout=45)

    def close(self) -> None:
        for closer in (self._ch, self._t):
            try:
                if closer is not None:
                    closer.close()
            except Exception:
                pass
        self._ch = self._t = None

    # --- HTTP side-channel ---------------------------------------------------

    def web_login(self, password: Optional[str] = None) -> None:
        """Authenticate to the CGI web UI. Works even when the CLI does not,
        which is what makes the firmware recovery path possible."""
        if requests is None:
            raise SystemExit("This step needs 'requests'.  Install it: pip install requests")
        session = requests.Session()
        try:
            response = session.post(
                f"http://{self.host}/cgi-bin/dispatcher.cgi?cmd={CMD_LOGIN}",
                data={"username": self.username,
                      "password": password or self.password, "login": "1"},
                timeout=20)
        except requests.RequestException as e:
            # Raw requests exceptions would reach the bench UI as a traceback;
            # the operator needs "it is not answering", not a urllib3 stack.
            raise SwitchUnreachable(
                f"{self.host} did not answer over HTTP — check the cable and "
                f"that this station has an address on {self.host.rsplit('.', 1)[0]}.x "
                f"({type(e).__name__})")
        if "hid=" not in response.headers.get("set-cookie", ""):
            raise SystemExit(f"web login refused on {self.host} "
                             "(wrong password, or this is not a PLANET web UI)")
        self._web = session

    def web_login_any(self, passwords: list[str]) -> str:
        """Log in with the first password that works; return it.

        A switch arrives either factory (`sw` + the last 6 MAC digits) or
        already provisioned (the shared password) — and a unit left half-done by
        an earlier run can be either, so both are tried rather than asking the
        operator which state it is in.

        At most `MAX_PASSWORD_ATTEMPTS`, because the switch locks out after 3
        wrong ones and then answers every later connection with a banner and a
        hang-up — indistinguishable, at the bench, from the broken-SSH firmware
        fault. Two known-good candidates is the whole point of deriving the
        factory password rather than guessing at it.
        """
        tried: list[str] = []
        for password in [p for p in passwords if p]:
            if password in tried:
                continue
            if len(tried) >= MAX_PASSWORD_ATTEMPTS:
                break
            tried.append(password)
            try:
                self.web_login(password)
                self.password = password
                return password
            except SwitchUnreachable:
                raise            # not a password problem — say so and stop
            except SystemExit:
                continue
        raise SystemExit(
            f"web login refused on {self.host} for every password tried "
            f"({', '.join(repr(p) for p in tried)}). If this switch has a "
            "password of its own it must be reset to factory defaults — and "
            "give it a couple of minutes first, since three wrong attempts "
            "start a lockout that looks like a dead management interface.")

    def _web_page(self, cmd: int) -> str:
        """Fetch one CGI page, renewing the session if it has timed out.

        The switch's web session expires on its own schedule and an expired one
        answers every request with the LOGIN PAGE rather than an error — so a
        caller that parses the result silently reads nothing at all. That cost a
        real run: the firmware check is the only thing read over HTTP, and it
        reported a switch as below the floor when its firmware was fine.
        """
        if self._web is None:
            self.web_login()
        page = self._get(cmd)
        if SESSION_EXPIRED_REDIRECT in page:
            log.info("Web session expired — logging in again.")
            self.web_login(self.password)
            page = self._get(cmd)
        return page

    def _get(self, cmd: int) -> str:
        return self._web.get(
            f"http://{self.host}/cgi-bin/dispatcher.cgi?cmd={cmd}", timeout=20).text

    def _web_post(self, data: dict, *, timeout: int = 30):
        if self._web is None:
            self.web_login()
        response = self._web.post(f"http://{self.host}/cgi-bin/dispatcher.cgi",
                                  data=data, timeout=timeout)
        # Same trap on the write side, and worse: a post to an expired session
        # is answered with the login page and changes nothing, silently.
        if SESSION_EXPIRED_REDIRECT in response.text:
            log.info("Web session expired — logging in again and retrying.")
            self.web_login(self.password)
            response = self._web.post(f"http://{self.host}/cgi-bin/dispatcher.cgi",
                                      data=data, timeout=timeout)
        return response

    def get_identity(self) -> dict:
        """Model / firmware / MAC / power inputs, read over HTTP.

        Over HTTP rather than the CLI because identity is needed BEFORE we know
        whether the CLI works — the whole point on a switch whose SSH may be
        broken.
        """
        page = self._web_page(CMD_SYSTEM_INFO)
        fields = parse_info_table(page)

        def field(name: str) -> str:
            return fields.get(name, "")

        mac = field("MAC Address")
        return {
            "model": field("System Name") or EXPECTED_MODEL,
            "firmware": field("Firmware Version"),
            "mac": mac,
            "ip": field("IP Address"),
            # The switch exposes no serial over any interface; the MAC is the
            # only stable per-unit handle, and it is what the QA label carries.
            "serial": mac.replace(":", ""),
            # Both supplies matter: 360 W of budget needs both, and a site
            # planned at 360 W on one supply browns out under load.
            "dual_power": "PWR2:ON" in page,
        }

    def ensure_ssh_service(self) -> bool:
        """Switch the SSH server on over the web UI if it is off.

        A factory-default IGS-4215 has **both SSH and telnet disabled** — ports
        22 and 23 are closed and the CGI web UI is the only way in at all. So
        the CLI this whole pipeline runs on has to be turned on before it can be
        used, and the web session we already hold is what turns it on.

        (A switch that someone has configured before usually has SSH on already,
        which is why this went unnoticed until the first genuinely factory unit.)

        Returns True when it had to change something.
        """
        page = self._web_page(CMD_SSH_SETTINGS)
        selected = re.search(r'name="sshd".*?value="(\d)"[^>]*selected', page, re.S)
        if selected and selected.group(1) == "1":
            return False

        # Preserve the page's own timeout / retry / silent-time values rather
        # than inventing them: they are what a later lockout is measured in, and
        # quietly changing them would surprise whoever reads the switch later.
        current = dict(re.findall(r'name="(cli[A-Za-z]+)"[^>]*value="([^"]*)"', page))
        log.info("SSH is disabled on this switch — enabling it over the web UI.")
        self._web_post({"cmd": str(CMD_SSH_APPLY), "sshd": "1",
                        "loginAuth": "Default", "enblAuth": "Default",
                        **current})
        if not wait_for_host(self.host, 22, timeout=60):
            raise SystemExit(
                f"enabled SSH on {self.host} but port 22 never opened — turn it "
                "on by hand in the web UI (Security > Access Management > SSH)")
        return True

    def active_partition(self) -> int:
        """Which flash partition is active (0 or 1). We flash the other one."""
        page = self._web_page(CMD_DUAL_IMAGE)
        match = re.search(r'name="partition"[^>]*value="(\d)"[^>]*checked', page)
        return int(match.group(1)) if match else 0

    def upgrade_firmware(self, bix_path: Path, *, reboot_wait: int = 300) -> str:
        """Flash `bix_path` to the INACTIVE partition, activate it and reboot.

        Returns the firmware version the switch reports once it is back. The
        known-good image is left on the other partition, so a bad flash is
        recoverable by flipping `partition` back in the web UI.
        """
        if not bix_path.is_file():
            raise SystemExit(f"firmware image not found: {bix_path}")
        target = 1 - self.active_partition()
        log.info("Flashing %s to partition %d (active stays on %d until reboot)…",
                 bix_path.name, target, 1 - target)
        with bix_path.open("rb") as image:
            self._web.post(
                f"http://{self.host}/cgi-bin/httpupload.cgi",
                data={"cmd": str(CMD_UPLOAD_FIRMWARE), "upmethod": "http",
                      "type": "0", "partition": str(target)},
                files={"http_file": (bix_path.name, image, "application/octet-stream")},
                timeout=600)
        # Persist the running config first: the switch reboots into its STARTUP
        # config, so anything set-but-unsaved would vanish across the flash.
        self._web_post({"cmd": str(CMD_SAVE_CONFIG), "srcFile": "1", "dstFile": "2"})
        self._web_post({"cmd": str(CMD_SET_ACTIVE_IMAGE), "partition": str(target)})
        self._web_post({"cmd": str(CMD_REBOOT)})
        self._web = None
        if not wait_for_host(self.host, 80, timeout=reboot_wait):
            raise SystemExit(f"the switch did not come back on {self.host} after "
                             f"the firmware reboot ({reboot_wait}s)")
        self.web_login()
        return self.get_identity().get("firmware", "unknown")

    # --- verification --------------------------------------------------------

    def show_poe(self) -> dict:
        """Parse `show poe` into {port: {enabled, limit_w, priority, pd_class}}."""
        ports: dict[int, dict] = {}
        for line in self.cli("show poe", timeout=25, check=False).splitlines():
            match = re.match(r"\s*0?(\d+)\s*\|\s*(\w+)\|[^|]*\|[^|]*\|[^|]*\|"
                             r"\s*(\w+)\s*\|\s*([\w-]+)\s*\|[^|]*\|[^|]*\|\s*([\d.]+)",
                             line)
            if match:
                ports[int(match.group(1))] = {
                    "enabled": match.group(2).strip().lower() == "enable",
                    "priority": match.group(3).strip().lower(),
                    "pd_class": match.group(4).strip(),
                    "limit_w": float(match.group(5)),
                }
        return ports

    def show_descriptions(self) -> dict:
        """Port descriptions, read out of running-config.

        `show interface description` does not exist on this firmware, so the
        running config is the only place the labels can be read back.
        """
        descriptions: dict[int, str] = {}
        current = None
        for line in self.cli("show running-config", timeout=40, check=False).splitlines():
            interface = re.match(r"\s*interface gi(\d+)\s*$", line)
            if interface:
                current = int(interface.group(1))
            described = re.match(r'\s*description "(.+)"\s*$', line)
            if described and current:
                descriptions[current] = described.group(1)
        return descriptions

    def verify_configuration(self, *, settings: dict,
                             minimum_firmware: str = "") -> list[dict]:
        checks: list[dict] = []

        def add(item, expected, actual, ok):
            checks.append({"item": item, "expected": expected,
                           "actual": actual, "ok": ok})

        if minimum_firmware:
            firmware = self.get_identity().get("firmware", "")
            add("firmware", f"{minimum_firmware} or newer", firmware or "unknown",
                planet_version_at_least(firmware, minimum_firmware))

        sntp = self.cli("show sntp", check=False)
        want_ntp = settings.get("ntp_server", DEFAULT_PLANET_NTP_SERVER)
        found_ntp = re.search(r"SNTP Server address:\s*(\S+)", sntp)
        add("ntp-server", want_ntp,
            found_ntp.group(1) if found_ntp else "unset", want_ntp in sntp)

        clock = self.cli("show clock", check=False)
        want_tz = f"UTC+{settings.get('timezone_offset', DEFAULT_PLANET_TZ_OFFSET)}"
        found_tz = re.search(r"\((UTC[+-]\d+)\)", clock)
        add("timezone", want_tz,
            found_tz.group(1) if found_tz else "unknown", want_tz in clock)

        poe_cfg = settings.get("poe", {}) or {}
        # Only checked when the bench sets PoE. Left to the switch, these rows
        # would hold it to limits nobody applied — see `poe_is_managed`.
        if poe_is_managed(poe_cfg):
            live = self.show_poe()
            for port, want in sorted(pse_ports(poe_cfg).items()):
                got = live.get(port)
                if not got:
                    add(f"poe-port-{port}", "configured", "not reported", False)
                    continue
                want_on = bool(want.get("enabled"))
                ok = got["enabled"] == want_on
                if want_on:
                    ok = ok and got["limit_w"] == float(want.get("limit_w", 0))
                expected = ("on @ %.1fW" % float(want.get("limit_w", 0))
                            if want_on else "off")
                add(f"poe-port-{port}", expected,
                    f"{'on' if got['enabled'] else 'off'} @ {got['limit_w']:.1f}W",
                    ok)

        live_descriptions = self.show_descriptions()
        for port, want in sorted(ports_of(poe_cfg).items()):
            wanted = (want.get("description") or "").strip()
            if wanted:
                add(f"port-name-{port}", wanted,
                    live_descriptions.get(port, "(none)"),
                    live_descriptions.get(port) == wanted)

        if settings.get("disable_telnet", True):
            running = self.cli("show running-config", timeout=40, check=False)
            telnet_off = not re.search(r"^ip telnet\s*$", running, re.M)
            add("telnet", "disabled",
                "disabled" if telnet_off else "ENABLED", telnet_off)
        return checks


# --- helpers ------------------------------------------------------------------

_PLANET_VERSION = re.compile(r"(\d+)\.(\d+)b(\d+)")


def planet_version_key(version: str) -> tuple:
    """Order PLANET versions: `1.305b260324` -> `(1, 305, 260324)`.

    bench_core's `fw_version_at_least` cannot do this and must not be used
    here. It reads DOTTED numeric segments only, so every `1.305bNNNNNN` build
    parses to `(1, 305)` and any two of them compare EQUAL — which would report
    a switch on the broken-SSH b251017 build as already at the b260324 floor
    and skip the upgrade that is the entire point of the step. The build date
    after the `b` is the ordering key, and it is the part that helper drops.

    Returns `()` for anything unparseable, so "we could not tell" cannot read
    as "new enough" — the same rule the shared helper follows.
    """
    match = _PLANET_VERSION.search(version or "")
    return ((int(match.group(1)), int(match.group(2)), int(match.group(3)))
            if match else ())


def planet_version_at_least(device_fw: str, minimum: str) -> bool:
    """True when `device_fw` is `minimum` or newer."""
    current, floor = planet_version_key(device_fw), planet_version_key(minimum)
    return bool(current) and bool(floor) and current >= floor


def planet_versions_match(a: str, b: str) -> bool:
    """True when both name the same build."""
    key = planet_version_key(a)
    return bool(key) and key == planet_version_key(b)


def wait_for_host(host: str, port: int, *, timeout: int = 300) -> bool:
    """Block until host:port accepts a connection. Used across the firmware
    reboot and the management-address move."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=3):
                return True
        except OSError:
            time.sleep(3)
    return False


def parse_info_table(page: str) -> dict:
    """`{label: value}` from the switch's System Information page.

    The page is a two-column table of nested tags with no ids to hook onto, so
    it is flattened to cells and read pairwise. The trap: an *editable* row
    (System Name, Location, Contact) carries an "Edit" link cell between the
    label and its value, while a read-only row (MAC Address, Firmware Version)
    does not. Taking the cell immediately after the label therefore yields
    "Edit" for some rows and the right answer for others — and an earlier
    regex that demanded the value adjacent to the label returned nothing at
    all, because the two are separated by markup either way.
    """
    cells = [cell.replace("&nbsp;", " ").strip()
             for cell in re.sub(r"<[^>]*>", "|", page).split("|")]
    cells = [cell for cell in cells if cell]
    table: dict[str, str] = {}
    for index, label in enumerate(cells):
        for candidate in cells[index + 1:index + 3]:
            if candidate != "Edit":
                table.setdefault(label, candidate)
                break
    return table


def read_device_mac(ip: str) -> Optional[str]:
    """The switch's MAC off the ARP table — the factory password derives from it.

    Imported lazily because `bench_core.bench_ui` pulls in the whole web stack,
    and this module is also the CLI: configuring a switch from a terminal
    should not need FastAPI installed.
    """
    try:
        from bench_core.bench_ui import read_device_mac as _read_device_mac
    except ImportError:  # pragma: no cover - only without the [ui] extra
        return None
    return _read_device_mac(ip)


def factory_password_for(mac: str) -> str:
    """PLANET's factory password: `sw` + the last 6 hex digits of the MAC.

        a8:f7:e0:f6:c4:3a  ->  swf6c43a

    Per-unit, but derivable rather than printed — so unlike the Teltonika
    devices there is still nothing to scan: the bench already reads the MAC off
    ARP to identify the switch, and the password falls out of it.

    Returns "" when the MAC is unusable, so the caller can say "we could not
    work out the factory password" instead of trying `sw` against the retry
    counter for nothing.
    """
    digits = re.sub(r"[^0-9a-f]", "", (mac or "").lower())
    return f"sw{digits[-6:]}" if len(digits) >= 6 else ""


def ports_of(poe_cfg: dict) -> dict:
    """Port map with integer keys — JSON object keys arrive as strings."""
    return {int(k): v for k, v in (poe_cfg.get("ports") or {}).items()}


def pse_ports(poe_cfg: dict) -> dict:
    """Only the ports with PoE hardware behind them (see PSE_PORT_COUNT)."""
    return {port: spec for port, spec in ports_of(poe_cfg).items()
            if port <= PSE_PORT_COUNT}


def poe_is_managed(poe_cfg: dict) -> bool:
    """Whether the bench sets this switch's PoE at all.

    OFF since 2026-09-14, at Naor's call: the IGS-4215 negotiates power per
    port on its own (802.3bt classification), and a site has not needed the
    bench to hold its hand. Nothing about the plan was deleted — `poe_cmds`,
    the budget warnings, the per-port limits in the config and the checks that
    read them all still work.

    TO RE-ENABLE: set `poe.managed` to true in planet.config.json. That is the
    whole switch. The run gains its `poe` step back, `verify` gains its
    `poe-port-N` rows, the page shows limits and the budget line again, and the
    printed port map goes back to naming the wattage per socket. Check the
    per-port `limit_w` and `budget_w` still match the site before you do —
    they are the values as of the day this was turned off.
    """
    return bool(poe_cfg.get("managed", False))


def poe_cmds(poe_cfg: dict) -> list[str]:
    """The PoE plan as CLI commands.

    `poe power-limit` is in DECI-WATTS on this platform — the running config
    writes `950` for 95.0 W — so watts from the config are multiplied here,
    once. PLANET's Command Guide example (`poe power-limit 95 all`) reads as
    watts and is wrong; sending 45 would cap a 35 W radar at 4.5 W and it would
    never come up.
    """
    budget = int(poe_cfg.get("budget_w", SINGLE_SUPPLY_BUDGET_W))
    cmds = ["poe admin-mode enable",
            f"poe limit-mode {poe_cfg.get('limit_mode', 'allocation')}",
            f"poe power_budget {budget}",
            "poe mode bt all",
            "poe pd type standard all"]
    for port, spec in sorted(ports_of(poe_cfg).items()):
        if port > PSE_PORT_COUNT:
            if spec.get("enabled"):
                raise SystemExit(f"gi{port} has no PoE hardware — it cannot be "
                                 f"enabled (only gi1-{PSE_PORT_COUNT} supply power)")
            continue
        if spec.get("enabled"):
            cmds += [f"poe port enable {port}",
                     f"poe power-limit {int(float(spec.get('limit_w', 0)) * 10)} {port}",
                     f"poe priority {spec.get('priority', 'low')} {port}"]
        else:
            cmds.append(f"poe port disable {port}")
    return cmds


def description_cmds(poe_cfg: dict) -> list[str]:
    """Port labels. `description` takes WORD<1-32>; quoting keeps spaces legal."""
    cmds: list[str] = []
    for port, spec in sorted(ports_of(poe_cfg).items()):
        text = (spec.get("description") or "").strip()
        if not text:
            continue
        if len(text) > 32:
            raise SystemExit(f"port {port} description is {len(text)} chars; "
                             "the switch accepts at most 32")
        cmds += [f"interface gi{port}", f'description "{text}"', "exit"]
    return cmds


def poe_budget_warnings(poe_cfg: dict, identity: dict) -> list[str]:
    """Power problems worth saying out loud before the radars brown out.

    Allocation mode reserves each port's configured limit against the budget,
    so a plan that over-subscribes is a site that loses a radar under load —
    and 360 W is only real with both supplies wired.
    """
    warnings: list[str] = []
    if not poe_is_managed(poe_cfg):
        return warnings      # nothing is allocated, so nothing over-allocates
    budget = int(poe_cfg.get("budget_w", SINGLE_SUPPLY_BUDGET_W))
    allocated = sum(float(spec.get("limit_w", 0))
                    for spec in pse_ports(poe_cfg).values() if spec.get("enabled"))
    if allocated > budget:
        warnings.append(f"PoE plan allocates {allocated:.0f} W against a "
                        f"{budget} W budget — a port will be denied power")
    if budget > SINGLE_SUPPLY_BUDGET_W and not identity.get("dual_power"):
        warnings.append(f"budget is {budget} W but only one power input is live "
                        f"({SINGLE_SUPPLY_BUDGET_W} W is the single-supply ceiling)")
    return warnings


# --- firmware floor -----------------------------------------------------------

def apply_firmware_floor(client: PlanetClient, settings: dict, identity: dict,
                         failures: list[str]) -> tuple[str, list[str]]:
    """Bring the switch up to the configured minimum firmware.

    Enabled by default, unlike the Teltonika tools' floor, because on this
    switch the firmware is not a nicety: below b260324 the SSH server is broken
    and nothing else in this pipeline can run.
    """
    fw_cfg = settings.get("firmware", {}) or {}
    if not fw_cfg.get("enabled", True):
        return "firmware step disabled in config", []
    minimum = (fw_cfg.get("minimum_version") or "").strip()
    if not minimum:
        return "no floor configured — firmware left as-is", []

    current = (identity.get("firmware") or "").strip()
    if planet_versions_match(current, minimum):
        log.info("Firmware is %s — the configured floor; nothing to do.", current)
        return f"{current} — at the floor", []
    if current and planet_version_at_least(current, minimum):
        note = f"{current} is newer than the {minimum} floor — left as-is"
        log.warning("Firmware %s is NEWER than the configured floor %s — leaving "
                    "it alone (the config names a minimum, not a pin).",
                    current, minimum)
        return note, [note]

    image = Path(fw_cfg.get("bix_path") or "")
    if not image.is_absolute():
        image = BASE_DIR / image
    if not image.is_file():
        message = (f"firmware: the switch is on {current or 'an unreadable version'}, "
                   f"below the {minimum} floor, and the image is missing ({image}) "
                   "— download it from planet.com.tw and set firmware.bix_path. "
                   "Below the floor the SSH CLI does not work, so the rest of "
                   "this run cannot proceed either.")
        failures.append(message)
        log.error("Step 'firmware' FAILED: %s", message)
        return f"{current or 'unknown'} — below {minimum}, image missing", []

    log.info("Firmware %s is below the %s floor — flashing.",
             current or "unknown", minimum)
    after = client.upgrade_firmware(
        image, reboot_wait=int(fw_cfg.get("reboot_wait", 300)))
    return f"{current or 'unknown'} -> {after} (flashed to meet {minimum})", []


# --- pipeline (shared by CLI + web UI) ----------------------------------------

def configure_planet(client: PlanetClient, *, initial_password: str,
                     settings: dict, target_ip: Optional[str] = None,
                     ip_mode: str = "", mac: str = "") -> dict:
    """Run the full provisioning pipeline for ONE IGS-4215. Returns identity +
    per-step failures + verification. Raises SystemExit on a hard failure
    (wrong model, unusable SSH, a firmware flash that goes wrong).
    """
    new_password = settings.get("new_password", DEFAULT_NEW_PASSWORD)
    ntp_server = settings.get("ntp_server", DEFAULT_PLANET_NTP_SERVER)
    acronym = str(settings.get("timezone_acronym", DEFAULT_PLANET_TZ_ACRONYM))[:4]
    offset = settings.get("timezone_offset", DEFAULT_PLANET_TZ_OFFSET)
    poe_cfg = settings.get("poe", {}) or {}
    minimum = ((settings.get("firmware", {}) or {}).get("minimum_version") or "").strip()

    # Fail before touching the device: a description the switch will refuse is a
    # config mistake, and finding it mid-run leaves the switch half-labelled.
    description_cmds(poe_cfg)

    # The factory password is `sw` + the last 6 digits of the MAC, so a fresh
    # switch needs its MAC before anything else. The caller normally has it off
    # ARP already; read it here when it does not (the CLI path).
    mac = mac or read_device_mac(client.host) or ""
    factory_password = settings.get("factory_password") or factory_password_for(mac)
    if not factory_password:
        log.warning("Could not read a MAC for %s, so the factory password "
                    "cannot be derived — only the shared password will be tried.",
                    client.host)

    # Which password to try FIRST is decided by where the switch answered.
    # The budget is three wrong attempts before a lockout that looks like a
    # dead management interface, so a wasted guess costs a third of it: a
    # switch still on the factory address is almost certainly still on the
    # factory password, and one already moved to the management subnet has
    # been provisioned before.
    on_factory_address = client.host == settings.get("host", DEFAULT_PLANET_HOST)
    candidates = ([factory_password, new_password] if on_factory_address
                  else [new_password, factory_password])

    # Identity over HTTP first — it is the only surface guaranteed to work on a
    # switch whose SSH may be broken, and it is what decides the firmware step.
    current_password = client.web_login_any([initial_password] + candidates)
    identity = client.get_identity()
    model = identity.get("model", "")
    if EXPECTED_MODEL not in model:
        raise SystemExit(f"This is a {model or 'device with no readable model'}, "
                         f"not a {EXPECTED_MODEL} — wrong tool for it.")

    failures, _step = make_step_runner(log)
    warnings = poe_budget_warnings(poe_cfg, identity)
    for warning in warnings:
        log.warning("%s", warning)

    firmware_note, fw_warnings = apply_firmware_floor(client, settings, identity,
                                                      failures)
    warnings += fw_warnings

    # A factory switch has SSH switched off entirely (and telnet too), so the
    # CLI has to be turned on before it can be used.
    _step("ssh-service", client.ensure_ssh_service)

    # SSH comes AFTER the firmware step on purpose: below b260324 the CLI is
    # unusable, and the upgrade is what makes the rest of this pipeline possible.
    try:
        client.login(current_password, new_password=new_password)
    except SshBroken as e:
        raise SystemExit(
            f"SSH is unusable on this switch: {e} Firmware reported: "
            f"{identity.get('firmware') or 'unknown'}.")

    # Usually nothing to do: a factory switch already got this password through
    # the forced change at login, and a re-run is on it already. Only a switch
    # found on some third password needs the (interactive) command.
    if client.password != new_password:
        _step("password",
              lambda: client.set_password(new_password, client.password))
    else:
        log.info("Password is already the shared one — nothing to change.")

    # See `poe_is_managed` for why this is off and how to turn it back on.
    if poe_is_managed(poe_cfg):
        _step("poe", lambda: client.configure(poe_cmds(poe_cfg)))
    else:
        log.info("PoE is left to the switch — the bench sets no limits, "
                 "budget or priorities (poe.managed is false).")
    _step("port-names", lambda: client.configure(description_cmds(poe_cfg)))
    _step("ntp", lambda: client.configure(["clock source sntp",
                                           f"sntp host {ntp_server}"]))
    # 1-4 characters: a longer acronym is accepted and silently ignored.
    _step("timezone", lambda: client.configure([f"clock timezone {acronym} {offset}"]))

    verification: list[dict] = []
    try:
        verification = client.verify_configuration(settings=settings,
                                                   minimum_firmware=minimum)
        for line in format_verification(verification).splitlines():
            log.info("%s", line)
    except SystemExit as e:
        log.error("Verification step could not run: %s", e)

    # Telnet next-to-last, and only now: it is the way back in when SSH is not
    # working, so it is dropped only after SSH has demonstrably carried a run.
    if settings.get("disable_telnet", True):
        _step("telnet", lambda: client.configure(["no ip telnet"]))

    # Management address LAST — after this the switch is no longer on the
    # address we are talking to it on.
    dhcp = ip_mode == MODE_DHCP
    lan_ip = "" if dhcp else (target_ip or settings.get("lan_ip") or "").strip()
    reached_at = client.host
    if lan_ip and lan_ip != client.host:
        netmask = settings.get("netmask", DEFAULT_PLANET_NETMASK)
        # The session dies mid-command by design — the switch applies the
        # address and drops us — so neither call is checked for a prompt.
        try:
            client.cli("configure", check=False)
            client.cli(f"ip address {lan_ip} mask {netmask}", check=False, timeout=5)
        except Exception:
            pass
        client.close()
        moved = wait_for_host(lan_ip, 22,
                              timeout=int(settings.get("move_timeout", 120)))
        verification.append({"item": "lan-ip", "expected": lan_ip,
                             "actual": lan_ip if moved else "unreachable",
                             "ok": moved})
        if moved:
            client.host = lan_ip
            client.login(new_password)
            # The address change itself still has to be saved, and the session
            # that made it is gone.
            client.save_config()
            reached_at = lan_ip
        else:
            failures.append(f"lan-ip: the switch did not answer on {lan_ip}")
            reached_at = ""

    verify_failed = [check["item"] for check in verification if check["ok"] is False]
    ok = not failures and not verify_failed
    name = f"{EXPECTED_MODEL} {identity.get('mac', 'unknown')}"
    if ok:
        log.info("Provisioning complete for %s — all steps verified.", name)
    else:
        problems = failures + [f"verify:{item}" for item in verify_failed]
        log.error("Provisioning of %s finished with problems: %s",
                  name, " | ".join(problems))
    return {"identity": identity, "warnings": warnings, "failures": failures,
            "verification": verification, "ok": ok, "ip": lan_ip,
            "ip_mode": ip_mode, "reached_at": reached_at,
            "firmware_note": firmware_note}


def verify_planet(client: PlanetClient, *, settings: dict, resolve=None,
                  target_ip: Optional[str] = None, ip_mode: str = "") -> dict:
    """Check ONE finished switch against the baseline, changing nothing.

    Nothing about an IGS-4215's intended state is per-unit — the PoE plan, port
    names, NTP server and timezone all come from the station config — so there
    is nothing to look up in history the way the router tools do.
    """
    minimum = ((settings.get("firmware", {}) or {}).get("minimum_version") or "").strip()
    # A switch being verified should be on the shared password; the derived
    # factory one is the fallback for a unit an earlier run left half-done.
    password = client.web_login_any(
        [settings.get("new_password", DEFAULT_NEW_PASSWORD),
         settings.get("factory_password")
         or factory_password_for(read_device_mac(client.host) or "")])
    identity = client.get_identity()
    base = {"identity": identity, "warnings": [], "ip": client.host,
            "ip_mode": ip_mode, "reached_at": client.host,
            "firmware_note": "no firmware step on a verify run"}
    try:
        client.ensure_ssh_service()
        client.login(password)
    except SshBroken as e:
        # Worth its own branch: a switch that cannot be verified because its
        # SSH is broken has a known fix, and saying so beats "verify failed".
        return {**base, "failures": [f"ssh: {e}"], "verification": [], "ok": False,
                "reached_at": ""}
    verification = client.verify_configuration(settings=settings,
                                               minimum_firmware=minimum)
    for line in format_verification(verification).splitlines():
        log.info("%s", line)
    failed = [check["item"] for check in verification if check["ok"] is False]
    return {**base, "failures": [], "verification": verification, "ok": not failed}


def main():
    parser = argparse.ArgumentParser(
        description="Provision one PLANET IGS-4215-8UP2T2S PoE switch")
    parser.add_argument("--config",
                        default=str(BASE_DIR / "config/planet.config.json"))
    parser.add_argument("--host", default="")
    parser.add_argument("--password", default="",
                        help="current admin password (default: the shared one, "
                             "falling back to the factory 'sw'+last-6-of-MAC)")
    parser.add_argument("--verify", action="store_true",
                        help="check a finished switch, change nothing")
    parser.add_argument("--no-firmware", action="store_true",
                        help="skip the firmware floor for this run")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format=LOG_LINE_FORMAT)
    # paramiko logs at INFO through the bench formatter, which wants a serial
    # field its records do not carry — the result is a "Logging error" block
    # after every SSH step that reads like a failure and is not.
    logging.getLogger("paramiko").setLevel(logging.WARNING)
    set_log_serial(None)
    settings = load_settings(args.config)
    if args.no_firmware:
        settings.setdefault("firmware", {})["enabled"] = False

    host = args.host or settings.get("host", DEFAULT_PLANET_HOST)
    password = args.password or settings.get("new_password", DEFAULT_NEW_PASSWORD)
    client = PlanetClient(host=host,
                          username=settings.get("username", DEFAULT_USERNAME),
                          password=password)
    try:
        if args.verify:
            result = verify_planet(client, settings=settings)
        else:
            # `mac=""` lets the pipeline read it off ARP and derive the factory
            # password itself, which is what a fresh switch needs.
            result = configure_planet(client, initial_password=args.password,
                                      settings=settings, mac="")
    finally:
        client.close()
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
