"""Pipeline + client tests for the TSW202 (tsw_configure.py).

No hardware: `FakeSwitch` stands in for `ssh_exec`, so these pin the exact UCI
commands a real switch would receive and the exact decision the firmware step
makes. The firmware branch is the one worth being strict about — the config
names a FLOOR, and getting the comparison wrong downgrades a switch that
arrived on Teltonika's newer "Latest" build.
"""
import re

import pytest
import requests

import bench_core
from bench_core import DEFAULT_NEW_PASSWORD, expected_utc_offset

import tsw_configure as mod
from tsw_configure import TswClient, apply_firmware_floor, configure_tsw

FLOOR = "TSW2_R_00.01.07.1"
NEWER = "TSW2_R_00.01.10"
OLDER = "TSW2_R_00.01.05"

NTP = "192.168.88.10"
ZONE = "Asia/Jerusalem"
POSIX_ZONE = "IST-2IDT,M3.4.4/26,M10.5.0"
# What a switch that really is on ZONE reports for `date +%z`, today. Falls back
# to a non-UTC offset on a machine with no tz database, which is the same thing
# the fallback branch of the check settles for.
CORRECT_OFFSET = expected_utc_offset(ZONE) or "+0300"


class FakeSwitch:
    """Stands in for `TswClient.ssh_exec`: canned answers for the reads, a
    recording of every command for the writes."""

    def __init__(self, *, version=FLOOR, uci=None, model="TSW202",
                 mnfinfo=True, board="", network=None, offset=None,
                 system_show=None, polling=None, ntpclient_show=""):
        self.commands: list[str] = []
        self.version = version
        self.model = model
        self.mnfinfo = mnfinfo
        self.board = board
        self.uci = dict(uci or {})
        # `date +%z` — the clock's real offset, deliberately settable
        # independently of the UCI options, because that is the failure mode:
        # the options were written and the clock stayed on UTC. The default is
        # computed rather than fixed, so the suite doesn't start failing in
        # winter when Asia/Jerusalem leaves IDT for +0200.
        self.offset = CORRECT_OFFSET if offset is None else offset
        # `uci show system`. None => derive it from self.uci so the common case
        # stays a one-dict setup.
        self.system_show = system_show
        # What the live ntpd is polling, per its command line. Separate from the
        # config for the same reason as the clock: the daemon not having picked
        # a committed config up is its own failure mode.
        self.polling = [NTP] if polling is None else polling
        # `uci show ntpclient`. Empty by default: the switch was believed to
        # have no such package, and the tests that predate finding one on a
        # real unit describe that build.
        self.ntpclient_show = ntpclient_show
        # {section: ip} as `uci show network` would report it. Defaults to what
        # the first real TSW202 had: an addressed section that is NOT `lan`.
        self.network = {"lan_mgmt": "192.0.2.2"} if network is None else network

    def __call__(self, command, check=True, exec_timeout=None):
        self.commands.append(command)

        # Writes land in the same dict the reads come out of, so a pipeline run
        # verifies against what it actually set rather than against a fixture.
        # `_uci` chains several sets into one shell line, hence findall.
        for path, value in re.findall(r"uci set (\S+?)=('[^']*'|\S+)", command):
            self.uci[path] = value.strip("'")

        if "uci show network" in command:
            return "\n".join(f"network.{s}=interface\nnetwork.{s}.ipaddr='{ip}'"
                             for s, ip in self.network.items())
        if "uci show ntpclient" in command:
            return self.ntpclient_show
        if "uci show system" in command:
            if self.system_show is not None:
                return self.system_show
            return "\n".join(f"{path}='{value}'"
                             for path, value in self.uci.items())
        if command.startswith("date +%z"):
            return self.offset
        if "grep '[n]tpd'" in command:
            return "\n".join(
                f" 6055 root  1680 S<  /usr/sbin/ntpd -n -N -p {s}"
                for s in self.polling)
        if "cat /etc/version" in command:
            return self.version
        if "mnfinfo" in command:
            if not self.mnfinfo:
                return ""
            return ('{"serial":"6010212527","mac":"20972B2B00F7",'
                    f'"name":"{self.model}"}}')
        if "system board" in command or "board.json" in command or "device-tree" in command:
            return self.board
        if command.startswith("gsmctl"):
            return ""          # no modem on a switch
        if "uci -q get" in command or "uci get" in command:
            path = re.search(r"uci (?:-q )?get ([^\s;]+)", command).group(1)
            value = self.uci.get(path)
            if value is None:
                # A missing entry only fails the CALL when the shell isn't
                # already handling it: `uci get X || uci set X=...` exits 0,
                # which is exactly how the NTP section gets created.
                if check and "||" not in command:
                    raise SystemExit(f"uci: Entry not found ({path})")
                return ""
            return value
        return ""

    def wrote(self, fragment: str) -> bool:
        return any(fragment in c for c in self.commands)


class NoRest:
    """The REST half of the client, unplugged. `get_identity` also GETs
    /system/device/status, and 192.0.2.x is a black hole (TEST-NET-1) rather
    than a refused connection — without this the suite hangs on the socket
    timeout instead of failing. The client already treats a REST error as "that
    source had nothing", which is the behaviour under test on a firmware line
    that may not serve the endpoint at all."""

    def get(self, *_a, **_k):
        raise requests.exceptions.ConnectionError("no REST in tests")

    post = get


