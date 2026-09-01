"""Installing the Tailscale package, and the clock problem that stops it.

A RUTM08 bench run failed with this, and it sent the operator looking in the
wrong place entirely:

    tailscale: Tailscale install incomplete (tailscale/tailscaled binary or
    /etc/init.d/tailscale missing). opkg said: Unknown package 'tailscale'.
    The default package feeds only include software supported by Teltonika. [...]

Nothing in that sentence is the cause. Read off the device afterwards:

    # opkg update; echo "rc=$?"
    *** Failed to download the package list from https://opkg.teltonika-...
     * opkg_download: Failed to download ...Packages.gz, wget returned 5.
    rc=1
    # nslookup opkg.teltonika-networks.com    -> resolves fine
    # date -u                                 -> Thu May 14 13:44:59 UTC 2026
    # cat /etc/version                        -> RUTM_R_00.07.22.3

The run was on 1 September. `wget returned 5` is certificate verification
failure, and the device's clock was **110 days in the past** — so the feed's
certificate had not been issued yet as far as the device was concerned. May 14 is
the firmware image's build date: a RutOS unit with no battery-backed clock boots
by seeding time from the newest mtime under /etc, and the FOTA step reboots
mid-run. Nothing on this bench then corrects it, because the configured time
server is on the assembly network and a RUTM08 is Ethernet-only, with no cellular
modem clock to fall back on the way an OTD500 has.

`opkg install` reported "Unknown package" because a package it has no index for
is, to opkg, unknown — and `ensure_tailscale_installed` used to run `opkg update`
with its result discarded, so the real cause could not surface at all.

Two things are tested here. That the clock is measured against this machine and
stepped before the fetch (a year sanity check does not help: 2026 is a perfectly
plausible year, and it was 110 days wrong). And that the three remaining failure
modes stay distinguishable, because they need three different actions:

  * the index did not download — say which of DNS or trust failed.
  * the index downloaded and does not list `tailscale` — this build's feed does
    not carry it, and no retry fixes that.
  * the index listed it, opkg ran, and the binary or RutOS service wrapper is
    still missing — a genuinely half-finished install.
"""
import time

from bench_core import TeltonikaClient

# The message the failing run actually produced, verbatim. It is the thing the
# operator must NOT be handed when the real cause was the clock.
UNKNOWN_PACKAGE = (
    "Unknown package 'tailscale'.\n"
    "The default package feeds only include software supported by Teltonika. If you "
    "want to use OpenWrt feeds, run opkg with '--force_feeds "
    "/etc/opkg/openwrt/distfeeds.conf'. Please be aware that packages from OpenWrt "
    "or other third-party feeds are not supported and may not work correctly. "
    "Package installation encountered an error, removing previously installed "
    "packages."
)
# A real RutOS feed URL: the path is an opaque per-build hash, with no model or
# firmware version in it to reconstruct from. Hence reading distfeeds.conf.
FEED_URL = ("https://opkg.teltonika-networks.com/"
            "e6d1d303c85c9f7b55756aaf1a5a74c7d91d910466b5685c5ebfabd09d161833")
DISTFEEDS = f"src/gz teltonika_base {FEED_URL}\n"
# wget 5 is certificate verification failure — the observed output.
UPDATE_FAILED_TRUST = (
    f"Downloading {FEED_URL}/Packages.gz\n"
    f"*** Failed to download the package list from {FEED_URL}/Packages.gz\n\n"
    "Collected errors:\n"
    f" * opkg_download: Failed to download {FEED_URL}/Packages.gz, wget returned 5.")
# wget 4 is a network failure, i.e. NOT a trust problem.
UPDATE_FAILED_NETWORK = UPDATE_FAILED_TRUST.replace("wget returned 5", "wget returned 4")

RUTM_FW = "RUTM_R_00.07.22.3"
# What the failing unit's clock was out by: the gap between the 07.22.3 build
# date it seeded from and the day of the run.
BEHIND = -110 * 86400
# The address every bench unit is pointed at. It lives on the ASSEMBLY network
# and is unreachable from the bench by construction (docs/verification-rows.md),
# which is what makes a wrong clock unrecoverable mid-run.
ASSEMBLY_NTP = "192.168.88.10"


