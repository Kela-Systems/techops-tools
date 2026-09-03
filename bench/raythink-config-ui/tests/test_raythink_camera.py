"""Tests for the Raythink camera client's addressing steps, driven through the
older (RPC2) client.

The addressing steps themselves live in `raythink_base` and are shared by both
camera generations — which is why the host-side helpers they use
(`find_ip_by_mac`, `renew_host_dhcp`, `host_iface_for`, `arp_table`,
`time.sleep`) are stubbed on `raythink_base` rather than on the client module.
What this file exercises through the RPC2 client is the base's logic plus that
client's read/write of the Network table; `test_raythink_rest.py` drives the
same base through the other generation's hooks.

No hardware/network: RPC2 calls are recorded by a fake `_rpc`.
"""
import pytest

import raythink_base as base
import raythink_camera as mod
from raythink_camera import RaythinkCameraClient

CAMERA_MAC = "00:1e:42:aa:bb:01"

# The nested-IPAddress schema; the flat one is covered separately.
NETWORK_TABLE = {
    "DefaultInterface": "eth0",
    "eth0": {
        "PhysicalAddress": CAMERA_MAC,
        "DhcpEnable": False,
        "IPAddress": {"IPAddress": "192.168.1.123",
                      "SubnetMask": "255.255.255.0",
                      "DefaultGateway": "192.168.1.1"},
    },
}


@pytest.fixture
def client(monkeypatch):
    """A client whose RPC2 transport is faked. `client.network` is the Network
    table getConfig hands back (mutate it to set up a read-back), and
    `client.writes` collects every setConfig call."""
    cam = RaythinkCameraClient(host="192.168.1.123")
    cam.network = {"DefaultInterface": "eth0",
                   "eth0": dict(NETWORK_TABLE["eth0"],
                                IPAddress=dict(NETWORK_TABLE["eth0"]["IPAddress"]))}
    cam.writes = []

    def fake_rpc(method, params=None, **kwargs):
        if method == "configManager.getConfig":
            name = (params or {}).get("name")
            tables = {"Network": cam.network,
                      "NTP": {"Enable": True, "Address": "192.168.88.10"}}
            return {"result": True, "params": {"table": tables.get(name, {})}}
        if method == "configManager.setConfig":
            cam.writes.append(params)
            return {"result": True}
        return {"result": True, "params": {}}

    monkeypatch.setattr(cam, "_rpc", fake_rpc)
    # Host-side effects and the retry pacing are not what these tests are about.
    monkeypatch.setattr(base, "host_iface_for", lambda ip: "en0")
    monkeypatch.setattr(base, "renew_host_dhcp", lambda iface: None)
    monkeypatch.setattr(base, "arp_table", dict)
    monkeypatch.setattr(base.time, "sleep", lambda s: None)
    return cam


def written_iface(cam):
    """The eth0 section of the last table written back to the device."""
    return cam.writes[-1]["table"]["eth0"]


# ── set_dhcp ────────────────────────────────────────────────────────────────

def test_set_dhcp_enables_dhcp_and_follows_the_lease(client, monkeypatch):
    monkeypatch.setattr(base, "find_ip_by_mac",
                        lambda mac, subnets, port: "192.168.88.57")

    row = client.set_dhcp(mac=CAMERA_MAC, subnets=["192.168.88.0/24"], wait=30)

    assert written_iface(client)["DhcpEnable"] is True
    assert row["ok"] is True
    assert "192.168.88.57" in row["actual"]
    # The client must follow the camera, so verification talks to the lease.
    assert client.host == "192.168.88.57"
    assert client.base == "http://192.168.88.57"


def test_set_dhcp_writes_the_alternate_field_name_too(client, monkeypatch):
    """Firmwares that use EnableDhcp get both fields; ones that don't must not
    grow a field they never had."""
    monkeypatch.setattr(base, "find_ip_by_mac", lambda *a, **k: "192.168.88.57")
    client.network["eth0"]["EnableDhcp"] = False

    client.set_dhcp(mac=CAMERA_MAC, subnets=["192.168.88.0/24"], wait=30)
    assert written_iface(client)["EnableDhcp"] is True

    client.writes.clear()
    del client.network["eth0"]["EnableDhcp"]
    client.set_dhcp(mac=CAMERA_MAC, subnets=["192.168.88.0/24"], wait=30)
    assert "EnableDhcp" not in written_iface(client)


def test_set_dhcp_leaves_the_static_fallback_alone(client, monkeypatch):
    """The static address stays as the camera's fallback if no lease arrives."""
    monkeypatch.setattr(base, "find_ip_by_mac", lambda *a, **k: "192.168.88.57")
    client.set_dhcp(mac=CAMERA_MAC, subnets=["192.168.88.0/24"], wait=30)
    assert written_iface(client)["IPAddress"]["IPAddress"] == "192.168.1.123"


def test_set_dhcp_keeps_looking_until_the_lease_appears(client, monkeypatch):
    """A camera needs a moment to boot and ask for a lease, so one empty sweep
    is not an answer."""
    attempts = []

    def locate(mac, subnets, port):
        attempts.append(mac)
        return "192.168.88.57" if len(attempts) > 1 else None

    monkeypatch.setattr(base, "find_ip_by_mac", locate)

    row = client.set_dhcp(mac=CAMERA_MAC, subnets=["192.168.88.0/24"], wait=5)

    assert len(attempts) == 2
    assert row["ok"] is True