def client(**kwargs) -> TswClient:
    switch = FakeSwitch(**kwargs)
    c = TswClient(host="192.0.2.2")
    c.ssh_exec = switch
    c.s = NoRest()
    c.switch = switch          # test handle
    c.password = DEFAULT_NEW_PASSWORD
    return c


# ── NTP: a list option, not a plain one ──────────────────────────────────────

def test_ntp_server_replaces_the_list_rather_than_appending():
    c = client(uci={"system.ntp": "timeserver"})
    c.set_ntp_server(NTP)
    # The delete is the point: `uci set` on a list leaves the factory pool
    # entries in front of ours, and the switch keeps asking the internet for
    # time it should be getting from the site.
    assert c.switch.wrote("uci -q delete system.ntp.server")
    assert c.switch.wrote(f"uci add_list system.ntp.server={NTP}")
    assert c.switch.wrote("uci set system.ntp.enabled='1'")
    assert c.switch.wrote("uci commit system")
    assert c.switch.wrote("/etc/init.d/sysntpd restart")


def test_ntp_creates_the_section_when_it_is_absent():
    # set_timezone writes into system.ntp too, but only options that already
    # exist — on a firmware that ships no timeserver section, add_list would
    # fail without this.
    c = client()
    c.set_ntp_server(NTP)
    assert c.switch.wrote("uci set system.ntp=timeserver")


# ── the stock pool the list option does not reach ────────────────────────────
#
# A switch off the bench read back `configured 192.168.88.10, time1..4.google.com
# (enabled=1); ntpd polling 192.168.88.10`: the list option was ours and the live
# daemon polled ours alone, while the four factory servers sat in a section EACH,
# which is the shape the WebUI renders and the shape clearing the list misses.

STOCK_POOL = ("system.ntp=timeserver\n"
              "system.ntp.enabled='1'\n"
              + "".join(f"system.@ntpserver[{i}]=ntpserver\n"
                        f"system.@ntpserver[{i}].hostname='time{i + 1}.google.com'\n"
                        for i in range(4)))


def test_the_stock_pool_sections_are_deleted_not_only_the_list():
    c = client(system_show=STOCK_POOL)
    c.set_ntp_server(NTP)
    for i in range(4):
        assert c.switch.wrote(f"uci delete 'system.@ntpserver[{i}]'")


def test_the_stock_pool_is_deleted_back_to_front():
    # Anonymous sections are addressed by index, so deleting @ntpserver[0] first
    # renumbers the three still to go and the last delete removes the wrong one.
    c = client(system_show=STOCK_POOL)
    c.set_ntp_server(NTP)
    line = next(cmd for cmd in c.switch.commands if "uci delete 'system.@ntpserver" in cmd)
    assert [int(i) for i in re.findall(r"@ntpserver\[(\d+)\]", line)] == [3, 2, 1, 0]


def test_the_timeserver_section_itself_is_never_deleted():
    # system.ntp is typed `timeserver`, not `ntpserver`, and holds the list
    # option written moments earlier — deleting it takes our own server with it.
    c = client(system_show=STOCK_POOL)
    c.set_ntp_server(NTP)
    deleted = re.findall(r"uci delete '([^']+)'", " ".join(c.switch.commands))
    assert deleted and all(d.startswith("system.@ntpserver[") for d in deleted)


def test_a_switch_with_no_stock_pool_is_left_alone():
    c = client(uci={"system.ntp": "timeserver"})
    c.set_ntp_server(NTP)
    assert not c.switch.wrote("uci delete ")


# ── the second time subsystem, on a build that has one ───────────────────────
#
# A switch off the bench failed the NTP row with a clean `system` package: the
# factory pool was in `ntpclient`, which `configured_ntp_servers` scans and
# `set_ntp_server` did not write. The row was right and the switch really was
# carrying four Google servers.

STOCK_NTPCLIENT = ("".join(f"ntpclient.{i}=ntpserver\n"
                           f"ntpclient.{i}.hostname='time{i}.google.com'\n"
                           for i in range(1, 5))
                   + "ntpclient.ntpdrift=ntpdrift\n"
                   "ntpclient.ntpclient=ntpclient\n"
                   "ntpclient.ntpclient.enabled='1'\n"
                   "ntpclient.ntpclient.interval='86400'\n")


def test_the_ntpclient_package_is_pointed_at_our_server_too():
    c = client(ntpclient_show=STOCK_NTPCLIENT)
    c.set_ntp_server(NTP)
    assert c.switch.wrote(f"uci set ntpclient.1.hostname={NTP}")
    assert c.switch.wrote("uci commit ntpclient")


def test_the_stock_ntpclient_servers_are_deleted():
    # Kept one section (rewritten above) and dropped the rest — four Google
    # servers left below ours cost a failover timeout each on an offline site.
    c = client(ntpclient_show=STOCK_NTPCLIENT)
    c.set_ntp_server(NTP)
    for section in ("2", "3", "4"):
        assert c.switch.wrote(f"uci delete ntpclient.{section}")
    assert not c.switch.wrote("uci delete ntpclient.1")


def test_a_switch_with_no_ntpclient_package_is_untouched():
    # The build the tool was written against. Delegating unconditionally would
    # abort the run: set_ntp_client raises when the package is absent.
    c = client(uci={"system.ntp": "timeserver"})
    c.set_ntp_server(NTP)
    # The probe itself (`uci show ntpclient`) is fine; a write is not.
    assert not c.switch.wrote("uci set ntpclient")
    assert not c.switch.wrote("uci delete ntpclient")


