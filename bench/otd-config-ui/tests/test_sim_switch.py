"""SIM-switch + per-operator data-limit provisioning (TEC-359).

No hardware: the device is a `FakeDevice` standing in for `ssh_exec`, so the
tests pin the exact UCI command strings and the rendered on-device script — the
things that actually decide whether a real OTD500 ends up with the rule set that
was validated on the bench (FW 07.22.3, re-validated on 07.24.3).
"""
import logging
import re
import shlex

import pytest

from bench_core import (
    QUOTA_SYNC_CRON,
    QUOTA_SYNC_INIT_PATH,
    QUOTA_SYNC_PATH,
    QUOTA_SYNC_RC_LINK,
    SYSUPGRADE_CONF,
    TeltonikaClient,
    VERIFIED_SIM_SWITCH_FW,
    icmp_host,
    render_quota_sync_script,
)

# The config block as it ships in site.config.example.json.
SIM_SWITCH_CFG = {
    "enabled": True,
    "check_interval": 30,
    "check_count": 5,
    "weak_signal_dbm": -105,
    "icmp_host": "8.8.8.8",
    "operators": [
        {"name": "partner", "iccid_prefixes": ["8997201"], "mccmnc": "425-01",
         "data_limit_mb": 3000000, "reset_day": 1, "enabled": False},
        {"name": "cellcom", "iccid_prefixes": ["8997202"], "mccmnc": "425-02",
         "data_limit_mb": 1330000, "reset_day": 15, "enabled": True},
        {"name": "pelephone", "iccid_prefixes": ["8997250"], "mccmnc": "425-03",
         "data_limit_mb": 2660000, "reset_day": 1, "enabled": True},
    ],
    "unknown_operator": {"data_limit_mb": 1330000, "reset_day": 1, "enabled": True},
}

# The per-slot rule set dumped off the bench OTD500, in the order it is written.
# Hard-coded on purpose: this is the ground truth the code has to reproduce, so
# it must not be derived from the code under test.
SLOT_RULES = (
    ("modem", "2-1"), ("position", "{slot}"), ("order", "{slot}"), ("enabled", "1"),
    ("interval", "30"), ("retry_count", "5"), ("on_signal", "1"),
    ("weak_signal", "-105"), ("data_limit", "1"), ("sms_limit", "0"),
    ("roaming", "0"), ("no_network", "1"), ("denied", "1"), ("sim_not_ready", "1"),
    ("data_fail", "2"), ("data_fail_host", "8.8.8.8"), ("data_fail_timeout", "3"),
    ("enable_back", "0"), ("fail_flag", "1"),
)
# The eSIM slot: present, and switched off.
ESIM_RULES = (("modem", "2-1"), ("position", "3"), ("order", "3"), ("enabled", "0"))

FW_VERIFIED = f"OTD5_R_00.{VERIFIED_SIM_SWITCH_FW[-1]}"


class FakeDevice:
    """Stands in for `TeltonikaClient.ssh_exec`: canned answers for the reads,
    a recording of every command for the writes."""

    def __init__(self, *, version=FW_VERIFIED, slots=(1, 2, 3), uci=None,
                 installed="", cron="", write_error="", keep=""):
        self.commands: list[str] = []
        self.version = version
        self.slots = slots            # slots the device already has a section for
        self.uci = {"simcard.@sim[0].modem": "2-1", **(uci or {})}
        self.installed = installed    # what the `test -x` probe reports
        self.cron = cron
        self.write_error = write_error  # what a refused file write reports
        self.keep = keep              # what /etc/sysupgrade.conf lists

    def section(self, slot: int) -> str:
        """How the device addresses that slot's (anonymous) sim_switch section."""
        return f"@sim[{self.slots.index(slot)}]"

    def __call__(self, command, check=True, exec_timeout=None):
        self.commands.append(command)
        if command.endswith("2>&1 && echo __OK__"):    # a _put_file write
            return self.write_error or "__OK__"
        if command.startswith("cat /etc/version"):
            return self.version
        if command.startswith("uci show sim_switch"):
            # One `uci show` now carries everything configure/verify read:
            # section list, positions, and whatever options `self.uci` holds.
            lines = []
            for i, slot in enumerate(self.slots):
                prefix = f"sim_switch.@sim[{i}]."
                lines.append(f"sim_switch.@sim[{i}]=sim")
                lines.append(f"{prefix}position='{slot}'")
                lines += [f"{k}='{v}'" for k, v in self.uci.items()
                          if k.startswith(prefix) and not k.endswith(".position")]
            return "\n".join(lines)
        if command.startswith("uci add sim_switch"):
            return "cfg0492bd"
        read = re.match(r"^uci -q get (\S+)$", command)
        if read:
            return self.uci.get(shlex.split(read.group(1))[0], "")
        if command.startswith("[ -x "):
            return self.installed
        if command.startswith("grep -F kela-quota-sync"):
            return self.cron
        if command.startswith("grep -v '^#' /etc/sysupgrade.conf"):
            return self.keep
        return ""


