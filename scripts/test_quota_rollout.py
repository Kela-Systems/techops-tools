"""Tests for the rollout's read-and-decide logic.

The bench unit runs with no SIMs inserted, so the paths that matter most — an
ICCID in a slot, a quota_limit section already present, a SIM that has moved
between slots — cannot be exercised against it. They are pinned here instead,
against `uci show` output in the exact format the device prints.

    scripts/.venv/bin/python -m pytest test_quota_rollout.py -q
"""
from __future__ import annotations

import quota_rollout as qr

# `uci show simcard` on a dual-SIM OTD500. Deliberately in the awkward shape:
# the section for slot 2 comes FIRST, so anything relying on file order to mean
# slot order gets it backwards.
SIMCARD = """simcard.@sim[0]=sim
simcard.@sim[0].position='2'
simcard.@sim[0].iccid='8997202045004581309'
simcard.@sim[0].modem='2-1'
simcard.@sim[1]=sim
simcard.@sim[1].position='1'
simcard.@sim[1].iccid='8997250400055406332'
simcard.@sim[1].modem='2-1'
"""

QUOTA = """quota_limit.mob1s1a1=interface
quota_limit.mob1s1a1.ifname='mob1s1a1'
quota_limit.mob1s1a1.sim='1'
quota_limit.mob1s1a1.enabled='0'
quota_limit.mob1s1a1.data_limit='500'
quota_limit.mob1s1a1.period='3'
quota_limit.mob1s1a1.reset_day='1'
"""


def test_uci_map_parses_sections_and_strips_quotes():
    parsed = qr._uci_map(QUOTA, "quota_limit")
    assert parsed["mob1s1a1"]["data_limit"] == "500"
    assert parsed["mob1s1a1"]["enabled"] == "0"


def test_uci_map_ignores_other_packages():
    mixed = QUOTA + "sim_switch.cfg01.enabled='1'\n"
    assert set(qr._uci_map(mixed, "quota_limit")) == {"mob1s1a1"}


def test_slot_iccids_follow_position_not_file_order():
    slots = qr._slot_iccids(qr._uci_map(SIMCARD, "simcard"))
    # Slot 1 is the SECOND section in the file. Reading by order would swap the
    # two ICCIDs, and with them the operator and the limit each slot gets.
    assert slots[1]["iccid"] == "8997250400055406332"
    assert slots[2]["iccid"] == "8997202045004581309"


def test_slot_iccids_fall_back_to_index_without_position():
    no_position = "simcard.@sim[0]=sim\nsimcard.@sim[0].iccid='8997201000000000001'\n"
    slots = qr._slot_iccids(qr._uci_map(no_position, "simcard"))
    assert slots[1]["iccid"] == "8997201000000000001"
    assert slots[2]["iccid"] == ""


def _facts(iccids: dict[int, str], quota: str = "", firmware: str = "OTD5_R_00.07.22.3"):
    return {
        "read_ok": True,
        "simcard_sections": len(iccids),
        "firmware": firmware,
        "slots": {slot: {"iccid": iccid, "modem": "2-1"}
                  for slot, iccid in iccids.items()},
        "quota": qr._uci_map(quota, "quota_limit"),
        "sim_switch_sections": 3,
        "quota_sync_installed": False,
        "modem": "2-1",
    }


OPERATORS = [
    {"name": "partner", "iccid_prefixes": ["8997201"], "data_limit_mb": 3000000,
     "reset_day": 1, "enabled": False},
    {"name": "cellcom", "iccid_prefixes": ["8997202"], "data_limit_mb": 1330000,
     "reset_day": 15, "enabled": True},
]
UNKNOWN = {"data_limit_mb": 1330000, "reset_day": 1, "enabled": True}


def test_plan_matches_the_operator_of_the_iccid_the_device_reports():
    rows = qr.plan_device(_facts({1: "8997202045004581309", 2: ""}), {}, {},
                          OPERATORS, UNKNOWN)
    assert rows[0]["operator"] == "cellcom"
    assert rows[0]["enforced"] is True
    assert rows[1]["empty"] is True


def test_plan_reports_only_the_options_that_would_change():
    rows = qr.plan_device(_facts({1: "8997202045004581309", 2: ""}, QUOTA), {}, {},
                          OPERATORS, UNKNOWN)
    changes = rows[0]["changes"]
    assert changes["enabled"] == "1"          # was 0
    assert changes["data_limit"] == "1330000"  # was 500
    assert "period" not in changes             # already 3


