"""A verify-only run must not change the device (TEC-348, TEC-851).

"Mutation-free" is a claim about code nobody re-reads, so it gets two
independent guards and this file tests both:

1. **The client refuses.** `set_read_only()` makes every writing method raise
   `MutationBlocked`, and `ssh_exec` screens the command itself. A runtime
   seatbelt for the case a pipeline is edited later.
2. **The checks don't try.** `verify_configuration` — the function a verify pass
   is built out of — issues no mutating command even when it is free to.

The screen is a deny-list over arbitrary shell, so its failure mode in the
OTHER direction matters just as much: a pattern that also matches a legitimate
READ would make verification refuse to run. The first version did exactly that
(`\\bsysupgrade\\b` matches `grep -v '^#' /etc/sysupgrade.conf`, a read the
SIM-switch check makes on every OTD500), so every read the verification paths
actually issue is pinned below.

The second half of the file is the Magos pair. Their guard has nothing in
common with this one — an HTTP API with a fixed set of endpoints instead of
arbitrary shell, so it gates the request rather than screening a string — but
the property being asserted is identical, and both halves belong wherever
somebody goes to check that a Verify button cannot change a finished unit.
"""
import sys
from pathlib import Path

import pytest

from bench_core import (
    MutationBlocked,
    TeltonikaClient,
    _MUTATING_COMMANDS,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "magos-config-ui"))
from apu_configure import APUClient        # noqa: E402
from magos_configure import MagosClient    # noqa: E402
import magos_verify as magos               # noqa: E402

SHARED = "test-shared-pw"


class FakeDevice:
    """Records every command, answers reads plausibly."""

    def __init__(self):
        self.commands: list[str] = []

    def __call__(self, command, check=True, exec_timeout=None):
        self.commands.append(command)
        if command.startswith("date +%z"):
            return "+0300"
        if "zoneName" in command:
            return "Asia/Jerusalem"
        if "hostname" in command:
            return "otd-haifa"
        if "/etc/version" in command:
            return "OTD5_R_00.07.20.3"
        return ""


def client(read_only=False) -> TeltonikaClient:
    c = TeltonikaClient(host="192.0.2.1")
    c.ssh_exec = FakeDevice()
    c.device = c.ssh_exec
    c.password = SHARED
    if read_only:
        c.set_read_only()
    return c


# ── the screen: reads the verification paths really make must be allowed ─────
#
# Copied from the actual call sites, `2>/dev/null` and all. If one of these
# starts matching, a check stops being able to run — which is worse than the
# mutation the pattern was added to catch, because it fails closed on the wrong
# thing and looks like a device fault.
ALLOWED_READS = [
    "uci get system.system.hostname 2>/dev/null",
    "uci -q get system.system.hostname",
    # The running-kernel half of the hostname row (TEC-348). It contains `>`
    # twice, so it is exactly the shape a redirect-hunting pattern gets wrong.
    "hostname 2>/dev/null || cat /proc/sys/kernel/hostname 2>/dev/null",
    "uci -q get system.system.zoneName",
    "uci -q get system.ntp.enabled",
    "uci show network 2>/dev/null",
    "uci get network.lan.ipaddr 2>/dev/null",
    "uci get simcard.@sim[1].service 2>/dev/null",
    "uci get rms_mqtt.rms_connect_mqtt.enable 2>/dev/null",
    "uci get tailscale.settings.enabled 2>/dev/null",
    "uci -q show sim_switch",
    "date +%z",
    # Clock READS. They sit one flag away from the `date -s` write below, which
    # is why both forms are listed on each side.
    "date -u +%s",
    "date -u",
    "cat /etc/version 2>/dev/null",
    "cat /proc/sys/kernel/hostname 2>/dev/null",
    "uname -n",
    "tailscale ip -4 2>/dev/null",
    "ubus call mnfinfo get 2>/dev/null",
    "ubus list 2>/dev/null | grep -i rms",
    "ubus call rms_connect_mqtt status 2>/dev/null",
    "ubus call system board 2>/dev/null",
    "gsmctl -i 2>/dev/null",
    "gsmctl -q 2>/dev/null",
    "gsmctl -j 2>/dev/null",
    "gsmctl --esim-list 2>/dev/null || gsmctl -A 'AT+ESIM?' 2>/dev/null",
    "gsmctl -A 'AT+COPS?' 2>/dev/null",
    # The one that broke the first version of the pattern.
    "grep -v '^#' /etc/sysupgrade.conf 2>/dev/null",
    "grep -F quota-sync /etc/crontabs/root 2>/dev/null",
    "[ -x /sbin/quota-sync ] && echo script; [ -x /etc/init.d/quota-sync ] && echo boot-hook; "
    "[ -e /etc/rc.d/S99quota-sync ] && echo enabled",
    "ps w 2>/dev/null | grep '[n]tpd'",
    "tr -d '\\000' < /proc/device-tree/model 2>/dev/null",
    # The TEC-857 rows. `uci show <pkg>` is a read whatever the package, but
    # these get listed because the deny-list matches `uci` verbs anywhere in a
    # line and a new package name is exactly where a near-miss would hide.
    "uci show ntpclient 2>/dev/null",
    "uci show firewall 2>/dev/null",
    "uci show dhcp 2>/dev/null",
    "uci show system 2>/dev/null",
    # The NTP daemon row. Three `$(...)` substitutions and a `>` in each — the
    # exact shape a redirect-hunting pattern gets wrong, and `/proc/` IS one of
    # the protected write targets, so it only passes because the `>` belongs to
    # a `2>/dev/null` pointing somewhere else.
    ("pid=$(ps w 2>/dev/null | grep '[n]tpclient' | awk '{print $1}' "
     "| head -n1); "
     'echo "pid=${pid:-}"; '
     'echo "started=$(date -r /proc/${pid:-0} +%s 2>/dev/null)"; '
     'echo "written=$(date -r /etc/config/ntpclient +%s 2>/dev/null)"'),
]


