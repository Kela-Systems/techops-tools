"""The NTP client has to be the ONLY server, in BOTH of RutOS's time subsystems.

A RutOS router carries two independent ones and can run with them disagreeing:
`system.ntp` (the OpenWrt timeserver section) and the `ntpclient` package (the
client an OTD500/RUTM08 actually polls with). The demo pair in TEC-846 was found
running `ntpclient.zoneName='Asia/Jerusalem'` while `system.ntp` sat disabled on
UTC — one device, two answers, and log timestamps that cannot be correlated.

Two faults these are written against, both of which pass a naive read-back:

* **Stock servers left in place.** RutOS ships `time2/3/4.google.com` in
  `ntpclient`, and `configured_ntp_servers()` could not see them at all — it
  scanned `uci show system` only, which is the same blind spot that shipped a
  TSW202 with four untouched Google servers and a green row.
* **`interval 86400`.** One probe per day is not sync; it is a daily chance to
  notice the clock is wrong.

Everything here is a read-back, deliberately. The server sits on the assembly
network, this bench does one device at a time, and an OTD500 is never cabled
behind the router that would carry it there — so there is no reachable server to
sync against. `docs/verification-rows.md` records why that gap is not closed with
a row.
"""
import pytest

from bench_core import (
    NTP_CLIENT_INTERVAL,
    NTP_CLIENT_INTERVAL_MIN,
    NTP_CLIENT_PACKAGE,
    MutationBlocked,
    TeltonikaClient,
)

ZONE = "Asia/Jerusalem"
OURS = "192.168.88.10"
# What a stock RutOS ntpclient ships with, servers 2-4.
STOCK = ["time2.google.com", "time3.google.com", "time4.google.com"]


class FakeDevice:
    """Stands in for `ssh_exec`. The two time subsystems are modelled
    separately, because a device holding one right and one wrong is the state
    this is all written against."""

    def __init__(self, *, servers=(OURS,), enabled="1",
                 interval=str(NTP_CLIENT_INTERVAL),
                 zone_shown=ZONE, package=True, system_servers=None,
                 system_enabled="1", shape="anonymous"):
        self.commands: list[str] = []
        self.servers = list(servers)
        self.enabled = enabled
        self.interval = interval
        self.zone_shown = zone_shown
        self.package = package
        self.shape = shape
        # `system.ntp`, independent of the ntpclient package above.
        self.system_servers = (list(self.servers[:1]) if system_servers is None
                               else list(system_servers))
        self.system_enabled = system_enabled

    def _ntpclient_show(self) -> str:
        if not self.package:
            return ""
        if self.shape == "rutos":
            return self._rutos_show()
        lines = [f"{NTP_CLIENT_PACKAGE}.@ntpclient[0]=ntpclient",
                 f"{NTP_CLIENT_PACKAGE}.@ntpclient[0].enabled='{self.enabled}'",
                 f"{NTP_CLIENT_PACKAGE}.@ntpclient[0].interval='{self.interval}'",
                 f"{NTP_CLIENT_PACKAGE}.@ntpclient[0].zoneName='{self.zone_shown}'"]
        # Named sections 1..N, the way RutOS numbers them on an OTD500.
        for i, server in enumerate(self.servers, start=1):
            lines += [f"{NTP_CLIENT_PACKAGE}.{i}=server",
                      f"{NTP_CLIENT_PACKAGE}.{i}.hostname='{server}'",
                      f"{NTP_CLIENT_PACKAGE}.{i}.port='123'"]
        return "\n".join(lines)

    def _rutos_show(self) -> str:
        """Transcribed from an OTD500 on OTD5_R_00.07.22.3 (bench, 2026-08-31).

        Three things the shape above does not have: the servers are typed
        `ntpserver` rather than `server`, the settings section is NAMED after
        the package, and there is a third section — `ntpdrift` — which owns
        only `freq` and is listed BEFORE the settings one.
        """
        lines = []
        for i, server in enumerate(self.servers, start=1):
            lines += [f"{NTP_CLIENT_PACKAGE}.{i}=ntpserver",
                      f"{NTP_CLIENT_PACKAGE}.{i}.hostname='{server}'",
                      f"{NTP_CLIENT_PACKAGE}.{i}.port='123'"]
        lines += [f"{NTP_CLIENT_PACKAGE}.ntpdrift=ntpdrift",
                  f"{NTP_CLIENT_PACKAGE}.ntpdrift.freq='0'",
                  f"{NTP_CLIENT_PACKAGE}.{NTP_CLIENT_PACKAGE}=ntpclient",
                  f"{NTP_CLIENT_PACKAGE}.{NTP_CLIENT_PACKAGE}.enabled='{self.enabled}'",
                  f"{NTP_CLIENT_PACKAGE}.{NTP_CLIENT_PACKAGE}.sync_enabled='1'",
                  f"{NTP_CLIENT_PACKAGE}.{NTP_CLIENT_PACKAGE}.zoneName='{self.zone_shown}'",
                  f"{NTP_CLIENT_PACKAGE}.{NTP_CLIENT_PACKAGE}.interval='{self.interval}'"]
        return "\n".join(lines)

    def __call__(self, command, check=True, exec_timeout=None):
        self.commands.append(command)
        if f"uci show {NTP_CLIENT_PACKAGE}" in command:
            return self._ntpclient_show()
        if "uci show system" in command:
            servers = " ".join(f"'{s}'" for s in self.system_servers)
            return ("system.system=system\n"
                    "system.system.hostname='rut-haifa'\n"
                    "system.ntp=timeserver\n"
                    + (f"system.ntp.server={servers}\n" if servers else "")
                    + f"system.ntp.enabled='{self.system_enabled}'\n")
        if "system.ntp.enabled" in command:
            return self.system_enabled
        return ""

    def wrote(self, fragment: str) -> bool:
        return any(fragment in c for c in self.commands)


