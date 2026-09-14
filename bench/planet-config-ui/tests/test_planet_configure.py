"""Command-building and firmware-decision tests for the IGS-4215 (planet_configure.py).

No hardware. Two areas are worth being strict about, because both fail SILENTLY
on a real switch — the commands are accepted and the wrong thing happens:

* **Deci-watts.** `poe power-limit 450` is 45.0 W. Sending the watts figure
  straight through caps a 35 W radar at 4.5 W, which reads on the bench as "the
  radar didn't power up" and sends someone hunting a cable fault.
* **The 4-character timezone acronym.** A longer one is accepted and ignored,
  leaving the switch on its factory +8 with every log timestamped wrong.
"""
import pytest

import planet_configure as mod
from planet_configure import (
    DEFAULT_PLANET_MIN_FIRMWARE,
    SINGLE_SUPPLY_BUDGET_W,
    apply_firmware_floor,
    description_cmds,
    poe_budget_warnings,
    poe_cmds,
    ports_of,
)

FLOOR = DEFAULT_PLANET_MIN_FIRMWARE          # 1.305b260324 — fixes the SSH server
OLDER = "1.305b251017"                       # the build with the broken SSH server
NEWER = "1.305b261120"

SITE_POE = {
    "managed": True,           # the plan under test IS the managed one
    "budget_w": 240,
    "limit_mode": "allocation",
    "ports": {
        "1": {"enabled": True, "limit_w": 45, "priority": "critical",
              "description": "Radar-AR300-1"},
        "6": {"enabled": False, "limit_w": 0, "priority": "low",
              "description": "Spare-PoE-2"},
        "8": {"enabled": True, "limit_w": 20, "priority": "high",
              "description": "Speaker-PR-HS15W-IP"},
        "9": {"enabled": False, "limit_w": 0, "priority": "low",
              "description": "Camera-RAYTHINK-PC464A1"},
    },
}


# --- PoE command building -----------------------------------------------------

def test_power_limits_are_sent_in_deci_watts():
    """45 W must go out as 450. This is the single most expensive thing to get
    wrong: the switch accepts `poe power-limit 45` and quietly caps at 4.5 W."""
    cmds = poe_cmds(SITE_POE)
    assert "poe power-limit 450 1" in cmds
    assert "poe power-limit 200 8" in cmds
    assert "poe power-limit 45 1" not in cmds


def test_disabled_ports_get_no_limit_or_priority():
    cmds = poe_cmds(SITE_POE)
    assert "poe port disable 6" in cmds
    assert not [c for c in cmds if c.endswith(" 6") and "power-limit" in c]


def test_the_non_poe_ports_are_named_but_never_powered():
    """gi9-12 have no PSE hardware. `poe port disable 9` is rejected and
    `show poe` never lists it, so the plan must not mention it at all — while
    the port still gets its label."""
    cmds = poe_cmds(SITE_POE)
    assert not [c for c in cmds if c.endswith(" 9")]
    assert 'description "Camera-RAYTHINK-PC464A1"' in description_cmds(SITE_POE)


def test_enabling_poe_on_a_data_only_port_is_refused():
    """A typo that moves a radar to gi9 must stop the run, not send a command
    the switch rejects while the radar sits dark."""
    plan = {"ports": {"9": {"enabled": True, "limit_w": 45}}}
    with pytest.raises(SystemExit, match="no PoE hardware"):
        poe_cmds(plan)


def test_the_global_plan_comes_before_any_port():
    """Ports are configured against a budget, so the budget must be set first."""
    cmds = poe_cmds(SITE_POE)
    assert cmds[0] == "poe admin-mode enable"
    assert cmds.index("poe power_budget 240") < cmds.index("poe port enable 1")


def test_priorities_are_carried_through():
    cmds = poe_cmds(SITE_POE)
    assert "poe priority critical 1" in cmds
    assert "poe priority high 8" in cmds


def test_json_string_port_keys_become_integers():
    """Port numbers arrive from JSON as strings and are sorted numerically —
    otherwise "10" would order before "2"."""
    assert sorted(ports_of(SITE_POE)) == [1, 6, 8, 9]


# --- port descriptions --------------------------------------------------------

def test_descriptions_are_quoted_so_spaces_survive():
    cmds = description_cmds(SITE_POE)
    assert 'description "Radar-AR300-1"' in cmds
    assert cmds[0] == "interface gi1"
    assert "exit" in cmds


