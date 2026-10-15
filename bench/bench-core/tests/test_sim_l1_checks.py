"""Standard v2 L1 radio/SIM rows (TEC-932): operator diversity, roaming, SIM PIN
and the APN.

The device answers below are the kela-fob-18 dump (OTD500, 07.24.3,
2026-10-14) — slot 1 a Cellcom card on auto-APN `sphone`, slot 2 a Partner card
on `uwap.orange.co.il` and active — then varied one fact at a time, so each test
is the real device with one thing wrong.

Two rules from docs/verification-rows.md shape them. An empty slot is a skip,
never a failure: SIMs usually go in in the field. And a row whose evidence is
missing says so with `ok=None` rather than passing.
"""
import pytest

from bench_core import TeltonikaClient, _MUTATING_COMMANDS

CELLCOM = "899720203506981335"
PARTNER = "89972010225030631274"
PARTNER_2 = "89972010225030639999"
FOREIGN = "89148000001234567890"

SIM_SWITCH = {
    "enabled": True,
    "operators": [
        {"name": "partner", "iccid_prefixes": ["8997201"], "data_limit_mb": 3000000,
         "enabled": False, "apns": ["uinternet", "uwap.orange.co.il"], "auto_apn": True},
        {"name": "cellcom", "iccid_prefixes": ["8997202"], "data_limit_mb": 1330000,
         "apns": ["internetg", "sphone"], "auto_apn": True},
        {"name": "pelephone", "iccid_prefixes": ["8997250"], "data_limit_mb": 2660000,
         "apns": ["internet.pelephone.net.il", "sphone.pelephone.net.il"],
         "auto_apn": False},
    ],
}
POLICY = {"deny_roaming": True, "sim_switch": SIM_SWITCH}

CPIN_READY = "+CPIN: READY\n"
CPIN_LOCKED = "+CPIN: SIM PIN\n"
CEREG_HOME = '+CEREG: 2,1,"084A","09DBC01",7\n'
CEREG_ROAMING = '+CEREG: 2,5,"084A","09DBC01",7\n'
CEREG_SEARCHING = "+CEREG: 2,2\n"


def cgdcont(apn: str) -> str:
    return (f'+CGDCONT: 1,"IPV4V6","{apn}","0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0",0,0,0,0\n'
            '+CGDCONT: 2,"IPV4V6","ims","0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0",0,0,0,0\n')


class FakeDevice:
    def __init__(self, *, iccids=(CELLCOM, PARTNER), active=PARTNER, slot=None,
                 deny_roaming=("1", "1"), cpin=CPIN_READY, cereg=CEREG_HOME,
                 net=None, context="uwap.orange.co.il"):
        self.commands: list[str] = []
        self.iccids, self.active = iccids, active
        # What `gsmctl -T` says; by default the slot holding the active card.
        if slot is None:
            slot = str(iccids.index(active) + 1) if active and active in iccids else ""
        self.slot = slot
        self.deny_roaming = deny_roaming
        self.cpin, self.cereg, self.context = cpin, cereg, context
        self.net = net if net is not None else {
            1: {"auto_apn": "1", "apn": "sphone", "pref_apn": "2305"},
            2: {"auto_apn": "1", "apn": "uwap.orange.co.il", "pref_apn": "2304"}}

    def simcard(self) -> str:
        lines = []
        for i, slot in enumerate((1, 2, 3)):
            lines += [f"simcard.@sim[{i}]=sim", f"simcard.@sim[{i}].modem='2-1'",
                      f"simcard.@sim[{i}].position='{slot}'"]
            if slot < 3 and self.iccids[slot - 1]:
                lines.append(f"simcard.@sim[{i}].iccid='{self.iccids[slot - 1]}'")
            if slot < 3 and self.deny_roaming[slot - 1]:
                lines.append(f"simcard.@sim[{i}].deny_roaming='{self.deny_roaming[slot - 1]}'")
        return "\n".join(lines)

    def network(self) -> str:
        lines = []
        for slot, opts in self.net.items():
            lines.append(f"network.mob1s{slot}a1=interface")
            lines += [f"network.mob1s{slot}a1.{k}='{v}'" for k, v in opts.items()]
        return "\n".join(lines)

    def __call__(self, command, check=True, exec_timeout=None):
        self.commands.append(command)
        if command.startswith("uci show simcard"):
            return self.simcard()
        if command.startswith("uci show network"):
            return self.network()
        if command.startswith("gsmctl -J"):
            return self.active or ""
        if command.startswith("gsmctl -T"):
            return self.slot
        if "AT+CPIN?" in command:
            return self.cpin
        if "AT+CEREG?" in command:
            return self.cereg
        if "AT+CGDCONT?" in command:
            return cgdcont(self.context) if self.context is not None else ""
        return ""