@pytest.mark.parametrize("command", ALLOWED_READS)
def test_a_read_the_checks_make_is_not_mistaken_for_a_write(command):
    assert _MUTATING_COMMANDS.search(command) is None, command


# Copied from the configure pipelines — every one of these must be caught.
BLOCKED_WRITES = [
    "uci set system.system.hostname='otd-haifa' && uci commit system",
    "uci set 'simcard.@sim[0].service=lte'",
    "uci -q delete system.ntp.server",
    # The DHCP move (TEC-848), verbatim — a `uci -q delete` in the middle of an
    # otherwise-innocuous-looking line is exactly what a screen can miss.
    ("uci set 'network.lan.proto=dhcp' || exit 1; "
     "uci -q delete 'network.lan.ipaddr'; uci -q delete 'network.lan.netmask'; "
     "uci -q delete 'network.lan.gateway'; uci commit network"),
    "uci add_list system.ntp.server='192.168.88.10'",
    "uci add sim_switch sim_switch",
    "uci commit network",
    "/etc/init.d/system reload",
    "/etc/init.d/sysntpd restart",
    "/etc/init.d/network restart",
    "sleep 1; /etc/init.d/network restart",
    "reboot",
    "sysupgrade /tmp/firmware.bin",
    "sysupgrade -n /tmp/firmware.bin",
    "echo 'root:test-shared-pw' | chpasswd",
    "echo otd-haifa > /proc/sys/kernel/hostname",
    "echo 'IST-2IDT' > /etc/TZ",
    "echo 'IST-2IDT' > /tmp/TZ",
    "tailscale up --authkey=xxx",
    "ubus call rms_connect_mqtt connect",
    "gsmctl --esim-download 'LPA:1$...'",
    "gsmctl -A 'AT+CFUN=1,1'",
    "sed -i 's/a/b/' /etc/config/system",
    "mkdir -p /etc/init.d && cat > /etc/init.d/quota-sync.new <<'EOF'",
    "chmod 755 /sbin/quota-sync.new && mv /sbin/quota-sync.new /sbin/quota-sync",
    "rm -f /etc/config/backup",
    "crontab -l | crontab -",
    "opkg install tailscale",
    # Stepping the clock before an opkg fetch. Every syntax the client tries,
    # because they differ only in the argument and the flag order is what the
    # pattern keys on.
    "date -u -s '@1756712220' >/dev/null 2>&1",
    "date -u -s '2026-09-01 09:17:00' >/dev/null 2>&1",
    "date -u -s 202609010917.00 >/dev/null 2>&1",
    "date -s '@1756712220'",
]