def test_set_dhcp_reports_a_lease_that_never_appears(client, monkeypatch):
    monkeypatch.setattr(base, "find_ip_by_mac", lambda *a, **k: None)

    row = client.set_dhcp(mac=CAMERA_MAC, subnets=["192.168.88.0/24"], wait=0)

    assert row["ok"] is False
    assert "192.168.88.0/24" in row["actual"]
    assert client.host == "192.168.1.123"   # nowhere else to point it


def test_set_dhcp_follows_a_camera_that_is_not_serving_yet(client, monkeypatch):
    """The bench-run failure this guards against: the camera took an address but
    its web server wasn't up before the window closed. Reporting the failure is
    right; leaving the client pointed at the address the camera LEFT is not —
    verification would then be skipped on an address that is dead by definition."""
    monkeypatch.setattr(base, "find_ip_by_mac", lambda *a, **k: None)
    monkeypatch.setattr(base, "arp_table",
                        lambda: {base.canonical_mac(CAMERA_MAC):
                                 ["192.168.1.123", "192.168.88.57"]})

    row = client.set_dhcp(mac=CAMERA_MAC, subnets=["192.168.88.0/24"], wait=0)

    assert row["ok"] is False
    assert "192.168.88.57" in row["actual"]
    # The address it left is not offered as where the camera is.
    assert "192.168.1.123" not in row["actual"]
    assert client.host == "192.168.88.57"


def test_set_dhcp_explains_a_camera_it_never_saw(client, monkeypatch):
    """The other failure mode looks identical from the outside but needs a
    different fix, so the message has to distinguish them."""
    monkeypatch.setattr(base, "find_ip_by_mac", lambda *a, **k: None)

    row = client.set_dhcp(mac=CAMERA_MAC, subnets=["192.168.88.0/24"], wait=0)

    assert "no sign of" in row["actual"]
    assert "DHCP server" in row["actual"]


def test_set_dhcp_without_a_mac_still_switches_but_fails_the_check(client, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("nothing to search for without a MAC")

    monkeypatch.setattr(base, "find_ip_by_mac", boom)

    row = client.set_dhcp(mac="", subnets=["192.168.88.0/24"], wait=30)

    assert written_iface(client)["DhcpEnable"] is True   # the switch still ran
    assert row["ok"] is False
    assert "no MAC" in row["actual"]


def test_set_dhcp_survives_the_dropped_reply(client, monkeypatch):
    """The setConfig reply usually never arrives — the camera drops the link
    applying it. That is the expected path, not a failure."""
    monkeypatch.setattr(base, "find_ip_by_mac", lambda *a, **k: "192.168.88.57")

    def rpc(method, params=None, **kwargs):
        if method == "configManager.getConfig":
            return {"result": True, "params": {"table": client.network}}
        raise mod.CameraError("connection failed")

    monkeypatch.setattr(client, "_rpc", rpc)
    row = client.set_dhcp(mac=CAMERA_MAC, subnets=["192.168.88.0/24"], wait=30)
    assert row["ok"] is True


# ── static IP still disables DHCP ───────────────────────────────────────────

def test_set_static_ip_disables_dhcp(client, monkeypatch):
    monkeypatch.setattr(client, "port_open", lambda host=None: True)

    row = client.set_static_ip("192.168.88.30", "255.255.255.0", "192.168.88.1", wait=30)

    iface = written_iface(client)
    assert iface["DhcpEnable"] is False
    assert iface["IPAddress"]["IPAddress"] == "192.168.88.30"
    assert row["ok"] is True
    assert client.host == "192.168.88.30"


# ── verification ────────────────────────────────────────────────────────────

def check(rows, item):
    return next(r for r in rows if r["item"] == item)


def verify(client, monkeypatch, **kwargs):
    """verify_configuration with the non-network checks stubbed out."""
    monkeypatch.setattr(client, "onvif_get_users", lambda pw: (True, "authenticated", []))
    client.password = "pw"
    return client.verify_configuration(new_password="pw", ntp_server="192.168.88.10",
                                       **kwargs)


def test_verify_dhcp_passes_when_the_camera_holds_a_lease(client, monkeypatch):
    client.network["eth0"]["DhcpEnable"] = True
    client.network["eth0"]["IPAddress"]["IPAddress"] = "192.168.88.57"

    rows = verify(client, monkeypatch, dhcp=True)

    assert check(rows, "DHCP")["ok"] is True
    assert "192.168.88.57" in check(rows, "DHCP")["actual"]
    # The DHCP server chose these, so there is nothing of ours to assert.
    assert check(rows, "subnet mask")["ok"] is None
    assert check(rows, "gateway")["ok"] is None


def test_verify_dhcp_fails_when_dhcp_did_not_take(client, monkeypatch):
    client.network["eth0"]["DhcpEnable"] = False
    rows = verify(client, monkeypatch, dhcp=True)
    assert check(rows, "DHCP")["ok"] is False


def test_verify_dhcp_fails_without_an_address(client, monkeypatch):
    client.network["eth0"]["DhcpEnable"] = True
    client.network["eth0"]["IPAddress"]["IPAddress"] = ""
    rows = verify(client, monkeypatch, dhcp=True)
    assert check(rows, "DHCP")["ok"] is False
    assert "no address" in check(rows, "DHCP")["actual"]


def test_verify_static_still_compares_every_field(client, monkeypatch):
    client.network["eth0"]["IPAddress"] = {"IPAddress": "192.168.88.30",
                                           "SubnetMask": "255.255.255.0",
                                           "DefaultGateway": "192.168.88.1"}
    rows = verify(client, monkeypatch, ip="192.168.88.30", netmask="255.255.255.0",
                  gateway="192.168.88.1")
    assert check(rows, "static IP")["ok"] is True
    assert check(rows, "subnet mask")["ok"] is True
    assert check(rows, "gateway")["ok"] is True