def rows(policy=POLICY, **kwargs) -> dict:
    c = TeltonikaClient(host="192.0.2.1")
    c.ssh_exec = FakeDevice(**kwargs)
    return {r["item"]: r for r in c.sim_policy_checks(policy)}


# ── negative tests first: the faults each row exists for ─────────────────────

def test_two_cards_on_one_operator_are_named_but_not_failed():
    # kela-fob-18 as TEC-861 found it: two Partner cards, one outage from dark.
    # Named, so the operator looks — but the inactive slot's ICCID is only the
    # last card seen there, so it cannot fail the unit on its own.
    row = rows(iccids=(PARTNER_2, PARTNER))["SIM operator diversity"]
    assert row["ok"] is None
    assert "both look like one operator" in row["actual"]
    assert "slot 1 partner (last seen), slot 2 partner (active)" in row["actual"]


def test_a_removed_cards_iccid_cannot_fail_a_one_sim_unit():
    # The bench provisions with ONE SIM. A unit that once had another Partner
    # card active in slot 1 still records it there after the card is gone; the
    # unit really has one card and must not go red for the one it no longer has.
    row = rows(iccids=(PARTNER_2, PARTNER))["SIM operator diversity"]
    assert row["ok"] is not False
    assert "Tobee" in row["actual"]


def test_roaming_denied_in_config_but_registered_roaming_fails():
    row = rows(cereg=CEREG_ROAMING)["SIM roaming"]
    assert row["ok"] is False
    assert "ROAMING" in row["actual"]


def test_roaming_not_denied_fails_without_needing_the_modem():
    row = rows(deny_roaming=("1", ""), cereg=CEREG_SEARCHING)["SIM roaming"]
    assert row["ok"] is False
    assert "slot 2=-" in row["actual"]


def test_a_pin_locked_active_card_fails():
    r = rows(cpin=CPIN_LOCKED)
    assert r["SIM PIN slot 2"]["ok"] is False
    assert "SIM PIN" in r["SIM PIN slot 2"]["actual"]


def test_a_locked_card_fails_even_when_the_active_slot_cannot_be_told():
    # A locked card may not give up its ICCID to gsmctl -J. Fail closed.
    r = rows(cpin=CPIN_LOCKED, active="")
    assert r["SIM PIN slot 1"]["ok"] is False
    assert r["SIM PIN slot 2"]["ok"] is False


def test_right_apn_in_config_but_modem_context_elsewhere_fails():
    row = rows(context="internet.example")["SIM APN slot 2"]
    assert row["ok"] is False
    assert "internet.example" in row["actual"]


def test_auto_apn_picking_an_apn_the_operator_does_not_use_fails():
    # The "registered, no data" case: auto chose from the wrong operator's list.
    net = {1: {"auto_apn": "1", "apn": "sphone"},
           2: {"auto_apn": "1", "apn": "sphone"}}
    row = rows(net=net, context="sphone")["SIM APN slot 2"]
    assert row["ok"] is False
    assert "does not use" in row["actual"]


def test_an_explicit_operator_left_on_auto_fails():
    # Pelephone is explicit in the table; a card still on auto was never synced.
    pele = "89972500000000000001"
    net = {1: {"auto_apn": "1", "apn": "sphone.pelephone.net.il"},
           2: {"auto_apn": "1", "apn": "uwap.orange.co.il"}}
    row = rows(iccids=(pele, PARTNER), net=net)["SIM APN slot 1"]
    assert row["ok"] is False
    assert row["expected"] == "pelephone: auto_apn=0 apn=internet.pelephone.net.il"


# ── the device as dumped: everything green where it can be ───────────────────

