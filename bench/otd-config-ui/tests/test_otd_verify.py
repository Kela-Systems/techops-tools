"""The OTD500 verify-only pass (otd_configure.verify_device, TEC-348).

The widest row set of the five tools, so this is mostly about the row set being
the SAME set the configure pipeline verifies. If the two drift apart a device
can pass a QA sweep and still not be what the pipeline would have produced —
which is worse than having no sweep, because it earns a label (TEC-352).

No hardware: `FakeOtd` stands in for `ssh_exec`. It answers reads and records
everything, so the mutation-freedom assertion is on what was SENT rather than
on what the read-only guard happened to block.

Unlike the router and the switch there is no LAN-move row: an OTD500 keeps its
factory address, which is also why detection finds a finished one at all.
"""
import pytest
import requests

import bench_core
from bench_core import (
    DEFAULT_NEW_PASSWORD as SHARED,
    NTP_CLIENT_INTERVAL,
    MutationBlocked,
    TeltonikaClient,
    expected_utc_offset,
)

import otd_configure as mod

ZONE = "Asia/Jerusalem"
NAME = "otd-haifa"
# `AT+COPS?` while registered: the last field is the access technology, 7 for
# LTE and 2 for UTRAN (bench_core.ACCESS_TECH).
ATTACHED_4G = '+COPS: 0,0,"Partner",7\n\nOK'
ATTACHED_3G = '+COPS: 0,0,"Partner",2\n\nOK'
NOT_ATTACHED = "+COPS: 0\n\nOK"
NTP = "192.168.88.10"
# The OTD500 aims at the site router's WAN address, not at the time server
# itself — it is upstream of that router and cannot address its LAN (TEC-857).
OTD_NTP = "192.168.1.2"
CORRECT_OFFSET = expected_utc_offset(ZONE) or "+0300"

needs_tzdata = pytest.mark.skipif(
    not expected_utc_offset(ZONE),
    reason="needs a tz database to say what the zone means today (tzdata)")