def test_a_description_over_32_chars_is_refused_before_the_switch_is_touched():
    """The switch takes WORD<1-32>. Catching it here keeps a config typo from
    leaving a switch half-labelled."""
    too_long = {"ports": {"1": {"enabled": True, "limit_w": 45,
                                "description": "x" * 33}}}
    with pytest.raises(SystemExit, match="at most 32"):
        description_cmds(too_long)


def test_ports_without_a_description_are_skipped():
    assert description_cmds({"ports": {"1": {"enabled": True}}}) == []


# --- budget warnings ----------------------------------------------------------

def test_over_allocation_is_warned_about():
    """In allocation mode every enabled port's limit is reserved, so a plan
    over budget means a port is denied power — at the site, not on the bench."""
    plan = {"managed": True, "budget_w": 100,
            "ports": {"1": {"enabled": True, "limit_w": 45},
                      "2": {"enabled": True, "limit_w": 45},
                      "3": {"enabled": True, "limit_w": 45}}}
    warnings = poe_budget_warnings(plan, {"dual_power": True})
    assert any("allocates 135 W" in w for w in warnings)


def test_a_budget_over_240_on_one_supply_is_warned_about():
    """360 W needs both power inputs; on one supply it browns out under load."""
    plan = {"managed": True, "budget_w": 360, "ports": {}}
    assert any("one power input" in w
               for w in poe_budget_warnings(plan, {"dual_power": False}))


def test_the_same_budget_with_both_supplies_is_fine():
    assert poe_budget_warnings({"managed": True, "budget_w": 360, "ports": {}},
                               {"dual_power": True}) == []


def test_nothing_is_warned_about_when_poe_is_left_to_the_switch():
    """No limit is reserved, so no plan can over-allocate. The warning would be
    about a budget the bench never applied."""
    plan = {"budget_w": 100,
            "ports": {"1": {"enabled": True, "limit_w": 45},
                      "2": {"enabled": True, "limit_w": 45},
                      "3": {"enabled": True, "limit_w": 45}}}
    assert poe_budget_warnings(plan, {"dual_power": False}) == []


def test_the_poe_plan_is_kept_but_not_applied():
    """Naor's call: the switch negotiates power itself. The plan stays in the
    config and `poe_cmds` still builds it — one flag turns the step back on."""
    assert mod.poe_is_managed({}) is False
    assert mod.poe_is_managed({"managed": True}) is True
    assert "poe port enable 1" in poe_cmds(SITE_POE)      # still builds


def test_the_site_plan_fits_its_budget():
    """4x45 + 20 = 200 W against 240 W — the shipped example must not warn."""
    assert poe_budget_warnings(SITE_POE, {"dual_power": False}) == []


# --- system information parsing -----------------------------------------------

# Trimmed from a real IGS-4215-8UP2T2S page. The shape that matters: editable
# rows carry an "Edit" link between the label and its value, read-only rows do
# not — a parser that assumes one layout silently returns nothing for the other.
INFO_PAGE = """
<tr><td>System Name</td><td></td><td><a>Edit</a></td><td>&nbsp;IGS-4215-8UP2T2S</td></tr>
<tr><td>System Location</td><td></td><td><a>Edit</a></td><td>&nbsp;Default Location</td></tr>
<tr><td>MAC Address</td><td>A8:F7:E0:F6:C4:3A</td></tr>
<tr><td>IP Address</td><td>192.168.0.100</td></tr>
<tr><td>Firmware Version</td><td>1.305b260324</td></tr>
<tr><td>Power Status</td><td>PWR1:ON</td><td>PWR2:OFF</td></tr>
"""


def test_read_only_rows_are_parsed():
    table = mod.parse_info_table(INFO_PAGE)
    assert table["MAC Address"] == "A8:F7:E0:F6:C4:3A"
    assert table["Firmware Version"] == "1.305b260324"
    assert table["IP Address"] == "192.168.0.100"


def test_an_editable_row_yields_its_value_not_the_edit_link():
    """System Name has an 'Edit' cell between label and value; returning that
    would report every switch's model as 'Edit'."""
    assert mod.parse_info_table(INFO_PAGE)["System Name"] == "IGS-4215-8UP2T2S"


def test_a_missing_label_is_absent_rather_than_wrong():
    assert "Serial Number" not in mod.parse_info_table(INFO_PAGE)


# --- web session expiry ---------------------------------------------------------

LOGIN_PAGE = ('<html><script>top.location.replace('
              '"/cgi-bin/dispatcher.cgi?cmd=11")</script></html>')


