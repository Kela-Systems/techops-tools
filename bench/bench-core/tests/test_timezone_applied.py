"""The timezone has to reach the CLOCK, and NTP has to reach the daemon.

The first TSW202 off the bench reported four green verification rows while
sitting on a +0000 clock. Its config was perfect:

    system.system.timezone='IST-2IDT,M3.4.4/26,M10.5.0'
    system.ntp.zoneName='Asia/Jerusalem'

...and `date` said `Thu Aug 27 13:20:03 UTC 2026`. Two separate faults met:
`set_timezone` never reloaded the system config, so nothing wrote the /etc/TZ
that libc actually reads; and `verify_configuration` read back the same two
options `set_timezone` had just written, which `uci set` creates whether or not
the device consumes them. A read-back of your own write cannot fail.

These cover the SHARED client, so they hold for the RUTM08 and OTD500 too —
they were checked the same hollow way and had no test on the timezone row at
all, which is why this went unnoticed.
"""
import pytest

from bench_core import (
    POSIX_TZ,
    TeltonikaClient,
    expected_utc_offset,
)

ZONE = "Asia/Jerusalem"
POSIX_ZONE = POSIX_TZ[ZONE]
HOST = "otd-haifa"
SHARED = "Kelasys123!"
# What a device really on ZONE reports for `date +%z`, today. Computed rather
# than fixed: Asia/Jerusalem is +0300 under IDT and +0200 in winter, so a
# hard-coded expectation would fail the suite for half the year.
CORRECT_OFFSET = expected_utc_offset(ZONE) or "+0300"

needs_tzdata = pytest.mark.skipif(
    not expected_utc_offset(ZONE),
    reason="needs a tz database to say what the zone means today (tzdata)")


class FakeDevice:
    """Stands in for `ssh_exec`. The clock is deliberately independent of the
    UCI options, because that independence IS the bug: the options were
    committed and the clock stayed on UTC."""

    def __init__(self, *, offset=CORRECT_OFFSET, hostname=HOST, polling=(),
                 zone_shown=ZONE):
        self.commands: list[str] = []
        self.offset = offset
        self.hostname = hostname
        self.polling = list(polling)
        # What the WebUI dropdown would render, i.e. system.system.zoneName.
        # Separate from the clock because the TSW202 had them disagree.
        self.zone_shown = zone_shown

    def __call__(self, command, check=True, exec_timeout=None):
        self.commands.append(command)
        if command.startswith("date +%z"):
            return self.offset
        if "system.system.zoneName" in command and "uci -q get" in command:
            return self.zone_shown
        if "system.system.hostname" in command and "uci get" in command:
            return self.hostname
        if "grep '[n]tpd'" in command:
            return "\n".join(f" 6055 root 1680 S< /usr/sbin/ntpd -n -N -p {s}"
                             for s in self.polling)
        return ""

    def wrote(self, fragment: str) -> bool:
        return any(fragment in c for c in self.commands)


def client(**kwargs) -> TeltonikaClient:
    c = TeltonikaClient(host="192.0.2.1")
    c.ssh_exec = FakeDevice(**kwargs)
    c.device = c.ssh_exec          # test handle
    c.password = SHARED
    return c


def verify(c):
    return c.verify_configuration(hostname=HOST, zonename=ZONE,
                                  new_password=SHARED, sim_4g=False,
                                  rms=False, tailscale=False)


def timezone_row(checks):
    return next(c for c in checks if c["item"] == "timezone")


# ── setting it: committing is not applying ───────────────────────────────────

def test_the_timezone_is_committed_and_then_applied():
    c = client()
    c.set_timezone(ZONE)
    assert c.device.wrote(f"uci set system.system.timezone='{POSIX_ZONE}'")
    assert c.device.wrote("uci commit system")
    # The step whose absence left a correctly-configured switch on UTC.
    assert c.device.wrote("/etc/init.d/system reload")


def test_every_consumer_of_the_zone_name_is_written():
    # Three keys, three readers. Proven on a TSW202 by setting the zone in the
    # WebUI and diffing `uci show system`: the dropdown owns the one in the
    # `system` section, which this tool used not to write at all.
    c = client()
    c.set_timezone(ZONE)
    assert c.device.wrote(f"uci set system.system.zoneName='{ZONE}'")
    assert c.device.wrote(f"uci set system.ntp.zoneName='{ZONE}'")


@needs_tzdata
def test_a_clock_the_reload_did_not_move_gets_the_tz_file_written():
    # Not every build's `system` init script owns /etc/TZ. When the clock is
    # still wrong after the reload, write what libc reads.
    c = client(offset="+0000")
    c.set_timezone(ZONE)
    assert c.device.wrote("> /tmp/TZ")
    assert c.device.wrote("> /etc/TZ")


