"""The path that carries an upstream OTD500's NTP through the router (TEC-857).

The OTD500 sits UPSTREAM of the site's RUTM08:

    OTD (LAN 192.168.1.1) -> RUT WAN 192.168.1.2 -> RUT LAN 192.168.88.1
                                                      -> NTP server .88.10

so the server is behind the router's NAT and the OTD cannot address it. Three
settings make the hop work, and each has a way of being wrong that reads back
perfectly:

* **A fixed WAN address**, so the OTD's config is one identical line fleet-wide
  instead of a per-site lookup. An interface left on `proto=dhcp` with a stale
  `ipaddr` reads back the right number and still takes whatever the site hands
  it.
* **A DHCP pool clear of that address.** The router holds it statically and
  will not defend it, so a pool that can still lease it out is a collision
  waiting for the second device to boot. Reading the two options back would be
  a tautology; the question worth failing on is whether the address is safe.
* **A port forward on the router.** It is matched by ZONE, so a rule naming
  `wan` while the wired interface is not a member of that zone is a green row
  over a dead path — the trap TEC-857 lists as a prerequisite after the demo
  unit's `wan` zone turned out to hold only the mobile interfaces.

All read-backs. The WAN faces the site's OTD500 and the server is on the
assembly network, so neither is reachable from this bench and there is nothing
to corroborate them against here — see `docs/verification-rows.md`.
"""
import pytest

from bench_core import (
    NTP_FORWARD_NAME,
    MutationBlocked,
    TeltonikaClient,
)

WAN_IP = "192.168.1.2"
GATEWAY = "192.168.1.1"
NETMASK = "255.255.255.0"
SERVER = "192.168.88.10"

GOOD_WAN = {"proto": "static", "ipaddr": WAN_IP, "netmask": NETMASK,
            "gateway": GATEWAY}
GOOD_REDIRECT = {"name": NTP_FORWARD_NAME, "target": "DNAT", "src": "wan",
                 "proto": "udp", "src_dport": "123", "dest_ip": SERVER,
                 "dest_port": "123", "src_ip": GATEWAY}


class FakeRouter:
    """Stands in for `ssh_exec` across the three packages this touches."""

    def __init__(self, *, wan=None, redirect=None, zone_networks=("wan", "wan6"),
                 dhcp=None, has_wan_interface=True):
        self.commands: list[str] = []
        self.wan = dict(GOOD_WAN) if wan is None else (dict(wan) if wan else {})
        self.redirect = (dict(GOOD_REDIRECT) if redirect is None
                         else (dict(redirect) if redirect else {}))
        self.zone_networks = list(zone_networks)
        self.dhcp = {"start": "100", "limit": "150"} if dhcp is None else dict(dhcp)
        self.has_wan_interface = has_wan_interface

    def __call__(self, command, check=True, exec_timeout=None):
        self.commands.append(command)
        if "uci show network" in command:
            lines = ["network.lan=interface", "network.lan.ipaddr='192.168.88.1'"]
            if self.has_wan_interface:
                lines.append("network.wan=interface")
                lines += [f"network.wan.{k}='{v}'" for k, v in self.wan.items()]
            return "\n".join(lines)
        if "uci show firewall" in command:
            members = " ".join(f"'{n}'" for n in self.zone_networks)
            lines = ["firewall.@zone[0]=zone", "firewall.@zone[0].name='wan'",
                     f"firewall.@zone[0].network={members}"]
            if self.redirect:
                lines.append("firewall.@redirect[0]=redirect")
                lines += [f"firewall.@redirect[0].{k}='{v}'"
                          for k, v in self.redirect.items()]
            return "\n".join(lines)
        if "uci show dhcp" in command:
            return "\n".join(["dhcp.lan=dhcp"]
                             + [f"dhcp.lan.{k}='{v}'" for k, v in self.dhcp.items()])
        if command.startswith("uci add firewall redirect"):
            return "cfg0492bd"
        return ""

    def wrote(self, fragment: str) -> bool:
        return any(fragment in c for c in self.commands)


def client(**kwargs) -> TeltonikaClient:
    c = TeltonikaClient(host="192.0.2.1")
    c.ssh_exec = FakeRouter(**kwargs)
    c.device = c.ssh_exec          # test handle
    return c