def fake_client(**kwargs) -> tuple[TeltonikaClient, FakeDevice]:
    client = TeltonikaClient(host="192.0.2.1")
    device = FakeDevice(**kwargs)
    client.ssh_exec = device
    return client, device


def expected_uci(device: FakeDevice, slot: int, rules) -> list[str]:
    section = f"sim_switch.{device.section(slot)}"
    return [f"uci set {shlex.quote(f'{section}.{o}={v.format(slot=slot)}')}"
            for o, v in rules]


def uci_set_command(device: FakeDevice) -> str:
    return next(c for c in device.commands if c.startswith("uci set "))


# ── the sim_switch rules ─────────────────────────────────────────────────────

def test_configure_writes_the_validated_rules_and_commits_once():
    client, device = fake_client()
    client.configure_sim_switch(SIM_SWITCH_CFG)

    assert uci_set_command(device) == " && ".join(
        expected_uci(device, 1, SLOT_RULES)
        + expected_uci(device, 2, SLOT_RULES)
        + expected_uci(device, 3, ESIM_RULES)
        + ["uci commit sim_switch"])
    # The rules are applied by restarting the service — which does NOT bounce the
    # modem, so nothing here needs a SIM or a data connection.
    assert device.commands[-1] == "/etc/init.d/sim_switch restart"
    assert not any("gsmctl" in c or "ubus" in c for c in device.commands)
    assert not any("primary" in c for c in device.commands)


def test_esim_slot_stays_disabled():
    client, device = fake_client()
    client.configure_sim_switch(SIM_SWITCH_CFG)

    esim = [arg for arg in uci_set_command(device).split(" && ")
            if device.section(3) in arg]
    assert esim == expected_uci(device, 3, ESIM_RULES)
    # No failover conditions on the eSIM slot: it is off, not off-and-configured.
    assert not any("interval" in arg or "data_fail" in arg for arg in esim)


def test_config_overrides_the_tunable_conditions():
    client, device = fake_client()
    client.configure_sim_switch({**SIM_SWITCH_CFG, "check_interval": 60,
                                 "check_count": 3, "weak_signal_dbm": -110,
                                 "icmp_host": "10.8.0.1"})
    command = uci_set_command(device)
    for option, value in (("interval", "60"), ("retry_count", "3"),
                          ("weak_signal", "-110"), ("data_fail_host", "10.8.0.1")):
        assert f".{option}={value}'" in command
    # Sticky failover is policy, not a per-site setting.
    assert ".enable_back=0'" in command


@pytest.mark.parametrize("host", ["8.8.8.8", "10.8.0.1", "2001:4860:4860::8888",
                                  "one.one.one.one", "ping.example.co.uk",
                                  "  8.8.4.4  "])
def test_a_usable_icmp_host_is_accepted(host):
    assert icmp_host({"icmp_host": host}) == host.strip()


@pytest.mark.parametrize("host", ["8.8.8.8,", "8.8.8.8.", "8.8.8", "999.999.999.999",
                                  "8.8.8.8 8.8.4.4", "8.8.8.8/32", "-bad.example.com",
                                  "exa mple.com", "8.8.8.8;reboot"])
