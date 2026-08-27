"""Pipeline + client tests for the TSW202 (tsw_configure.py).

No hardware: `FakeSwitch` stands in for `ssh_exec`, so these pin the exact UCI
commands a real switch would receive and the exact decision the firmware step
makes. The firmware branch is the one worth being strict about — the config
names a FLOOR, and getting the comparison wrong downgrades a switch that
arrived on Teltonika's newer "Latest" build.
"""
import re

import pytest
import requests

import bench_core
from bench_core import DEFAULT_NEW_PASSWORD

import tsw_configure as mod
from tsw_configure import TswClient, apply_firmware_floor, configure_tsw

FLOOR = "TSW2_R_00.01.07.1"
NEWER = "TSW2_R_00.01.10"
OLDER = "TSW2_R_00.01.05"

NTP = "192.168.88.10"
ZONE = "Asia/Jerusalem"
POSIX_ZONE = "IST-2IDT,M3.4.4/26,M10.5.0"


class FakeSwitch:
    """Stands in for `TswClient.ssh_exec`: canned answers for the reads, a
    recording of every command for the writes."""

    def __init__(self, *, version=FLOOR, uci=None, model="TSW202",
                 mnfinfo=True, board="", network=None):
        self.commands: list[str] = []
        self.version = version
        self.model = model
        self.mnfinfo = mnfinfo
        self.board = board
        self.uci = dict(uci or {})
        # {section: ip} as `uci show network` would report it. Defaults to what
        # the first real TSW202 had: an addressed section that is NOT `lan`.
        self.network = {"lan_mgmt": "192.0.2.2"} if network is None else network

    def __call__(self, command, check=True, exec_timeout=None):
        self.commands.append(command)

        if "uci show network" in command:
            return "\n".join(f"network.{s}=interface\nnetwork.{s}.ipaddr='{ip}'"
                             for s, ip in self.network.items())
        if "cat /etc/version" in command:
            return self.version
        if "mnfinfo" in command:
            if not self.mnfinfo:
                return ""
            return ('{"serial":"6010212527","mac":"20972B2B00F7",'
                    f'"name":"{self.model}"}}')
        if "system board" in command or "board.json" in command or "device-tree" in command:
            return self.board
        if command.startswith("gsmctl"):
            return ""          # no modem on a switch
        if "uci -q get" in command or "uci get" in command:
            path = re.search(r"uci (?:-q )?get ([^\s;]+)", command).group(1)
            value = self.uci.get(path)
            if value is None:
                # A missing entry only fails the CALL when the shell isn't
                # already handling it: `uci get X || uci set X=...` exits 0,
                # which is exactly how the NTP section gets created.
                if check and "||" not in command:
                    raise SystemExit(f"uci: Entry not found ({path})")
                return ""
            return value
        return ""

    def wrote(self, fragment: str) -> bool:
        return any(fragment in c for c in self.commands)


class NoRest:
    """The REST half of the client, unplugged. `get_identity` also GETs
    /system/device/status, and 192.0.2.x is a black hole (TEST-NET-1) rather
    than a refused connection — without this the suite hangs on the socket
    timeout instead of failing. The client already treats a REST error as "that
    source had nothing", which is the behaviour under test on a firmware line
    that may not serve the endpoint at all."""

    def get(self, *_a, **_k):
        raise requests.exceptions.ConnectionError("no REST in tests")

    post = get


def client(**kwargs) -> TswClient:
    switch = FakeSwitch(**kwargs)
    c = TswClient(host="192.0.2.2")
    c.ssh_exec = switch
    c.s = NoRest()
    c.switch = switch          # test handle
    c.password = DEFAULT_NEW_PASSWORD
    return c


# ── NTP: a list option, not a plain one ──────────────────────────────────────

def test_ntp_server_replaces_the_list_rather_than_appending():
    c = client(uci={"system.ntp": "timeserver"})
    c.set_ntp_server(NTP)
    # The delete is the point: `uci set` on a list leaves the factory pool
    # entries in front of ours, and the switch keeps asking the internet for
    # time it should be getting from the site.
    assert c.switch.wrote("uci -q delete system.ntp.server")
    assert c.switch.wrote(f"uci add_list system.ntp.server={NTP}")
    assert c.switch.wrote("uci set system.ntp.enabled='1'")
    assert c.switch.wrote("uci commit system")
    assert c.switch.wrote("/etc/init.d/sysntpd restart")


