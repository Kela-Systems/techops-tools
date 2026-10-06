"""What an edge-role RUTM08 writes, and how its rows read it back.

An edge router sits inside a Gotcha edge box, in front of the camera, radars,
APUs and speaker, with its WAN on the server box's subnet. Four things make
that work, each with a way of being wrong that a naive write or read-back
would miss:

* **Port forwards** are reconciled, not appended. A re-run must leave one copy
  of each, a forward dropped from the config must not live on in the field,
  and a redirect the tool does not own (`kela-ntp`, or one an engineer added)
  must survive untouched.
* **WAN access** enables RutOS's own remote-access rules and never adds one.
  They are found by their values, because the section numbers are a build
  detail — and a rule with the right name but the wrong values is a build the
  tool does not know, so nothing is written.
* **The DHCP pool** is served (stock `dhcp.lan` has no `ignore` option, so one
  that is present is deleted rather than written as 0) and must stay clear of
  every device static.
* **The static WAN** changes only the addressing options, leaving the ones
  RutOS uses to identify the port.

The fake below is a small stateful UCI, laid out like `uci show` on a RUTM08
running RUTM_R_00.07.24.3: numeric named sections (`firewall.15`), not
`@redirect[N]`. So the reconcile is checked against the state it leaves, not
against the commands it happened to send.
"""
import shlex

import pytest

from bench_core import (
    PORT_FORWARD_PREFIX,
    MutationBlocked,
    TeltonikaClient,
    _MUTATING_COMMANDS,
)

RULES = [
    {"name": "cam-web", "ext_port": 8080, "dest_ip": "192.168.89.30", "dest_port": 80},
    {"name": "cam-rtsp", "ext_port": 554, "dest_ip": "192.168.89.30", "dest_port": 554},
    {"name": "radar1-web", "ext_port": 8050, "dest_ip": "192.168.89.50", "dest_port": 80},
]
STATICS = ["192.168.89.30", "192.168.89.50", "192.168.89.51", "192.168.89.52",
           "192.168.89.53", "192.168.89.60", "192.168.89.61", "192.168.89.70"]


def access_rule(name, port, **over):
    """A stock remote-access rule, as 07.24.3 ships it: disabled."""
    return {"proto": "tcp", "name": name, "target": "ACCEPT", "src": "wan",
            "enabled": "0", "dest_port": port, **over}


def stock_firewall(**rules):
    """The firewall package of a factory RUTM08 on 07.24.3, trimmed to the
    sections these methods look at. `rules` replaces or adds sections by id."""
    sections = {
        "2": ("zone", {"name": "lan", "network": ["lan"]}),
        "3": ("zone", {"name": "wan", "network": ["wan", "wan6"]}),
        "5": ("rule", {"name": "Allow-DHCP-Renew", "src": "wan", "proto": "udp",
                       "dest_port": "68", "target": "ACCEPT"}),
        "15": ("rule", access_rule("Enable_SSH_WAN", "22", priority="10")),
        "16": ("rule", access_rule("Enable_HTTP_WAN", "80", priority="11")),
        "17": ("rule", access_rule("Enable_HTTPS_WAN", "443", priority="12")),
    }
    sections.update(rules)
    return {k: v for k, v in sections.items() if v is not None}


def redirect(**options):
    return ("redirect", options)


def owned(rule, **over):
    """A redirect exactly as `set_port_forwards` writes `rule`."""
    return redirect(**{"name": f"{PORT_FORWARD_PREFIX}{rule['name']}",
                       "target": "DNAT", "src": "wan", "dest": "lan",
                       "proto": "tcp", "src_dport": str(rule["ext_port"]),
                       "dest_ip": rule["dest_ip"],
                       "dest_port": str(rule["dest_port"]), "enabled": "1",
                       **over})


NTP_RULE = redirect(name="kela-ntp", target="DNAT", src="wan", proto="udp",
                    src_dport="123", dest_ip="192.168.88.10", dest_port="123")
OPERATOR_RULE = redirect(name="nvr-from-site", target="DNAT", src="wan",
                         proto="tcp", src_dport="9000", dest_ip="192.168.89.90",
                         dest_port="9000")