@pytest.mark.parametrize("command", BLOCKED_WRITES)
def test_a_write_from_the_configure_pipelines_is_caught(command):
    assert _MUTATING_COMMANDS.search(command) is not None, command


# ── the client refuses ───────────────────────────────────────────────────────

# These drive the REAL ssh_exec, not the FakeDevice stub — the screen lives
# inside it, and a test that stubbed it would be testing nothing. Safe without
# a device because the check runs before the SSH client is opened, which is
# also the property being asserted: a refused command never reaches the wire.
def test_ssh_exec_refuses_a_write_on_a_read_only_client():
    c = TeltonikaClient(host="192.0.2.1")
    c.set_read_only()
    with pytest.raises(MutationBlocked):
        c.ssh_exec("uci set system.system.hostname='x' && uci commit system")


def test_the_refusal_names_the_command_so_the_bug_is_findable():
    c = TeltonikaClient(host="192.0.2.1")
    c.set_read_only()
    with pytest.raises(MutationBlocked, match="uci commit system"):
        c.ssh_exec("uci commit system")


def test_a_refused_command_never_opens_a_connection(monkeypatch):
    c = TeltonikaClient(host="192.0.2.1")
    c.set_read_only()
    monkeypatch.setattr(c, "_ssh_client",
                        lambda: pytest.fail("opened SSH for a refused command"))
    with pytest.raises(MutationBlocked):
        c.ssh_exec("reboot")


def test_ssh_exec_still_reads_on_a_read_only_client():
    c = client(read_only=True)
    assert c.ssh_exec("date +%z") == "+0300"


def test_read_only_is_off_by_default():
    # A flag you had to turn OFF to provision a device would be the wrong way
    # round — the configure run is the normal case.
    assert client().read_only is False
    client().ssh_exec("uci commit system")  # no raise


@pytest.mark.parametrize("call", [
    lambda c: c._uci("system.system.hostname='x'", package="system"),
    lambda c: c._uci_add("sim_switch", "sim_switch"),
    lambda c: c._put_file("/sbin/quota-sync", "#!/bin/sh\n"),
    lambda c: c._fire_and_forget("reboot"),
    lambda c: c.upgrade_firmware(fota=True),
    lambda c: c.move_lan("192.168.88.1"),
    lambda c: c.move_lan_dhcp(mac="20:97:27:2b:00:f7", subnets=["192.168.88.0/24"]),
    lambda c: c.set_admin_password("something-else"),
])
def test_every_writing_method_refuses(call):
    c = client(read_only=True)
    with pytest.raises(MutationBlocked):
        call(c)
    # And nothing reached the device on the way to refusing.
    assert c.device.commands == []


def test_setting_the_password_it_already_has_is_not_a_mutation():
    # set_admin_password returns early when the device is already on the shared
    # password. Refusing that would be refusing to do nothing, and it is the
    # state every finished device is in.
    c = client(read_only=True)
    c.set_admin_password(SHARED)
    assert c.device.commands == []


# ── the checks themselves don't try to write ─────────────────────────────────

def test_verify_configuration_mutates_nothing():
    # The strong guarantee: not "it was blocked", but "it never asked".
    c = client()
    c.verify_configuration(hostname="otd-haifa", zonename="Asia/Jerusalem",
                           new_password=SHARED, sim_4g=True, rms=True,
                           tailscale=True, esim=True,
                           expected_firmware="OTD5_R_00.07.20.3",
                           sim_switch={"enabled": True})
    offenders = [cmd for cmd in c.device.commands
                 if _MUTATING_COMMANDS.search(cmd)]
    assert offenders == []


def test_verify_configuration_runs_unchanged_on_a_read_only_client():
    # Same call, same rows, with the seatbelt on. If the two disagreed, one of
    # them would be lying about what verification does.
    free = client()
    locked = client(read_only=True)
    kwargs = dict(hostname="otd-haifa", zonename="Asia/Jerusalem",
                  new_password=SHARED, sim_4g=True, rms=True, tailscale=True,
                  esim=True, expected_firmware="OTD5_R_00.07.20.3",
                  sim_switch={"enabled": True})
    assert free.verify_configuration(**kwargs) == locked.verify_configuration(**kwargs)
    assert free.device.commands == locked.device.commands


