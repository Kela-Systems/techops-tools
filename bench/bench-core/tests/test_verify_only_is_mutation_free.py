"""A verify-only run must not change the device (bench_core, TEC-348).

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
"""
import pytest

from bench_core import (
    MutationBlocked,
    TeltonikaClient,
    _MUTATING_COMMANDS,
)

SHARED = "Kelasys123!"


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
    "[ -x /sbin/quota-sync ] && echo script; [ -x /etc/init.d/quota-sync ] && echo boot-hook",
    "ps w 2>/dev/null | grep '[n]tpd'",
    "tr -d '\\000' < /proc/device-tree/model 2>/dev/null",
]


@pytest.mark.parametrize("command", ALLOWED_READS)
def test_a_read_the_checks_make_is_not_mistaken_for_a_write(command):
    assert _MUTATING_COMMANDS.search(command) is None, command


# Copied from the configure pipelines — every one of these must be caught.
BLOCKED_WRITES = [
    "uci set system.system.hostname='otd-haifa' && uci commit system",
    "uci set 'simcard.@sim[0].service=lte'",
    "uci -q delete system.ntp.server",
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
    "echo 'root:Kelasys123!' | chpasswd",
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


def test_mutation_blocked_is_not_absorbed_by_a_step_runner():
    # Pipelines catch SystemExit everywhere to record a soft step failure. A
    # blocked mutation is a bug in the verify pipeline, not a device problem,
    # and must not be reported as one red row among many.
    assert not issubclass(MutationBlocked, SystemExit)