def test_ntp_creates_the_section_when_it_is_absent():
    # set_timezone writes into system.ntp too, but only options that already
    # exist — on a firmware that ships no timeserver section, add_list would
    # fail without this.
    c = client()
    c.set_ntp_server(NTP)
    assert c.switch.wrote("uci set system.ntp=timeserver")


def test_ntp_servers_reads_the_list_back():
    c = client(uci={"system.ntp.server": f"{NTP} 1.pool.ntp.org"})
    assert c.ntp_servers() == [NTP, "1.pool.ntp.org"]


def test_ntp_servers_is_empty_when_unset():
    assert client().ntp_servers() == []


# ── identity: the model must be readable or the run is refused ────────────────

def test_model_comes_from_mnfinfo_when_it_is_there():
    assert client().get_identity()["model"] == "TSW202"


def test_model_falls_back_to_the_board_when_mnfinfo_is_absent():
    # assert_device_model refuses a device whose model it cannot read, which is
    # right — but it would refuse EVERY switch if this firmware line ships no
    # mnfinfo object. The board knows.
    c = client(mnfinfo=False, board="Teltonika TSW202")
    assert c.get_identity()["model"] == "TSW202"


def test_an_unreadable_model_stays_unknown():
    # Better than a guess: assert_device_model turns this into a refusal.
    c = client(mnfinfo=False, board="")
    assert c.get_identity()["model"] == "unknown"


# ── firmware: the floor, in all three directions ─────────────────────────────

def firmware_settings(bin_path="", minimum=FLOOR):
    return {"firmware": {"minimum_version": minimum, "bin_path": bin_path,
                         "keep_settings": True}}


def flashing_client(version, monkeypatch):
    """A client whose upgrade_firmware records the call instead of flashing."""
    c = client(version=version)
    flashes = []
    monkeypatch.setattr(c, "upgrade_firmware",
                        lambda **kw: flashes.append(kw))
    monkeypatch.setattr(c, "get_identity", lambda: {"firmware": FLOOR})
    c.flashes = flashes
    return c


def test_a_switch_at_the_floor_is_left_alone(monkeypatch):
    c = flashing_client(FLOOR, monkeypatch)
    failures = []
    note, warnings = apply_firmware_floor(c, firmware_settings(), failures)
    assert c.flashes == []
    assert failures == [] and warnings == []
    assert "at the floor" in note


def test_a_newer_switch_is_not_downgraded(monkeypatch, tmp_path):
    # Teltonika's "Latest" runs ahead of its "Stable" for this family, so a
    # switch arriving newer than the pin is routine. Flashing it backwards
    # would be the surprise.
    image = tmp_path / "fw.bin"
    image.write_bytes(b"not really firmware")
    c = flashing_client(NEWER, monkeypatch)
    failures = []
    note, warnings = apply_firmware_floor(c, firmware_settings(str(image)), failures)
    assert c.flashes == []
    assert failures == []
    assert warnings == [note]      # surfaced in the record, not silent
    assert "newer than" in note and NEWER in note


def test_an_older_switch_is_flashed(monkeypatch, tmp_path):
    image = tmp_path / "fw.bin"
    image.write_bytes(b"not really firmware")
    c = flashing_client(OLDER, monkeypatch)
    failures = []
    note, warnings = apply_firmware_floor(c, firmware_settings(str(image)), failures)
    assert c.flashes == [{"bin_path": str(image), "keep_settings": True}]
    assert failures == [] and warnings == []
    assert note.startswith(f"{OLDER} -> ")


def test_an_older_switch_with_no_image_fails_the_step_but_not_the_run(monkeypatch):
    # A missing image is a station config problem. Aborting would leave the
    # switch with a new password and nothing else; recording the failure and
    # carrying on gets the rest applied and still reports it.
    c = flashing_client(OLDER, monkeypatch)
    failures = []
    note, warnings = apply_firmware_floor(c, firmware_settings("/nope/missing.bin"),
                                          failures)
    assert c.flashes == []
    assert len(failures) == 1
    assert "below the" in failures[0] and "firmware.bin_path" in failures[0]
    assert "image missing" in note


