"""The RUTM08 verify-only pass (rutm_configure.verify_rutm, TEC-348).

No hardware: `FakeRouter` stands in for `ssh_exec`, so these pin what a real
router would be ASKED and, more importantly, what it would not be told to do.

Two things are specific to this tool and are what most of this file is about:

* **The hostname is per-unit.** It comes from a site name somebody typed during
  the original run, so a verify pass has to recover it from the unit's configure
  record. When it can't, the row must FAIL rather than compare the device's
  hostname against itself — which would pass by construction and is the exact
  tautology TEC-348 exists to remove.
* **The LAN IP row stops being a maybe.** `move_lan` logs "no answer on
  192.168.88.1 — may still be fine", because during a configure run a stale
  station DHCP lease is indistinguishable from a router that failed to move.
  Here the router is in front of us at a known address, which is conclusive.
"""
import pytest
import requests

import bench_core
from bench_core import (
    NTP_CLIENT_INTERVAL,
    MutationBlocked,
    expected_utc_offset,
)

SHARED = "test-shared-pw"   # a fake on purpose: proving the pipeline
                            # applies the configured password needs a
                            # value, not THE value

import rutm_configure as mod
from rutm_configure import RutmClient

ZONE = "Asia/Jerusalem"
NAME = "rut-haifa"
LAN_IP = "192.168.88.1"
FACTORY = "192.168.1.1"
CORRECT_OFFSET = expected_utc_offset(ZONE) or "+0300"

needs_tzdata = pytest.mark.skipif(
    not expected_utc_offset(ZONE),
    reason="needs a tz database to say what the zone means today (tzdata)")


class FakeRouter:
    """Stands in for `ssh_exec`: canned answers for reads, a recording of
    everything sent. The clock is independent of the UCI options on purpose —
    that independence is what makes the timezone row a real check."""

    def __init__(self, *, hostname=NAME, running_hostname=None,
                 offset=CORRECT_OFFSET, zone_shown=ZONE,
                 lan_ip=LAN_IP, ntp="192.168.88.10", polling=None,
                 version="RUTM_R_00.07.20", ts_ip="100.64.0.5", rms_enable="1",
                 rms_status='{"connection_state":"connected"}',
                 ntp_servers=None, ntp_enabled="1",
                 ntp_interval=str(NTP_CLIENT_INTERVAL),
                 ntp_package=True, wan=None, redirect=None,
                 wan_zone_networks=("wan", "wan6"),
                 ntp_pid="15296", ntp_started=2000, ntp_written=1000):
        self.commands: list[str] = []
        self.hostname = hostname
        # The RUNNING kernel hostname, independent of the UCI option for the
        # same reason the clock is independent of the zone option: reading back
        # only what was written cannot fail (TEC-348).
        self.running_hostname = hostname if running_hostname is None else running_hostname
        self.offset = offset
        self.zone_shown = zone_shown
        self.lan_ip = lan_ip
        self.ntp = ntp
        self.polling = [ntp] if polling is None else polling
        self.version = version
        self.ts_ip = ts_ip
        self.rms_enable = rms_enable
        self.rms_status = rms_status
        # The `ntpclient` package, the client a RutOS router actually polls
        # with — a separate subsystem from the system.ntp lines below, and the
        # pair disagreeing is the TEC-846 finding.
        self.ntp_servers = [ntp] if ntp_servers is None else list(ntp_servers)
        self.ntp_enabled = ntp_enabled
        self.ntp_interval = ntp_interval
        self.ntp_package = ntp_package
        # A WAN pinned to the fleet-constant address, and the NTP forward that
        # carries the upstream OTD500 through it (TEC-857).
        self.wan = {"proto": "static", "ipaddr": "192.168.1.2",
                    "netmask": "255.255.255.0",
                    "gateway": "192.168.1.1"} if wan is None else wan
        self.redirect = {"name": "kela-ntp", "target": "DNAT", "src": "wan",
                         "proto": "udp", "src_dport": "123",
                         "dest_ip": "192.168.88.10", "dest_port": "123",
                         "src_ip": "192.168.1.1"} if redirect is None else redirect
        self.wan_zone_networks = list(wan_zone_networks)
        # The daemon that reads the ntpclient config above, once, at startup.
        self.ntp_pid = ntp_pid
        self.ntp_started = ntp_started
        self.ntp_written = ntp_written

    def _network_show(self) -> str:
        lines = [f"network.lan=interface", f"network.lan.ipaddr='{self.lan_ip}'"]
        if self.wan:
            lines.append("network.wan=interface")
            lines += [f"network.wan.{k}='{v}'" for k, v in self.wan.items()]
        return "\n".join(lines)

    def _firewall_show(self) -> str:
        members = " ".join(f"'{n}'" for n in self.wan_zone_networks)
        lines = ["firewall.@zone[0]=zone", "firewall.@zone[0].name='wan'",
                 f"firewall.@zone[0].network={members}"]
        if self.redirect:
            lines.append("firewall.@redirect[0]=redirect")
            lines += [f"firewall.@redirect[0].{k}='{v}'"
                      for k, v in self.redirect.items()]
        return "\n".join(lines)

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
        if "system.system.zoneName" in command:
            return self.zone_shown
        if "system.system.hostname" in command:
            return self.hostname
        if command.startswith("hostname 2>/dev/null"):
            return self.running_hostname
        if "uci show ntpclient" in command:
            return self._ntpclient_show()
        if "[n]tpclient" in command:
            # The daemon row: a healthy device has the process running and
            # started after the last write to the config it reads at startup.
            return (f"pid={self.ntp_pid}\n"
                    f"started={self.ntp_started}\n"
                    f"written={self.ntp_written}\n")
        if "uci show firewall" in command:
            return self._firewall_show()
        if "uci show network" in command:
            return self._network_show()
        if "network.lan.ipaddr" in command:
            return self.lan_ip
        if "grep '[n]tpd'" in command:
            return "\n".join(f" 6055 root 1680 S< /usr/sbin/ntpd -n -N -p {s}"
                             for s in self.polling)
        if "uci show system" in command:
            return (f"system.ntp.server='{self.ntp}'\n"
                    "system.ntp.enabled='1'\n")
        if "system.ntp.enabled" in command:
            return "1"
        if "cat /etc/version" in command:
            return self.version
        if "mnfinfo" in command:
            return ('{"serial":"6010212527","mac":"20972732F638",'
                    '"name":"RUTM08"}')
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
        return ""

    def wrote(self, fragment: str) -> bool:
        return any(fragment in c for c in self.commands)

    @property
    def mutations(self) -> list[str]:
        return [c for c in self.commands
                if bench_core._MUTATING_COMMANDS.search(c)]