# ── the timezone has to reach the CLOCK, not just the config ─────────────────
#
# The first real TSW202 committed both `system.system.timezone` and
# `system.ntp.zoneName` exactly right and ran on a +0000 clock: libc reads
# /etc/TZ, and nothing writes it until the system config is reloaded.

def test_the_timezone_is_applied_and_not_only_committed():
    c = client()
    c.set_timezone(ZONE)
    assert c.switch.wrote(f"uci set system.system.timezone='{POSIX_ZONE}'")
    assert c.switch.wrote("uci commit system")
    assert c.switch.wrote("/etc/init.d/system reload")


@pytest.mark.skipif(not expected_utc_offset(ZONE),
                    reason="the fallback only triggers when this machine can "
                           "say what the zone means today (needs tzdata)")
def test_a_clock_that_reloading_did_not_move_gets_the_tz_file_written():
    c = client(offset="+0000")          # the reload didn't take on this build
    c.set_timezone(ZONE)
    assert c.switch.wrote("/etc/init.d/system reload")
    assert c.switch.wrote("> /tmp/TZ")
    assert c.switch.wrote("> /etc/TZ")


def test_a_clock_the_reload_did_move_is_left_alone():
    c = client()                        # offset already correct for ZONE
    c.set_timezone(ZONE)
    assert not c.switch.wrote("> /etc/TZ")


def test_configured_ntp_servers_reads_a_list_option():
    c = client(uci={"system.ntp.server": f"{NTP} 1.pool.ntp.org"})
    assert c.configured_ntp_servers() == [NTP, "1.pool.ntp.org"]


def test_configured_ntp_servers_reads_one_section_per_server_too():
    # The shape the TSW202 WebUI's "Hostname" column implies. A reader that only
    # knows `list server` reports an empty, clean-looking config here.
    show = ("system.@ntpserver[0].hostname='time1.google.com'\n"
            "system.@ntpserver[1].hostname='time2.google.com'\n")
    assert client(system_show=show).configured_ntp_servers() == [
        "time1.google.com", "time2.google.com"]


def test_configured_ntp_servers_is_empty_when_unset():
    assert client(system_show="").configured_ntp_servers() == []


# ── identity: the model must be readable or the run is refused ────────────────

def test_model_comes_from_mnfinfo_when_it_is_there():
    assert client().get_identity()["model"] == "TSW202"


def test_model_falls_back_to_the_board_when_mnfinfo_is_absent():
    # assert_device_model refuses a device whose model it cannot read, which is
    # right — but it would refuse EVERY switch if this firmware line ships no
    # mnfinfo object. The board knows.
    c = client(mnfinfo=False, board="Teltonika TSW202")
    assert c.get_identity()["model"] == "TSW202"


def test_an_unreadable_model_stays_unknown():
    # Better than a guess: assert_device_model turns this into a refusal.
    c = client(mnfinfo=False, board="")
    assert c.get_identity()["model"] == "unknown"


# ── firmware: the floor, in all three directions ─────────────────────────────

def firmware_settings(bin_path="", minimum=FLOOR):
    return {"firmware": {"minimum_version": minimum, "bin_path": bin_path,
                         "keep_settings": True}}


def flashing_client(version, monkeypatch):
    """A client whose upgrade_firmware records the call instead of flashing."""
    c = client(version=version)
    flashes = []
    monkeypatch.setattr(c, "upgrade_firmware",
                        lambda **kw: flashes.append(kw))
    monkeypatch.setattr(c, "get_identity", lambda: {"firmware": FLOOR})
    c.flashes = flashes
    return c


def test_a_switch_at_the_floor_is_left_alone(monkeypatch):
    c = flashing_client(FLOOR, monkeypatch)
    failures = []
    note, warnings = apply_firmware_floor(c, firmware_settings(), failures)
    assert c.flashes == []
    assert failures == [] and warnings == []
    assert "at the floor" in note


def test_a_newer_switch_is_not_downgraded(monkeypatch, tmp_path):
    # Teltonika's "Latest" runs ahead of its "Stable" for this family, so a
    # switch arriving newer than the pin is routine. Flashing it backwards
    # would be the surprise.
    image = tmp_path / "fw.bin"
    image.write_bytes(b"not really firmware")
    c = flashing_client(NEWER, monkeypatch)
    failures = []
    note, warnings = apply_firmware_floor(c, firmware_settings(str(image)), failures)
    assert c.flashes == []
    assert failures == []
    assert warnings == [note]      # surfaced in the record, not silent
    assert "newer than" in note and NEWER in note


def test_an_older_switch_is_flashed(monkeypatch, tmp_path):
    image = tmp_path / "fw.bin"
    image.write_bytes(b"not really firmware")
    c = flashing_client(OLDER, monkeypatch)
    failures = []
    note, warnings = apply_firmware_floor(c, firmware_settings(str(image)), failures)
    assert c.flashes == [{"bin_path": str(image), "keep_settings": True}]
    assert failures == [] and warnings == []
    assert note.startswith(f"{OLDER} -> ")