# ── the WAN address ──────────────────────────────────────────────────────────

def test_an_interface_still_on_dhcp_fails_despite_the_right_address():
    # The read-back that cannot fail if you only ask for `ipaddr`: the number is
    # right and the interface takes a lease anyway, so the OTD's fleet-constant
    # line points at an address this router may not have tomorrow.
    c = client(wan={**GOOD_WAN, "proto": "dhcp"})
    check = c.wan_static_check(WAN_IP, netmask=NETMASK, gateway=GATEWAY)
    assert check["ok"] is False
    assert "dhcp" in check["actual"]


def test_a_wan_on_another_address_fails():
    c = client(wan={**GOOD_WAN, "ipaddr": "192.168.1.156"})
    assert c.wan_static_check(WAN_IP, netmask=NETMASK,
                              gateway=GATEWAY)["ok"] is False


def test_a_missing_gateway_fails():
    # Without it the router has no route back up to the OTD.
    c = client(wan={k: v for k, v in GOOD_WAN.items() if k != "gateway"})
    assert c.wan_static_check(WAN_IP, netmask=NETMASK,
                              gateway=GATEWAY)["ok"] is False


def test_a_correctly_pinned_wan_passes():
    assert client().wan_static_check(WAN_IP, netmask=NETMASK,
                                     gateway=GATEWAY)["ok"] is True


def test_the_step_writes_the_whole_interface_and_reloads():
    c = client()
    c.set_wan_static(WAN_IP, netmask=NETMASK, gateway=GATEWAY, dns="1.1.1.1")
    assert c.device.wrote("network.wan.proto=static")
    assert c.device.wrote(f"network.wan.ipaddr={WAN_IP}")
    assert c.device.wrote(f"network.wan.gateway={GATEWAY}")
    assert c.device.wrote("network.wan.dns=1.1.1.1")
    assert c.device.wrote("uci commit network")
    # `reload` and not `restart`: only the changed interface is reconfigured,
    # so the LAN session this runs over survives.
    assert c.device.wrote("/etc/init.d/network reload")
    assert not c.device.wrote("/etc/init.d/network restart")


def test_a_device_with_no_wan_interface_fails_the_step():
    c = client(has_wan_interface=False)
    with pytest.raises(SystemExit, match="network.wan"):
        c.set_wan_static(WAN_IP)


# ── the DHCP pool ────────────────────────────────────────────────────────────

def test_a_pool_that_can_lease_the_routers_address_fails():
    # Both options committed exactly as written and the answer is still wrong:
    # .2 is inside .1-.150, and the router will not defend it.
    c = client(dhcp={"start": "1", "limit": "150"})
    check = c.dhcp_pool_check(1, 150, reserved=WAN_IP)
    assert check["ok"] is False
    assert "INSIDE the pool" in check["actual"]


def test_a_pool_clear_of_the_reserved_address_passes():
    check = client().dhcp_pool_check(100, 150, reserved=WAN_IP)
    assert check["ok"] is True
    assert "excluded" in check["actual"]


def test_a_pool_wider_than_configured_fails():
    c = client(dhcp={"start": "100", "limit": "155"})
    assert c.dhcp_pool_check(100, 150, reserved=WAN_IP)["ok"] is False


def test_an_unreadable_pool_fails_rather_than_passing_quietly():
    c = client(dhcp={})
    check = c.dhcp_pool_check(100, 150)
    assert check["ok"] is False
    assert "unreadable" in check["actual"]


def test_the_pool_step_writes_both_bounds():
    c = client()
    c.set_dhcp_pool(100, 150)
    assert c.device.wrote("dhcp.lan.start=100")
    assert c.device.wrote("dhcp.lan.limit=150")
    assert c.device.wrote("uci commit dhcp")


# ── the port forward ─────────────────────────────────────────────────────────

def test_a_rule_on_a_zone_the_wired_port_is_not_in_fails():
    # The prerequisite TEC-857 calls out: the demo unit's `wan` zone listed only
    # the mobile interfaces, so nothing arriving on the wired port matched. The
    # rule itself is perfect here, which is what makes it worth a row.
    c = client(zone_networks=["mob1s1a1", "mob1s2a1", "mob1s3a1"])
    check = c.ntp_forward_check(dest_ip=SERVER, src_ip=GATEWAY)
    assert check["ok"] is False
    assert "does NOT cover wan" in check["actual"]