STOCK_DHCP = {"lan": ("dhcp", {"interface": "lan", "start": "100", "limit": "150",
                               "leasetime": "12h", "dhcpv6": "server",
                               "ra": "server", "ra_slaac": "1",
                               "ra_flags": ["managed-config", "other-config"],
                               "ignore_ipv6": "1"})}
STOCK_NETWORK = {"lan": ("interface", {"ipaddr": "192.168.1.1"}),
                 "wan": ("interface", {"device": "wan", "proto": "dhcp",
                                       "metric": "1", "area_type": "wan"})}


class Uci:
    """A stateful stand-in for `ssh_exec` that speaks the UCI subset the client
    uses: show, set, add, delete, commit. Sections keep insertion order, the way
    `uci show` lists them."""

    def __init__(self, firewall=None, dhcp=None, network=None):
        self.packages = {
            "firewall": {k: (t, dict(o)) for k, (t, o) in (firewall or stock_firewall()).items()},
            "dhcp": {k: (t, dict(o)) for k, (t, o) in (dhcp or STOCK_DHCP).items()},
            "network": {k: (t, dict(o)) for k, (t, o) in (network or STOCK_NETWORK).items()},
        }
        self.commands: list[str] = []
        self.next_id = 30

    def show(self, package: str) -> str:
        lines = []
        for section, (stype, options) in self.packages.get(package, {}).items():
            lines.append(f"{package}.{section}={stype}")
            for key, value in options.items():
                rendered = (" ".join(f"'{v}'" for v in value)
                            if isinstance(value, list) else f"'{value}'")
                lines.append(f"{package}.{section}.{key}={rendered}")
        return "\n".join(lines)

    def __call__(self, command, check=True, exec_timeout=None):
        self.commands.append(command)
        out = ""
        for part in command.split(" && "):
            argv = shlex.split(part)
            if argv[:2] == ["uci", "show"]:
                out = self.show(argv[2])
            elif argv[:2] == ["uci", "add"]:
                section = str(self.next_id)
                self.next_id += 1
                self.packages[argv[2]][section] = (argv[3], {})
                out = section
            elif argv[:2] == ["uci", "set"]:
                path, value = argv[2].split("=", 1)
                package, section, option = path.split(".", 2)
                self.packages[package][section][1][option] = value
            elif argv[:2] == ["uci", "delete"]:
                package, rest = argv[2].split(".", 1)
                section, _, option = rest.partition(".")
                if option:
                    self.packages[package][section][1].pop(option, None)
                else:
                    del self.packages[package][section]
        return out

    def section(self, package, section) -> dict:
        return self.packages[package][section][1]

    def redirects(self) -> dict:
        return {s: o for s, (t, o) in self.packages["firewall"].items() if t == "redirect"}

    def owned_names(self) -> list[str]:
        return sorted(o["name"] for o in self.redirects().values()
                      if o.get("name", "").startswith(PORT_FORWARD_PREFIX))

    def wrote(self, fragment: str) -> bool:
        return any(fragment in c for c in self.commands)

    @property
    def writes(self) -> list[str]:
        return [c for c in self.commands if _MUTATING_COMMANDS.search(c)]


def client(**kwargs) -> TeltonikaClient:
    c = TeltonikaClient(host="192.0.2.1")
    c.ssh_exec = Uci(**kwargs)
    c.device = c.ssh_exec          # test handle
    return c


# ── port forwards ────────────────────────────────────────────────────────────

def test_the_forwards_are_written_whole_and_named_as_the_tools():
    c = client()
    c.set_port_forwards(RULES)
    assert c.device.owned_names() == sorted(f"kela-fwd-{r['name']}" for r in RULES)
    cam = next(o for o in c.device.redirects().values() if o["name"] == "kela-fwd-cam-web")
    assert cam == {"name": "kela-fwd-cam-web", "target": "DNAT", "src": "wan",
                   "dest": "lan", "proto": "tcp", "src_dport": "8080",
                   "dest_ip": "192.168.89.30", "dest_port": "80", "enabled": "1"}
    assert c.device.wrote("uci commit firewall")
    assert c.device.wrote("/etc/init.d/firewall reload")