def test_an_older_switch_with_no_image_fails_the_step_but_not_the_run(monkeypatch):
    # A missing image is a station config problem. Aborting would leave the
    # switch with a new password and nothing else; recording the failure and
    # carrying on gets the rest applied and still reports it.
    c = flashing_client(OLDER, monkeypatch)
    failures = []
    note, warnings = apply_firmware_floor(c, firmware_settings("/nope/missing.bin"),
                                          failures)
    assert c.flashes == []
    assert len(failures) == 1
    assert "below the" in failures[0] and "firmware.bin_path" in failures[0]
    assert "image missing" in note


def test_no_floor_configured_skips_the_step(monkeypatch):
    c = flashing_client(OLDER, monkeypatch)
    failures = []
    note, _ = apply_firmware_floor(c, firmware_settings(minimum=""), failures)
    assert c.flashes == [] and failures == []
    assert "no floor" in note


def test_an_unreadable_version_is_treated_as_below_the_floor(monkeypatch, tmp_path):
    # fw_version_at_least says False when it cannot tell, so "we don't know"
    # flashes rather than silently skipping a needed upgrade.
    image = tmp_path / "fw.bin"
    image.write_bytes(b"x")
    c = flashing_client("", monkeypatch)
    failures = []
    apply_firmware_floor(c, firmware_settings(str(image)), failures)
    assert c.flashes == [{"bin_path": str(image), "keep_settings": True}]


# ── the management section is discovered, not assumed ────────────────────────
#
# The first TSW202 on the bench failed the move with `uci: Invalid argument`,
# which is what UCI says when a section does not resolve: its management
# address is not on `network.lan` the way every RutOS router's is. Writing a
# hard-coded section is therefore not just wrong for this switch, it is a
# family-wide assumption that had never been checked.

def test_the_section_holding_our_address_wins():
    c = client(network={"lan": "10.0.0.1", "lan_mgmt": "192.0.2.2"})
    # Matched by address, so the presence of a `lan` section is not enough to
    # claim it — that is exactly the trap that produced the bench failure.
    assert c.mgmt_section() == "lan_mgmt"


def test_lan_is_still_used_where_it_is_the_convention():
    # Every RutOS router: the move must behave identically to before.
    c = client(network={"lan": "192.0.2.2"})
    assert c.mgmt_section() == "lan"


def test_lan_is_the_fallback_when_no_address_matches():
    c = client(network={"lan": "10.0.0.1", "wan": "10.0.1.1"})
    assert c.mgmt_section() == "lan"


def test_a_single_addressed_section_is_used_even_when_unmatched():
    c = client(network={"mgmt": "10.0.0.1"})
    assert c.mgmt_section() == "mgmt"


def test_an_ambiguous_config_refuses_rather_than_guessing():
    # Moving the wrong interface strands the device, so "I cannot tell" has to
    # stop the step rather than pick one.
    c = client(network={"eth0": "10.0.0.1", "eth1": "10.0.1.1"})
    assert c.mgmt_section() == ""


def test_an_anonymous_section_is_addressed_as_uci_addresses_it():
    c = client(network={"@interface[0]": "192.0.2.2"})
    assert c.mgmt_section() == "@interface[0]"


def test_network_addresses_parses_quoted_and_bare_values():
    c = client()
    c.ssh_exec = lambda *a, **k: (
        "network.lan=interface\n"
        "network.lan.ipaddr='192.168.1.2'\n"
        "network.lan.netmask='255.255.255.0'\n"
        "network.wan.ipaddr=10.0.0.5\n"
        "network.lan.proto='static'\n")
    assert c.network_addresses() == {"lan": "192.168.1.2", "wan": "10.0.0.5"}


# ── verification ─────────────────────────────────────────────────────────────

def verify(c, **overrides):
    kwargs = {"new_password": DEFAULT_NEW_PASSWORD, "zonename": ZONE,
              "ntp_server": NTP, "minimum_firmware": FLOOR}
    kwargs.update(overrides)
    return c.verify_configuration(**kwargs)


def row(checks, item):
    return next(c for c in checks if c["item"] == item)


def configured_uci():
    # system.system.zoneName is the one the WebUI dropdown renders; a switch
    # missing it shows UTC on a correct clock.
    return {"system.system.zoneName": ZONE, "system.ntp.zoneName": ZONE,
            "system.system.timezone": POSIX_ZONE,
            "system.ntp.server": NTP, "system.ntp.enabled": "1"}


def test_a_fully_configured_switch_passes_every_check():
    c = client(uci=configured_uci())
    checks = verify(c)
    assert [check["ok"] for check in checks] == [True, True, True, True]


def test_a_leftover_pool_entry_fails_the_ntp_check():
    # The switch would drift to internet time on a site that has none, so an
    # extra server is a real finding rather than a cosmetic one.
    uci = {**configured_uci(), "system.ntp.server": f"{NTP} 1.pool.ntp.org"}
    check = row(verify(client(uci=uci)), "NTP server")
    assert check["ok"] is False
    assert "1.pool.ntp.org" in check["actual"]


def test_a_disabled_ntp_client_fails_the_check():
    uci = {**configured_uci(), "system.ntp.enabled": "0"}
    assert row(verify(client(uci=uci)), "NTP server")["ok"] is False


def test_the_firmware_check_uses_floor_semantics():
    # A switch newer than the floor must PASS verification, or every
    # arriving-newer unit would report as failed after being correctly left alone.
    c = client(version=NEWER, uci=configured_uci())
    check = row(verify(c), "firmware")
    assert check["ok"] is True
    assert check["expected"] == f"{FLOOR} or newer"

    c = client(version=OLDER, uci=configured_uci())
    assert row(verify(c), "firmware")["ok"] is False