def test_an_expired_web_session_is_renewed_not_parsed():
    """An expired session is answered with the LOGIN PAGE, not an error, so a
    caller that parses it reads nothing and reports it as a real result. This
    cost a run: the firmware check is the only thing read over HTTP, and it
    called a switch below the floor when its firmware was fine."""
    pages = [LOGIN_PAGE, INFO_PAGE]
    logins = []

    class Client(mod.PlanetClient):
        def _get(self, cmd):
            return pages.pop(0)

        def web_login(self, password=None):
            logins.append(password)
            self._web = object()

    client = Client("192.168.0.100", password="Kelasys123!")
    client._web = object()
    assert "MAC Address" in client._web_page(mod.CMD_SYSTEM_INFO)
    assert logins == ["Kelasys123!"], "should have logged in again exactly once"


def test_a_live_session_is_not_re_logged_in():
    pages = [INFO_PAGE]
    logins = []

    class Client(mod.PlanetClient):
        def _get(self, cmd):
            return pages.pop(0)

        def web_login(self, password=None):
            logins.append(password)

    client = Client("192.168.0.100")
    client._web = object()
    client._web_page(mod.CMD_SYSTEM_INFO)
    assert logins == []


# --- factory password ---------------------------------------------------------

def test_the_factory_password_is_derived_from_the_mac():
    """`sw` + the last 6 hex digits, lowercase. The value is from a real unit."""
    assert mod.factory_password_for("a8:f7:e0:f6:c4:3a") == "swf6c43a"


@pytest.mark.parametrize("mac", [
    "A8:F7:E0:F6:C4:3A",     # upper case, as some ARP tables print it
    "a8-f7-e0-f6-c4-3a",     # dash separated, as Windows `arp -a` prints it
    "a8f7e0f6c43a",          # bare, as a device page may report it
])
def test_every_mac_format_gives_the_same_password(mac):
    """The MAC reaches us from ARP on three platforms and from the device's own
    web page; a format difference must not change the password."""
    assert mod.factory_password_for(mac) == "swf6c43a"


def test_an_unusable_mac_yields_no_password():
    """Empty rather than a bogus 'sw' — a wrong attempt costs a third of the
    switch's retry budget before it locks out."""
    assert mod.factory_password_for("") == ""
    assert mod.factory_password_for("??") == ""


def test_at_most_two_passwords_are_ever_tried(monkeypatch):
    """The switch locks out after 3 wrong attempts and then answers every
    connection with a banner and a hang-up — which reads as the broken-SSH
    firmware fault and sends the operator down the wrong path."""
    attempts = []

    class Client(mod.PlanetClient):
        def web_login(self, password=None):
            attempts.append(password)
            raise SystemExit("nope")

    client = Client("192.168.0.100")
    with pytest.raises(SystemExit):
        client.web_login_any(["one", "two", "three", "four"])
    assert attempts == ["one", "two"]


def test_an_unreachable_switch_stops_immediately_instead_of_trying_more():
    """A switch that is not answering is not a password problem. Trying the
    next candidate against it wastes the retry budget and reports the wrong
    cause; the operator needs 'it is not answering'."""
    attempts = []

    class Client(mod.PlanetClient):
        def web_login(self, password=None):
            attempts.append(password)
            raise mod.SwitchUnreachable("no route to host")

    with pytest.raises(mod.SwitchUnreachable):
        Client("192.168.0.100").web_login_any(["one", "two"])
    assert attempts == ["one"], "should not have tried a second password"


def test_a_connection_error_becomes_a_readable_message(monkeypatch):
    """requests' own exceptions would reach the bench UI as a traceback."""
    import requests

    class Boom:
        def post(self, *a, **k):
            raise requests.ConnectionError("nope")

    monkeypatch.setattr(mod.requests, "Session", lambda: Boom())
    with pytest.raises(mod.SwitchUnreachable, match="did not answer over HTTP"):
        mod.PlanetClient("192.168.0.100").web_login("pw")