def test_proto_defaults_to_tcp_but_a_rule_may_say_otherwise():
    c = client()
    c.set_port_forwards([{**RULES[0], "proto": "tcp udp"}])
    (rule,) = c.device.redirects().values()
    assert rule["proto"] == "tcp udp"


def test_a_re_run_leaves_exactly_one_copy_of_each():
    c = client()
    c.set_port_forwards(RULES)
    after_first = {k: dict(v) for k, v in c.device.redirects().items()}
    c.device.commands.clear()
    c.set_port_forwards(RULES)
    assert c.device.redirects() == after_first
    assert not c.device.wrote("uci add")
    assert c.port_forwards_check(RULES)["ok"] is True


def test_a_rule_that_drifted_is_corrected_in_place():
    c = client(firewall=stock_firewall(**{"20": owned(RULES[0], dest_ip="192.168.89.99")}))
    c.set_port_forwards(RULES[:1])
    assert c.device.section("firewall", "20")["dest_ip"] == "192.168.89.30"
    assert not c.device.wrote("uci add")


def test_a_forward_dropped_from_the_config_is_deleted_by_its_section_id():
    old = {"name": "old-nvr", "ext_port": 9000, "dest_ip": "192.168.89.90", "dest_port": 80}
    c = client(firewall=stock_firewall(**{"20": owned(old), "21": owned(RULES[0])}))
    c.set_port_forwards(RULES[:1])
    assert "20" not in c.device.redirects()
    assert c.device.owned_names() == ["kela-fwd-cam-web"]
    # Addressed by the id `uci show` listed, never by a positional index.
    assert c.device.wrote("uci delete firewall.20")
    assert not c.device.wrote("@redirect[")


def test_a_duplicate_owned_rule_is_reduced_to_one():
    c = client(firewall=stock_firewall(**{"20": owned(RULES[0]), "21": owned(RULES[0])}))
    c.set_port_forwards(RULES[:1])
    assert c.device.owned_names() == ["kela-fwd-cam-web"]
    assert "20" in c.device.redirects() and "21" not in c.device.redirects()


def test_redirects_the_tool_does_not_own_are_never_touched():
    c = client(firewall=stock_firewall(**{"22": NTP_RULE, "23": OPERATOR_RULE}))
    c.set_port_forwards(RULES)
    c.set_port_forwards([])
    assert c.device.section("firewall", "22") == NTP_RULE[1]
    assert c.device.section("firewall", "23") == OPERATOR_RULE[1]
    assert not any("firewall.22" in w or "firewall.23" in w for w in c.device.writes)
    assert c.device.owned_names() == []


def test_the_check_passes_a_reconciled_router():
    c = client()
    c.set_port_forwards(RULES)
    check = c.port_forwards_check(RULES)
    assert check["ok"] is True
    assert f"all {len(RULES)} present" in check["actual"]
    assert "zone covers wan" in check["actual"]


def test_the_check_names_what_is_missing_wrong_and_extra():
    old = {"name": "old-nvr", "ext_port": 9000, "dest_ip": "192.168.89.90", "dest_port": 80}
    c = client(firewall=stock_firewall(**{
        "20": owned(RULES[0], dest_port="81"),
        "21": owned(old),
    }))
    check = c.port_forwards_check(RULES[:2])
    assert check["ok"] is False
    assert "missing cam-rtsp" in check["actual"]
    assert "wrong cam-web (dest_port)" in check["actual"]
    assert "extra old-nvr" in check["actual"]


def test_a_disabled_or_duplicated_forward_fails_the_check():
    c = client(firewall=stock_firewall(**{"20": owned(RULES[0], enabled="0")}))
    assert "disabled" in c.port_forwards_check(RULES[:1])["actual"]
    c = client(firewall=stock_firewall(**{"20": owned(RULES[0]), "21": owned(RULES[0])}))
    check = c.port_forwards_check(RULES[:1])
    assert check["ok"] is False
    assert "cam-web x2" in check["actual"]


def test_the_check_ignores_redirects_it_does_not_own():
    c = client(firewall=stock_firewall(**{"22": NTP_RULE, "23": OPERATOR_RULE}))
    c.set_port_forwards(RULES)
    assert c.port_forwards_check(RULES)["ok"] is True