def client(**kwargs) -> TeltonikaClient:
    c = TeltonikaClient(host="192.0.2.1")
    c.ssh_exec = FakeDevice(**kwargs)
    c.device = c.ssh_exec          # test handle
    return c


# ── the failures the row exists to catch ─────────────────────────────────────

def test_the_stock_google_servers_left_behind_fail_the_row():
    # The whole point. Ours is first and correct, so `uci get` on the option we
    # wrote reads back exactly right — and the device still falls through to
    # three internet servers on a site that has no internet.
    c = client(servers=[OURS] + STOCK)
    check = c.ntp_client_check(OURS)
    assert check["ok"] is False
    assert "time2.google.com" in check["actual"]


def test_a_server_the_scan_could_not_previously_see_is_now_found():
    # Before TEC-857 this scanned `uci show system` only, so a leftover in the
    # ntpclient package was invisible and the row passed.
    c = client(servers=[OURS] + STOCK)
    assert c.configured_ntp_servers() == [OURS] + STOCK


def test_the_stock_daily_interval_fails_the_row():
    c = client(interval="86400")
    check = c.ntp_client_check(OURS, interval=3600)
    assert check["ok"] is False
    assert "86400" in check["actual"]


def test_a_disabled_client_fails_even_with_the_right_server():
    c = client(enabled="0")
    assert c.ntp_client_check(OURS)["ok"] is False


def test_the_other_subsystem_left_disabled_fails_the_row():
    # The TEC-846 field state: the client is right, `system.ntp` is off. The
    # two disagreeing is what makes one device's log timestamps disagree.
    c = client(system_enabled="0")
    check = c.ntp_client_check(OURS)
    assert check["ok"] is False
    assert "system.ntp enabled=0" in check["actual"]


def test_the_other_subsystem_pointed_somewhere_else_fails_the_row():
    c = client(servers=[OURS], system_servers=["pool.ntp.org"])
    assert c.ntp_client_check(OURS)["ok"] is False


def test_a_correctly_configured_device_passes():
    assert client().ntp_client_check(OURS)["ok"] is True


def test_a_faster_poll_than_asked_for_still_passes():
    # The config is a ceiling, not a pin: a station that tightened it further
    # has not misconfigured the device.
    assert client(interval="600").ntp_client_check(OURS, interval=3600)["ok"] is True


def test_a_device_without_the_package_is_amber_not_a_pass():
    # A TSW202 keeps its client in system.ntp and has no ntpclient package.
    # "Not applicable" must not be rounded up to "fine" (rows doc, rule 4).
    check = client(package=False).ntp_client_check(OURS)
    assert check["ok"] is None
    assert NTP_CLIENT_PACKAGE in check["actual"]


def test_a_switch_without_the_package_still_reports_its_own_servers():
    # The widened scan must not regress the TSW202 row, whose servers live in
    # system.ntp and nowhere else.
    c = client(package=False, system_servers=[OURS])
    assert c.configured_ntp_servers() == [OURS]


# ── what the configure step writes ───────────────────────────────────────────

def test_the_stock_servers_are_deleted_not_left_below_ours():
    c = client(servers=[OURS] + STOCK)
    c.set_ntp_client(OURS)
    for section in (4, 3, 2):
        assert c.device.wrote(f"uci delete {NTP_CLIENT_PACKAGE}.{section}")
    assert not c.device.wrote(f"uci delete {NTP_CLIENT_PACKAGE}.1")


def test_the_section_is_discovered_rather_than_assumed():
    # TEC-857 quotes `ntpclient.1.hostname`, but that numbering is a build
    # detail. A device whose sections are anonymous must still be configured.
    c = client(servers=[OURS])
    c.device.servers = [OURS]
    c.set_ntp_client("192.168.1.2")
    assert c.device.wrote(f"{NTP_CLIENT_PACKAGE}.1.hostname=192.168.1.2")