@needs_tzdata
def test_a_clock_the_reload_did_move_is_left_alone():
    c = client()
    c.set_timezone(ZONE)
    assert not c.device.wrote("> /etc/TZ")


def test_an_unmappable_zone_is_refused_rather_than_half_applied():
    c = client()
    with pytest.raises(SystemExit):
        c.set_timezone("Mars/Olympus_Mons")
    assert c.device.commands == []


# ── checking it: by effect, not by reading our own write back ────────────────

@needs_tzdata
def test_a_clock_still_on_utc_fails_even_though_the_options_read_back():
    # The exact state of the bench unit. The old check passed this.
    row = timezone_row(verify(client(offset="+0000")))
    assert row["ok"] is False
    assert "+0000" in row["actual"]


@needs_tzdata
def test_a_clock_on_the_zone_passes():
    assert timezone_row(verify(client()))["ok"] is True


def test_an_unreadable_clock_fails_rather_than_passes():
    assert timezone_row(verify(client(offset="")))["ok"] is False


@needs_tzdata
def test_a_right_clock_with_a_utc_webui_fails():
    # The exact state that provoked this: clock correct after the reload, and
    # the Date & Time page still showing UTC because system.system.zoneName was
    # never written. One Save & Apply on that page and the clock goes too, so
    # this must not report a pass.
    row = timezone_row(verify(client(zone_shown="")))
    assert row["ok"] is False
    assert "unset" in row["actual"]


@needs_tzdata
def test_a_webui_showing_another_zone_fails():
    row = timezone_row(verify(client(zone_shown="Europe/Vilnius")))
    assert row["ok"] is False
    assert "Europe/Vilnius" in row["actual"]


def test_the_row_reports_both_halves_so_a_reader_can_tell_them_apart():
    # "timezone FAILED" is not actionable; which of the clock and the UI is
    # wrong decides whether an operator re-runs or edits the page.
    row = timezone_row(verify(client()))
    assert "clock at" in row["actual"] and "WebUI shows" in row["actual"]


def test_the_row_names_the_zone_a_human_asked_for():
    # The operator set "Asia/Jerusalem", not "IST-2IDT,M3.4.4/26,M10.5.0";
    # an offset alone would make the row unreadable at the bench.
    assert ZONE in timezone_row(verify(client()))["expected"]


def test_no_check_carries_the_password():
    # TEC-349: these rows are shipped to bench-central verbatim.
    for check in verify(client()):
        assert SHARED not in str(check), check


# ── the same failure mode, for NTP: a config the daemon never picked up ──────

def test_running_ntp_servers_reads_the_live_command_line():
    c = client(polling=["192.168.88.10"])
    assert c.running_ntp_servers() == ["192.168.88.10"]


def test_running_ntp_servers_is_empty_when_no_daemon_runs():
    assert client(polling=[]).running_ntp_servers() == []


def test_the_ntpd_lookup_cannot_match_its_own_grep():
    # `grep ntpd` matches the grep process itself and would report a daemon on
    # a device that has none; the bracket trick is load-bearing.
    c = client(polling=[])
    c.running_ntp_servers()
    assert any("[n]tpd" in cmd for cmd in c.device.commands)


def test_configured_ntp_servers_ignores_the_device_hostname():
    # `system.system.hostname` is an option named hostname in the same package
    # as the time servers, and is not one.
    c = TeltonikaClient(host="192.0.2.1")
    c.ssh_exec = lambda *a, **k: ("system.system.hostname='TSW202'\n"
                                  "system.ntp.server='192.168.88.10'\n")
    assert c.configured_ntp_servers() == ["192.168.88.10"]


# ── the host-side expectation ────────────────────────────────────────────────

@needs_tzdata
def test_the_expected_offset_follows_dst_rather_than_being_tabled():
    # Asia/Jerusalem is +0300 under IDT and +0200 in winter. Whichever it is
    # today, it must be one of the two — and it must be a real offset, since
    # returning "" is how the check degrades to "at least not UTC".
    assert expected_utc_offset(ZONE) in ("+0200", "+0300")


def test_an_unknown_zone_yields_no_expectation_instead_of_raising():
    # The check falls back to "the clock left UTC"; it must not crash the run.
    assert expected_utc_offset("Mars/Olympus_Mons") == ""


@needs_tzdata
def test_utc_is_expected_to_be_utc():
    assert expected_utc_offset("UTC") == "+0000"