def test_droam_usage_is_only_used_when_the_iccid_still_matches():
    iccid = "8997202045004581309"
    predicted = {"slots": [{"slot": 1, "iccid": iccid, "used_mib": 900000}]}
    rows = qr.plan_device(_facts({1: iccid, 2: ""}), {}, predicted, OPERATORS, UNKNOWN)
    assert rows[0]["used_mib"] == 900000

    # Same slot, but the device reports a different SIM than RMS recorded: the
    # stale figure must not be attributed to the SIM that is actually there.
    rows = qr.plan_device(_facts({1: "8997202099999999999", 2: ""}), {}, predicted,
                          OPERATORS, UNKNOWN)
    assert rows[0]["used_mib"] is None


def test_device_counter_is_advisory_unless_explicitly_trusted():
    usage = {1: {"counter_mib": 5000.0, "raw": "rx=1 + tx=2"}}
    facts = _facts({1: "8997202045004581309", 2: ""})
    assert qr.plan_device(facts, usage, {}, OPERATORS, UNKNOWN)[0]["used_mib"] is None
    trusted = qr.plan_device(facts, usage, {}, OPERATORS, UNKNOWN, trust_counter=True)
    assert trusted[0]["used_mib"] == 5000.0


def test_gate_refuses_a_slot_that_would_be_cut():
    iccid = "8997202045004581309"
    predicted = {"slots": [{"slot": 1, "iccid": iccid, "used_mib": 1400000}]}
    facts = _facts({1: iccid, 2: ""})
    rows = qr.plan_device(facts, {}, predicted, OPERATORS, UNKNOWN)
    stop = qr.gate(facts, rows, allow_unverified_fw=False, allow_unknown_usage=False)
    assert any("would cut this SIM's data now" in r for r in stop)


def test_gate_refuses_unknown_usage_but_not_an_unenforced_operator():
    partner = _facts({1: "8997201000000000001", 2: ""})
    rows = qr.plan_device(partner, {}, {}, OPERATORS, UNKNOWN)
    assert not qr.gate(partner, rows, allow_unverified_fw=False,
                       allow_unknown_usage=False)

    cellcom = _facts({1: "8997202045004581309", 2: ""})
    rows = qr.plan_device(cellcom, {}, {}, OPERATORS, UNKNOWN)
    stop = qr.gate(cellcom, rows, allow_unverified_fw=False, allow_unknown_usage=False)
    assert any("no usage figure" in r for r in stop)
    assert not qr.gate(cellcom, rows, allow_unverified_fw=False,
                       allow_unknown_usage=True)


def test_gate_refuses_unverified_firmware_and_unreadable_devices():
    facts = _facts({1: "", 2: ""}, firmware="OTD5_R_00.07.24.1")
    rows = qr.plan_device(facts, {}, {}, OPERATORS, UNKNOWN)
    assert any("not the verified" in r for r in
               qr.gate(facts, rows, allow_unverified_fw=False, allow_unknown_usage=False))
    assert not qr.gate(facts, rows, allow_unverified_fw=True, allow_unknown_usage=False)

    facts["read_ok"] = False
    assert any("do not run over SSH" in r for r in
               qr.gate(facts, rows, allow_unverified_fw=True, allow_unknown_usage=True))


def test_gate_refuses_a_device_that_reported_no_simcard_sections():
    # The failure that actually happened on the bench unit: commands answered,
    # but the config read came back empty. Silently reading that as "both slots
    # empty" would let a device through with no idea what is in it.
    facts = _facts({1: "", 2: ""})
    facts["simcard_sections"] = 0
    rows = qr.plan_device(facts, {}, {}, OPERATORS, UNKNOWN)
    assert any("no `simcard` sections" in r for r in
               qr.gate(facts, rows, allow_unverified_fw=True, allow_unknown_usage=True))


def test_describe_shows_what_an_empty_slot_gets_written():
    # This line is the whole report for most people, and it used to say only
    # "s2 empty" while an enforced fallback cap was written to that slot.
    rows = qr.plan_device(_facts({1: "8997201000000000001", 2: ""}), {}, {},
                          OPERATORS, UNKNOWN)
    line = qr.describe({"slots": rows})
    assert "s2 empty" in line
    assert "1,298.8 GB fallback ENFORCED" in line
    assert "data_limit=1330000" in line