class FakeDevice:
    """Stands in for `ssh_exec`.

    Two things are modelled as EFFECTS rather than flags, because that is what
    stops a do-nothing implementation from passing:

      * `opkg install` flips `installed`, so the presence check afterwards reads
        what the install actually did.
      * `opkg update` fails while `skew` is large and succeeds once it is small —
        the real coupling. A clock "fix" that does not move the clock therefore
        cannot make the good-path test pass.
    """

    def __init__(self, *, installed=False, update_rc=0, update_output=None,
                 feed_lists_ts=True, distfeeds=DISTFEEDS, resolves=True,
                 skew=0, date_s_accepts=("@epoch", "iso", "numeric"),
                 install_works=True, fw=RUTM_FW,
                 ntp_servers=(ASSEMBLY_NTP,), ntp_reachable=False):
        self.commands: list[str] = []
        self.installed = installed
        self.update_rc = update_rc
        self.update_output = update_output
        self.feed_lists_ts = feed_lists_ts
        self.distfeeds = distfeeds
        self.resolves = resolves
        self.skew = skew
        self.date_s_accepts = tuple(date_s_accepts)
        self.install_works = install_works
        self.fw = fw
        self.ntp_servers = list(ntp_servers)
        self.ntp_reachable = ntp_reachable

    @staticmethod
    def _syntax(arg: str) -> str:
        """Which `date -s` spelling this is. BusyBox builds accept different
        subsets, which is why the client tries more than one."""
        if arg.startswith("@"):
            return "@epoch"
        return "iso" if "-" in arg else "numeric"

    def __call__(self, command, check=True, exec_timeout=None):
        self.commands.append(command)
        if "command -v tailscale" in command:
            return "__HAVE__" if self.installed else "__MISS__"
        # Before the bare `date -u` read: that is a prefix of this.
        if command.startswith("date -u -s "):
            arg = command[len("date -u -s "):].split(" >")[0].strip().strip("'\"")
            if self._syntax(arg) in self.date_s_accepts:
                self.skew = 0
            return ""
        if command.startswith("date -u +%s"):
            return str(int(time.time()) + self.skew)
        if command.startswith("date -u"):
            return time.strftime("%a %b %e %H:%M:%S UTC %Y",
                                 time.gmtime(time.time() + self.skew))
        if command.startswith("opkg update"):
            # The coupling that made the real bug: the feed is HTTPS, so the
            # index cannot download while the clock is wrong.
            rc = 1 if abs(self.skew) > 3600 else self.update_rc
            out = "" if rc == 0 else (self.update_output or UPDATE_FAILED_TRUST)
            return f"{out}\n__rc={rc}"
        if command.startswith("cat /etc/opkg/distfeeds.conf"):
            return self.distfeeds
        if command.startswith("nslookup"):
            return "OK" if self.resolves else "FAIL"
        if command.startswith("ping "):
            return "OK" if self.ntp_reachable else "FAIL"
        if command.startswith("uci show ntpclient"):
            # No servers means no package at all — the TSW202 shape, where the
            # client lives in `system.ntp` and `uci show ntpclient` says nothing.
            if not self.ntp_servers:
                return ""
            # The shape `_uci_package` parses. RutOS numbers the server sections
            # `1`..`4`, a build detail the client discovers rather than assumes.
            lines = []
            for i, server in enumerate(self.ntp_servers, start=1):
                lines += [f"ntpclient.{i}=server",
                          f"ntpclient.{i}.hostname='{server}'"]
            lines += ["ntpclient.settings=ntpclient",
                      "ntpclient.settings.enabled='1'"]
            return "\n".join(lines)
        if command.startswith("opkg list tailscale"):
            return "tailscale - 1.62.0-1" if self.feed_lists_ts else ""
        if command.startswith("opkg install tailscale"):
            self.installed = self.install_works
            return "Installing tailscale" if self.install_works else UNKNOWN_PACKAGE
        if command.startswith("cat /etc/version"):
            return self.fw
        return ""


def client(**kwargs) -> TeltonikaClient:
    c = TeltonikaClient(host="192.0.2.1")
    c.ssh_exec = FakeDevice(**kwargs)
    c.device = c.ssh_exec          # test handle
    return c


def install(c) -> str:
    """Run the step and return the failure message, or "" when it succeeded."""
    try:
        c.ensure_tailscale_installed()
    except SystemExit as e:
        return str(e)
    return ""


def ran(c, prefix: str) -> bool:
    return any(cmd.startswith(prefix) for cmd in c.device.commands)


def index_of(c, prefix: str) -> int:
    return next(i for i, cmd in enumerate(c.device.commands)
               if cmd.startswith(prefix))


# ── already there ────────────────────────────────────────────────────────────

def test_an_installed_package_is_left_alone():
    c = client(installed=True)
    assert install(c) == ""
    assert not ran(c, "opkg"), "a present package must not be re-fetched"
    assert not ran(c, "date -u -s"), "and its clock is not anyone's business"


# ── the clock: the actual cause of the reported failure ──────────────────────

