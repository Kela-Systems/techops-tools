"""The name has to reach the RUNNING system, not just the config (TEC-348).

`set_hostname` writes two things — `system.system.hostname` and
`/proc/sys/kernel/hostname` — and the check used to read back only the first.
That is the same shape of fault as the TSW202 that shipped on a UTC clock with
four green rows: `uci set` creates an option whether or not anything consumes
it, so a read-back of your own write cannot fail.

The two halves are not redundant, and neither is sufficient:

  * the RUNNING name is what the device calls itself to syslog, to DHCP and to
    RMS, so it is the name an engineer will later search for. Wrong here and the
    unit is effectively nameless in every system that matters.
  * the STORED name is what survives a reboot. Wrong here and the unit is
    correct right now and reverts the next time it restarts.

These cover the shared client, so they hold for the RUTM08 and the OTD500 — the
two tools whose hostname the shared `verify_configuration` checks.
"""
import pytest

from bench_core import TeltonikaClient, expected_utc_offset

ZONE = "Asia/Jerusalem"
NAME = "otd-haifa"
SHARED = "Kelasys123!"
CORRECT_OFFSET = expected_utc_offset(ZONE) or "+0300"


class FakeDevice:
    """Stands in for `ssh_exec`. The running hostname is deliberately
    independent of the UCI option, because that independence IS the check: on a
    device where they are wired together by construction the row proves
    nothing."""

    def __init__(self, *, stored=NAME, running=NAME, has_hostname_applet=True):
        self.commands: list[str] = []
        self.stored = stored
        self.running = running
        # Not every OpenWrt build ships the `hostname` applet; the check falls
        # back to /proc, and must get the same answer either way.
        self.has_hostname_applet = has_hostname_applet

    def __call__(self, command, check=True, exec_timeout=None):
        self.commands.append(command)
        if command.startswith("date +%z"):
            return CORRECT_OFFSET
        if "system.system.zoneName" in command:
            return ZONE
        if "system.system.hostname" in command:
            return self.stored
        if command.startswith("hostname 2>/dev/null"):
            # The real command is `hostname || cat /proc/...`, so a build
            # without the applet still answers from the proc file.
            return self.running if self.has_hostname_applet else self.running
        return ""


def client(**kwargs) -> TeltonikaClient:
    c = TeltonikaClient(host="192.0.2.1")
    c.ssh_exec = FakeDevice(**kwargs)
    c.device = c.ssh_exec          # test handle
    c.password = SHARED
    return c


def hostname_row(checks) -> dict:
    return next(c for c in checks if c["item"] == "hostname")


def verify(c) -> list[dict]:
    return c.verify_configuration(hostname=NAME, zonename=ZONE,
                                  new_password=SHARED, sim_4g=False,
                                  rms=False, tailscale=False)


# ── the row, in isolation ────────────────────────────────────────────────────

def test_both_halves_right_passes():
    row = client().hostname_check(NAME)
    assert row["ok"] is True
    assert row["expected"] == NAME
    assert NAME in row["actual"]


def test_the_config_right_and_the_running_name_wrong_goes_red():
    # THE negative test: exactly the state the old read-back row called green.
    # `uci set system.system.hostname` took, the kernel write did not, and the
    # device answers to 'OTD500' everywhere that matters.
    row = client(stored=NAME, running="OTD500").hostname_check(NAME)
    assert row["ok"] is False
    assert "running OTD500" in row["actual"]
    assert f"stored {NAME}" in row["actual"]


def test_the_running_name_right_and_the_config_wrong_goes_red():
    # Correct today, nameless after the next reboot.
    row = client(stored="", running=NAME).hostname_check(NAME)
    assert row["ok"] is False
    assert "stored (unset)" in row["actual"]


def test_a_device_that_was_never_named_goes_red():
    row = client(stored="", running="").hostname_check(NAME)
    assert row["ok"] is False
    assert "running (unset), stored (unset)" == row["actual"]


def test_both_halves_wrong_the_same_way_still_goes_red():
    # A device carrying somebody else's name is self-consistent. Consistency is
    # not correctness, and this is what a mixed-up batch looks like.
    row = client(stored="otd-golan", running="otd-golan").hostname_check(NAME)
    assert row["ok"] is False
    assert "otd-golan" in row["actual"]


def test_a_build_without_the_hostname_applet_still_answers():
    c = client(has_hostname_applet=False)
    assert c.hostname_check(NAME)["ok"] is True
    assert any("/proc/sys/kernel/hostname" in cmd for cmd in c.device.commands)


def test_the_row_reads_and_never_writes():
    c = client()
    c.hostname_check(NAME)
    for command in c.device.commands:
        assert "uci set" not in command
        assert ">" not in command.replace("2>/dev/null", "")


# ── the row as the full verification sees it ─────────────────────────────────

def test_verify_configuration_uses_the_two_sided_check():
    assert hostname_row(verify(client()))["ok"] is True
    assert hostname_row(verify(client(running="OTD500")))["ok"] is False


def test_a_half_named_device_fails_the_whole_verification():
    # The point of converting the row: this device used to pass.
    checks = verify(client(running="OTD500"))
    assert [c["item"] for c in checks if c["ok"] is False] == ["hostname"]


# ── the writer's side, so the check keeps matching what it checks ────────────

def test_set_hostname_writes_both_halves():
    # If a later change drops one of these writes, the row above starts failing
    # every device and this test says which write went missing.
    c = client()
    c.set_hostname(NAME)
    assert any(f"uci set system.system.hostname={NAME}" in cmd
               for cmd in c.device.commands)
    assert any("/proc/sys/kernel/hostname" in cmd and "echo" in cmd
               for cmd in c.device.commands)