def test_the_bench_dump_passes_what_it_can_and_says_what_it_cannot():
    r = rows()
    # The inactive slot's ICCID is the last card seen there, and says so.
    assert r["SIM operator diversity"]["ok"] is None
    assert r["SIM operator diversity"]["actual"] == (
        "slot 1 cellcom (last seen), slot 2 partner (active); "
        "only the active card is confirmed (field check: Tobee, TEC-815)")
    assert r["SIM roaming"]["ok"] is True
    assert r["SIM PIN slot 2"]["ok"] is True          # the active card
    assert r["SIM PIN slot 1"]["ok"] is None          # inactive: unreadable
    assert "procedure" in r["SIM PIN slot 1"]["actual"]
    assert r["SIM APN slot 1"]["ok"] is True          # auto -> sphone, listed
    assert r["SIM APN slot 2"]["ok"] is True
    assert "modem context uwap.orange.co.il" in r["SIM APN slot 2"]["actual"]


def test_an_explicit_apn_passes_once_written():
    pele = "89972500000000000001"
    net = {1: {"auto_apn": "0", "apn": "internet.pelephone.net.il"},
           2: {"auto_apn": "1", "apn": "uwap.orange.co.il"}}
    assert rows(iccids=(pele, PARTNER), net=net)["SIM APN slot 1"]["ok"] is True


# ── the inactive slot's ICCID is only the last card seen there ───────────────
#
# otd-avis-test, 2026-10-15: a Pelephone card moved from slot 2 to slot 1, a
# Partner card put in slot 2. RutOS refreshed slot 1's ICCID when it went
# active and left slot 2's alone, so both sections named the Pelephone card —
# and the first version of the diversity row failed the unit for it.

PELEPHONE_F = "8997250000094155815F"      # 19 digits + the 'F' pad, verbatim
# That run's table: Pelephone on auto, which is the evidence it produced.
POLICY_ALL_AUTO = {"deny_roaming": True, "sim_switch": {**SIM_SWITCH, "operators": [
    {**op, "auto_apn": True} for op in SIM_SWITCH["operators"]]}}


def test_a_stored_iccid_equal_to_the_active_cards_is_stale_not_a_second_card():
    r = rows(POLICY_ALL_AUTO,
             iccids=(PELEPHONE_F, PELEPHONE_F), active=PELEPHONE_F, slot="1",
             net={1: {"auto_apn": "1", "apn": "sphone.pelephone.net.il"},
                  2: {"auto_apn": "1", "apn": "sphone.pelephone.net.il"}},
             context="sphone.pelephone.net.il")
    row = r["SIM operator diversity"]
    assert row["ok"] is None
    assert "slot 2" in row["actual"] and "unreadable" in row["actual"]
    # The active card is still judged in full ...
    assert r["SIM PIN slot 1"]["ok"] is True
    assert r["SIM APN slot 1"]["ok"] is True
    # ... and the stale slot is not judged as the card it is not.
    assert r["SIM APN slot 2"]["ok"] is None
    assert r["SIM PIN slot 2"]["ok"] is None


def test_the_active_slot_comes_from_the_modem_not_from_iccid_matching():
    # Without gsmctl -T, a duplicate ICCID would have matched slot 1 by luck.
    r = rows(iccids=(PELEPHONE_F, PELEPHONE_F), active=PELEPHONE_F, slot="2",
             context="sphone.pelephone.net.il")
    assert "slot 1" in r["SIM operator diversity"]["actual"]
    assert r["SIM PIN slot 2"]["ok"] is True


def test_a_padded_iccid_still_identifies_the_active_slot_without_gsmctl_t():
    r = rows(iccids=(PELEPHONE_F, PARTNER), active=PELEPHONE_F, slot="",
             net={1: {"auto_apn": "1", "apn": "sphone.pelephone.net.il"},
                  2: {"auto_apn": "1", "apn": "uwap.orange.co.il"}},
             context="sphone.pelephone.net.il")
    assert r["SIM PIN slot 1"]["ok"] is True
    assert "could not tell" not in r["SIM PIN slot 1"]["actual"]
    assert "slot 1 pelephone (active)" in r["SIM operator diversity"]["actual"]


# ── skip, not fail ───────────────────────────────────────────────────────────