@pytest.mark.parametrize("host, first", [
    ("192.168.0.100", "swf6c43a"),      # factory address -> factory password
    ("192.168.88.3", "Kelasys123!"),    # management address -> shared password
])
def test_the_first_password_tried_follows_where_the_switch_answered(
        host, first, monkeypatch):
    """Three wrong attempts start a lockout, so a wasted guess costs a third of
    the budget. A switch on the factory address is almost certainly still on
    the factory password; one on the management subnet has been provisioned."""
    order = []

    class Client(mod.PlanetClient):
        def web_login(self, password=None):
            order.append(password)

        def get_identity(self):
            return {"model": mod.EXPECTED_MODEL, "firmware": FLOOR, "mac": "a8f7e0f6c43a"}

        def ensure_ssh_service(self):
            return False

        def login(self, password=None, new_password=None):
            raise mod.SshBroken("stop here — the passwords are what we are testing")

    monkeypatch.setattr(mod, "read_device_mac", lambda ip: "a8:f7:e0:f6:c4:3a")
    with pytest.raises(SystemExit):
        mod.configure_planet(Client(host), initial_password="",
                             settings={"host": "192.168.0.100",
                                       "new_password": "Kelasys123!",
                                       "firmware": {"enabled": False},
                                       "poe": {"ports": {}}})
    assert order[0] == first


def test_the_shared_password_is_tried_before_the_factory_one():
    """Within web_login_any the order given is respected exactly."""
    shared, order = "Kelasys123!", []

    class Client(mod.PlanetClient):
        def web_login(self, password=None):
            order.append(password)
            if password != shared:
                raise SystemExit("nope")

    assert Client("192.168.0.100").web_login_any([shared, "swf6c43a"]) == shared
    assert order == [shared], "the factory password should not have been tried"


# --- SSH service ---------------------------------------------------------------

SSH_PAGE_OFF = '<select name="sshd"><option value="1">Enable</option>' \
               '<option value="0" selected>Disable</option></select>' \
               '<input name="cliSshTo" value="10">' \
               '<input name="cliSshPassRety" value="3">' \
               '<input name="cliSshSilt" value="120">'
SSH_PAGE_ON = SSH_PAGE_OFF.replace('value="1">Enable', 'value="1" selected>Enable') \
                          .replace('value="0" selected>Disable', 'value="0">Disable')


def _ssh_client(page, monkeypatch):
    posted = {}

    class Client(mod.PlanetClient):
        def _web_page(self, cmd):
            return page

        def _web_post(self, data, *, timeout=30):
            posted.update(data)

    monkeypatch.setattr(mod, "wait_for_host", lambda *a, **k: True)
    return Client("192.168.0.100"), posted


def test_a_factory_switch_has_ssh_switched_on(monkeypatch):
    """A factory IGS-4215 has BOTH ssh and telnet disabled — ports 22 and 23
    closed — so the CLI this whole pipeline runs on must be enabled first."""
    client, posted = _ssh_client(SSH_PAGE_OFF, monkeypatch)
    assert client.ensure_ssh_service() is True
    assert posted["sshd"] == "1"


def test_enabling_ssh_preserves_the_lockout_settings(monkeypatch):
    """Timeout, retry count and silent time are what a later lockout is
    measured in; changing them silently would mislead whoever reads the switch."""
    client, posted = _ssh_client(SSH_PAGE_OFF, monkeypatch)
    client.ensure_ssh_service()
    assert posted["cliSshPassRety"] == "3"
    assert posted["cliSshSilt"] == "120"


def test_a_switch_with_ssh_already_on_is_left_alone(monkeypatch):
    client, posted = _ssh_client(SSH_PAGE_ON, monkeypatch)
    assert client.ensure_ssh_service() is False
    assert posted == {}


def test_ssh_never_opening_is_a_clear_failure(monkeypatch):
    client, _ = _ssh_client(SSH_PAGE_OFF, monkeypatch)
    monkeypatch.setattr(mod, "wait_for_host", lambda *a, **k: False)
    with pytest.raises(SystemExit, match="port 22 never opened"):
        client.ensure_ssh_service()


# --- forced password change at first login -------------------------------------

class FakeChannel:
    """Replays the factory switch's forced-password-change dialogue."""

    def __init__(self, refuse=False):
        self.sent, self.refuse = [], False if not refuse else True
        self._replies = [
            "(New)Password: ",
            "***********\r\nVerify (New)Password: ",
            # A refusal re-prompts rather than reaching a CLI prompt.
            ("***********\r\nPassword is too weak\r\n(New)Password: " if refuse
             else "***********\r\nSuccess.\r\nIGS-4215-8UP2T2S# "),
        ]

    def send(self, text):
        self.sent.append(text)
        self._pending = self._replies.pop(0) if self._replies else "IGS-4215-8UP2T2S# "

    def recv_ready(self):
        return bool(getattr(self, "_pending", ""))

    def recv(self, _n):
        out, self._pending = self._pending, ""
        return out.encode()