def test_the_firmware_check_is_out_of_scope_without_a_floor():
    c = client(uci=configured_uci())
    assert row(verify(c, minimum_firmware=""), "firmware")["ok"] is None


# ── the checks must observe EFFECT, not read back our own writes ─────────────
#
# The first real TSW202 passed both of these while sitting on UTC with four
# Google servers: the old checks read the same UCI paths set_timezone and
# set_ntp_server had just written, and `uci set` creates an option whether or
# not the device consumes it. Both regressions are pinned here.

def test_a_clock_still_on_utc_fails_the_timezone_check_despite_the_uci_option():
    # zoneName reads back perfectly. The clock never moved. That is a FAIL.
    c = client(uci=configured_uci(), offset="+0000")
    check = row(verify(c), "timezone")
    assert check["ok"] is False
    assert "+0000" in check["actual"]


def test_a_clock_on_the_zone_passes_the_timezone_check():
    assert row(verify(client(uci=configured_uci())), "timezone")["ok"] is True


def test_an_unreadable_clock_fails_rather_than_passes():
    assert row(verify(client(uci=configured_uci(), offset="")),
               "timezone")["ok"] is False


def test_ntp_servers_the_device_keeps_elsewhere_still_fail_the_check():
    # The shape the TSW202 WebUI shows: one section per server, under
    # `hostname`, with our own untouched `system.ntp.server` write sitting
    # alongside it. Reading our write back said PASS; the switch was still
    # asking Google.
    show = (f"system.ntp.server='{NTP}'\n"
            "system.ntp.enabled='1'\n"
            "system.@ntpserver[0].hostname='time1.google.com'\n"
            "system.@ntpserver[1].hostname='time2.google.com'\n")
    check = row(verify(client(uci=configured_uci(), system_show=show)),
                "NTP server")
    assert check["ok"] is False
    assert "time1.google.com" in check["actual"]


def test_a_config_the_ntp_daemon_never_picked_up_fails_the_check():
    # Committed, enabled, correct — and the live daemon still polling the pool
    # it was started with. The same "written but not applied" failure the
    # timezone had.
    c = client(uci=configured_uci(), polling=["1.pool.ntp.org"])
    check = row(verify(c), "NTP server")
    assert check["ok"] is False
    assert "1.pool.ntp.org" in check["actual"]


def test_no_ntp_daemon_at_all_fails_the_check():
    check = row(verify(client(uci=configured_uci(), polling=[])), "NTP server")
    assert check["ok"] is False
    assert "polling nothing" in check["actual"]


def test_the_first_real_units_ntp_state_passes():
    # Exactly what `uci show system` and `ps` reported off the bench unit: this
    # switch's NTP was CORRECT, and the WebUI screenshot that looked like four
    # Google servers was not the device's state.
    show = ("system.system.hostname='TSW202'\n"
            "system.system.devicename='TSW202'\n"
            f"system.system.timezone='{POSIX_ZONE}'\n"
            "system.ntp=timeserver\n"
            "system.ntp.enabled='1'\n"
            "system.ntp.enable_server='0'\n"
            f"system.ntp.zoneName='{ZONE}'\n"
            f"system.ntp.server='{NTP}'\n")
    c = client(uci=configured_uci(), system_show=show)
    assert row(verify(c), "NTP server")["ok"] is True


def test_the_device_hostname_is_not_mistaken_for_a_time_server():
    show = (f"system.ntp.server='{NTP}'\n"
            "system.ntp.enabled='1'\n"
            "system.system.hostname='TSW202'\n")
    check = row(verify(client(uci=configured_uci(), system_show=show)),
                "NTP server")
    assert check["ok"] is True
    assert "TSW202" not in check["actual"]


def test_the_device_hostname_is_not_mistaken_for_a_time_server_when_anonymous():
    # The same option, on the shape a build may render the system section in.
    # Matching only the named form counted the switch's own name as a server and
    # failed a row that should pass.
    show = (f"system.ntp.server='{NTP}'\n"
            "system.ntp.enabled='1'\n"
            "system.@system[0].hostname='TSW202'\n")
    check = row(verify(client(uci=configured_uci(), system_show=show)),
                "NTP server")
    assert check["ok"] is True
    assert "TSW202" not in check["actual"]


def test_the_stock_pool_alongside_a_correct_daemon_fails_the_row():
    # The switch that prompted the write-path fix. The daemon half is GREEN —
    # ours is the only server being polled — which is what made this look like a
    # bench-reachability problem when it was a config the write never cleared.
    show = (f"system.ntp.server='{NTP}'\n"
            "system.ntp.enabled='1'\n"
            + "".join(f"system.@ntpserver[{i}].hostname='time{i + 1}.google.com'\n"
                      for i in range(4)))
    check = row(verify(client(uci=configured_uci(), system_show=show)),
                "NTP server")
    assert check["ok"] is False
    assert f"ntpd polling {NTP}" in check["actual"]
    assert "time4.google.com" in check["actual"]


# ── no check may carry a password (TEC-349) ──────────────────────────────────
#
# These rows are written to the run record verbatim and shipped to
# bench-central. On the failing path the password still in use is the device's
# own label password, which the bench is handed off the sticker.

LABEL_PW = "qN4$8xTr"


