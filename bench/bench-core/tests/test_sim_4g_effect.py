"""4G-only has to reach the MODEM, not just the config (TEC-348).

`set_sims_4g_only` writes `simcard.@sim[N].service='lte'` and the check read the
same option back. `uci set` creates an option whether or not the modem obeys it,
so the row could not fail — the same fault class as the TSW202 that shipped on a
UTC clock with four green rows.

The effect half is what `AT+COPS?` says the modem is currently ATTACHED on. Two
things follow from that being a live radio state rather than a stored setting:

  * a device whose config says `lte` while it sits on 3G now goes red, which is
    the whole point;
  * a device that has not registered at all cannot be judged, and says so
    (`ok=None`, "cannot confirm"). It must never come out green — a sweep that
    turns "I couldn't check" into a pass is the tautology in a new place.

The exact `AT+COPS?` reply format varies a little between modem firmwares, so
the parser is deliberately strict and anything it does not recognise degrades to
"cannot confirm" rather than to an answer. The values are 3GPP TS 27.007 §7.3.
"""
import pytest

from bench_core import (
    ACCESS_TECH,
    FOURG_ACCESS_TECH,
    TeltonikaClient,
)

SHARED = "test-shared-pw"

# What a modem really says. Taken from the AT spec's response format:
#   +COPS: <mode>,<format>,<oper>,<AcT>
ATTACHED_4G = '+COPS: 0,0,"Partner",7\n\nOK'
ATTACHED_3G = '+COPS: 0,0,"Partner",2\n\nOK'
ATTACHED_2G = '+COPS: 0,0,"Cellcom",3\n\nOK'
ATTACHED_5G = '+COPS: 0,0,"Partner",11\n\nOK'
# Not registered: the operator and AcT fields are simply absent.
NOT_ATTACHED = "+COPS: 0\n\nOK"
NO_SIM = "+CME ERROR: SIM not inserted"


class FakeDevice:
    """Stands in for `ssh_exec`. The modem's attach state is independent of the
    UCI option, because that independence IS the check."""

    def __init__(self, *, service="lte", slots=2, cops=ATTACHED_4G):
        self.commands: list[str] = []
        self.service = service
        self.slots = slots
        self.cops = cops

    def __call__(self, command, check=True, exec_timeout=None):
        self.commands.append(command)
        if "uci show simcard" in command:
            # The real read pipes `uci show` through sed to leave the indices.
            return "\n".join(str(i) for i in range(self.slots))
        if "simcard.@sim[" in command and ".service" in command:
            return self.service
        if "AT+COPS?" in command:
            return self.cops
        return ""


def client(**kwargs) -> TeltonikaClient:
    c = TeltonikaClient(host="192.0.2.1")
    c.ssh_exec = FakeDevice(**kwargs)
    c.device = c.ssh_exec          # test handle
    c.password = SHARED
    return c


# ── reading what the modem attached on ───────────────────────────────────────

def test_the_access_technology_is_read_off_a_registered_modem():
    act, raw = client(cops=ATTACHED_4G).attached_access_tech()
    assert act == 7
    assert raw.startswith("+COPS:")


def test_a_numeric_operator_field_is_parsed_too():
    # Format 2 answers with a numeric operator code instead of a quoted name.
    act, _ = client(cops="+COPS: 0,2,42501,7\n\nOK").attached_access_tech()
    assert act == 7


def test_an_unregistered_modem_reports_no_technology():
    # `+COPS: 0` with the operator and AcT fields absent: genuinely "not on a
    # network", not "unparseable".
    assert client(cops=NOT_ATTACHED).attached_access_tech()[0] is None


def test_a_modem_with_no_sim_reports_no_technology():
    assert client(cops=NO_SIM).attached_access_tech()[0] is None


def test_an_unrecognised_reply_reports_no_technology():
    # A firmware whose format this parser does not know must degrade, not guess.
    assert client(cops="something else entirely").attached_access_tech()[0] is None