def test_a_mistyped_icmp_host_is_rejected(host):
    # It lands in data_fail_host, the address the device pings to decide the
    # active SIM has no data. A typo there is invisible on the device: the check
    # just never succeeds, and the SIM is counted as failed on every interval.
    with pytest.raises(SystemExit, match="icmp_host"):
        icmp_host({"icmp_host": host})


def test_an_unset_icmp_host_falls_back_to_the_default():
    assert icmp_host({}) == icmp_host({"icmp_host": ""}) == "8.8.8.8"


def test_missing_slot_section_is_added():
    client, device = fake_client(slots=(1, 2))   # no eSIM section on this device
    client.configure_sim_switch(SIM_SWITCH_CFG)

    assert "uci add sim_switch sim" in device.commands
    assert "uci set sim_switch.cfg0492bd.enabled=0" in uci_set_command(device)


def test_leftover_duplicate_section_is_disabled():
    # Two sections claiming position 1 (a leftover from an interrupted run):
    # the unclaimed one is switched off in the same commit — left enabled it
    # would keep failing over with whatever stale rules it still carries.
    client, device = fake_client(slots=(1, 1, 2, 3))
    client.configure_sim_switch(SIM_SWITCH_CFG)
    assert "uci set 'sim_switch.@sim[1].enabled=0'" in uci_set_command(device)


def test_modem_id_comes_from_the_device():
    client, device = fake_client(uci={"simcard.@sim[0].modem": "3-1"})
    client.configure_sim_switch(SIM_SWITCH_CFG)
    assert "uci set 'sim_switch.@sim[0].modem=3-1'" in uci_set_command(device)


@pytest.fixture
def teltonika_caplog(caplog, monkeypatch):
    """caplog listening on the 'teltonika' logger regardless of test order:
    importing otd_app (test_otd_app.py) attaches the UI log handler and sets
    propagate=False on that logger, which would hide its records from caplog's
    root-logger handler for the rest of the session."""
    monkeypatch.setattr(logging.getLogger("teltonika"), "propagate", True)
    with caplog.at_level(logging.WARNING, logger="teltonika"):
        yield caplog


def test_unverified_firmware_warns_but_still_configures(teltonika_caplog):
    # 07.24.1 sits BETWEEN the two verified releases: a near miss on the last
    # segment has to warn, or the guard would wave through any 07.24.x.
    client, device = fake_client(version="OTD5_R_00.07.24.1")
    client.configure_sim_switch(SIM_SWITCH_CFG)
    assert "uci export sim_switch" in teltonika_caplog.text
    # Every verified version is named, so the operator can see what the device
    # would have had to be running to skip the warning.
    for version in VERIFIED_SIM_SWITCH_FW:
        assert version in teltonika_caplog.text
    assert uci_set_command(device)          # the warning does not stop the step


@pytest.mark.parametrize("version", VERIFIED_SIM_SWITCH_FW)
def test_every_verified_firmware_does_not_warn(teltonika_caplog, version):
    # Both the outgoing standard (07.22.3) and the incoming one (07.24.3) are
    # verified, and devices arrive on both while TEC-861's upgrade campaign
    # runs — warning on either would be noise the operator learns to ignore.
    client, _ = fake_client(version=f"OTD5_R_00.{version}")
    client.configure_sim_switch(SIM_SWITCH_CFG)
    assert teltonika_caplog.text == ""


# ── the on-device operator→quota script ──────────────────────────────────────

def test_script_embeds_the_operator_table():
    script = render_quota_sync_script(SIM_SWITCH_CFG)
    # "<name> <data_limit_mb> <reset_day> <enabled>" per ICCID prefix.
    assert "8997201*) echo 'partner 3000000 1 0' ;;" in script
    assert "8997202*) echo 'cellcom 1330000 15 1' ;;" in script
    assert "8997250*) echo 'pelephone 2660000 1 1' ;;" in script
    # An unrecognised SIM (or empty slot) falls back to the conservative limit.
    assert "*) echo 'unknown 1330000 1 1' ;;" in script
    assert "SLOTS='1 2'" in script and "PERIOD='3'" in script
    # Detection is by ICCID (readable on inactive slots), never by IMSI — which
    # would mean asking the modem about the active SIM.
    assert 'uci -q get "simcard.@sim[$index].iccid"' in script
    assert ".imsi" not in script and "gsmctl" not in script
    # event_sent is quota_limit's runtime counter: named in a comment saying so,
    # never written.
    assert "event_sent" in script and "$section.event_sent" not in script