class NoRest:
    """The REST half unplugged. get_identity() also GETs the device status;
    without this the suite would try to reach a real address.

    Must be the `requests` ConnectionError, not the builtin: the client catches
    `requests.RequestException` to mean "that source had nothing", which is the
    behaviour under test."""

    def get(self, *_a, **_k):
        raise requests.exceptions.ConnectionError("no REST in tests")

    post = get


def client(monkeypatch, *, host=LAN_IP, password=SHARED, **kwargs) -> RutmClient:
    c = RutmClient(host=host)
    c.ssh_exec = FakeRouter(**kwargs)
    c.s = NoRest()
    c.device = c.ssh_exec          # test handle
    monkeypatch.setattr(c, "login", lambda pw: setattr(c, "password", password))
    return c


def settings(**overrides):
    base = {"new_password": SHARED, "timezone": ZONE, "lan_ip": LAN_IP,
            "host": FACTORY, "name_prefix": "rut-",
            "rms": {"enabled": False}, "tailscale": {"enabled": False},
            "firmware": {}}
    base.update(overrides)
    return base


def resolver(expected=None, prior=None):
    """`BenchConfigurator.verify_resolver`'s contract."""
    return lambda identity: (expected or {}, prior)


def recorded(hostname=NAME, site="haifa"):
    return resolver({"hostname": hostname, "site_name": site})


def row(result, item):
    return next(c for c in result["verification"] if c["item"] == item)


# ── mutation-freedom ─────────────────────────────────────────────────────────

def test_a_verify_pass_sends_nothing_that_writes(monkeypatch):
    c = client(monkeypatch)
    mod.verify_rutm(c, settings=settings(), resolve=recorded())
    assert c.device.mutations == []


def test_the_client_is_read_only_for_the_whole_pass(monkeypatch):
    c = client(monkeypatch)
    mod.verify_rutm(c, settings=settings(), resolve=recorded())
    assert c.read_only is True
    # And it is enforced, not merely recorded.
    with pytest.raises(MutationBlocked):
        c.set_hostname("rut-somewhere-else")