def test_the_lan_ip_row_is_available_without_moving_anything():
    c = client(read_only=True)
    c.host = "192.168.88.1"
    row = c.lan_ip_check("192.168.88.1")
    assert row["item"] == "LAN IP"
    assert row["ok"] is True
    assert "answering on 192.168.88.1" in row["actual"]


def test_the_lan_ip_row_fails_when_the_unit_is_not_at_its_final_address():
    # The exact case TEC-348 opens with. On a configure run "no answer on
    # 192.168.88.1" is inconclusive; here the device is in front of us on the
    # factory address, which is conclusive.
    c = client(read_only=True)
    c.host = "192.168.1.1"
    row = c.lan_ip_check("192.168.88.1")
    assert row["ok"] is False
    assert "192.168.1.1" in row["actual"]


def test_the_lan_ip_row_catches_an_address_set_by_hand_but_not_committed():
    # Answering on the right address with the wrong config: it works today and
    # moves on the next reboot. A row that only probed reachability would pass.
    c = client(read_only=True)
    c.host = "192.168.88.1"
    c.ssh_exec = lambda *a, **k: "192.168.1.1"
    row = c.lan_ip_check("192.168.88.1")
    assert row["ok"] is False
    assert "next reboot" in row["actual"]


# ── the DHCP twin of the LAN IP row (TEC-848) ────────────────────────────────
#
# A device may now be left on DHCP, where there is no address to hold it to.
# The answerable question becomes whether it is configured to ASK for one, and
# these pin the three answers that question has.

def dhcp_client(uci_show: str, host="192.168.88.57"):
    """A client whose whole `network` package is `uci_show`, answering both the
    dump and the single-option read `current_lan_ip` makes off it."""
    lines = dict(line.split("=", 1) for line in uci_show.split() if "=" in line)

    def answer(cmd, **kw):
        if "uci show network" in cmd:
            return uci_show
        path = cmd.split("uci get ", 1)[-1].split(" ", 1)[0]
        return lines.get(path, "").strip("'\"")

    c = client(read_only=True)
    c.host = host
    c.ssh_exec = answer
    return c


def test_the_dhcp_row_passes_a_device_that_is_asking_for_a_lease():
    c = dhcp_client("network.loopback.proto='static'\nnetwork.lan.proto='dhcp'\n")
    row = c.lan_dhcp_check()
    assert row["ok"] is True
    assert "currently reached at 192.168.88.57" in row["actual"]


def test_the_dhcp_row_fails_a_device_that_was_left_static():
    # The unit did not get the treatment the record says it got — the same
    # class of finding as a switch sitting on the wrong static address.
    c = dhcp_client("network.lan.proto='static'\nnetwork.lan.ipaddr='192.168.88.2'\n")
    row = c.lan_dhcp_check()
    assert row["ok"] is False
    assert "not left on DHCP" in row["actual"]


def test_a_leftover_static_address_under_dhcp_is_a_finding():
    # `proto=dhcp` with an `ipaddr` still configured means "on DHCP, or else
    # back on the bench address if no lease arrives" — two different devices to
    # ship, from one record. Deleting the option is what move_lan_dhcp does;
    # this is the row that catches one where it did not happen.
    c = dhcp_client("network.lan.proto='dhcp'\nnetwork.lan.ipaddr='192.168.88.2'\n",
                    host="192.168.88.2")
    row = c.lan_dhcp_check()
    assert row["ok"] is False
    assert "fall back" in row["actual"]


def test_the_dhcp_row_asks_the_device_nothing_it_could_change():
    c = client(read_only=True)
    c.lan_dhcp_check()
    assert [cmd for cmd in c.device.commands if _MUTATING_COMMANDS.search(cmd)] == []


def test_mutation_blocked_is_not_absorbed_by_a_step_runner():
    # Pipelines catch SystemExit everywhere to record a soft step failure. A
    # blocked mutation is a bug in the verify pipeline, not a device problem,
    # and must not be reported as one red row among many.
    assert not issubclass(MutationBlocked, SystemExit)