class FakeOtd:
    """Stands in for `ssh_exec`. The clock is independent of the UCI options on
    purpose — that independence is what makes the timezone row a real check."""

    def __init__(self, *, hostname=NAME, running_hostname=None,
                 offset=CORRECT_OFFSET, zone_shown=ZONE,
                 version="OTD5_R_00.07.20.3", sim_service="lte", sim_slots=2,
                 cops=ATTACHED_4G, ts_ip="100.64.0.5", rms_enable="1",
                 rms_status='{"connection_state":"connected"}',
                 esim_profile="profile: 8944...", quota_installed="script boot-hook",
                 quota_cron=None, quota_keep=None,
                 ntp_servers=(OTD_NTP,), ntp_enabled="1",
                 ntp_interval=str(NTP_CLIENT_INTERVAL),
                 ntp_package=True, dhcp_start="100", dhcp_limit="150",
                 ntp_pid="15296", ntp_started=2000, ntp_written=1000):
        self.commands: list[str] = []
        self.hostname = hostname
        # The RUNNING kernel hostname, independent of the UCI option for the
        # same reason the clock is independent of the zone option: reading back
        # only what was written cannot fail (TEC-348).
        self.running_hostname = hostname if running_hostname is None else running_hostname
        self.offset = offset
        self.zone_shown = zone_shown
        self.version = version
        self.sim_service = sim_service
        self.sim_slots = sim_slots
        # What the modem says it is ATTACHED on, independent of the SIM service
        # option for the same reason (TEC-348).
        self.cops = cops
        self.ts_ip = ts_ip
        self.rms_enable = rms_enable
        self.rms_status = rms_status
        self.esim_profile = esim_profile
        self.quota_installed = quota_installed
        self.quota_cron = quota_cron
        self.quota_keep = quota_keep
        # The `ntpclient` package: server sections named 1..4 the way RutOS
        # numbers them, plus the settings section that owns the poll interval.
        self.ntp_servers = list(ntp_servers)
        self.ntp_enabled = ntp_enabled
        self.ntp_interval = ntp_interval
        self.ntp_package = ntp_package
        self.dhcp_start = dhcp_start
        self.dhcp_limit = dhcp_limit
        # The daemon that reads the config above, once, at startup.
        self.ntp_pid = ntp_pid
        self.ntp_started = ntp_started
        self.ntp_written = ntp_written

    def _ntpclient_show(self) -> str:
        if not self.ntp_package:
            return ""
        lines = ["ntpclient.@ntpclient[0]=ntpclient",
                 f"ntpclient.@ntpclient[0].enabled='{self.ntp_enabled}'",
                 f"ntpclient.@ntpclient[0].interval='{self.ntp_interval}'",
                 f"ntpclient.@ntpclient[0].zoneName='{self.zone_shown}'"]
        for i, server in enumerate(self.ntp_servers, start=1):
            lines += [f"ntpclient.{i}=server",
                      f"ntpclient.{i}.hostname='{server}'",
                      f"ntpclient.{i}.port='123'"]
        return "\n".join(lines)

    def __call__(self, command, check=True, exec_timeout=None):
        self.commands.append(command)
        if command.startswith("date +%z"):
            return self.offset
        if "uci show ntpclient" in command:
            return self._ntpclient_show()
        if "uci show system" in command:
            # The other time subsystem. It carries the same server, because a
            # device whose two subsystems disagree is the TEC-846 finding.
            first = self.ntp_servers[0] if self.ntp_servers else ""
            return (f"system.system.hostname='{self.hostname}'\n"
                    f"system.ntp=timeserver\n"
                    f"system.ntp.server='{first}'\n"
                    f"system.ntp.enabled='1'\n")
        if "uci show dhcp" in command:
            return (f"dhcp.lan=dhcp\ndhcp.lan.start='{self.dhcp_start}'\n"
                    f"dhcp.lan.limit='{self.dhcp_limit}'\n")
        if "[n]tpclient" in command:
            # The daemon row: a healthy device has the process running and
            # started after the last write to the config it reads at startup.
            return (f"pid={self.ntp_pid}\n"
                    f"started={self.ntp_started}\n"
                    f"written={self.ntp_written}\n")
        if "system.ntp.enabled" in command:
            return self.ntp_enabled
        if "system.system.zoneName" in command:
            return self.zone_shown
        if "system.system.hostname" in command:
            return self.hostname
        if command.startswith("hostname 2>/dev/null"):
            return self.running_hostname
        if "cat /etc/version" in command:
            return self.version
        if "mnfinfo" in command:
            return ('{"serial":"6010212527","mac":"20972732F638",'
                    '"name":"OTD500"}')
        if "uci show simcard" in command:
            # The real read pipes `uci show` through sed to leave the section
            # indices, so that is what comes back.
            return "\n".join(str(i) for i in range(self.sim_slots))
        if "simcard.@sim[" in command and ".service" in command:
            return self.sim_service
        if "AT+COPS?" in command:
            return self.cops
        if "uci show sim_switch" in command or "uci -q show sim_switch" in command:
            return ""
        if "tailscale ip" in command:
            return self.ts_ip
        if "tailscale.settings.enabled" in command:
            return "1"
        if "rms_mqtt.rms_connect_mqtt.enable" in command:
            return self.rms_enable
        if "ubus list" in command:
            return "rms_connect_mqtt"
        if "ubus call" in command and "status" in command:
            return self.rms_status
        if "esim" in command.lower():
            return self.esim_profile
        if command.startswith("[ -x "):
            return self.quota_installed
        if "grep -F" in command and "quota-sync" in command:
            return "" if self.quota_cron is None else self.quota_cron
        if "sysupgrade.conf" in command:
            return "" if self.quota_keep is None else self.quota_keep
        return ""

    def wrote(self, fragment: str) -> bool:
        return any(fragment in c for c in self.commands)

    @property
    def mutations(self) -> list[str]:
        return [c for c in self.commands
                if bench_core._MUTATING_COMMANDS.search(c)]


class NoRest:
    """The REST half unplugged. Must be the `requests` ConnectionError: the
    client catches `requests.RequestException` to mean "that source had
    nothing", which is the behaviour under test."""

    def get(self, *_a, **_k):
        raise requests.exceptions.ConnectionError("no REST in tests")

    post = get


def client(monkeypatch, *, password=SHARED, **kwargs) -> TeltonikaClient:
    c = TeltonikaClient(host="192.0.2.1")
    c.ssh_exec = FakeOtd(**kwargs)
    c.s = NoRest()
    c.device = c.ssh_exec          # test handle
    monkeypatch.setattr(c, "login", lambda pw: setattr(c, "password", password))
    return c


def settings(**overrides):
    base = {"new_password": SHARED, "timezone": ZONE, "name_prefix": "otd-",
            "sim_4g_only": True, "sim_switch": {}, "rms": {"enabled": False},
            "tailscale": {"enabled": False}, "esim": {"enabled": False},
            "firmware": {}}
    base.update(overrides)
    return base


def resolver(expected=None, prior=None):
    """`BenchConfigurator.verify_resolver`'s contract."""
    return lambda identity: (expected or {}, prior)