def test_both_subsystems_are_written():
    c = client()
    c.set_ntp_client(OURS)
    assert c.device.wrote(f"uci commit {NTP_CLIENT_PACKAGE}")
    assert c.device.wrote("uci add_list system.ntp.server=192.168.88.10")
    assert c.device.wrote("uci set system.ntp.enabled=1")
    assert c.device.wrote("uci commit system")


def test_the_poll_interval_is_written():
    c = client(interval="86400")
    c.set_ntp_client(OURS, interval=3600)
    assert c.device.wrote("interval=3600")


# ── the poll interval's floor ────────────────────────────────────────────────
#
# On this fleet the interval is a RETRY LATENCY, not an accuracy knob: a unit
# cannot reach its time server from the bench, so its early polls fail and the
# interval is how long it then sits on the firmware image's build date. Hence
# the default sits on the floor the binary accepts. The floor itself is the
# trap — the binary validates 60..2147483647 and SILENTLY substitutes 600 for
# anything outside it, so a request for 30 buys a poll ten times slower than
# the default it was trying to beat, with nothing on the device saying so.

def test_the_default_interval_is_the_floor_the_binary_accepts():
    assert NTP_CLIENT_INTERVAL == NTP_CLIENT_INTERVAL_MIN == 60


def test_a_below_floor_request_is_clamped_not_passed_through():
    c = client()
    c.set_ntp_client(OURS, interval=30)
    assert c.device.wrote(f"interval={NTP_CLIENT_INTERVAL_MIN}")
    assert not c.device.wrote("interval=30"), \
        "30 would come back as 600 — slower than not asking at all"


def test_the_clamp_is_reported_rather_than_applied_quietly(caplog):
    # A station that asked for 30 and got 60 must be able to find out why from
    # the run log; a silent clamp is the same class of fault as the silent
    # substitution it exists to prevent.
    c = client()
    with caplog.at_level("WARNING"):
        c.set_ntp_client(OURS, interval=30)
    assert "30" in caplog.text and "600" in caplog.text


def test_the_floor_itself_is_written_unchanged():
    c = client()
    c.set_ntp_client(OURS, interval=NTP_CLIENT_INTERVAL_MIN)
    assert c.device.wrote(f"interval={NTP_CLIENT_INTERVAL_MIN}")


def test_an_interval_above_the_floor_is_left_alone():
    # The clamp is a floor, not a pin. A site with a reachable server may well
    # want to poll less often, and that is not a misconfiguration.
    c = client()
    c.set_ntp_client(OURS, interval=3600)
    assert c.device.wrote("interval=3600")


def test_the_daemon_is_restarted_so_the_config_is_more_than_committed():
    # The fault this suite's sibling was written for: a committed option the
    # running daemon never picked up.
    c = client()
    c.set_ntp_client(OURS)
    assert c.device.wrote(f"/etc/init.d/{NTP_CLIENT_PACKAGE} restart")


def test_a_device_without_the_package_fails_the_step_loudly():
    # Silently configuring nothing would leave the run green and the device on
    # modem time — the exact silent-failure mode TEC-846 documents.
    c = client(package=False)
    with pytest.raises(SystemExit, match=NTP_CLIENT_PACKAGE):
        c.set_ntp_client(OURS)


# ── the section shape a real OTD500 has ──────────────────────────────────────
# Found on the bench, 2026-08-31, and not what the fake above had assumed. The
# `ntpdrift` section sorts before the settings one, so "the first section that
# is not a server" — the rule both the read and the write used — picked it.

def test_the_settings_are_read_off_the_section_that_owns_them():
    # The serious half. `ntpdrift` owns only `freq`, so reading the interval
    # there reports a correctly configured device as blank, and the row passes
    # only on a device a previous run had already written those options onto.
    c = client(shape="rutos", interval="3600", enabled="1")
    assert c.ntp_client_settings()["interval"] == "3600"
    assert c.ntp_client_settings()["enabled"] == "1"
    assert c.ntp_client_settings()["zonename"] == ZONE


def test_a_stock_device_is_read_as_daily_rather_than_as_blank():
    c = client(shape="rutos", interval="86400")
    check = c.ntp_client_check(OURS, interval=3600)
    assert check["ok"] is False
    assert "86400" in check["actual"]


def test_the_settings_are_written_only_to_the_section_that_owns_them():
    c = client(shape="rutos", interval="86400")
    c.set_ntp_client(OURS, interval=3600, zonename=ZONE)
    assert c.device.wrote(f"{NTP_CLIENT_PACKAGE}.{NTP_CLIENT_PACKAGE}.interval=3600")
    for option in ("enabled", "interval", "zoneName"):
        assert not c.device.wrote(f"{NTP_CLIENT_PACKAGE}.ntpdrift.{option}")