def test_the_lan_row_never_reaches_for_move_lan(monkeypatch):
    # move_lan restarts a shipped router's network. Reaching for it here would
    # be the worst possible bug in this mode, so it is made to explode.
    c = client(monkeypatch)
    monkeypatch.setattr(c, "move_lan",
                        lambda *a, **k: pytest.fail("verify moved the LAN"))
    mod.verify_rutm(c, settings=settings(), resolve=recorded())


def test_no_tailscale_key_is_minted_for_a_verify_run(monkeypatch):
    # Minting a key is a mutation of the tailnet, and a verify pass reads the
    # node's existing address off the device instead.
    c = client(monkeypatch)
    mod.verify_rutm(c, settings=settings(tailscale={"enabled": True}),
                    resolve=recorded())
    assert not c.device.wrote("tailscale up")


# ── the LAN IP row: the ambiguity TEC-348 opens with ─────────────────────────

def test_a_router_at_its_final_address_passes_conclusively(monkeypatch):
    c = client(monkeypatch, host=LAN_IP, lan_ip=LAN_IP)
    check = row(mod.verify_rutm(c, settings=settings(), resolve=recorded()),
                "LAN IP")
    assert check["ok"] is True
    # Not "may still be fine": we are talking to it, right now, at that address.
    assert "answering on 192.168.88.1" in check["actual"]
    assert "may still be fine" not in check["actual"]


def test_a_router_still_on_the_factory_address_fails(monkeypatch):
    c = client(monkeypatch, host=FACTORY, lan_ip=FACTORY)
    result = mod.verify_rutm(c, settings=settings(), resolve=recorded())
    assert result["ok"] is False
    check = row(result, "LAN IP")
    assert check["ok"] is False
    assert FACTORY in check["actual"]


def test_an_address_set_by_hand_but_not_committed_fails(monkeypatch):
    # Answering on 192.168.88.1 with UCI still saying 192.168.1.1: it works
    # today and moves on the next reboot. A row that only probed reachability
    # would call this a pass.
    c = client(monkeypatch, host=LAN_IP, lan_ip=FACTORY)
    check = row(mod.verify_rutm(c, settings=settings(), resolve=recorded()),
                "LAN IP")
    assert check["ok"] is False
    assert "next reboot" in check["actual"]


# ── the hostname: the one per-unit expectation ───────────────────────────────

def test_the_hostname_comes_from_the_configure_record(monkeypatch):
    c = client(monkeypatch, hostname="rut-golan")
    result = mod.verify_rutm(c, settings=settings(),
                             resolve=recorded(hostname="rut-golan"))
    check = row(result, "hostname")
    assert check["ok"] is True
    assert result["name"] == "rut-golan"


def test_a_half_named_router_fails_the_sweep(monkeypatch):
    # The converted tautology (TEC-348), seen from the pipeline: the UCI option
    # holds the right name and the running kernel hostname does not, which is
    # what the router reports to syslog, DHCP and RMS. This used to pass.
    c = client(monkeypatch, hostname=NAME, running_hostname="RUTM08")
    result = mod.verify_rutm(c, settings=settings(), resolve=recorded())
    check = row(result, "hostname")
    assert check["ok"] is False
    assert "running RUTM08" in check["actual"]
    assert result["ok"] is False


def test_a_renamed_router_fails_against_its_record(monkeypatch):
    # Somebody re-ran the unit under a different site name, or set the hostname
    # by hand. Either way the unit is not what the record says it is.
    c = client(monkeypatch, hostname="rut-somewhere-else")
    result = mod.verify_rutm(c, settings=settings(), resolve=recorded())
    assert result["ok"] is False
    assert row(result, "hostname")["ok"] is False


def test_no_recorded_name_fails_rather_than_comparing_the_device_to_itself(
        monkeypatch):
    # The tautology trap. With nothing to compare against, the tempting move is
    # to read the hostname and call it expected — which cannot fail. The row
    # says what is missing instead.
    c = client(monkeypatch, hostname="rut-whatever")
    result = mod.verify_rutm(c, settings=settings(), resolve=resolver())
    check = row(result, "hostname")
    assert check["ok"] is False
    assert "no recorded name" in check["actual"]
    assert "rut-whatever" in check["actual"]   # still shown, as information
    assert result["ok"] is False


def test_there_is_exactly_one_hostname_row_when_the_name_is_unknown(monkeypatch):
    # The substituted row must REPLACE the read-back one, not sit next to it —
    # two hostname rows disagreeing is unreadable at the bench.
    c = client(monkeypatch)
    result = mod.verify_rutm(c, settings=settings(), resolve=resolver())
    assert [c_["item"] for c_ in result["verification"]].count("hostname") == 1