def recorded(hostname=NAME, site="haifa", **extra):
    return resolver({"hostname": hostname, "site_name": site, **extra})


def row(result, item):
    return next(c for c in result["verification"] if c["item"] == item)


def items(result):
    return [c["item"] for c in result["verification"]]


# ── mutation-freedom ─────────────────────────────────────────────────────────

def test_a_verify_pass_sends_nothing_that_writes(monkeypatch):
    c = client(monkeypatch)
    mod.verify_device(c, settings=settings(rms={"enabled": True},
                                           tailscale={"enabled": True},
                                           esim={"enabled": True},
                                           sim_switch={"enabled": True}),
                      resolve=recorded(esim_activation_code="LPA:1$x"))
    assert c.device.mutations == []


def test_the_client_is_read_only_for_the_whole_pass(monkeypatch):
    c = client(monkeypatch)
    mod.verify_device(c, settings=settings(), resolve=recorded())
    assert c.read_only is True
    with pytest.raises(MutationBlocked):
        c.set_hostname("otd-somewhere-else")


def test_the_sim_switch_rows_are_read_without_being_rewritten(monkeypatch):
    # The SIM-switch block is the one place a "verify" could plausibly reach for
    # `uci add` (its sections are anonymous and may not exist yet). Creating one
    # to check it would be a mutation on a shipped device.
    c = client(monkeypatch)
    mod.verify_device(c, settings=settings(sim_switch={"enabled": True}),
                      resolve=recorded())
    assert not c.device.wrote("uci add sim_switch")
    assert c.device.mutations == []


# ── the row set has to match the configure pipeline's ────────────────────────

def test_the_full_row_set_is_asked(monkeypatch):
    c = client(monkeypatch)
    result = mod.verify_device(
        c, settings=settings(rms={"enabled": True}, tailscale={"enabled": True},
                             esim={"enabled": True}, sim_switch={"enabled": True}),
        resolve=recorded(esim_activation_code="LPA:1$x"))
    asked = items(result)
    for expected_row in ("admin/root password", "hostname", "timezone",
                         "SIM 4G-only", "RMS", "Tailscale", "eSIM profile",
                         "firmware", "NTP client", "NTP daemon", "DHCP pool"):
        assert expected_row in asked, expected_row
    # The SIM-switch block contributes its own rows.
    assert any("quota sync" in item for item in asked)


def test_a_station_that_loads_no_esim_gets_no_esim_row(monkeypatch):
    # An always-skipped row is noise on every unit of a fleet that has no eSIM.
    c = client(monkeypatch)
    result = mod.verify_device(c, settings=settings(), resolve=recorded())
    assert "eSIM profile" not in items(result)


def test_a_unit_whose_record_shows_no_esim_gets_no_esim_row(monkeypatch):
    # The station loads eSIM profiles, but this particular device did not get
    # one. Asking for it would fail a device that is correct.
    c = client(monkeypatch, esim_profile="")
    result = mod.verify_device(c, settings=settings(esim={"enabled": True}),
                              resolve=recorded())
    assert "eSIM profile" not in items(result)


def test_there_is_no_lan_ip_row(monkeypatch):
    # An OTD500 keeps its factory address — that is why detection finds a
    # finished one. A LAN IP row here would be checking something the pipeline
    # never sets.
    c = client(monkeypatch)
    assert "LAN IP" not in items(mod.verify_device(c, settings=settings(),
                                                   resolve=recorded()))


# ── the hostname: the one per-unit expectation ───────────────────────────────

def test_the_hostname_comes_from_the_configure_record(monkeypatch):
    c = client(monkeypatch, hostname="otd-golan")
    result = mod.verify_device(c, settings=settings(),
                               resolve=recorded(hostname="otd-golan"))
    assert row(result, "hostname")["ok"] is True
    assert result["name"] == "otd-golan"


def test_no_recorded_name_fails_rather_than_comparing_the_device_to_itself(
        monkeypatch):
    c = client(monkeypatch, hostname="otd-whatever")
    result = mod.verify_device(c, settings=settings(), resolve=resolver())
    check = row(result, "hostname")
    assert check["ok"] is False
    assert "no recorded name" in check["actual"]
    assert items(result).count("hostname") == 1


def test_a_half_named_device_fails_the_sweep(monkeypatch):
    # The converted tautology (TEC-348), seen from the pipeline: the UCI option
    # holds the right name and the running kernel hostname does not, which is
    # what the device reports to syslog, DHCP and RMS. This used to pass.
    c = client(monkeypatch, hostname=NAME, running_hostname="OTD500")
    result = mod.verify_device(c, settings=settings(), resolve=recorded())
    check = row(result, "hostname")
    assert check["ok"] is False
    assert "running OTD500" in check["actual"]
    assert result["ok"] is False