def test_the_password_change_is_reported_as_an_outcome():
    check = row(verify(client(uci=configured_uci())), "admin/root password")
    assert check["ok"] is True
    assert check["actual"] == "in use"


def test_a_failed_password_change_is_reported_as_failed():
    c = client(uci=configured_uci())
    c.password = LABEL_PW           # the change did not take
    check = row(verify(c), "admin/root password")
    assert check["ok"] is False
    assert "NOT set" in check["actual"]


@pytest.mark.parametrize("current,secret", [
    (DEFAULT_NEW_PASSWORD, DEFAULT_NEW_PASSWORD),
    (LABEL_PW, DEFAULT_NEW_PASSWORD),
    (LABEL_PW, LABEL_PW),
])
def test_no_check_contains_a_password(current, secret):
    c = client(uci=configured_uci())
    c.password = current
    for check in verify(c):
        assert secret not in str(check), check


# ── the pipeline, end to end ─────────────────────────────────────────────────

def pipeline_client(monkeypatch, *, version=FLOOR, model="TSW202", uci=None):
    c = client(version=version, model=model, uci=uci or configured_uci())
    monkeypatch.setattr(c, "login", lambda pw: setattr(c, "password", pw))
    monkeypatch.setattr(c, "set_admin_password",
                        lambda pw: setattr(c, "password", pw))
    moves = []
    monkeypatch.setattr(c, "move_lan", lambda ip, **kw: moves.append((ip, kw)) or
                        {"item": "LAN IP", "expected": ip,
                         "actual": f"answering on {ip}", "ok": True})
    c.moves = moves
    return c


def settings(**overrides):
    base = {"new_password": DEFAULT_NEW_PASSWORD, "ntp_server": NTP,
            "timezone": ZONE, "lan_ip": "192.168.88.2",
            "netmask": "255.255.255.0", "gateway": "192.168.88.1",
            "firmware": {"minimum_version": FLOOR}}
    base.update(overrides)
    return base


def test_a_clean_run_passes(monkeypatch):
    c = pipeline_client(monkeypatch)
    result = configure_tsw(c, initial_password="", settings=settings())
    assert result["ok"] is True
    assert result["failures"] == []
    assert result["ip"] == "192.168.88.2"


def test_the_management_move_runs_last_and_states_the_subnet(monkeypatch):
    c = pipeline_client(monkeypatch)
    configure_tsw(c, initial_password="", settings=settings())
    ip, kwargs = c.moves[0]
    assert ip == "192.168.88.2"
    assert kwargs["netmask"] == "255.255.255.0"
    assert kwargs["gateway"] == "192.168.88.1"
    # A switch serves no DHCP; renewing would throw away the station's static
    # bench address instead of following the device.
    assert kwargs["renew_dhcp"] is False


def test_the_move_is_skipped_when_no_lan_ip_is_configured(monkeypatch):
    c = pipeline_client(monkeypatch)
    result = configure_tsw(c, initial_password="", settings=settings(lan_ip=""))
    assert c.moves == []
    assert result["ip"] == ""


def test_the_wrong_model_is_refused_before_anything_is_written(monkeypatch):
    # A TSW212 would take a TSW202 image. The guard runs after login but
    # before the password change, so a mis-tabbed device is left untouched.
    c = pipeline_client(monkeypatch, model="TSW212")
    with pytest.raises(SystemExit, match="Wrong device"):
        configure_tsw(c, initial_password="", settings=settings())
    assert not c.switch.wrote("uci commit")
    assert c.moves == []


def test_an_unmappable_timezone_fails_before_touching_the_device(monkeypatch):
    # set_timezone would raise three steps in, leaving the switch on the shared
    # password and nothing else — so the config is checked up front.
    c = pipeline_client(monkeypatch)
    with pytest.raises(SystemExit, match="No POSIX TZ mapping"):
        configure_tsw(c, initial_password="", settings=settings(timezone="Mars/Olympus"))
    assert c.switch.commands == []


def test_a_rejected_label_password_retries_with_the_shared_one(monkeypatch):
    # A switch left half-provisioned by an earlier failed run is already on the
    # shared password; the label one no longer works.
    c = pipeline_client(monkeypatch)
    tried = []

    def login(pw):
        tried.append(pw)
        if pw != DEFAULT_NEW_PASSWORD:
            raise SystemExit("Login failed (HTTP 403)")
        c.password = pw

    monkeypatch.setattr(c, "login", login)
    result = configure_tsw(c, initial_password="label-pw", settings=settings())
    assert tried == ["label-pw", DEFAULT_NEW_PASSWORD]
    assert result["ok"] is True


def test_an_empty_password_goes_straight_to_the_shared_one(monkeypatch):
    c = pipeline_client(monkeypatch)
    tried = []
    monkeypatch.setattr(c, "login", lambda pw: tried.append(pw) or
                        setattr(c, "password", pw))
    configure_tsw(c, initial_password="", settings=settings())
    assert tried == [DEFAULT_NEW_PASSWORD]


def test_a_verification_failure_fails_the_run(monkeypatch):
    uci = {**configured_uci(), "system.ntp.server": "1.pool.ntp.org"}
    c = pipeline_client(monkeypatch, uci=uci)
    result = configure_tsw(c, initial_password="", settings=settings())
    assert result["ok"] is False
    assert any(check["item"] == "NTP server" and check["ok"] is False
               for check in result["verification"])