def test_usage_payload_from_the_bench_unit_sums_tx_and_rx():
    class FakeClient:
        def ssh_exec(self, command, check=True):
            return '{"tx":941741055,"rx":4730135753}'

    usage = qr.read_usage(FakeClient(), {"modem": "2-1"})
    assert usage[1]["counter_mib"] == (941741055 + 4730135753) / (1024 * 1024)
    assert "tx=941741055" in usage[1]["raw"]


class FakeRelay:
    """A shell that understands only the commands the RMS transport issues.

    Enough to prove the real command strings work end to end: the base64 heredoc
    decodes back to the exact file, the md5 gate is what decides, and nothing is
    moved into place unless it matched.
    """

    def __init__(self, corrupt: bool = False):
        self.corrupt = corrupt
        self.staged = ""
        self.files: dict[str, str] = {}
        self.commands: list[str] = []

    def __call__(self, device_id, command, timeout=None, poll_interval=None):
        self.commands.append(command)
        body = command.rpartition("\n")[0] or command   # drop the `printf $?` wrapper
        out = self.run(body)
        return "completed", (f"{out}\n" if out else "") + f"{qr.RmsClient.RC_MARK}0"

    def run(self, body: str) -> str:
        import base64 as b64mod
        import hashlib as hashmod

        eof = qr.RmsClient.PUT_FILE_EOF
        if f"<<'{eof}'" in body:
            # The whole write arrives as one command: heredoc, decode, md5.
            payload = body.split(f"<<'{eof}'\n", 1)[1].split(f"\n{eof}", 1)[0]
            self.staged = b64mod.b64decode(payload).decode()
            if self.corrupt:
                self.staged = self.staged[:-5]
            if "md5sum" in body:
                return f"{hashmod.md5(self.staged.encode()).hexdigest()}  /tmp/staged"
            return ""
        if "mv " in body:
            dest = body.split("mv ")[1].split()[1]
            self.files[dest] = self.staged
            return ""
        return ""


def test_rms_transport_wraps_commands_to_recover_the_exit_status(monkeypatch):
    seen = {}

    def relay(device_id, command, timeout=None, poll_interval=None):
        seen["command"] = command
        return "completed", f"hello\n{qr.RmsClient.RC_MARK}0"

    monkeypatch.setattr("sim_audit.rms_run_command", relay)
    assert qr.RmsClient(7).ssh_exec("uci show simcard") == "hello"
    # The relay gives no exit status of its own, so the device has to report it.
    assert seen["command"].startswith("uci show simcard\nprintf")


def test_rms_transport_raises_on_nonzero_exit_but_not_when_unchecked(monkeypatch):
    monkeypatch.setattr("sim_audit.rms_run_command",
                        lambda *a, **k: ("completed", f"nope\n{qr.RmsClient.RC_MARK}1"))
    client = qr.RmsClient(7)
    try:
        client.ssh_exec("uci commit sim_switch")
        raise AssertionError("a failed command must not look like success")
    except SystemExit as exc:
        assert "rc=1" in str(exc)
    assert client.ssh_exec("uci commit sim_switch", check=False) == "nope"


def test_rms_transport_refuses_a_reply_with_no_exit_status(monkeypatch):
    # A truncated reply is the dangerous case: it could mean a half-written UCI
    # section, so it must never be read as success.
    monkeypatch.setattr("sim_audit.rms_run_command",
                        lambda *a, **k: ("completed", "partial output"))
    try:
        qr.RmsClient(7).ssh_exec("uci show sim_switch")
        raise AssertionError("a reply without the marker must fail")
    except SystemExit as exc:
        assert "no exit status" in str(exc)


def test_rms_transport_retries_once_on_a_relay_timeout(monkeypatch):
    calls = []

    def relay(device_id, command, timeout=None, poll_interval=None):
        calls.append(command)
        return ("timeout", "") if len(calls) == 1 else ("completed",
                                                        f"ok\n{qr.RmsClient.RC_MARK}0")

    monkeypatch.setattr("sim_audit.rms_run_command", relay)
    assert qr.RmsClient(7).ssh_exec("echo ok") == "ok"
    assert len(calls) == 2