def test_no_floor_configured_skips_the_step(monkeypatch):
    c = flashing_client(OLDER, monkeypatch)
    failures = []
    note, _ = apply_firmware_floor(c, firmware_settings(minimum=""), failures)
    assert c.flashes == [] and failures == []
    assert "no floor" in note


def test_an_unreadable_version_is_treated_as_below_the_floor(monkeypatch, tmp_path):
    # fw_version_at_least says False when it cannot tell, so "we don't know"
    # flashes rather than silently skipping a needed upgrade.
    image = tmp_path / "fw.bin"
    image.write_bytes(b"x")
    c = flashing_client("", monkeypatch)
    failures = []
    apply_firmware_floor(c, firmware_settings(str(image)), failures)
    assert c.flashes == [{"bin_path": str(image), "keep_settings": True}]


# ── the management section is discovered, not assumed ────────────────────────
#
# The first TSW202 on the bench failed the move with `uci: Invalid argument`,
# which is what UCI says when a section does not resolve: its management
# address is not on `network.lan` the way every RutOS router's is. Writing a
# hard-coded section is therefore not just wrong for this switch, it is a
# family-wide assumption that had never been checked.

def test_the_section_holding_our_address_wins():
    c = client(network={"lan": "10.0.0.1", "lan_mgmt": "192.0.2.2"})
    # Matched by address, so the presence of a `lan` section is not enough to
    # claim it — that is exactly the trap that produced the bench failure.
    assert c.mgmt_section() == "lan_mgmt"


def test_lan_is_still_used_where_it_is_the_convention():
    # Every RutOS router: the move must behave identically to before.
    c = client(network={"lan": "192.0.2.2"})
    assert c.mgmt_section() == "lan"


def test_lan_is_the_fallback_when_no_address_matches():
    c = client(network={"lan": "10.0.0.1", "wan": "10.0.1.1"})
    assert c.mgmt_section() == "lan"


def test_a_single_addressed_section_is_used_even_when_unmatched():
    c = client(network={"mgmt": "10.0.0.1"})
    assert c.mgmt_section() == "mgmt"


def test_an_ambiguous_config_refuses_rather_than_guessing():
    # Moving the wrong interface strands the device, so "I cannot tell" has to
    # stop the step rather than pick one.
    c = client(network={"eth0": "10.0.0.1", "eth1": "10.0.1.1"})
    assert c.mgmt_section() == ""


def test_an_anonymous_section_is_addressed_as_uci_addresses_it():
    c = client(network={"@interface[0]": "192.0.2.2"})
    assert c.mgmt_section() == "@interface[0]"


def test_network_addresses_parses_quoted_and_bare_values():
    c = client()
    c.ssh_exec = lambda *a, **k: (
        "network.lan=interface\n"
        "network.lan.ipaddr='192.168.1.2'\n"
        "network.lan.netmask='255.255.255.0'\n"
        "network.wan.ipaddr=10.0.0.5\n"
        "network.lan.proto='static'\n")
    assert c.network_addresses() == {"lan": "192.168.1.2", "wan": "10.0.0.5"}


# ── verification ─────────────────────────────────────────────────────────────

def verify(c, **overrides):
    kwargs = {"new_password": DEFAULT_NEW_PASSWORD, "zonename": ZONE,
              "ntp_server": NTP, "minimum_firmware": FLOOR}
    kwargs.update(overrides)
    return c.verify_configuration(**kwargs)


def row(checks, item):
    return next(c for c in checks if c["item"] == item)


def configured_uci():
    return {"system.ntp.zoneName": ZONE, "system.system.timezone": POSIX_ZONE,
            "system.ntp.server": NTP, "system.ntp.enabled": "1"}


def test_a_fully_configured_switch_passes_every_check():
    c = client(uci=configured_uci())
    checks = verify(c)
    assert [check["ok"] for check in checks] == [True, True, True, True]