def test_the_reported_failure_now_provisions():
    """End to end on the failing unit's exact condition: 110 days behind, which
    is why the HTTPS index fetch was rejected. The fake only lets `opkg update`
    succeed once the clock is right, so this passes only if the clock moved."""
    c = client(skew=BEHIND)
    assert install(c) == ""
    assert c.device.installed


def test_the_clock_is_stepped_before_the_index_fetch():
    """Order is the whole point: fixing it afterwards fixes nothing."""
    c = client(skew=BEHIND)
    install(c)
    assert index_of(c, "date -u -s") < index_of(c, "opkg update")


def test_a_plausible_year_is_not_mistaken_for_a_good_clock():
    """The regression against the obvious wrong fix. The failing unit read 2026 —
    the correct year — and was still 110 days out, so any check of the form
    "is the year sane" passes exactly the failure it was written for."""
    c = client(skew=BEHIND)
    install(c)
    assert ran(c, "date -u -s"), "a wrong-but-plausible year must still be fixed"


def test_a_correct_clock_is_left_alone():
    """Stepping a synced clock on every run would be churn, and on a device
    whose ntpclient refuses to step backwards it is a real hazard."""
    c = client()
    install(c)
    assert not ran(c, "date -u -s")


def test_a_small_drift_is_left_alone():
    c = client(skew=120)
    install(c)
    assert not ran(c, "date -u -s")


def test_the_unambiguous_date_syntax_is_tried_first():
    """`@epoch` cannot be misread; the others are locale- and order-sensitive."""
    c = client(skew=BEHIND)
    install(c)
    first = c.device.commands[index_of(c, "date -u -s")]
    assert first.startswith("date -u -s @")


def test_a_build_that_rejects_epoch_syntax_still_gets_set():
    """BusyBox builds accept different `date -s` spellings, so the client tries
    several and reads the clock back instead of trusting an exit status."""
    c = client(skew=BEHIND, date_s_accepts=("iso",))
    assert install(c) == ""
    assert c.device.skew == 0


def test_a_build_that_rejects_every_syntax_fails_the_step_clearly():
    c = client(skew=BEHIND, date_s_accepts=())
    msg = install(c)
    assert "clock" in msg
    assert "110 days" in msg, "quantify it — 'wrong clock' is not actionable"
    assert "certificate" in msg


def test_an_unsettable_clock_says_it_will_not_fix_itself():
    """Whether a retry is worth anything is the operator's actual question."""
    msg = install(client(skew=BEHIND, date_s_accepts=()))
    assert "will not correct itself" in msg
    assert ASSEMBLY_NTP in msg, "name the server that isn't answering"


def test_a_reachable_time_server_is_not_blamed():
    """A unit that can still sync must not be reported as stuck — that sends an
    operator for an engineer over something about to fix itself."""
    msg = install(client(skew=BEHIND, date_s_accepts=(), ntp_reachable=True))
    assert "clock" in msg
    assert "will not correct itself" not in msg


def test_every_unreachable_time_server_is_named():
    msg = install(client(skew=BEHIND, date_s_accepts=(),
                         ntp_servers=(ASSEMBLY_NTP, "time.example.net")))
    assert ASSEMBLY_NTP in msg and "time.example.net" in msg


def test_a_device_with_no_ntp_client_package_does_not_break_the_message():
    """A TSW202 keeps its client in `system.ntp`, so `ntpclient` reads as empty.
    That must degrade to the plain clock report, not crash the diagnosis."""
    msg = install(client(skew=BEHIND, date_s_accepts=(), ntp_servers=()))
    assert "clock" in msg
    assert "will not correct itself" not in msg


def test_a_device_that_will_not_report_its_clock_is_not_guessed_at():
    """No reading means no claim: the step goes on to opkg rather than stepping
    a clock it cannot measure."""
    c = client()
    c.device.skew = 0
    assert c.device_clock_skew() is not None
    c.ssh_exec = lambda cmd, check=True, exec_timeout=None: ""
    assert c.device_clock_skew() is None
    c.ensure_clock_sane()          # must not raise


def test_a_time_server_is_only_probed_when_the_clock_cannot_be_set():
    """The probe costs a ping per configured server, and only earns it on the
    branch where the answer changes what the operator does."""
    c = client(skew=BEHIND)
    install(c)
    assert not ran(c, "ping "), "the clock was fixed; nothing to explain"


# ── cause 1: the index never downloaded ──────────────────────────────────────

def test_a_failed_index_download_is_named_as_the_cause():
    msg = install(client(update_rc=1))
    assert "opkg package index" in msg
    assert "opkg update said" in msg


def test_a_failed_index_download_does_not_blame_the_package():
    """The original regression. With no index, `opkg install` says "Unknown
    package" — so the old code could only report the one cause that wasn't true."""
    msg = install(client(update_rc=1))
    assert "Unknown package" not in msg
    assert "does not list" not in msg