def test_script_is_idempotent_and_only_restarts_on_change():
    script = render_quota_sync_script(SIM_SWITCH_CFG)
    assert 'cur=$(uci -q get "quota_limit.$1.$2")' in script
    assert '[ "$cur" = "$3" ] && return 0' in script
    assert script.count("uci commit quota_limit") == 1
    commit = script.index("uci commit quota_limit")
    assert script.index('if [ "$changed" = 1 ]; then') < commit


def test_script_resolves_the_slot_by_simcard_position_not_file_order():
    script = render_quota_sync_script(SIM_SWITCH_CFG)
    # The simcard section for a slot is matched by its `position` option, not
    # by assuming the sections appear in slot order.
    assert "index_for() {" in script
    assert "position='$1'" in script
    assert 'index=$(index_for "$slot")' in script


def test_script_takes_a_lock_so_boot_and_cron_runs_cannot_interleave():
    script = render_quota_sync_script(SIM_SWITCH_CFG)
    assert 'mkdir "$LOCK" 2>/dev/null || exit 0' in script
    assert "trap 'rmdir \"$LOCK\"' EXIT" in script


def test_bad_iccid_prefix_is_rejected():
    with pytest.raises(SystemExit, match="iccid_prefixes"):
        render_quota_sync_script({**SIM_SWITCH_CFG, "operators": [
            {"name": "typo", "iccid_prefixes": ["8997 202"], "data_limit_mb": 1,
             "reset_day": 1, "enabled": True}]})


def test_enabled_operator_without_a_limit_is_rejected():
    # data_limit_mb defaulting to 0 on an enabled operator would write a 0 MB
    # limit to the device — data cut on the first sync.
    with pytest.raises(SystemExit, match="data_limit_mb"):
        render_quota_sync_script({**SIM_SWITCH_CFG, "operators": [
            {"name": "nolimit", "iccid_prefixes": ["8991"], "reset_day": 1,
             "enabled": True}]})


def test_non_numeric_limit_is_a_step_failure_not_a_crash():
    # A bare int() would raise ValueError, which escapes the step runner (it
    # catches SystemExit only) and takes down the whole run.
    with pytest.raises(SystemExit, match="whole number"):
        render_quota_sync_script({**SIM_SWITCH_CFG, "operators": [
            {"name": "typo", "iccid_prefixes": ["8991"], "data_limit_mb": "1,330,000",
             "reset_day": 1, "enabled": True}]})


def test_install_deploys_the_script_boot_hook_and_cron(monkeypatch):
    client, device = fake_client()
    files: dict[str, str] = {}
    monkeypatch.setattr(client, "_put_file",
                        lambda path, content, mode="644": files.update({path: mode}))

    client.install_quota_sync(SIM_SWITCH_CFG)

    assert files == {QUOTA_SYNC_PATH: "755", QUOTA_SYNC_INIT_PATH: "755"}
    assert f"{QUOTA_SYNC_INIT_PATH} enable" in device.commands
    cron = next(c for c in device.commands if "crontabs" in c)
    # Any earlier entry is dropped first, so a changed schedule replaces it
    # instead of running alongside it.
    assert "sed -i '/kela-quota-sync/d' /etc/crontabs/root" in cron
    assert f"echo {shlex.quote(QUOTA_SYNC_CRON)}" in cron
    # ... and the limits are written before the device leaves the bench.
    assert device.commands[-1] == QUOTA_SYNC_PATH


def test_boot_hook_and_cron_run_the_same_path():
    from bench_core import QUOTA_SYNC_INIT
    assert QUOTA_SYNC_PATH in QUOTA_SYNC_INIT
    assert QUOTA_SYNC_PATH in QUOTA_SYNC_CRON