def test_forwards_on_a_zone_the_wired_wan_is_not_in_fail():
    # The NTP-forward finding (TEC-857), for the same reason: a rule matched by
    # zone is a green row over a dead path if the port is not in the zone.
    c = client(firewall=stock_firewall(**{"3": ("zone", {"name": "wan",
                                                         "network": ["mob1s1a1"]})}))
    c.set_port_forwards(RULES)
    check = c.port_forwards_check(RULES)
    assert check["ok"] is False
    assert "does NOT cover wan" in check["actual"]


# ── WAN access ───────────────────────────────────────────────────────────────

def test_stock_rules_are_enabled_explicitly():
    c = client()
    c.set_wan_access(webui=True, ssh=True)
    for section in ("15", "16", "17"):
        assert c.device.section("firewall", section)["enabled"] == "1", section
    assert c.device.wrote("/etc/init.d/firewall reload")
    assert not c.device.wrote("uci add")


def test_the_check_fails_stock_rules_and_passes_enabled_ones():
    c = client()
    before = c.wan_access_check(webui=True, ssh=True)
    assert before["ok"] is False
    assert "disabled" in before["actual"]
    c.set_wan_access(webui=True, ssh=True)
    assert c.wan_access_check(webui=True, ssh=True)["ok"] is True


def test_a_rule_with_no_enabled_option_counts_as_on():
    # What the WebUI's Access control toggle leaves behind: it deletes the
    # option rather than writing 1, and RutOS reads that as enabled.
    c = client(firewall=stock_firewall(**{
        "16": ("rule", {k: v for k, v in access_rule("Enable_HTTP_WAN", "80").items()
                        if k != "enabled"}),
        "17": ("rule", {k: v for k, v in access_rule("Enable_HTTPS_WAN", "443").items()
                        if k != "enabled"})}))
    assert c.wan_access_check(webui=True, ssh=False)["ok"] is True


def test_the_rules_are_found_by_their_values_not_their_section_numbers():
    c = client(firewall=stock_firewall(**{
        "15": None, "16": None, "17": None,
        "40": ("rule", access_rule("Enable_SSH_WAN", "22")),
        "41": ("rule", access_rule("Enable_HTTP_WAN", "80")),
        "42": ("rule", access_rule("Enable_HTTPS_WAN", "443"))}))
    c.set_wan_access(webui=True, ssh=True)
    assert all(c.device.section("firewall", s)["enabled"] == "1" for s in ("40", "41", "42"))


def test_only_what_is_asked_for_is_opened():
    c = client()
    c.set_wan_access(webui=True, ssh=False)
    assert c.device.section("firewall", "15")["enabled"] == "0"   # SSH untouched
    assert c.device.section("firewall", "16")["enabled"] == "1"


def test_a_missing_rule_stops_the_step_before_anything_is_written():
    c = client(firewall=stock_firewall(**{"15": None}))
    with pytest.raises(SystemExit, match="Enable_SSH_WAN"):
        c.set_wan_access(webui=True, ssh=True)
    assert c.device.writes == []


@pytest.mark.parametrize("field,value", [
    ("proto", "udp"), ("src", "lan"), ("target", "REJECT"), ("dest_port", "2222"),
])
def test_a_rule_with_the_right_name_and_other_values_is_refused(field, value):
    # The constants describe one rule. A rule called Enable_SSH_WAN that does
    # something else would open whatever it opens, so nothing is written.
    c = client(firewall=stock_firewall(**{
        "15": ("rule", access_rule("Enable_SSH_WAN", "22", **{field: value}))}))
    with pytest.raises(SystemExit, match="Enable_SSH_WAN"):
        c.set_wan_access(webui=True, ssh=True)
    assert c.device.writes == []


def test_the_check_reports_a_missing_rule_instead_of_raising():
    c = client(firewall=stock_firewall(**{"15": None}))
    check = c.wan_access_check(webui=True, ssh=True)
    assert check["ok"] is False
    assert "no 'Enable_SSH_WAN' rule" in check["actual"]


# ── the DHCP pool ────────────────────────────────────────────────────────────

