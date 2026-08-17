"""Tests for the ARP-based host lookup (bench_core.arp_table / find_ip_by_mac).

No network: the subnet sweep (`tcp_port_open`) and the ARP listing
(`subprocess.run`) are both faked, so these cover the parsing and the
matching rules rather than any real host.
"""
import subprocess
import sys

import pytest

import bench_core
from bench_core import arp_table, canonical_mac, find_ip_by_mac

# The three listing formats the parser has to cope with.
MACOS_ARP = """\
? (192.168.88.1) at 0:11:22:33:44:55 on en0 ifscope [ethernet]
? (192.168.88.57) at 0:1e:42:aa:bb:1 on en0 ifscope [ethernet]
? (192.168.88.99) at (incomplete) on en0 ifscope [ethernet]
"""

WINDOWS_ARP = """\
Interface: 192.168.88.20 --- 0xd
  Internet Address      Physical Address      Type
  192.168.88.1          00-11-22-33-44-55     dynamic
  192.168.88.57         00-1e-42-aa-bb-01     dynamic
  192.168.88.255        ff-ff-ff-ff-ff-ff     static
"""

IP_NEIGH = """\
192.168.88.1 dev eth0 lladdr 00:11:22:33:44:55 REACHABLE
192.168.88.57 dev eth0 lladdr 00:1e:42:aa:bb:01 STALE
192.168.88.99 dev eth0  FAILED
"""

CAMERA_MAC = "00:1e:42:aa:bb:01"


def fake_arp(monkeypatch, output, *, fail_first=False):
    """Make every ARP listing command return `output`. With fail_first the first
    command raises, so the fallback command is the one that answers."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if fail_first and len(calls) == 1:
            raise FileNotFoundError(cmd[0])
        return subprocess.CompletedProcess(cmd, 0, stdout=output, stderr="")

    monkeypatch.setattr(bench_core.subprocess, "run", run)
    return calls


def only_open(monkeypatch, *ips):
    """Only `ips` answer the TCP probe. Returns the list of probed addresses."""
    probed = []

    def probe(ip, port, timeout=0.3):
        probed.append((ip, timeout))
        return ip in ips

    monkeypatch.setattr(bench_core, "tcp_port_open", probe)
    return probed


# ── arp_table parsing ────────────────────────────────────────────────────────

def test_arp_table_parses_macos_format(monkeypatch):
    fake_arp(monkeypatch, MACOS_ARP)
    table = arp_table()
    # macOS prints un-padded octets ('0:1e:...'), which must still match a
    # device's own zero-padded MAC.
    assert table[canonical_mac(CAMERA_MAC)] == ["192.168.88.57"]


def test_arp_table_parses_windows_format(monkeypatch):
    fake_arp(monkeypatch, WINDOWS_ARP)
    table = arp_table()
    assert table[canonical_mac(CAMERA_MAC)] == ["192.168.88.57"]
    # The interface header line carries an IP but no MAC, and broadcast is not
    # a device.
    assert canonical_mac("ff:ff:ff:ff:ff:ff") not in table


def test_arp_table_parses_ip_neigh_format(monkeypatch):
    # 'dev eth0' sits between the IP and the MAC — digits in the gap must not
    # break the row match.
    fake_arp(monkeypatch, IP_NEIGH)
    assert arp_table()[canonical_mac(CAMERA_MAC)] == ["192.168.88.57"]


def test_arp_table_keeps_every_address_a_mac_appears_at(monkeypatch):
    """A device that just moved is often cached at its old address as well as
    its new one; dropping either is how you end up chasing the one it left."""
    fake_arp(monkeypatch, "? (192.168.1.123) at 0:1e:42:aa:bb:1 on en0 [ethernet]\n"
                          "? (192.168.88.57) at 0:1e:42:aa:bb:1 on en0 [ethernet]\n")
    assert arp_table()[canonical_mac(CAMERA_MAC)] == ["192.168.1.123", "192.168.88.57"]


@pytest.mark.skipif(sys.platform == "win32",
                    reason="only the POSIX branch has a fallback command")
def test_arp_table_falls_back_to_the_second_command(monkeypatch):
    calls = fake_arp(monkeypatch, IP_NEIGH, fail_first=True)
    assert arp_table()[canonical_mac(CAMERA_MAC)] == ["192.168.88.57"]
    assert len(calls) == 2


def test_arp_table_empty_when_no_command_works(monkeypatch):
    def boom(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(bench_core.subprocess, "run", boom)
    assert arp_table() == {}


# ── find_ip_by_mac ───────────────────────────────────────────────────────────

def test_finds_the_host_with_that_mac(monkeypatch):
    fake_arp(monkeypatch, MACOS_ARP)
    only_open(monkeypatch, "192.168.88.57")
    assert find_ip_by_mac(CAMERA_MAC, ["192.168.88.0/24"], port=80) == "192.168.88.57"


def test_ignores_a_stale_entry_that_no_longer_answers(monkeypatch):
    """A cache entry still pointing at the device's old address must not be
    reported as the new one — the hit has to answer on the probed port."""
    fake_arp(monkeypatch, MACOS_ARP)
    only_open(monkeypatch, "192.168.88.1")   # a different device answers
    assert find_ip_by_mac(CAMERA_MAC, ["192.168.88.0/24"], port=80) is None


def test_skips_a_stale_entry_to_reach_the_live_one(monkeypatch):
    """The regression that broke a real bench run: the device was cached at BOTH
    the address it had left and its new one. Taking the first cached address
    blind means probing the dead one and reporting 'not found' — every cached
    address has to be tried."""
    fake_arp(monkeypatch, "? (192.168.1.123) at 0:1e:42:aa:bb:1 on en0 [ethernet]\n"
                          "? (192.168.88.57) at 0:1e:42:aa:bb:1 on en0 [ethernet]\n")
    only_open(monkeypatch, "192.168.88.57")
    found = find_ip_by_mac(CAMERA_MAC, ["192.168.88.0/24"], port=80)
    assert found == "192.168.88.57"


def test_confirmation_is_patient_even_though_the_sweep_is_not(monkeypatch):
    """The sweep is deliberately impatient (it only has to provoke ARP), but the
    'is it answering?' check must not be — a camera that just rebooted needs
    more than 0.3s to accept a connection."""
    fake_arp(monkeypatch, MACOS_ARP)
    probed = only_open(monkeypatch, "192.168.88.57")

    find_ip_by_mac(CAMERA_MAC, ["192.168.88.0/24"], port=80,
                   confirm_timeout=2.0, timeout=0.3)

    sweep = [t for ip, t in probed if ip != "192.168.88.57"]
    confirm = [t for ip, t in probed if ip == "192.168.88.57"]
    assert set(sweep) == {0.3}
    assert 2.0 in confirm


def test_finds_a_device_that_is_not_serving_yet_on_the_next_call(monkeypatch):
    """A device mid-boot answers ARP but not TCP. The sweep must still refresh
    the cache (address resolution happens before the connection attempt), so the
    caller's next attempt finds it once its web server is up."""
    fake_arp(monkeypatch, MACOS_ARP)
    booting = only_open(monkeypatch)          # nothing answers yet
    assert find_ip_by_mac(CAMERA_MAC, ["192.168.88.0/24"], port=80) is None
    assert booting, "the sweep must still run so the ARP cache is refreshed"

    only_open(monkeypatch, "192.168.88.57")   # web server now up
    assert find_ip_by_mac(CAMERA_MAC, ["192.168.88.0/24"], port=80) == "192.168.88.57"