# ── the Magos pair: the same property over an HTTP API (TEC-851) ─────────────
#
# No shell to screen here — every change is a POST to one of a handful of
# dashboard endpoints. So the gate is on the request, which makes it stronger
# than a deny-list (a write added later cannot fail to be covered) and gives it
# one blind spot the tests below pin: the RF channel goes over a WebSocket.

MAGOS_HOST = "192.168.88.51"
APU_HOST = "192.168.88.60"


class FakeMagosDevice:
    """Records every request and answers reads plausibly, so a blocked write is
    asserted on what reached the wire rather than on what the flag caught."""

    def __init__(self):
        self.requests: list[tuple[str, str]] = []
        self.cookies = {"session": "abc"}
        self.headers: dict = {}
        self.verify = True

    def get(self, url, **kw):
        return self._answer("GET", url)

    def post(self, url, json=None, **kw):
        return self._answer("POST", url)

    def _answer(self, method, url):
        path = "/" + url.split("//", 1)[-1].split("/", 1)[-1]
        self.requests.append((method, path))
        return FakeMagosResponse(self._body(path))

    def _body(self, path) -> dict:
        if path.endswith("/system"):
            return {"ntpServer": "192.168.88.10", "ntpAutomatic": False,
                    "timezone": "Asia/Jerusalem"}
        if path.endswith("/networking"):
            return {"netInterfaces": {"port1": {
                "ip4Method": "manual", "ip4Address": "192.168.88.51",
                "ip4Netmask": "255.255.255.0", "ip4Gateway": "192.168.88.1",
                "ip4DNS": ["192.168.88.1"]}}}
        if path.endswith("/systemStatus"):
            return {"serialNumber": "AR300-0091", "model": "AR-300",
                    "softwareVersion": "3.1.2"}
        if path.endswith("/apu/v1/settings"):
            return {"radars": [{"radar_id": "radar_0",
                                "remote_base_url": "http://192.168.88.50"}]}
        return {}

    @property
    def writes(self) -> list[tuple[str, str]]:
        """A POST to /login is a session, not a device change."""
        return [(m, p) for m, p in self.requests
                if m == "POST" and not p.endswith("/login")]