def test_a_leftover_pool_entry_fails_the_ntp_check():
    # The switch would drift to internet time on a site that has none, so an
    # extra server is a real finding rather than a cosmetic one.
    uci = {**configured_uci(), "system.ntp.server": f"{NTP} 1.pool.ntp.org"}
    check = row(verify(client(uci=uci)), "NTP server")
    assert check["ok"] is False
    assert "1.pool.ntp.org" in check["actual"]


def test_a_disabled_ntp_client_fails_the_check():
    uci = {**configured_uci(), "system.ntp.enabled": "0"}
    assert row(verify(client(uci=uci)), "NTP server")["ok"] is False


def test_the_firmware_check_uses_floor_semantics():
    # A switch newer than the floor must PASS verification, or every
    # arriving-newer unit would report as failed after being correctly left alone.
    c = client(version=NEWER, uci=configured_uci())
    check = row(verify(c), "firmware")
    assert check["ok"] is True
    assert check["expected"] == f"{FLOOR} or newer"

    c = client(version=OLDER, uci=configured_uci())
    assert row(verify(c), "firmware")["ok"] is False


def test_the_firmware_check_is_out_of_scope_without_a_floor():
    c = client(uci=configured_uci())
    assert row(verify(c, minimum_firmware=""), "firmware")["ok"] is None


def test_a_wrong_timezone_fails():
    uci = {**configured_uci(), "system.ntp.zoneName": "UTC"}
    assert row(verify(client(uci=uci)), "timezone")["ok"] is False


# ── no check may carry a password (TEC-349) ──────────────────────────────────
#
# These rows are written to the run record verbatim and shipped to
# bench-central. On the failing path the password still in use is the device's
# own label password, which the bench is handed off the sticker.

LABEL_PW = "qN4$8xTr"


def test_the_password_change_is_reported_as_an_outcome():
    check = row(verify(client(uci=configured_uci())), "admin/root password")
    assert check["ok"] is True
    assert check["actual"] == "in use"


def test_a_failed_password_change_is_reported_as_failed():
    c = client(uci=configured_uci())
    c.password = LABEL_PW           # the change did not take
    check = row(verify(c), "admin/root password")
    assert check["ok"] is False
    assert "NOT set" in check["actual"]


@pytest.mark.parametrize("current,secret", [
    (DEFAULT_NEW_PASSWORD, DEFAULT_NEW_PASSWORD),
    (LABEL_PW, DEFAULT_NEW_PASSWORD),
    (LABEL_PW, LABEL_PW),
])
def test_no_check_contains_a_password(current, secret):
    c = client(uci=configured_uci())
    c.password = current
    for check in verify(c):
        assert secret not in str(check), check


# ── the pipeline, end to end ─────────────────────────────────────────────────

def pipeline_client(monkeypatch, *, version=FLOOR, model="TSW202", uci=None):
    c = client(version=version, model=model, uci=uci or configured_uci())
    monkeypatch.setattr(c, "login", lambda pw: setattr(c, "password", pw))
    monkeypatch.setattr(c, "set_admin_password",
                        lambda pw: setattr(c, "password", pw))
    moves = []
    monkeypatch.setattr(c, "move_lan", lambda ip, **kw: moves.append((ip, kw)) or
                        {"item": "LAN IP", "expected": ip,
                         "actual": f"answering on {ip}", "ok": True})
    c.moves = moves
    return c


def settings(**overrides):
    base = {"new_password": DEFAULT_NEW_PASSWORD, "ntp_server": NTP,
            "timezone": ZONE, "lan_ip": "192.168.88.2",
            "netmask": "255.255.255.0", "gateway": "192.168.88.1",
            "firmware": {"minimum_version": FLOOR}}
    base.update(overrides)
    return base


def test_a_clean_run_passes(monkeypatch):
    c = pipeline_client(monkeypatch)
    result = configure_tsw(c, initial_password="", settings=settings())
    assert result["ok"] is True
    assert result["failures"] == []
    assert result["ip"] == "192.168.88.2"