def test_the_pipeline_writes_ntp_and_timezone(monkeypatch):
    c = pipeline_client(monkeypatch)
    configure_tsw(c, initial_password="", settings=settings())
    assert c.switch.wrote(f"uci add_list system.ntp.server={NTP}")
    assert c.switch.wrote(f"system.ntp.zoneName='{ZONE}'")
    assert c.switch.wrote(f"system.system.timezone='{POSIX_ZONE}'")


def test_there_is_no_rms_tailscale_or_sim_step(monkeypatch):
    # The baseline in TEC-791 is password, firmware, time, address. Nothing
    # should be reaching for a router's surface on a switch — not in the
    # commands sent, and not as "skipped" rows the record would carry.
    c = pipeline_client(monkeypatch)
    result = configure_tsw(c, initial_password="", settings=settings())
    joined = " ".join(c.switch.commands).lower()
    for absent in ("rms", "tailscale", "simcard", "gsmctl -q", "sim_switch"):
        assert absent not in joined, absent
    assert [check["item"] for check in result["verification"]] == [
        "admin/root password", "timezone", "NTP server", "firmware", "LAN IP"]


def move_client(monkeypatch, network):
    """A client whose real move_lan runs, with everything after the commit
    (the interface restart, the host-side renew, the settle sleep, the
    reachability wait) stubbed out — the commands sent are what these assert on.

    The patches land in `bench_core`, where move_lan resolves its globals;
    patching them in this module would leave the real ones running."""
    c = client(network=network)
    monkeypatch.setattr(c, "_fire_and_forget", lambda *a, **k: None)
    monkeypatch.setattr(c, "_port_open", lambda *a, **k: True)
    monkeypatch.setattr(c, "close", lambda: None)
    monkeypatch.setattr(bench_core, "host_iface_for", lambda *a, **k: "en9")
    monkeypatch.setattr(bench_core, "renew_host_dhcp", lambda *a, **k: None)
    monkeypatch.setattr(bench_core.time, "sleep", lambda *a: None)
    return c


def test_the_move_writes_the_discovered_section(monkeypatch):
    # The regression test for the bench failure: the emitted UCI must name the
    # section the switch actually has, not `lan`.
    c = move_client(monkeypatch, {"lan_mgmt": "192.0.2.2"})
    check = c.move_lan("192.168.88.2", netmask="255.255.255.0",
                       gateway="192.168.88.1", renew_dhcp=False)
    assert check["ok"] is True
    assert c.switch.wrote("uci set network.lan_mgmt.ipaddr=192.168.88.2")
    assert c.switch.wrote("uci set network.lan_mgmt.netmask=255.255.255.0")
    assert c.switch.wrote("uci set network.lan_mgmt.gateway=192.168.88.1")
    assert c.switch.wrote("uci commit network")
    assert not c.switch.wrote("network.lan.")


def test_an_anonymous_section_is_quoted_against_the_shell(monkeypatch):
    # `[0]` is a shell glob; an unquoted path would be mangled before uci ever
    # sees it. This is what _uci_arg is for.
    c = move_client(monkeypatch, {"@interface[0]": "192.0.2.2"})
    c.move_lan("192.168.88.2", renew_dhcp=False)
    assert c.switch.wrote("uci set 'network.@interface[0].ipaddr=192.168.88.2'")


def test_the_move_refuses_an_ambiguous_config(monkeypatch):
    c = move_client(monkeypatch, {"eth0": "10.0.0.1", "eth1": "10.0.1.1"})
    with pytest.raises(SystemExit, match="Cannot tell which network section"):
        c.move_lan("192.168.88.2", renew_dhcp=False)
    assert not c.switch.wrote("uci commit network")


def test_the_move_is_skipped_when_already_on_the_target(monkeypatch):
    c = move_client(monkeypatch, {"lan_mgmt": "192.168.88.2"})
    c.host = "192.168.88.2"
    check = c.move_lan("192.168.88.2", renew_dhcp=False)
    assert check["ok"] is True and "already set" in check["actual"]
    assert not c.switch.wrote("uci commit network")


def test_a_router_still_gets_only_ipaddr(monkeypatch):
    # The RUTM08 call site passes no netmask/gateway and must keep behaving
    # exactly as it did before the section discovery went in.
    c = move_client(monkeypatch, {"lan": "192.0.2.2"})
    c.move_lan("192.168.88.1")
    assert c.switch.wrote("uci set network.lan.ipaddr=192.168.88.1 "
                          "&& uci commit network")


# ── the verify-only pass (TEC-348) ───────────────────────────────────────────

def verify_client(monkeypatch, *, host="192.168.88.2", version=FLOOR,
                  model="TSW202", uci=None, network=None):
    """A finished switch, reachable on its final management address."""
    c = client(version=version, model=model, uci=uci or configured_uci(),
               network=network if network is not None else {"lan_mgmt": host})
    c.host = host
    monkeypatch.setattr(c, "login", lambda pw: setattr(c, "password", pw))
    return c


def prior_run(row=None):
    """`BenchConfigurator.verify_resolver`'s contract: (expected, prior_row)."""
    return lambda identity: ({}, row)


def test_a_finished_switch_passes_and_says_where_it_was_reached(monkeypatch):
    c = verify_client(monkeypatch)
    result = mod.verify_tsw(c, settings=settings(), resolve=prior_run())
    assert result["ok"] is True
    assert result["ip"] == "192.168.88.2"
    assert [check["item"] for check in result["verification"]] == [
        "admin/root password", "timezone", "NTP server", "firmware", "LAN IP"]