def _client_at_forced_change(refuse=False):
    client = mod.PlanetClient("192.168.0.100")
    client._ch = FakeChannel(refuse)
    return client


def test_the_forced_password_change_is_completed():
    """A factory switch will not open a CLI until its password is changed; the
    dialogue is Enter -> new -> verify -> 'Success.' and the prompt."""
    client = _client_at_forced_change()
    client._change_password_at_login("Kelasys123!")
    assert client._ch.sent == ["\n", "Kelasys123!\n", "Kelasys123!\n"]
    assert client.password == "Kelasys123!"


def test_a_password_the_switch_rejects_fails_before_anything_is_configured():
    """The switch demands 8-32 chars with upper, lower, a numeral and a symbol.
    Failing here beats leaving a half-provisioned switch behind."""
    client = _client_at_forced_change(refuse=True)
    with pytest.raises(SystemExit, match="refused the new password"):
        client._change_password_at_login("weak")


def test_no_configured_password_is_a_clear_error():
    client = _client_at_forced_change()
    with pytest.raises(SystemExit, match="no new password is configured"):
        client._change_password_at_login("")


def test_the_banner_marker_matches_the_real_greeting():
    """Pinned to the switch's actual wording — if this drifts, the tool would
    sail past the dialogue and every later command would hang on a prompt that
    never arrives."""
    greeting = ("You are required to change and store a new password to be "
                "able to get into the switch.")
    assert mod.FORCED_PASSWORD_CHANGE in greeting


# --- version ordering ---------------------------------------------------------

def test_planet_versions_order_by_build_date():
    """`1.305bNNNNNN` orders on the build date after the 'b'."""
    assert mod.planet_version_at_least(NEWER, FLOOR)
    assert mod.planet_version_at_least(FLOOR, FLOOR)
    assert not mod.planet_version_at_least(OLDER, FLOOR)


def test_the_shared_helper_would_get_this_wrong():
    """Why this module carries its own comparison.

    bench_core.fw_version_at_least reads dotted numeric segments only, so every
    1.305bNNNNNN build parses to (1, 305) and any two compare EQUAL — it calls
    the broken-SSH b251017 build 'at or above' the b260324 floor and the
    firmware step would skip the upgrade that is the whole point. If this test
    ever fails, the shared helper learned this format and the local one can go.
    """
    from bench_core import fw_version_at_least
    assert fw_version_at_least(OLDER, FLOOR) is True      # wrong, hence the local one
    assert mod.planet_version_at_least(OLDER, FLOOR) is False


def test_an_unreadable_version_is_never_new_enough():
    """'We could not tell' must not read as 'new enough' and skip an upgrade."""
    assert not mod.planet_version_at_least("", FLOOR)
    assert not mod.planet_version_at_least("garbage", FLOOR)
    assert not mod.planet_versions_match("", "")


def test_a_newer_line_beats_an_older_one():
    assert mod.planet_version_at_least("2.000b200101", FLOOR)


# --- firmware floor -----------------------------------------------------------

class FakeClient:
    """Records whether a flash happened, and what it was handed."""

    def __init__(self, after=FLOOR):
        self.flashed = None
        self._after = after

    def upgrade_firmware(self, image, *, reboot_wait=300):
        self.flashed = image
        return self._after


def test_a_switch_at_the_floor_is_left_alone():
    client = FakeClient()
    note, warnings = apply_firmware_floor(
        client, {"firmware": {"minimum_version": FLOOR}}, {"firmware": FLOOR}, [])
    assert client.flashed is None
    assert "at the floor" in note and warnings == []


def test_a_newer_switch_is_not_downgraded():
    """The config names a minimum, not a pin."""
    client = FakeClient()
    note, warnings = apply_firmware_floor(
        client, {"firmware": {"minimum_version": FLOOR}}, {"firmware": NEWER}, [])
    assert client.flashed is None
    assert "newer" in note and warnings


def test_an_older_switch_is_flashed(tmp_path):
    image = tmp_path / "fw.bix"
    image.write_bytes(b"x")
    client = FakeClient()
    failures = []
    note, _ = apply_firmware_floor(
        client,
        {"firmware": {"minimum_version": FLOOR, "bix_path": str(image)}},
        {"firmware": OLDER}, failures)
    assert client.flashed == image
    assert failures == [] and "flashed" in note