def test_none_when_nothing_answers(monkeypatch):
    fake_arp(monkeypatch, MACOS_ARP)
    only_open(monkeypatch)
    assert find_ip_by_mac(CAMERA_MAC, ["192.168.88.0/24"], port=80) is None


def test_none_for_an_unusable_mac(monkeypatch):
    fake_arp(monkeypatch, MACOS_ARP)
    only_open(monkeypatch, "192.168.88.57")
    for mac in ("", "00:00:00:00:00:00"):
        assert find_ip_by_mac(mac, ["192.168.88.0/24"], port=80) is None


def test_searches_every_configured_subnet(monkeypatch):
    fake_arp(monkeypatch, "? (192.168.1.44) at 0:1e:42:aa:bb:1 on en0 [ethernet]")
    only_open(monkeypatch, "192.168.1.44")
    found = find_ip_by_mac(CAMERA_MAC, ["192.168.88.0/24", "192.168.1.0/24"], port=80)
    assert found == "192.168.1.44"


def test_invalid_subnet_is_skipped_not_fatal(monkeypatch):
    fake_arp(monkeypatch, MACOS_ARP)
    only_open(monkeypatch, "192.168.88.57")
    found = find_ip_by_mac(CAMERA_MAC, ["not-a-subnet", "192.168.88.0/24"], port=80)
    assert found == "192.168.88.57"


def test_no_subnets_probes_nothing(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("should not probe with nothing to scan")

    monkeypatch.setattr(bench_core, "tcp_port_open", boom)
    assert find_ip_by_mac(CAMERA_MAC, [], port=80) is None