def test_rms_put_file_sends_the_exact_content_in_one_command(monkeypatch):
    relay = FakeRelay()
    monkeypatch.setattr("sim_audit.rms_run_command", relay)
    content = "#!/bin/sh\n" + "payload 'with quotes'\n" * 150
    qr.RmsClient(42, "otd-x")._put_file("/usr/local/bin/kela-quota-sync", content,
                                        mode="755")
    assert relay.files["/usr/local/bin/kela-quota-sync"] == content
    # One command carries the file, one moves it: the appends this replaced took
    # a dozen, and a retried append would have doubled up.
    assert len(relay.commands) == 2


def test_rms_put_file_refuses_a_file_too_big_for_one_command(monkeypatch):
    # Splitting is what this deliberately does not do, so the limit has to be an
    # error rather than a quiet fallback to something unretryable.
    relay = FakeRelay()
    monkeypatch.setattr("sim_audit.rms_run_command", relay)
    try:
        qr.RmsClient(42)._put_file("/usr/local/bin/kela-quota-sync",
                                   "x" * qr.RmsClient.MAX_COMMAND)
        raise AssertionError("an oversized file must not be sent")
    except SystemExit as exc:
        assert "does not fit in one RMS relay command" in str(exc)
    assert relay.commands == []


def test_rms_transport_retries_an_rms_reported_timeout_too(monkeypatch):
    # RMS reports "error"/"Timeout." for commands that have in fact run, which is
    # why every command sent has to be idempotent — and retried, not failed.
    calls = []

    def relay(device_id, command, timeout=None, poll_interval=None):
        calls.append(command)
        return ("error", "Timeout.") if len(calls) == 1 else ("completed",
                                                              f"ok\n{qr.RmsClient.RC_MARK}0")

    monkeypatch.setattr("sim_audit.rms_run_command", relay)
    assert qr.RmsClient(7).ssh_exec("echo ok") == "ok"
    assert len(calls) == 2
    # A device error is the device's answer, not transport noise: never retried.
    assert not qr.RmsClient._retryable("error", "uci: Entry not found")


def test_rms_put_file_refuses_to_move_a_corrupted_file_into_place(monkeypatch):
    relay = FakeRelay(corrupt=True)
    monkeypatch.setattr("sim_audit.rms_run_command", relay)
    try:
        qr.RmsClient(42)._put_file("/usr/local/bin/kela-quota-sync", "x" * 2000)
        raise AssertionError("a corrupted upload must not be installed")
    except SystemExit as exc:
        assert "did not arrive intact" in str(exc)
    assert relay.files == {}


def test_resolve_host_prefers_the_live_tailnet_over_the_cached_address():
    # The report's address goes stale every time a device is removed from the
    # tailnet and rejoined, and a released address can end up on another node.
    device = {"name": "otd-avis-test", "tailscale_ip": "100.121.156.25"}
    host, note = qr.resolve_host(device, {"otd-avis-test": "100.70.1.1"})
    assert host == "100.70.1.1"
    host, note = qr.resolve_host(device, {})
    assert (host, "not confirmed" in note) == ("100.121.156.25", True)


def test_resolve_host_matches_the_tailnet_name_exactly_only():
    # A node named otd-avis-test-1 is NOT this device as far as resolution is
    # concerned; it falls through to the reported address, and the serial check
    # after connecting is what stops a wrong box being written to.
    host, note = qr.resolve_host({"name": "otd-avis-test", "tailscale_ip": "100.1.1.1"},
                                 {"otd-avis-test-1": "100.70.2.2"})
    assert host == "100.1.1.1"
    assert "not confirmed" in note


def test_resolve_host_reports_nothing_when_there_is_nothing():
    assert qr.resolve_host({"name": "otd-c"}, {}) == ("", "")


def test_identity_mismatch_catches_a_stale_address_pointing_at_another_device():
    device = {"name": "otd-avis-test", "serial": "6008219573"}
    assert qr.identity_mismatch(device, {"serial": "6008219573"}) == ""
    assert "DIFFERENT device" in qr.identity_mismatch(device, {"serial": "6009122294"})
    # Unreadable is not the same claim as wrong, and says so.
    unreadable = qr.identity_mismatch(device, {"serial": "unknown"})
    assert "no way to confirm" in unreadable and "DIFFERENT" not in unreadable
    # Unknowable rather than wrong: no serial on either side is not a mismatch.
    assert qr.identity_mismatch({"name": "x"}, {"serial": "6009122294"}) == ""
    assert qr.identity_mismatch(device, {}) == ""