def test_install_lists_the_files_in_the_upgrade_keep_list(monkeypatch):
    # /etc/crontabs is in RutOS's own keep list but /usr/local and /etc/init.d
    # are not, so without this a keep-settings upgrade leaves the cron entry
    # calling a script that no longer exists — limits frozen, silently.
    client, device = fake_client()
    monkeypatch.setattr(client, "_put_file", lambda *a, **k: None)

    client.install_quota_sync(SIM_SWITCH_CFG)

    keep = next(c for c in device.commands if "sysupgrade.conf" in c)
    for path in (QUOTA_SYNC_PATH, QUOTA_SYNC_INIT_PATH, QUOTA_SYNC_RC_LINK):
        assert f"\n{path}\n" in keep
    # sysupgrade.conf lists ITSELF: it is not in RutOS's keep list either, so
    # otherwise the block would survive one upgrade and be gone for the next.
    assert f"\n{SYSUPGRADE_CONF}\n" in keep
    # Our previous block is dropped first, so re-running adds nothing.
    assert "sed -i -e '\\,kela-quota-sync,d'" in keep
    assert "-e '\\,^/etc/sysupgrade.conf$,d'" in keep


def keep_block(command: str) -> list[str]:
    """The lines the keep-list command appends, out of its heredoc."""
    eof = TeltonikaClient.PUT_FILE_EOF
    body = command.split(f"<<'{eof}'\n", 1)[1].split(f"\n{eof}", 1)[0]
    return [line for line in body.splitlines() if line.strip()]


def test_every_line_of_the_keep_block_is_one_the_cleanup_can_find_again(monkeypatch):
    # The cleanup sed matches on the script's name, so a line written WITHOUT it
    # in it is never removed: one orphan per re-provision, forever. Comment
    # lines are the easy ones to get wrong.
    client, device = fake_client()
    monkeypatch.setattr(client, "_put_file", lambda *a, **k: None)
    client.install_quota_sync(SIM_SWITCH_CFG)

    command = next(c for c in device.commands if "sysupgrade.conf" in c)
    patterns = re.findall(r"-e '\\,(.+?),d'", command)
    assert patterns, "expected the command to delete its old lines first"
    for line in keep_block(command):
        assert any(re.search(p, line) for p in patterns), \
            f"{line!r} would survive the cleanup and accumulate on re-runs"


def test_re_provisioning_does_not_grow_the_keep_list(monkeypatch):
    # The whole point of the pattern check above, end to end: applying the same
    # command twice must leave the file exactly as one run does.
    client, device = fake_client()
    monkeypatch.setattr(client, "_put_file", lambda *a, **k: None)
    client.install_quota_sync(SIM_SWITCH_CFG)
    command = next(c for c in device.commands if "sysupgrade.conf" in c)
    patterns = re.findall(r"-e '\\,(.+?),d'", command)
    block = keep_block(command)

    def install(conf: list[str]) -> list[str]:
        return [ln for ln in conf
                if not any(re.search(p, ln) for p in patterns)] + block

    stock = ["## This file contains files and directories that should",
             "## be preserved during an upgrade.", "# /etc/example.conf"]
    once = install(stock)
    assert install(once) == once
    # ... and the operator's own lines are still there.
    assert once[:len(stock)] == stock


def test_the_script_re_enables_its_own_boot_hook_when_the_symlink_is_gone():
    # A keep-settings upgrade restores the files in /etc/sysupgrade.conf but
    # regenerates /etc/rc.d, so the init script comes back and its enable-state
    # does not (TEC-861, kela-fob-18-otd). The bench can't fix that: the device
    # is upgraded in the field, long after provisioning. Cron runs this script
    # every 10 minutes, which makes it the only thing on the device positioned
    # to notice — so it repairs the hook itself rather than just degrading.
    script = render_quota_sync_script(SIM_SWITCH_CFG)

    assert f"[ ! -e {QUOTA_SYNC_RC_LINK} ]" in script
    assert f"{QUOTA_SYNC_INIT_PATH} enable" in script
    # Guarded by the init script still being there: `enable` on a device where
    # the file itself is gone would fail every 10 minutes and log noise.
    assert f"[ -x {QUOTA_SYNC_INIT_PATH} ]" in script
    # Inside the lock, so a boot-time run and a cron tick can't both enable.
    assert script.index('mkdir "$LOCK"') < script.index(f"[ ! -e {QUOTA_SYNC_RC_LINK} ]")