def test_an_older_switch_with_no_image_fails_the_step_and_says_why():
    """A missing image matters more here than on the Teltonika tools: below the
    floor the SSH CLI does not work, so the run cannot continue either. The
    message has to say that, not just 'image missing'."""
    client = FakeClient()
    failures = []
    apply_firmware_floor(
        client, {"firmware": {"minimum_version": FLOOR, "bix_path": "nope.bix"}},
        {"firmware": OLDER}, failures)
    assert client.flashed is None
    assert len(failures) == 1
    assert "SSH CLI does not work" in failures[0]


def test_the_step_can_be_turned_off():
    client = FakeClient()
    note, _ = apply_firmware_floor(
        client, {"firmware": {"enabled": False, "minimum_version": FLOOR}},
        {"firmware": OLDER}, [])
    assert client.flashed is None and "disabled" in note


def test_the_step_is_on_when_the_config_does_not_say():
    """Enabled by default — the whole reason this tool can talk to a switch at
    all on the firmware it ships with."""
    client = FakeClient()
    failures = []
    apply_firmware_floor(client, {"firmware": {"minimum_version": FLOOR}},
                         {"firmware": OLDER}, failures)
    assert failures, "an older switch with no image should fail the step"


# --- the broken-SSH signature -------------------------------------------------

def test_ssh_broken_is_a_distinct_error_type():
    """The caller's response to it is specific — upgrade the firmware — so it
    must not be indistinguishable from a wrong password."""
    assert issubclass(mod.SshBroken, SystemExit)


def test_the_broken_ssh_message_names_the_fix():
    """Whatever raises it, the operator must be told the firmware upgrade is
    the fix; that is the one thing that turns a dead bench into a working one."""
    error = mod.SshBroken(
        "the switch closed the CLI session immediately after its welcome banner "
        f"— the known fault on firmware below {FLOOR}. Upgrading the firmware "
        "fixes it (leave firmware.enabled on).")
    assert "Upgrading the firmware" in str(error)
    assert FLOOR in str(error)


# --- prompt matching ----------------------------------------------------------

@pytest.mark.parametrize("prompt", [
    "IGS-4215-8UP2T2S# ",
    "IGS-4215-8UP2T2S(config)# ",
    "IGS-4215-8UP2T2S(config-if)# ",
])
def test_config_mode_prompts_are_matched(prompt):
    """The parenthesised config-mode prompts are the ones a naive regex misses,
    and missing them makes every config command time out."""
    assert mod.PROMPT.search(prompt.rstrip())


def test_unknown_command_is_treated_as_a_rejection():
    """This firmware answers `Unknown command` where others say `Invalid` — a
    check that silently passes is worse than no check."""
    assert mod.CLI_REJECT.search("show interface description\nUnknown command")


# --- PoE left to the switch (2026-09-14) --------------------------------------

def _verifying_client(poe_seen: list):
    """A PlanetClient whose CLI answers enough of `verify_configuration` to see
    which rows it emits, and records whether `show poe` was asked for at all."""

    class Client(mod.PlanetClient):
        def cli(self, command, **kwargs):
            if command == "show poe":
                poe_seen.append(command)
                return ""
            if command == "show sntp":
                return "SNTP Server address: 192.168.88.10"
            if command == "show clock":
                return "Time source is SNTP\n  (UTC+3) Jerusalem"
            return ""

        def show_descriptions(self):
            return {}

    return Client("192.168.88.3")


def test_verify_asks_the_switch_nothing_about_poe_when_it_is_unmanaged():
    """Not merely "the rows pass" — the switch is not asked. A row comparing a
    limit nobody applied is a red check on a correctly provisioned switch."""
    seen = []
    checks = _verifying_client(seen).verify_configuration(
        settings={"poe": dict(SITE_POE, managed=False),
                  "ntp_server": "192.168.88.10", "timezone_offset": 3,
                  "disable_telnet": False})
    assert seen == []
    assert not [c for c in checks if c["item"].startswith("poe-port")]
    # The port names are not PoE and are still checked.
    assert [c for c in checks if c["item"].startswith("port-name")]


def test_verify_checks_the_ports_again_the_moment_poe_is_re_enabled():
    seen = []
    checks = _verifying_client(seen).verify_configuration(
        settings={"poe": SITE_POE, "ntp_server": "192.168.88.10",
                  "timezone_offset": 3, "disable_telnet": False})
    assert seen == ["show poe"]
    assert [c for c in checks if c["item"].startswith("poe-port")]