def test_a_failed_index_download_skips_the_install_attempt():
    """`opkg install` carries a 300s budget. Spending it on a request that
    cannot succeed is how a run takes five minutes to tell you nothing."""
    c = client(update_rc=1)
    install(c)
    assert not ran(c, "opkg install")


def test_a_rejected_certificate_on_a_correct_clock_points_at_the_ca_bundle():
    """`wget returned 5` is a trust failure. With the clock already corrected,
    the remaining candidate is the device's CA store — so the message must not
    send anyone back to the clock."""
    msg = install(client(update_rc=1, update_output=UPDATE_FAILED_TRUST))
    assert "certificate" in msg
    assert "ca-bundle" in msg or "ca-certificates" in msg


def test_a_network_failure_is_not_reported_as_a_trust_failure():
    """Only wget 5 means trust. Reading any non-zero exit as a certificate
    problem would be a confident wrong answer."""
    msg = install(client(update_rc=1, update_output=UPDATE_FAILED_NETWORK))
    assert "certificate" not in msg


def test_unresolvable_feed_host_is_reported():
    """`wait_for_internet` is satisfied by an ICMP ping to a literal address, so
    a device with routing and no resolver reads as online and then cannot fetch
    a feed named by hostname."""
    msg = install(client(update_rc=1, resolves=False))
    assert "resolve" in msg
    assert "opkg.teltonika-networks.com" in msg, "name the host it actually uses"


def test_the_feed_host_comes_from_the_device_not_a_constant():
    """RutOS feed paths are an opaque per-build hash, so there is nothing to
    reconstruct — diagnosing a hardcoded host would name one this device never
    contacts."""
    feeds = "src/gz teltonika_base https://feeds.example.net/9f2a1c/base\n"
    msg = install(client(update_rc=1, resolves=False, distfeeds=feeds))
    assert "feeds.example.net" in msg


def test_a_device_with_no_feeds_configured_still_reports_the_download_failure():
    msg = install(client(update_rc=1, distfeeds=""))
    assert "opkg package index" in msg


# ── cause 2: the feed does not carry the package ─────────────────────────────

def test_a_feed_without_the_package_is_distinguished_from_a_broken_download():
    msg = install(client(feed_lists_ts=False))
    assert "does not list 'tailscale'" in msg
    assert "opkg update said" not in msg, "the index downloaded fine"


def test_a_feed_without_the_package_reports_the_running_firmware():
    """Which firmware is running is the actionable fact: the feed is per build,
    so this is usually the firmware step not having run."""
    msg = install(client(feed_lists_ts=False, fw="RUTM_R_00.07.06.1"))
    assert "RUTM_R_00.07.06.1" in msg


def test_a_feed_without_the_package_quotes_the_feed_it_searched():
    """The URL is the one thing you can paste into a browser to settle whether
    the package is there, and it cannot be derived from the model and version."""
    msg = install(client(feed_lists_ts=False))
    assert FEED_URL in msg


def test_a_feed_without_the_package_skips_the_install_attempt():
    c = client(feed_lists_ts=False)
    install(c)
    assert not ran(c, "opkg install")


def test_the_force_feeds_suggestion_is_warned_against():
    """opkg's own output invites `--force_feeds`, which pulls unsupported
    OpenWrt builds onto a fleet device. The run should not let that read as the
    obvious next step."""
    msg = install(client(feed_lists_ts=False))
    assert "force_feeds" in msg
    assert "unsupported" in msg


# ── cause 3: the install really did half-finish ───────────────────────────────

def test_a_half_finished_install_still_reports_the_opkg_output():
    c = client(install_works=False)
    msg = install(c)
    assert "install incomplete" in msg
    assert "Unknown package" in msg, "here opkg's output IS the evidence"
    assert ran(c, "opkg install"), "this cause is only reachable via the install"


# ── the good path ────────────────────────────────────────────────────────────

def test_a_working_install_does_the_three_opkg_steps_in_order():
    c = client()
    assert install(c) == ""
    opkg = [cmd.split()[1] for cmd in c.device.commands if cmd.startswith("opkg")]
    assert opkg == ["update", "list", "install"]


def test_the_presence_check_is_read_after_the_install_not_before():
    """The pass is the binary plus the RutOS service wrapper being on the
    device. opkg exiting 0 is not the same claim."""
    c = client()
    install(c)
    presence = [i for i, cmd in enumerate(c.device.commands)
                if "command -v tailscale" in cmd]
    last_install = max(i for i, cmd in enumerate(c.device.commands)
                       if cmd.startswith("opkg install"))
    assert presence[-1] > last_install