def test_an_operator_supplied_site_name_beats_the_record(monkeypatch):
    # An engineer checking a unit against what it SHOULD be, not what a
    # mistaken earlier run recorded.
    c = client(monkeypatch, hostname="rut-typo")
    result = mod.verify_rutm(c, settings=settings(),
                             resolve=recorded(hostname="rut-typo"),
                             site_name="Haifa Port")
    assert result["name"] == "rut-haifa-port"
    assert row(result, "hostname")["ok"] is False


# ── the rest of the row set ──────────────────────────────────────────────────

def test_the_row_set_matches_what_a_configure_run_verifies(monkeypatch):
    # Same questions, minus the ones that only make sense while mutating. If
    # these drift apart, a device can pass verification and still not be what
    # the configure pipeline would have produced.
    c = client(monkeypatch)
    result = mod.verify_rutm(c, settings=settings(rms={"enabled": True},
                                                  tailscale={"enabled": True}),
                             resolve=recorded())
    assert [check["item"] for check in result["verification"]] == [
        "admin/root password", "hostname", "timezone", "RMS", "Tailscale",
        "firmware", "NTP client", "NTP daemon", "LAN IP"]


def test_the_wan_rows_appear_only_when_the_station_applies_them(monkeypatch):
    # The WAN block is opt-in (it ends internet on the bench), so a station that
    # has not switched it on must not collect two skipped rows on every router.
    c = client(monkeypatch)
    plain = [r["item"] for r in
             mod.verify_rutm(c, settings=settings(), resolve=recorded())["verification"]]
    assert "WAN address" not in plain and "NTP forward" not in plain

    c = client(monkeypatch)
    opted_in = [r["item"] for r in mod.verify_rutm(
        c, settings=settings(wan={"enabled": True},
                             ntp_forward={"enabled": True}),
        resolve=recorded())["verification"]]
    assert "WAN address" in opted_in and "NTP forward" in opted_in


def test_there_is_no_sim_row_on_a_modemless_router(monkeypatch):
    # A RUTM08 has no modem. A "skipped" SIM row would be noise on every unit.
    c = client(monkeypatch)
    result = mod.verify_rutm(c, settings=settings(), resolve=recorded())
    assert not any(check["item"] == "SIM 4G-only"
                   for check in result["verification"])


@needs_tzdata
def test_a_router_left_on_utc_fails(monkeypatch):
    # The fault that shipped a TSW202 with four green rows, asked of a RUTM08.
    c = client(monkeypatch, offset="+0000")
    result = mod.verify_rutm(c, settings=settings(), resolve=recorded())
    assert result["ok"] is False
    assert row(result, "timezone")["ok"] is False


def test_a_router_on_another_password_fails(monkeypatch):
    c = client(monkeypatch, password="something-else")
    result = mod.verify_rutm(c, settings=settings(), resolve=recorded())
    assert result["ok"] is False
    assert row(result, "admin/root password")["ok"] is False


def test_a_missing_configure_record_fails_the_pass(monkeypatch):
    missing = {"item": "prior run", "expected": "a recorded configure run",
               "actual": "none found", "ok": False}
    c = client(monkeypatch)
    result = mod.verify_rutm(c, settings=settings(),
                             resolve=resolver({"hostname": NAME}, missing))
    assert result["ok"] is False
    assert result["verification"][0]["item"] == "prior run"


def test_a_fully_correct_router_passes(monkeypatch):
    c = client(monkeypatch)
    result = mod.verify_rutm(c, settings=settings(), resolve=recorded())
    assert result["ok"] is True
    assert all(check["ok"] is not False for check in result["verification"])


def test_the_wrong_model_is_refused(monkeypatch):
    # OTD500 and RUTM08 share the factory IP and now also share the bench
    # subnet, so an operator on the wrong tab must be stopped here too — not
    # only on the configure path.
    c = client(monkeypatch)
    monkeypatch.setattr(c, "get_identity", lambda: {"model": "OTD500",
                                                    "serial": "SN-1"})
    with pytest.raises(SystemExit):
        mod.verify_rutm(c, settings=settings(), resolve=recorded())


def test_no_row_carries_the_password(monkeypatch):
    # These rows are shipped to bench-central verbatim (TEC-349).
    c = client(monkeypatch)
    result = mod.verify_rutm(c, settings=settings(), resolve=recorded())
    for check in result["verification"]:
        assert SHARED not in str(check), check