def test_the_server_section_is_found_on_the_type_rutos_actually_uses():
    # RutOS types these `ntpserver`; the lookup asked for `server` and matched
    # nothing, leaving the hostname fallback to carry it.
    c = client(shape="rutos", servers=[OURS] + STOCK)
    c.set_ntp_client("192.168.1.2")
    assert c.device.wrote(f"{NTP_CLIENT_PACKAGE}.1.hostname=192.168.1.2")
    for section in (4, 3, 2):
        assert c.device.wrote(f"uci delete {NTP_CLIENT_PACKAGE}.{section}")


def test_the_timezone_skips_the_drift_section_too():
    c = client(shape="rutos")
    c.set_timezone(ZONE)
    assert c.device.wrote(f"{NTP_CLIENT_PACKAGE}.{NTP_CLIENT_PACKAGE}.zoneName={ZONE}")
    assert not c.device.wrote(f"{NTP_CLIENT_PACKAGE}.ntpdrift.zoneName")


def test_a_correctly_configured_real_device_passes():
    assert client(shape="rutos").ntp_client_check(OURS)["ok"] is True


# ── the running daemon, not just the file ────────────────────────────────────
#
# RutOS starts it as `ntpclient -s -l` and leaves the servers in the config, so
# there is nothing on the command line to read the way `running_ntp_servers()`
# reads `ntpd -p <server>`. What is left is whether the process predates the
# file — which is the same question, since it reads the file once at startup.

def daemon_client(*, pid="15296", started=2000, written=1000) -> TeltonikaClient:
    def answer(command, check=True, exec_timeout=None):
        return (f"pid={pid}\n"
                f"started={started if pid else ''}\n"
                f"written={written}\n")

    c = TeltonikaClient(host="192.0.2.1")
    c.ssh_exec = answer
    return c


def test_a_daemon_that_never_reread_the_config_fails():
    # The failure the row exists for: correct file, green config row, and a
    # daemon still polling what it read at boot because the restart that makes
    # a commit live is best-effort and quietly did not happen.
    check = daemon_client(started=1000, written=2000).ntp_daemon_check()
    assert check["ok"] is False
    assert "still polling what it read at startup" in check["actual"]
    assert "1000s BEFORE" in check["actual"]


def test_a_daemon_that_is_not_running_at_all_fails():
    check = daemon_client(pid="").ntp_daemon_check()
    assert check["ok"] is False
    assert "nothing is polling" in check["actual"]


def test_a_daemon_started_after_the_write_passes():
    check = daemon_client(started=2000, written=1000).ntp_daemon_check()
    assert check["ok"] is True
    assert "pid 15296" in check["actual"]


def test_a_restart_in_the_same_second_as_the_commit_passes():
    # set_ntp_client commits and then restarts, so equal timestamps are the
    # normal case on a fast device, not a stale daemon.
    assert daemon_client(started=1000, written=1000).ntp_daemon_check()["ok"] is True


def test_timestamps_the_device_will_not_give_are_amber_not_a_pass():
    # `date -r` is not on every busybox. "Could not tell" must not be rounded
    # up to "fine" (rows doc, rule 4).
    check = daemon_client(started=0, written=0).ntp_daemon_check()
    assert check["ok"] is None
    assert "would not report" in check["actual"]


def test_the_daemon_row_is_a_read_on_a_read_only_client():
    c = daemon_client()
    c.set_read_only()
    assert c.ntp_daemon_check()["ok"] is True


# ── the timezone's fourth consumer ───────────────────────────────────────────

def test_the_timezone_also_reaches_the_ntpclient_zone_name():
    c = client()
    c.set_timezone(ZONE)
    assert c.device.wrote(f"{NTP_CLIENT_PACKAGE}.@ntpclient[0].zoneName={ZONE}")


def test_a_device_without_the_package_gets_no_ntpclient_timezone_write():
    # The TSW202 has nothing to disagree with, and a write to a package that
    # is not there would fail a step that is otherwise fine.
    c = client(package=False)
    c.set_timezone(ZONE)
    assert not c.device.wrote(f"uci commit {NTP_CLIENT_PACKAGE}")


# ── verify-only safety ───────────────────────────────────────────────────────

def test_the_row_is_a_read_on_a_read_only_client():
    c = client()
    c.set_read_only()
    assert c.ntp_client_check(OURS)["ok"] is True


def test_the_step_is_refused_on_a_read_only_client():
    c = client()
    c.set_read_only()
    with pytest.raises(MutationBlocked):
        c.set_ntp_client(OURS)


def test_the_default_interval_is_not_the_stock_daily_one():
    assert NTP_CLIENT_INTERVAL < 86400