def test_the_management_move_runs_last_and_states_the_subnet(monkeypatch):
    c = pipeline_client(monkeypatch)
    configure_tsw(c, initial_password="", settings=settings())
    ip, kwargs = c.moves[0]
    assert ip == "192.168.88.2"
    assert kwargs["netmask"] == "255.255.255.0"
    assert kwargs["gateway"] == "192.168.88.1"
    # A switch serves no DHCP; renewing would throw away the station's static
    # bench address instead of following the device.
    assert kwargs["renew_dhcp"] is False


def test_the_move_is_skipped_when_no_lan_ip_is_configured(monkeypatch):
    c = pipeline_client(monkeypatch)
    result = configure_tsw(c, initial_password="", settings=settings(lan_ip=""))
    assert c.moves == []
    assert result["ip"] == ""


def test_the_wrong_model_is_refused_before_anything_is_written(monkeypatch):
    # A TSW212 would take a TSW202 image. The guard runs after login but
    # before the password change, so a mis-tabbed device is left untouched.
    c = pipeline_client(monkeypatch, model="TSW212")
    with pytest.raises(SystemExit, match="Wrong device"):
        configure_tsw(c, initial_password="", settings=settings())
    assert not c.switch.wrote("uci commit")
    assert c.moves == []


def test_an_unmappable_timezone_fails_before_touching_the_device(monkeypatch):
    # set_timezone would raise three steps in, leaving the switch on the shared
    # password and nothing else — so the config is checked up front.
    c = pipeline_client(monkeypatch)
    with pytest.raises(SystemExit, match="No POSIX TZ mapping"):
        configure_tsw(c, initial_password="", settings=settings(timezone="Mars/Olympus"))
    assert c.switch.commands == []


def test_a_rejected_label_password_retries_with_the_shared_one(monkeypatch):
    # A switch left half-provisioned by an earlier failed run is already on the
    # shared password; the label one no longer works.
    c = pipeline_client(monkeypatch)
    tried = []

    def login(pw):
        tried.append(pw)
        if pw != DEFAULT_NEW_PASSWORD:
            raise SystemExit("Login failed (HTTP 403)")
        c.password = pw

    monkeypatch.setattr(c, "login", login)
    result = configure_tsw(c, initial_password="label-pw", settings=settings())
    assert tried == ["label-pw", DEFAULT_NEW_PASSWORD]
    assert result["ok"] is True


def test_an_empty_password_goes_straight_to_the_shared_one(monkeypatch):
    c = pipeline_client(monkeypatch)
    tried = []
    monkeypatch.setattr(c, "login", lambda pw: tried.append(pw) or
                        setattr(c, "password", pw))
    configure_tsw(c, initial_password="", settings=settings())
    assert tried == [DEFAULT_NEW_PASSWORD]


def test_a_verification_failure_fails_the_run(monkeypatch):
    uci = {**configured_uci(), "system.ntp.server": "1.pool.ntp.org"}
    c = pipeline_client(monkeypatch, uci=uci)
    result = configure_tsw(c, initial_password="", settings=settings())
    assert result["ok"] is False
    assert any(check["item"] == "NTP server" and check["ok"] is False
               for check in result["verification"])


def test_the_pipeline_writes_ntp_and_timezone(monkeypatch):
    c = pipeline_client(monkeypatch)
    configure_tsw(c, initial_password="", settings=settings())
    assert c.switch.wrote(f"uci add_list system.ntp.server={NTP}")
    assert c.switch.wrote(f"system.ntp.zoneName='{ZONE}'")
    assert c.switch.wrote(f"system.system.timezone='{POSIX_ZONE}'")


def test_there_is_no_rms_tailscale_or_sim_step(monkeypatch):
    # The baseline in TEC-791 is password, firmware, time, address. Nothing
    # should be reaching for a router's surface on a switch — not in the
    # commands sent, and not as "skipped" rows the record would carry.
    c = pipeline_client(monkeypatch)
    result = configure_tsw(c, initial_password="", settings=settings())
    joined = " ".join(c.switch.commands).lower()
    for absent in ("rms", "tailscale", "simcard", "gsmctl -q", "sim_switch"):
        assert absent not in joined, absent
    assert [check["item"] for check in result["verification"]] == [
        "admin/root password", "timezone", "NTP server", "firmware", "LAN IP"]


