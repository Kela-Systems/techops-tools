"""SIM-switch + per-operator data-limit provisioning (TEC-359).

No hardware: the device is a `FakeDevice` standing in for `ssh_exec`, so the
tests pin the exact UCI command strings and the rendered on-device script — the
things that actually decide whether a real OTD500 ends up with the rule set that
was validated on the bench (FW 07.22.3).
"""
import logging
import re
import shlex

import pytest

from bench_core import (
    QUOTA_SYNC_CRON,
    QUOTA_SYNC_INIT_PATH,
    QUOTA_SYNC_PATH,
    TeltonikaClient,
    VERIFIED_SIM_SWITCH_FW,
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

FW_VERIFIED = f"OTD5_R_00.{VERIFIED_SIM_SWITCH_FW}"


class FakeDevice:
    """Stands in for `TeltonikaClient.ssh_exec`: canned answers for the reads,
    a recording of every command for the writes."""

    def __init__(self, *, version=FW_VERIFIED, slots=(1, 2, 3), uci=None,
                 installed="", cron=""):
        self.commands: list[str] = []
        self.version = version
        self.slots = slots            # slots the device already has a section for
        self.uci = {"simcard.@sim[0].modem": "2-1", **(uci or {})}
        self.installed = installed    # what the `test -x` probe reports
        self.cron = cron

    def section(self, slot: int) -> str:
        """How the device addresses that slot's (anonymous) sim_switch section."""
        return f"@sim[{self.slots.index(slot)}]"

    def __call__(self, command, check=True, exec_timeout=None):
        self.commands.append(command)
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
    client, device = fake_client(version="OTD5_R_00.07.24.1")
    client.configure_sim_switch(SIM_SWITCH_CFG)
    assert "uci export sim_switch" in teltonika_caplog.text
    assert VERIFIED_SIM_SWITCH_FW in teltonika_caplog.text
    assert uci_set_command(device)          # the warning does not stop the step


def test_verified_firmware_does_not_warn(teltonika_caplog):
    client, _ = fake_client()
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


def test_verification_pairs_expected_with_what_the_device_reports():
    rows = verify(uci=device_uci({1: GOOD_SLOT, 2: {**GOOD_SLOT, "weak_signal": "-50"}}),
                  installed="script\nboot-hook", cron=QUOTA_SYNC_CRON)

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


def test_verification_flags_a_missing_script_and_cron_entry():
    rows = verify(uci=device_uci({1: GOOD_SLOT, 2: GOOD_SLOT}), installed="script")
    assert rows["quota sync script"]["ok"] is False        # boot hook missing
    assert rows["quota sync script"]["actual"] == "script"
    assert rows["quota sync cron"]["ok"] is False
    assert rows["quota sync cron"]["actual"] == "(no cron entry)"


def test_unset_options_read_as_absent_not_matching():
    rows = verify()   # device knows nothing about sim_switch options
    assert rows["SIM switch slot 1"]["ok"] is False
    assert rows["SIM switch slot 1"]["actual"] == " ".join(
        f"{o}=-" for o in VERIFIED_OPTIONS)


def test_disabled_sim_switch_is_skipped_not_failed():
    client, device = fake_client()
    rows = client._verify_sim_switch({"enabled": False})
    assert [r["item"] for r in rows] == ["SIM switch slot 1", "SIM switch slot 2",
                                         "quota sync script", "quota sync cron"]
    assert all(r["ok"] is None for r in rows)
    assert device.commands == []   # a skipped feature asks the device nothing


# ── pipeline wiring ──────────────────────────────────────────────────────────

SETTINGS = {"timezone": "Asia/Jerusalem", "sim_4g_only": True,
            "sim_switch": SIM_SWITCH_CFG, "firmware": {"mode": "none"},
            "rms": {"enabled": False}, "tailscale": {"enabled": False},
            "esim": {"enabled": False}}


class StubClient:
    """Records what the pipeline calls. Enough client surface for a run with
    everything but the SIM steps switched off."""
    fw_target = ""

    def __init__(self):
        self.calls: list[str] = []
        self.verified_with: dict = {}

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append(name)
        return record

    def get_identity(self):
        return {"model": "OTD500", "serial": "SN-1", "mac": "aa:bb:cc:dd:ee:01",
                "imei": "350000000000001", "firmware": FW_VERIFIED}

    def verify_identity(self, identity, expected):
        return []

    def verify_configuration(self, **kwargs):
        self.verified_with = kwargs
        return []


def run_pipeline(settings) -> StubClient:
    import otd_configure
    client = StubClient()
    result = otd_configure.configure_device(client, label_password="pw",
                                            site_name="haifa", settings=settings)
    assert result["failures"] == []
    return client


def test_pipeline_configures_the_switch_before_the_modem_bounce():
    client = run_pipeline(SETTINGS)
    # The switch rules are pure UCI, written before sim-4g-only — the one step
    # that re-attaches the modem. The quota-sync FILES deploy later (see the
    # firmware-ordering test below).
    assert client.calls.index("configure_sim_switch") \
        < client.calls.index("set_sims_4g_only") \
        < client.calls.index("install_quota_sync")
    assert client.verified_with["sim_switch"] == SIM_SWITCH_CFG


def test_pipeline_installs_quota_sync_after_a_firmware_flash():
    # A keep-settings sysupgrade preserves /etc/config (the sim_switch UCI is
    # safe) but wipes /usr/bin and /etc/init.d — deploying the quota script
    # before the flash would delete it right after installing it.
    client = run_pipeline({**SETTINGS, "firmware": {"mode": "local"}})
    assert client.calls.index("upgrade_firmware") \
        < client.calls.index("install_quota_sync")


def test_pipeline_rejects_a_bad_operator_table_before_touching_the_device():
    import otd_configure
    client = StubClient()
    settings = {**SETTINGS, "sim_switch": {**SIM_SWITCH_CFG, "operators": [
        {"name": "typo", "iccid_prefixes": ["8997 202"], "data_limit_mb": 1,
         "reset_day": 1, "enabled": True}]}}
    with pytest.raises(SystemExit, match="iccid_prefixes"):
        otd_configure.configure_device(client, label_password="pw",
                                       site_name="haifa", settings=settings)
    assert client.calls == []   # config validated before login, let alone UCI


def test_pipeline_skips_the_sim_steps_when_disabled():
    client = run_pipeline({**SETTINGS, "sim_switch": {"enabled": False}})
    assert "configure_sim_switch" not in client.calls
    assert "install_quota_sync" not in client.calls
    # Still reported to verification, so the table shows it as skipped.
    assert client.verified_with["sim_switch"] == {"enabled": False}


def test_pipeline_without_a_sim_switch_block_still_runs():
    settings = {k: v for k, v in SETTINGS.items() if k != "sim_switch"}
    client = run_pipeline(settings)
    assert "configure_sim_switch" not in client.calls
    assert client.verified_with["sim_switch"] == {}