def test_the_rc_symlink_matches_the_boot_hooks_priority():
    # The keep list names the rc.d symlink literally; if START ever changes,
    # `enable` writes S<new> and the kept path would point at nothing.
    from bench_core import QUOTA_SYNC_INIT, QUOTA_SYNC_START
    assert f"START={QUOTA_SYNC_START}\n" in QUOTA_SYNC_INIT
    assert QUOTA_SYNC_RC_LINK == f"/etc/rc.d/S{QUOTA_SYNC_START}kela-quota-sync"


def test_files_go_over_the_exec_channel_and_land_atomically():
    # NOT over SFTP: RutOS's sftp-server reports every refused open as a bare
    # "Failure", so a read-only or full filesystem is indistinguishable — and
    # some firmware ships no sftp subsystem at all.
    client, device = fake_client()
    client._put_file("/usr/local/bin/kela-quota-sync", "#!/bin/sh\nexit 0\n", mode="755")

    write, move = device.commands
    # A QUOTED delimiter: the script is almost entirely $variables, and the
    # device's shell must expand none of them.
    # The parent directory is created first: /usr/local/bin exists on stock
    # firmware, but assuming it is one more way for this step to fail.
    assert ("mkdir -p /usr/local/bin && "
            "cat > /usr/local/bin/kela-quota-sync.new <<'__KELA_BENCH_EOF__'\n") in write
    assert write.count("__KELA_BENCH_EOF__") == 2
    assert "#!/bin/sh\nexit 0\n__KELA_BENCH_EOF__" in write
    # Executable before it has the real name, so cron can never run a partial file.
    assert ("chmod 755 /usr/local/bin/kela-quota-sync.new && "
            "mv /usr/local/bin/kela-quota-sync.new /usr/local/bin/kela-quota-sync") in move


def test_a_refused_write_reports_the_devices_own_error():
    client, _ = fake_client(
        write_error="cat: can't create '/usr/bin/x.new': Read-only file system")
    with pytest.raises(SystemExit) as err:
        client._put_file("/usr/bin/x", "hi")
    # SystemExit is what the step runner catches, so a device that won't take the
    # file fails THIS step instead of aborting the run — and says why, rather
    # than passing paramiko's bare "Failure" on to the operator.
    assert "Could not write /usr/bin/x on the device" in str(err.value)
    assert "Read-only file system" in str(err.value)


def test_content_carrying_the_delimiter_is_refused():
    client, device = fake_client()
    with pytest.raises(SystemExit, match="delimiter"):
        client._put_file("/usr/bin/x", f"echo {TeltonikaClient.PUT_FILE_EOF}\n")
    assert device.commands == []   # nothing half-written on the device


# ── verification ─────────────────────────────────────────────────────────────

VERIFIED_OPTIONS = ("enabled", "interval", "retry_count", "weak_signal",
                    "enable_back", "data_fail_host")
GOOD_SLOT = {"enabled": "1", "interval": "30", "retry_count": "5",
             "weak_signal": "-105", "enable_back": "0", "data_fail_host": "8.8.8.8"}


def device_uci(slot_values: dict) -> dict:
    return {f"sim_switch.@sim[{slot - 1}].{option}": value
            for slot, values in slot_values.items()
            for option, value in values.items()}


def verify(**kwargs) -> dict:
    client, _ = fake_client(**kwargs)
    rows = client._verify_sim_switch(SIM_SWITCH_CFG)
    return {row["item"]: row for row in rows}


ALL_KEPT = "\n".join((SYSUPGRADE_CONF, QUOTA_SYNC_PATH, QUOTA_SYNC_INIT_PATH,
                      QUOTA_SYNC_RC_LINK))
# What the `test -x`/`test -e` probe reports on a correctly installed device:
# the script, its init script, AND the rc.d symlink that runs it at boot.
FULLY_INSTALLED = "script\nboot-hook\nenabled"