def move_client(monkeypatch, network):
    """A client whose real move_lan runs, with everything after the commit
    (the interface restart, the host-side renew, the settle sleep, the
    reachability wait) stubbed out — the commands sent are what these assert on.

    The patches land in `bench_core`, where move_lan resolves its globals;
    patching them in this module would leave the real ones running."""
    c = client(network=network)
    monkeypatch.setattr(c, "_fire_and_forget", lambda *a, **k: None)
    monkeypatch.setattr(c, "_port_open", lambda *a, **k: True)
    monkeypatch.setattr(c, "close", lambda: None)
    monkeypatch.setattr(bench_core, "host_iface_for", lambda *a, **k: "en9")
    monkeypatch.setattr(bench_core, "renew_host_dhcp", lambda *a, **k: None)
    monkeypatch.setattr(bench_core.time, "sleep", lambda *a: None)
    return c


def test_the_move_writes_the_discovered_section(monkeypatch):
    # The regression test for the bench failure: the emitted UCI must name the
    # section the switch actually has, not `lan`.
    c = move_client(monkeypatch, {"lan_mgmt": "192.0.2.2"})
    check = c.move_lan("192.168.88.2", netmask="255.255.255.0",
                       gateway="192.168.88.1", renew_dhcp=False)
    assert check["ok"] is True
    assert c.switch.wrote("uci set network.lan_mgmt.ipaddr=192.168.88.2")
    assert c.switch.wrote("uci set network.lan_mgmt.netmask=255.255.255.0")
    assert c.switch.wrote("uci set network.lan_mgmt.gateway=192.168.88.1")
    assert c.switch.wrote("uci commit network")
    assert not c.switch.wrote("network.lan.")


def test_an_anonymous_section_is_quoted_against_the_shell(monkeypatch):
    # `[0]` is a shell glob; an unquoted path would be mangled before uci ever
    # sees it. This is what _uci_arg is for.
    c = move_client(monkeypatch, {"@interface[0]": "192.0.2.2"})
    c.move_lan("192.168.88.2", renew_dhcp=False)
    assert c.switch.wrote("uci set 'network.@interface[0].ipaddr=192.168.88.2'")


def test_the_move_refuses_an_ambiguous_config(monkeypatch):
    c = move_client(monkeypatch, {"eth0": "10.0.0.1", "eth1": "10.0.1.1"})
    with pytest.raises(SystemExit, match="Cannot tell which network section"):
        c.move_lan("192.168.88.2", renew_dhcp=False)
    assert not c.switch.wrote("uci commit network")


def test_the_move_is_skipped_when_already_on_the_target(monkeypatch):
    c = move_client(monkeypatch, {"lan_mgmt": "192.168.88.2"})
    c.host = "192.168.88.2"
    check = c.move_lan("192.168.88.2", renew_dhcp=False)
    assert check["ok"] is True and "already set" in check["actual"]
    assert not c.switch.wrote("uci commit network")


def test_a_router_still_gets_only_ipaddr(monkeypatch):
    # The RUTM08 call site passes no netmask/gateway and must keep behaving
    # exactly as it did before the section discovery went in.
    c = move_client(monkeypatch, {"lan": "192.0.2.2"})
    c.move_lan("192.168.88.1")
    assert c.switch.wrote("uci set network.lan.ipaddr=192.168.88.1 "
                          "&& uci commit network")


def test_the_defaults_match_the_committed_example():
    # The module defaults are what the CLI and a config-less run fall back to,
    # so they must not drift from the example the installer seeds.
    import json
    from pathlib import Path
    example = json.loads(
        (Path(mod.BASE_DIR) / "config" / "tsw.config.example.json")
        .read_text(encoding="utf-8"))
    assert example["host"] == mod.DEFAULT_TSW_HOST
    assert example["lan_ip"] == mod.DEFAULT_TSW_LAN_IP
    assert example["netmask"] == mod.DEFAULT_TSW_NETMASK
    assert example["gateway"] == mod.DEFAULT_TSW_GATEWAY
    assert example["ntp_server"] == mod.DEFAULT_TSW_NTP_SERVER
    assert example["firmware"]["minimum_version"] == mod.DEFAULT_TSW_MIN_FIRMWARE