def test_a_missing_rule_fails():
    c = client(redirect={})
    check = c.ntp_forward_check(dest_ip=SERVER, src_ip=GATEWAY)
    assert check["ok"] is False
    assert NTP_FORWARD_NAME in check["actual"]


def test_a_rule_pointed_at_the_wrong_server_fails():
    c = client(redirect={**GOOD_REDIRECT, "dest_ip": "192.168.88.99"})
    assert c.ntp_forward_check(dest_ip=SERVER, src_ip=GATEWAY)["ok"] is False


def test_a_disabled_rule_fails():
    c = client(redirect={**GOOD_REDIRECT, "enabled": "0"})
    check = c.ntp_forward_check(dest_ip=SERVER, src_ip=GATEWAY)
    assert check["ok"] is False
    assert "DISABLED" in check["actual"]


def test_a_rule_without_the_enabled_option_is_treated_as_on():
    # RutOS omits `enabled` on a rule that is on; only an explicit 0 means off.
    c = client(redirect={k: v for k, v in GOOD_REDIRECT.items() if k != "enabled"})
    assert c.ntp_forward_check(dest_ip=SERVER, src_ip=GATEWAY)["ok"] is True


def test_an_unreadable_zone_does_not_fail_the_row():
    # Says nothing either way, and rounding that up to a failure would be as
    # dishonest as rounding it up to a pass (rows doc, rule 4).
    c = client(zone_networks=[])
    check = c.ntp_forward_check(dest_ip=SERVER, src_ip=GATEWAY)
    assert check["ok"] is True
    assert "unreadable" in check["actual"]


def test_a_correct_forward_passes():
    check = client().ntp_forward_check(dest_ip=SERVER, src_ip=GATEWAY)
    assert check["ok"] is True
    assert "zone covers wan" in check["actual"]


def test_the_step_writes_the_whole_rule():
    c = client(redirect={})
    c.set_ntp_port_forward(dest_ip=SERVER, src_ip=GATEWAY)
    for fragment in (f"firewall.cfg0492bd.name={NTP_FORWARD_NAME}",
                     "firewall.cfg0492bd.target=DNAT",
                     "firewall.cfg0492bd.src=wan",
                     "firewall.cfg0492bd.proto=udp",
                     "firewall.cfg0492bd.src_dport=123",
                     f"firewall.cfg0492bd.dest_ip={SERVER}",
                     "firewall.cfg0492bd.dest_port=123",
                     f"firewall.cfg0492bd.src_ip={GATEWAY}"):
        assert c.device.wrote(fragment), fragment
    assert c.device.wrote("uci commit firewall")


def test_re_running_updates_the_rule_instead_of_stacking_a_second_one():
    # A router re-run on the bench would otherwise collect a duplicate DNAT per
    # run, and the operator would have no way to tell from the tool.
    c = client()          # already carries the named rule
    c.set_ntp_port_forward(dest_ip=SERVER, src_ip=GATEWAY)
    assert not c.device.wrote("uci add firewall redirect")
    assert c.device.wrote("firewall.@redirect[0].dest_ip=192.168.88.10")


# ── verify-only safety ───────────────────────────────────────────────────────

@pytest.mark.parametrize("row", [
    lambda c: c.wan_static_check(WAN_IP, netmask=NETMASK, gateway=GATEWAY),
    lambda c: c.dhcp_pool_check(100, 150, reserved=WAN_IP),
    lambda c: c.ntp_forward_check(dest_ip=SERVER, src_ip=GATEWAY),
])
def test_every_row_is_a_read_on_a_read_only_client(row):
    c = client()
    c.set_read_only()
    assert row(c)["ok"] is True


@pytest.mark.parametrize("step", [
    lambda c: c.set_wan_static(WAN_IP),
    lambda c: c.set_dhcp_pool(100, 150),
    lambda c: c.set_ntp_port_forward(dest_ip=SERVER),
])
def test_every_step_is_refused_on_a_read_only_client(step):
    c = client()
    c.set_read_only()
    with pytest.raises(MutationBlocked):
        step(c)