class FakeMagosResponse:
    def __init__(self, body, status_code=200):
        self.status_code = status_code
        self._body = body
        self.headers: dict = {}

    @property
    def text(self):
        return str(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


def magos_client(cls=MagosClient, host=MAGOS_HOST, read_only=False):
    c = cls(host)
    c.s = FakeMagosDevice()
    if read_only:
        c.set_read_only()
    return c


MAGOS_MUTATORS = [
    ("set_ntp", lambda c: c.set_ntp("192.168.88.10")),
    ("set_channel", lambda c: c.set_channel("2")),
    ("set_network", lambda c: c.set_network("192.168.88.99/24", "192.168.88.1",
                                            "192.168.88.1")),
]

APU_MUTATORS = [
    ("set_ntp_tz", lambda c: c.set_ntp_tz("192.168.88.10", "Asia/Jerusalem")),
    ("set_radars", lambda c: c.set_radars([{"radar_id": "radar_0",
                                           "ip": "192.168.88.50",
                                           "name": "Radar 0"}])),
    ("set_network", lambda c: c.set_network("port1", "192.168.88.99",
                                           "255.255.255.0", "192.168.88.1",
                                           "192.168.88.1")),
]


@pytest.mark.parametrize("name,call", MAGOS_MUTATORS, ids=[n for n, _ in MAGOS_MUTATORS])
def test_every_radar_mutator_refuses_without_touching_the_device(name, call):
    c = magos_client(read_only=True)
    with pytest.raises(MutationBlocked):
        call(c)
    # Not "it was blocked" but "it never asked" — `set_ntp` reads the current
    # settings before writing them, and on a verify run even that must not
    # happen.
    assert c.s.requests == []


@pytest.mark.parametrize("name,call", APU_MUTATORS, ids=[n for n, _ in APU_MUTATORS])
def test_every_apu_mutator_refuses_without_touching_the_device(name, call):
    c = magos_client(APUClient, APU_HOST, read_only=True)
    with pytest.raises(MutationBlocked):
        call(c)
    assert c.s.requests == []


def test_the_rf_channel_is_refused_even_though_no_http_gate_sees_it():
    # `set_channel` pushes the variant over the /radar/v1/detections WebSocket.
    # The request gate cannot cover it, so it carries its own check — and this
    # is the test that notices if somebody removes it.
    c = magos_client(read_only=True)
    with pytest.raises(MutationBlocked, match="RF channel"):
        c.set_channel("2")


@pytest.mark.parametrize("cls,host", [(MagosClient, MAGOS_HOST),
                                      (APUClient, APU_HOST)])
def test_any_post_other_than_login_is_refused_at_the_request(cls, host):
    # The backstop, tested directly: a write added later is covered without
    # anybody remembering to guard it.
    c = magos_client(cls, host, read_only=True)
    with pytest.raises(MutationBlocked):
        c._post(f"{c.base}/some-endpoint-nobody-has-written-yet", json={})
    assert c.s.requests == []


@pytest.mark.parametrize("cls,host", [(MagosClient, MAGOS_HOST),
                                      (APUClient, APU_HOST)])
def test_logging_in_is_not_a_mutation(cls, host):
    # A session is not a device change, and refusing it would make the mode
    # unable to read anything at all.
    c = magos_client(cls, host, read_only=True)
    c.login("admin", SHARED)
    assert c.s.writes == []


@pytest.mark.parametrize("cls,host", [(MagosClient, MAGOS_HOST),
                                      (APUClient, APU_HOST)])
def test_a_read_only_magos_client_still_reads(cls, host):
    # A guard that blocked the reads alongside the writes would make the mode
    # useless while looking like it worked.
    c = magos_client(cls, host, read_only=True)
    assert c.get_system()["timezone"] == "Asia/Jerusalem"
    assert c.get_networking()["netInterfaces"]["port1"]["ip4Method"] == "manual"


@pytest.mark.parametrize("cls,host", [(MagosClient, MAGOS_HOST),
                                      (APUClient, APU_HOST)])
def test_read_only_is_off_by_default_on_the_magos_clients(cls, host):
    # A flag you had to turn OFF to provision a device would be the wrong way
    # round — the configure run is the normal case.
    assert magos_client(cls, host).read_only is False


MAGOS_SETTINGS = {"scheme": "http", "insecure": False, "username": "admin",
                  "password": SHARED, "ntp": "192.168.88.10",
                  "timezone": "Asia/Jerusalem", "netmask": "255.255.255.0",
                  "gateway": "192.168.88.1", "dns": "192.168.88.1",
                  "iface": "port1"}


def test_a_radar_verify_pass_sends_only_reads_and_a_login():
    # The strong guarantee, on the pass an operator actually presses: not "it
    # was blocked", but "it never asked".
    c = magos_client()
    magos.verify_radar(c, settings=MAGOS_SETTINGS,
                       resolve=lambda ident: ({"ip": MAGOS_HOST, "channel": "1"},
                                              None),
                       reached=MAGOS_HOST)
    assert c.s.writes == []
    assert [p for m, p in c.s.requests if m == "POST"] == ["/dshb/v1/login"]


def test_an_apu_verify_pass_sends_only_reads_and_a_login():
    c = magos_client(APUClient, APU_HOST)
    magos.verify_apu(c, settings=MAGOS_SETTINGS,
                     resolve=lambda ident: ({"ip": APU_HOST, "radars": []}, None),
                     reached=APU_HOST)
    assert c.s.writes == []
    assert [p for m, p in c.s.requests if m == "POST"] == ["/dshb/v1/login"]


@pytest.mark.parametrize("verify,cls,host", [
    (magos.verify_radar, MagosClient, MAGOS_HOST),
    (magos.verify_apu, APUClient, APU_HOST),
])
def test_a_magos_verify_pass_leaves_the_client_read_only(verify, cls, host):
    c = magos_client(cls, host)
    verify(c, settings=MAGOS_SETTINGS,
           resolve=lambda ident: ({"ip": host, "channel": "1", "radars": []}, None),
           reached=host)
    assert c.read_only is True