def test_the_edge_pool_is_written_and_the_rest_of_the_section_left_alone():
    c = client()
    c.set_dhcp_pool(200, 50, serve=True)
    lan = c.device.section("dhcp", "lan")
    assert (lan["start"], lan["limit"]) == ("200", "50")
    for key, value in STOCK_DHCP["lan"][1].items():
        if key not in ("start", "limit"):
            assert lan[key] == value, key
    # Stock has no `ignore`, so there is nothing to delete — and nothing is.
    assert not c.device.wrote("uci delete")
    assert "ignore" not in lan


def test_an_ignore_option_is_deleted_not_written_as_zero():
    dhcp = {"lan": ("dhcp", {**STOCK_DHCP["lan"][1], "ignore": "1"})}
    c = client(dhcp=dhcp)
    c.set_dhcp_pool(200, 50, serve=True)
    assert "ignore" not in c.device.section("dhcp", "lan")
    assert c.device.wrote("uci delete dhcp.lan.ignore")
    assert not c.device.wrote("ignore=0")


def test_without_serve_the_pool_step_is_what_it_always_was():
    dhcp = {"lan": ("dhcp", {**STOCK_DHCP["lan"][1], "ignore": "1"})}
    c = client(dhcp=dhcp)
    c.set_dhcp_pool(100, 150)
    assert c.device.section("dhcp", "lan")["ignore"] == "1"


def test_a_pool_clear_of_every_device_static_passes():
    c = client()
    c.set_dhcp_pool(200, 50, serve=True)
    check = c.dhcp_pool_check(200, 50, reserved=STATICS, require_served=True)
    assert check["ok"] is True
    assert "leases .200-.249" in check["actual"]
    assert f"all {len(STATICS)} reserved addresses excluded" in check["actual"]


def test_the_stock_pool_fails_and_names_the_statics_it_covers():
    # Stock is .100-.249, so none of the statics are inside — but a pool moved
    # down to .1 swallows every one of them.
    c = client(dhcp={"lan": ("dhcp", {**STOCK_DHCP["lan"][1], "start": "1"})})
    check = c.dhcp_pool_check(200, 50, reserved=STATICS, require_served=True)
    assert check["ok"] is False
    assert "192.168.89.30" in check["actual"] and "INSIDE the pool" in check["actual"]


def test_a_pool_switched_off_fails_when_it_must_be_served():
    dhcp = {"lan": ("dhcp", {**STOCK_DHCP["lan"][1], "start": "200", "limit": "50",
                             "ignore": "1"})}
    check = client(dhcp=dhcp).dhcp_pool_check(200, 50, reserved=STATICS,
                                              require_served=True)
    assert check["ok"] is False
    assert "NOT served" in check["actual"]


def test_an_absent_ignore_is_served():
    c = client(dhcp={"lan": ("dhcp", {**STOCK_DHCP["lan"][1], "start": "200",
                                      "limit": "50"})})
    assert c.dhcp_pool_check(200, 50, require_served=True)["ok"] is True


# ── the static WAN ───────────────────────────────────────────────────────────

def test_the_static_wan_keeps_the_options_that_identify_the_port():
    # Stock network.wan is device/proto/metric/area_type. Only the addressing
    # changes; dropping `device` would leave an interface bound to nothing.
    c = client()
    c.set_wan_static("192.168.88.20", netmask="255.255.255.0",
                     gateway="192.168.88.1", dns="192.168.88.1")
    assert c.device.section("network", "wan") == {
        "device": "wan", "proto": "static", "metric": "1", "area_type": "wan",
        "ipaddr": "192.168.88.20", "netmask": "255.255.255.0",
        "gateway": "192.168.88.1", "dns": "192.168.88.1"}
    assert not c.device.wrote("uci delete")
    assert c.wan_static_check("192.168.88.20", netmask="255.255.255.0",
                              gateway="192.168.88.1")["ok"] is True


# ── verify-only safety ───────────────────────────────────────────────────────

@pytest.mark.parametrize("row", [
    lambda c: c.port_forwards_check(RULES),
    lambda c: c.wan_access_check(webui=True, ssh=True),
    lambda c: c.dhcp_pool_check(200, 50, reserved=STATICS, require_served=True),
])
def test_every_edge_row_is_a_pure_read(row):
    c = client()
    c.set_read_only()
    row(c)
    assert c.device.writes == []