def test_verification_pairs_expected_with_what_the_device_reports():
    rows = verify(uci=device_uci({1: GOOD_SLOT, 2: {**GOOD_SLOT, "weak_signal": "-50"}}),
                  installed=FULLY_INSTALLED, cron=QUOTA_SYNC_CRON, keep=ALL_KEPT)

    expected = " ".join(f"{o}={GOOD_SLOT[o]}" for o in VERIFIED_OPTIONS)
    assert rows["SIM switch slot 1"]["expected"] == expected
    assert rows["SIM switch slot 1"]["actual"] == expected
    assert rows["SIM switch slot 1"]["ok"] is True

    # A single drifted option fails the slot and shows the value it drifted to.
    assert rows["SIM switch slot 2"]["expected"] == expected
    assert "weak_signal=-50" in rows["SIM switch slot 2"]["actual"]
    assert rows["SIM switch slot 2"]["ok"] is False

    assert rows["quota sync script"]["ok"] is True
    assert rows["quota sync cron"]["ok"] is True
    assert rows["quota sync survives upgrade"]["ok"] is True
    assert rows["quota sync survives upgrade"]["actual"] == "all listed"


def test_verification_flags_a_missing_script_and_cron_entry():
    rows = verify(uci=device_uci({1: GOOD_SLOT, 2: GOOD_SLOT}), installed="script")
    assert rows["quota sync script"]["ok"] is False        # boot hook missing
    assert rows["quota sync script"]["actual"] == "script"
    assert rows["quota sync cron"]["ok"] is False
    assert rows["quota sync cron"]["actual"] == "(no cron entry)"


def test_verification_flags_a_boot_hook_that_is_present_but_not_enabled():
    # The post-upgrade state seen in the field (TEC-861): a keep-settings
    # upgrade restores the files listed in /etc/sysupgrade.conf but regenerates
    # /etc/rc.d, so the init script is there and the symlink that runs it is
    # not. Both files present used to be enough to pass this row, which made
    # the one check that could have caught it report green.
    rows = verify(uci=device_uci({1: GOOD_SLOT, 2: GOOD_SLOT}),
                  installed="script\nboot-hook", cron=QUOTA_SYNC_CRON, keep=ALL_KEPT)
    row = rows["quota sync script"]
    assert row["ok"] is False
    assert row["actual"] == "script + boot-hook"           # 'enabled' absent
    assert "enabled" in row["expected"]
    # The rest of the feature is fine — only the boot hook row may fail, or the
    # operator cannot tell this apart from a device that never got provisioned.
    assert rows["quota sync cron"]["ok"] is True
    assert rows["quota sync survives upgrade"]["ok"] is True


def test_verification_names_the_paths_an_upgrade_would_drop():
    # A partial keep list is the case worth naming: the operator has to know
    # WHICH file a firmware upgrade would take away.
    rows = verify(uci=device_uci({1: GOOD_SLOT, 2: GOOD_SLOT}),
                  installed=FULLY_INSTALLED, cron=QUOTA_SYNC_CRON,
                  keep=f"{SYSUPGRADE_CONF}\n{QUOTA_SYNC_PATH}")
    row = rows["quota sync survives upgrade"]
    assert row["ok"] is False
    assert QUOTA_SYNC_INIT_PATH in row["actual"] and QUOTA_SYNC_RC_LINK in row["actual"]
    assert QUOTA_SYNC_PATH not in row["actual"]


def test_unset_options_read_as_absent_not_matching():
    rows = verify()   # device knows nothing about sim_switch options
    assert rows["SIM switch slot 1"]["ok"] is False
    assert rows["SIM switch slot 1"]["actual"] == " ".join(
        f"{o}=-" for o in VERIFIED_OPTIONS)


def test_disabled_sim_switch_is_skipped_not_failed():
    client, device = fake_client()
    rows = client._verify_sim_switch({"enabled": False})
    assert [r["item"] for r in rows] == ["SIM switch slot 1", "SIM switch slot 2",
                                         "quota sync script", "quota sync cron",
                                         "quota sync survives upgrade"]
    assert all(r["ok"] is None for r in rows)
    assert device.commands == []   # a skipped feature asks the device nothing


# ── pipeline wiring ──────────────────────────────────────────────────────────