def test_no_cards_skips_every_card_row_and_fails_none():
    r = rows(iccids=("", ""), active="", cpin="+CME ERROR: 10\n",
             cereg="+CEREG: 2,0\n", context="")
    for item in ("SIM operator diversity", "SIM PIN slot 1", "SIM PIN slot 2",
                 "SIM APN slot 1", "SIM APN slot 2"):
        assert r[item]["ok"] is None, item
    # The config half is still decidable, but cannot be confirmed on the modem.
    assert r["SIM roaming"]["ok"] is None
    assert "cannot confirm" in r["SIM roaming"]["actual"]


def test_one_card_has_nothing_to_compare():
    row = rows(iccids=(CELLCOM, ""), active=CELLCOM)["SIM operator diversity"]
    assert row["ok"] is None
    assert "1 card(s)" in row["actual"]


def test_an_unidentified_card_cannot_be_compared():
    r = rows(iccids=(FOREIGN, PARTNER))
    assert r["SIM operator diversity"]["ok"] is None
    assert "8914800" in r["SIM operator diversity"]["actual"]
    assert r["SIM APN slot 1"]["ok"] is None


def test_auto_that_has_not_resolved_yet_cannot_confirm():
    net = {1: {"auto_apn": "1"}, 2: {"auto_apn": "1", "apn": "uwap.orange.co.il"}}
    assert rows(net=net)["SIM APN slot 1"]["ok"] is None


def test_apn_rows_skip_when_quota_sync_is_not_installed():
    policy = {"deny_roaming": True, "sim_switch": {**SIM_SWITCH, "enabled": False}}
    r = rows(policy)
    assert r["SIM APN slot 1"]["ok"] is None and r["SIM APN slot 1"]["expected"] == "(skipped)"
    # Diversity still uses the table: it is a fact about the cards.
    assert "slot 1 cellcom" in r["SIM operator diversity"]["actual"]


def test_roaming_row_skips_when_the_policy_is_off():
    row = rows({"deny_roaming": False, "sim_switch": SIM_SWITCH})["SIM roaming"]
    assert row == {"item": "SIM roaming", "expected": "(skipped)", "actual": "-",
                   "ok": None}


# ── the write, and read-only-ness ────────────────────────────────────────────

def test_deny_roaming_is_written_on_every_sim_section_in_one_commit():
    c = TeltonikaClient(host="192.0.2.1")
    commands: list[str] = []

    def device(command, check=True, exec_timeout=None):
        commands.append(command)
        return "0\n1\n2" if command.startswith("uci show simcard") else ""

    c.ssh_exec = device
    c.set_sims_deny_roaming()
    assert commands[-1] == (
        "uci set 'simcard.@sim[0].deny_roaming=1' && "
        "uci set 'simcard.@sim[1].deny_roaming=1' && "
        "uci set 'simcard.@sim[2].deny_roaming=1' && uci commit simcard")
    # No modem reset of its own: the 4G-only step that follows applies it.
    assert not any("CFUN" in cmd for cmd in commands)


@pytest.mark.parametrize("kwargs", [{}, {"iccids": ("", ""), "active": ""}])
def test_the_rows_ask_the_device_nothing_it_could_change(kwargs):
    c = TeltonikaClient(host="192.0.2.1")
    c.ssh_exec = FakeDevice(**kwargs)
    c.sim_policy_checks(POLICY)
    assert [cmd for cmd in c.ssh_exec.commands if _MUTATING_COMMANDS.search(cmd)] == []


def test_the_rows_run_on_a_read_only_client():
    c = TeltonikaClient(host="192.0.2.1")
    c.ssh_exec = FakeDevice()
    c.set_read_only()
    assert len(c.sim_policy_checks(POLICY)) == 6


def test_a_tool_without_a_modem_gets_no_rows():
    c = TeltonikaClient(host="192.0.2.1")
    c.ssh_exec = FakeDevice()
    c.password = "pw"
    items = [r["item"] for r in c.verify_configuration(
        hostname="", zonename="Asia/Jerusalem", new_password="pw", sim_4g=False,
        rms=False, tailscale=False)]
    assert not any(i.startswith(("SIM roaming", "SIM PIN", "SIM APN",
                                 "SIM operator")) for i in items)