def test_every_four_g_code_is_a_known_technology():
    # The pass set and the name table have to stay in step, or a passing row
    # would print "attached on None".
    for code in FOURG_ACCESS_TECH:
        assert "4G" in ACCESS_TECH[code]


# ── the row ──────────────────────────────────────────────────────────────────

def test_config_right_and_attached_on_4g_passes():
    row = client().sim_4g_check()
    assert row["ok"] is True
    assert "E-UTRAN (4G)" in row["actual"]
    assert "lte,lte" in row["actual"]


def test_config_right_but_attached_on_3g_goes_red():
    # THE negative test: exactly the state the old read-back row called green.
    row = client(cops=ATTACHED_3G).sim_4g_check()
    assert row["ok"] is False
    assert "UTRAN (3G)" in row["actual"]
    assert "not on 4G" in row["actual"]


def test_config_right_but_attached_on_2g_goes_red():
    row = client(cops=ATTACHED_2G).sim_4g_check()
    assert row["ok"] is False
    assert "2G" in row["actual"]


def test_config_right_but_attached_on_5g_goes_red():
    # A 5G attach on a device restricted to 4G means the restriction is not in
    # force, whatever the config says.
    row = client(cops=ATTACHED_5G).sim_4g_check()
    assert row["ok"] is False
    assert "5G" in row["actual"]


def test_an_unattached_modem_cannot_confirm_and_does_not_pass():
    # The bench has no outdoor antenna, so this is the common case there — and
    # the one most likely to be quietly rounded up to a pass.
    row = client(cops=NOT_ATTACHED).sim_4g_check()
    assert row["ok"] is None
    assert "cannot confirm" in row["actual"]


def test_a_modem_with_no_sim_cannot_confirm():
    row = client(cops=NO_SIM).sim_4g_check()
    assert row["ok"] is None
    assert "cannot confirm" in row["actual"]


def test_the_wrong_config_fails_even_while_attached_on_4g():
    # `service='auto'` and a 4G attach: it is on 4G today by coincidence of
    # coverage, and will roam onto 3G the moment that changes.
    row = client(service="auto", cops=ATTACHED_4G).sim_4g_check()
    assert row["ok"] is False
    assert "config says auto,auto" in row["actual"]


def test_the_wrong_config_fails_without_needing_the_modem():
    # Decidable from the config alone, so it must not degrade to "cannot
    # confirm" just because the modem is unregistered.
    row = client(service="auto", cops=NOT_ATTACHED).sim_4g_check()
    assert row["ok"] is False


def test_a_device_with_no_sim_sections_fails():
    row = client(slots=0).sim_4g_check()
    assert row["ok"] is False
    assert "(none)" in row["actual"]


def test_the_row_reads_and_never_writes():
    c = client()
    c.sim_4g_check()
    for command in c.device.commands:
        assert "uci set" not in command
        assert "CFUN" not in command      # re-attaching the modem is a mutation


# ── the row as the full verification sees it ─────────────────────────────────

def verify(c, sim_4g=True) -> list[dict]:
    return c.verify_configuration(hostname="", zonename="Asia/Jerusalem",
                                  new_password=SHARED, sim_4g=sim_4g,
                                  rms=False, tailscale=False)


def sim_row(checks) -> dict:
    return next(c for c in checks if c["item"] == "SIM 4G-only")


def test_verify_configuration_uses_the_effect_based_check():
    assert sim_row(verify(client()))["ok"] is True
    assert sim_row(verify(client(cops=ATTACHED_3G)))["ok"] is False


def test_a_tool_without_sims_still_skips_the_row():
    # The switch and the router pass sim_4g=False; the row must stay a skip
    # rather than becoming an unconfirmable modem read.
    row = sim_row(verify(client(), sim_4g=False))
    assert row["ok"] is None
    assert row["expected"] == "(skipped)"


def test_a_device_on_3g_fails_the_whole_verification():
    # The point of converting the row: this device used to pass.
    checks = verify(client(cops=ATTACHED_3G))
    assert "SIM 4G-only" in [c["item"] for c in checks if c["ok"] is False]