SETTINGS = {"timezone": "Asia/Jerusalem", "sim_4g_only": True,
            "sim_switch": SIM_SWITCH_CFG, "firmware": {"mode": "none"},
            "rms": {"enabled": False}, "tailscale": {"enabled": False},
            "esim": {"enabled": False}}


# `run_pipeline` and `stub_client` are the shared OTD pipeline harness, in
# conftest.py — `test_otd_configure.py` drives the same pipeline for the time
# source and needs the identical stand-in.


def test_pipeline_configures_the_switch_before_the_modem_bounce(run_pipeline):
    client = run_pipeline(SETTINGS)
    # The switch rules are pure UCI, written before sim-4g-only — the one step
    # that re-attaches the modem. The quota-sync FILES deploy later (see the
    # firmware-ordering test below).
    assert client.calls.index("configure_sim_switch") \
        < client.calls.index("set_sims_4g_only") \
        < client.calls.index("install_quota_sync")
    assert client.verified_with["sim_switch"] == SIM_SWITCH_CFG


def test_pipeline_installs_quota_sync_after_a_firmware_flash(run_pipeline):
    # A keep-settings sysupgrade preserves /etc/config (the sim_switch UCI is
    # safe) but wipes /usr/bin and /etc/init.d — deploying the quota script
    # before the flash would delete it right after installing it.
    client = run_pipeline({**SETTINGS, "firmware": {"mode": "local"}})
    assert client.calls.index("upgrade_firmware") \
        < client.calls.index("install_quota_sync")


def test_pipeline_rejects_a_bad_operator_table_before_touching_the_device(
        stub_client):
    import otd_configure
    settings = {**SETTINGS, "sim_switch": {**SIM_SWITCH_CFG, "operators": [
        {"name": "typo", "iccid_prefixes": ["8997 202"], "data_limit_mb": 1,
         "reset_day": 1, "enabled": True}]}}
    with pytest.raises(SystemExit, match="iccid_prefixes"):
        otd_configure.configure_device(stub_client, label_password="pw",
                                       site_name="haifa", settings=settings)
    assert stub_client.calls == []   # validated before login, let alone UCI


def test_pipeline_rejects_a_mistyped_icmp_host_before_touching_the_device(
        stub_client):
    import otd_configure
    settings = {**SETTINGS,
                "sim_switch": {**SIM_SWITCH_CFG, "icmp_host": "8.8.8.8,"}}
    with pytest.raises(SystemExit, match="icmp_host"):
        otd_configure.configure_device(stub_client, label_password="pw",
                                       site_name="haifa", settings=settings)
    assert stub_client.calls == []


def test_a_device_that_refuses_the_file_fails_one_step_not_the_run(stub_client):
    import otd_configure

    def refuse(cfg):
        stub_client.calls.append("install_quota_sync")
        raise SystemExit("Could not write /usr/local/bin/kela-quota-sync on "
                         "the device: Read-only file system")

    stub_client.install_quota_sync = refuse
    result = otd_configure.configure_device(stub_client, label_password="pw",
                                            site_name="haifa", settings=SETTINGS)
    assert result["ok"] is False
    assert len(result["failures"]) == 1
    assert result["failures"][0].startswith("quota-sync: Could not write")
    # The run carried on: the operator gets the verification table and the other
    # steps, not a run that stopped at the first file the device wouldn't take.
    assert "set_sims_4g_only" in stub_client.calls
    assert stub_client.verified_with["sim_switch"] == SIM_SWITCH_CFG


def test_pipeline_skips_the_sim_steps_when_disabled(run_pipeline):
    client = run_pipeline({**SETTINGS, "sim_switch": {"enabled": False}})
    assert "configure_sim_switch" not in client.calls
    assert "install_quota_sync" not in client.calls
    # Still reported to verification, so the table shows it as skipped.
    assert client.verified_with["sim_switch"] == {"enabled": False}


def test_pipeline_without_a_sim_switch_block_still_runs(run_pipeline):
    settings = {k: v for k, v in SETTINGS.items() if k != "sim_switch"}
    client = run_pipeline(settings)
    assert "configure_sim_switch" not in client.calls
    assert client.verified_with["sim_switch"] == {}