def test_an_operator_supplied_site_name_beats_the_record(monkeypatch):
    c = client(monkeypatch, hostname="otd-typo")
    result = mod.verify_device(c, settings=settings(),
                               resolve=recorded(hostname="otd-typo"),
                               site_name="Haifa Port")
    assert result["name"] == "otd-haifa-port"
    assert row(result, "hostname")["ok"] is False


# ── the failures a sweep exists to catch ─────────────────────────────────────

@needs_tzdata
def test_a_device_left_on_utc_fails(monkeypatch):
    c = client(monkeypatch, offset="+0000")
    result = mod.verify_device(c, settings=settings(), resolve=recorded())
    assert result["ok"] is False
    assert row(result, "timezone")["ok"] is False


def test_a_device_on_another_password_fails(monkeypatch):
    c = client(monkeypatch, password="something-else")
    result = mod.verify_device(c, settings=settings(), resolve=recorded())
    assert result["ok"] is False
    assert row(result, "admin/root password")["ok"] is False


def test_a_device_that_never_reached_rms_fails(monkeypatch):
    c = client(monkeypatch, rms_status="")
    result = mod.verify_device(c, settings=settings(rms={"enabled": True}),
                               resolve=recorded())
    assert result["ok"] is False
    assert row(result, "RMS")["ok"] is False


def test_a_device_that_never_joined_the_tailnet_fails(monkeypatch):
    c = client(monkeypatch, ts_ip="")
    result = mod.verify_device(c, settings=settings(tailscale={"enabled": True}),
                               resolve=recorded())
    assert result["ok"] is False
    assert row(result, "Tailscale")["ok"] is False


def test_a_device_sitting_on_3g_fails(monkeypatch):
    # The other converted tautology (TEC-348): `service='lte'` is in the config
    # and the modem attached on UTRAN anyway. The row used to read back the
    # option it had just been told about, so this device passed.
    c = client(monkeypatch, sim_service="lte", cops=ATTACHED_3G)
    result = mod.verify_device(c, settings=settings(), resolve=recorded())
    assert result["ok"] is False
    assert row(result, "SIM 4G-only")["ok"] is False


def test_an_unregistered_modem_cannot_confirm_4g_and_does_not_pass(monkeypatch):
    # The usual state on a bench with no outdoor antenna. Honest amber: the
    # operator is told the radio was not checked rather than that it is fine.
    c = client(monkeypatch, cops=NOT_ATTACHED)
    result = mod.verify_device(c, settings=settings(), resolve=recorded())
    check = row(result, "SIM 4G-only")
    assert check["ok"] is None
    assert "cannot confirm" in check["actual"]


def test_firmware_below_the_expected_version_fails(monkeypatch):
    c = client(monkeypatch, version="OTD5_R_00.07.19")
    result = mod.verify_device(
        c, settings=settings(firmware={"expected_version": "OTD5_R_00.07.20.3"}),
        resolve=recorded())
    assert result["ok"] is False
    assert row(result, "firmware")["ok"] is False


def test_a_missing_configure_record_fails_the_pass(monkeypatch):
    missing = {"item": "prior run", "expected": "a recorded configure run",
               "actual": "none found", "ok": False}
    c = client(monkeypatch)
    result = mod.verify_device(c, settings=settings(),
                               resolve=resolver({"hostname": NAME}, missing))
    assert result["ok"] is False
    assert result["verification"][0]["item"] == "prior run"


def test_a_fully_correct_device_passes(monkeypatch):
    c = client(monkeypatch)
    result = mod.verify_device(c, settings=settings(), resolve=recorded())
    assert result["ok"] is True


def test_the_wrong_model_is_refused(monkeypatch):
    # OTD500 and RUTM08 share the factory IP, so an operator on the wrong tab
    # must be stopped on the verify path too, not only on configure.
    c = client(monkeypatch)
    monkeypatch.setattr(c, "get_identity", lambda: {"model": "RUTM08",
                                                    "serial": "SN-1"})
    with pytest.raises(SystemExit):
        mod.verify_device(c, settings=settings(), resolve=recorded())


def test_no_row_carries_the_password(monkeypatch):
    # These rows are shipped to bench-central verbatim (TEC-349).
    c = client(monkeypatch)
    result = mod.verify_device(c, settings=settings(rms={"enabled": True},
                                                    tailscale={"enabled": True}),
                               resolve=recorded())
    for check in result["verification"]:
        assert SHARED not in str(check), check