def test_the_verify_pass_changes_nothing(monkeypatch):
    # The whole point. Asserted on what was SENT, not on what was blocked —
    # a pipeline that tried and got refused would fail the run instead, which
    # is a different (and much louder) outcome than this test allows.
    c = verify_client(monkeypatch)
    mod.verify_tsw(c, settings=settings(), resolve=prior_run())
    offenders = [cmd for cmd in c.switch.commands
                 if bench_core._MUTATING_COMMANDS.search(cmd)]
    assert offenders == []


def test_the_client_is_read_only_for_the_whole_pass(monkeypatch):
    # The seatbelt is actually engaged, not just intended: if a later edit adds
    # a mutating step it fails loudly rather than quietly provisioning.
    c = verify_client(monkeypatch)
    mod.verify_tsw(c, settings=settings(), resolve=prior_run())
    assert c.read_only is True


def test_the_lan_ip_row_is_not_a_move(monkeypatch):
    # move_lan would re-address the switch. If verify_tsw ever reaches for it,
    # this blows up rather than silently restarting a shipped unit's network.
    c = verify_client(monkeypatch)
    monkeypatch.setattr(c, "move_lan",
                        lambda *a, **k: pytest.fail("verify moved the LAN"))
    row_ = row(mod.verify_tsw(c, settings=settings(),
                              resolve=prior_run())["verification"], "LAN IP")
    assert row_["ok"] is True
    assert "answering on 192.168.88.2" in row_["actual"]


def test_a_switch_still_on_the_factory_address_fails(monkeypatch):
    # The ambiguity TEC-348 opens with, resolved: on a configure run "no answer
    # on the final address" might just be a stale station lease. Here the switch
    # is in front of us on 192.168.1.2, which is conclusive.
    c = verify_client(monkeypatch, host="192.168.1.2")
    result = mod.verify_tsw(c, settings=settings(), resolve=prior_run())
    assert result["ok"] is False
    assert row(result["verification"], "LAN IP")["ok"] is False


def test_a_switch_never_configured_by_us_fails(monkeypatch):
    # No configure record anywhere. The rows themselves can all pass (a unit set
    # up by hand to the same baseline would), so this row is the only thing
    # standing between an unprovisioned box and a QA label.
    missing = {"item": "prior run", "expected": "a recorded configure run",
               "actual": "none found", "ok": False}
    c = verify_client(monkeypatch)
    result = mod.verify_tsw(c, settings=settings(), resolve=prior_run(missing))
    assert result["ok"] is False
    assert result["verification"][0]["item"] == "prior run"


def test_a_wrong_clock_fails_the_verify_pass(monkeypatch):
    # The TSW202 bring-up bug, caught by the mode that exists to catch it.
    c = verify_client(monkeypatch)
    c.switch.offset = "+0000"
    result = mod.verify_tsw(c, settings=settings(), resolve=prior_run())
    assert result["ok"] is False
    assert row(result["verification"], "timezone")["ok"] is False


def test_a_switch_on_another_password_fails_rather_than_erroring(monkeypatch):
    # login() is stubbed to accept anything here; on a real device it would
    # raise and the run would be recorded as an error. What this pins is the
    # in-between state: authenticated, but not on the shared password.
    c = verify_client(monkeypatch)
    monkeypatch.setattr(c, "login", lambda pw: setattr(c, "password", LABEL_PW))
    result = mod.verify_tsw(c, settings=settings(), resolve=prior_run())
    assert result["ok"] is False
    assert row(result["verification"], "admin/root password")["ok"] is False


def test_a_verify_pass_carries_no_password(monkeypatch):
    c = verify_client(monkeypatch)
    result = mod.verify_tsw(c, settings=settings(), resolve=prior_run())
    for check in result["verification"]:
        assert DEFAULT_NEW_PASSWORD not in str(check), check


def test_the_wrong_model_is_refused_before_anything_is_reported(monkeypatch):
    c = verify_client(monkeypatch, model="TSW212")
    with pytest.raises(SystemExit):
        mod.verify_tsw(c, settings=settings(), resolve=prior_run())


def test_a_cli_verify_needs_no_resolver(monkeypatch):
    # `--verify` from the command line has no station log dir to look in. It
    # runs the config-derived rows and simply carries no prior-run row.
    c = verify_client(monkeypatch)
    result = mod.verify_tsw(c, settings=settings())
    assert result["ok"] is True
    assert not any(check["item"] == "prior run"
                   for check in result["verification"])


def test_the_defaults_match_the_committed_example():
    # The module defaults are what the CLI and a config-less run fall back to,
    # so they must not drift from the example the installer seeds.
    import json
    from pathlib import Path
    example = json.loads(
        (Path(mod.BASE_DIR) / "config" / "tsw.config.example.json")
        .read_text(encoding="utf-8"))
    assert example["host"] == mod.DEFAULT_TSW_HOST
    assert example["lan_ip"] == mod.DEFAULT_TSW_LAN_IP
    assert example["netmask"] == mod.DEFAULT_TSW_NETMASK
    assert example["gateway"] == mod.DEFAULT_TSW_GATEWAY
    assert example["ntp_server"] == mod.DEFAULT_TSW_NTP_SERVER
    assert example["firmware"]["minimum_version"] == mod.DEFAULT_TSW_MIN_FIRMWARE
