"""The RUTM08 configure pipeline — what runs, and in what order (TEC-857).

Ordering is the whole subject here, because two steps in this pipeline destroy
the connectivity the steps around them need, and the damage is invisible until a
run reaches the bench:

* **`wan-static` ends internet.** Pinning the WAN to the fleet-constant address
  stops the port taking a lease from the bench uplink and starts it waiting for
  a gateway that only exists at the site. FOTA, RMS registration and Tailscale
  all need that uplink, so every one of them has to be finished first.
* **`move_lan` ends the session.** It has always run last, and the WAN block
  must not push past it — after the move the tool is re-establishing itself on
  192.168.88.1 and a failure there is indistinguishable from a stale station
  DHCP lease.

The `ntp` step is the opposite case and sits at the top with the timezone it
shares a zoneName with: pure UCI, nothing online needed, and a keep-settings
sysupgrade preserves it.

No hardware: `StubClient` records the calls and returns the little that the
pipeline reads back. What the individual steps WRITE is pinned in
bench-core's `test_ntp_client_applied.py` / `test_ntp_path_applied.py`; this
file is only about the sequence.
"""
import pytest

import rutm_configure as mod

FW_VERSION = "RUTM_R_00.07.20"

SETTINGS = {
    "timezone": "Asia/Jerusalem",
    "name_prefix": "rut-",
    "firmware": {"mode": "none"},
    "rms": {"enabled": False},
    "tailscale": {"enabled": False},
    "lan_ip": "192.168.88.1",
}


class StubClient:
    """Records what the pipeline calls. Enough client surface for a run with
    the network-dependent steps switched off."""
    fw_target = ""

    def __init__(self):
        self.calls: list[str] = []
        self.verified_with: dict = {}
        self.ntp_server = ""
        self.wan_kwargs: dict = {}
        self.forward_kwargs: dict = {}

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append(name)
        return record

    def get_identity(self):
        return {"model": "RUTM08", "serial": "SN-1", "mac": "aa:bb:cc:dd:ee:01",
                "firmware": FW_VERSION}

    def verify_identity(self, identity, expected):
        return []

    def verify_configuration(self, **kwargs):
        self.verified_with = kwargs
        return []

    def ensure_online(self, *_a, **_k):
        self.calls.append("ensure_online")
        return True

    def set_ntp_client(self, server, **kwargs):
        self.calls.append("set_ntp_client")
        self.ntp_server = server

    def set_wan_static(self, ipaddr, **kwargs):
        self.calls.append("set_wan_static")
        self.wan_kwargs = {"ipaddr": ipaddr, **kwargs}

    def set_ntp_port_forward(self, **kwargs):
        self.calls.append("set_ntp_port_forward")
        self.forward_kwargs = kwargs

    def move_lan(self, new_ip, **kwargs):
        self.calls.append("move_lan")
        return {"item": "LAN IP", "expected": new_ip,
                "actual": f"answering on {new_ip}", "ok": True}

    # The rows the pipeline appends. Real rows so they can be rendered; what
    # they assert lives in the bench-core suites.
    def ntp_client_check(self, server, **kwargs):
        self.calls.append("ntp_client_check")
        return {"item": "NTP client", "expected": server, "actual": server,
                "ok": True}

    def wan_static_check(self, ipaddr, **kwargs):
        self.calls.append("wan_static_check")
        return {"item": "WAN address", "expected": ipaddr, "actual": ipaddr,
                "ok": True}

    def ntp_forward_check(self, **kwargs):
        self.calls.append("ntp_forward_check")
        return {"item": "NTP forward", "expected": "", "actual": "", "ok": True}

    def ntp_daemon_check(self):
        self.calls.append("ntp_daemon_check")
        return {"item": "NTP daemon", "expected": "running", "actual": "running",
                "ok": True}


def run_pipeline(settings) -> StubClient:
    client = StubClient()
    result = mod.configure_rutm(client, site_name="haifa", initial_password="pw",
                                settings=settings)
    assert result["failures"] == []
    return client


def with_network(**overrides):
    """Settings with everything that needs the uplink switched on."""
    return {**SETTINGS,
            "firmware": {"mode": "fota"},
            "rms": {"enabled": True, "auth_code": "x"},
            "tailscale": {"enabled": True, "auth_key": "tskey-x"},
            "wan": {"enabled": True},
            "ntp_forward": {"enabled": True},
            **overrides}


# ── the ordering that matters ────────────────────────────────────────────────

def test_the_wan_is_pinned_only_after_everything_that_needs_the_uplink():
    # The step that ends internet on the bench. Anything above this line that
    # needed the network would fail on a real router, and the failure would look
    # like a flaky uplink rather than a pipeline that is in the wrong order.
    client = run_pipeline(with_network())
    pinned = client.calls.index("set_wan_static")
    for earlier in ("upgrade_firmware", "enable_rms", "join_tailscale"):
        assert client.calls.index(earlier) < pinned, earlier


def test_the_wan_is_pinned_before_the_lan_move_ends_the_session():
    # After move_lan the tool is re-establishing itself on the new address, so a
    # step that ran later would be attempting UCI over a connection that is
    # coming back up — and its failure would be unattributable.
    client = run_pipeline(with_network())
    assert client.calls.index("set_wan_static") < client.calls.index("move_lan")
    assert client.calls.index("set_ntp_port_forward") < client.calls.index("move_lan")


def test_the_time_source_is_set_before_a_firmware_flash():
    # Pure UCI, and a keep-settings sysupgrade preserves it. Landing after the
    # flash would leave a window where a reboot ships a device on no time
    # source at all.
    client = run_pipeline({**SETTINGS, "firmware": {"mode": "local"}})
    assert client.calls.index("set_ntp_client") \
        < client.calls.index("upgrade_firmware")


def test_the_forward_is_written_before_the_address_it_will_be_reached_on():
    # Both land in the same run, but the forward is the rule and the WAN pin is
    # what makes it reachable — writing the rule first means the router is never
    # briefly reachable on the constant address with no rule behind it.
    client = run_pipeline(with_network())
    assert client.calls.index("set_ntp_port_forward") \
        < client.calls.index("set_wan_static")


# ── what each step is given ──────────────────────────────────────────────────

def test_the_router_points_at_the_server_directly_not_at_its_own_wan():
    # The asymmetry that TEC-857 turns on: the OTD500 upstream aims at this
    # router's WAN address because it cannot address the server, but the router
    # IS the gateway of the server's LAN and must aim at the real thing.
    client = run_pipeline(SETTINGS)
    assert client.ntp_server == mod.DEFAULT_RUTM_NTP_SERVER
    assert client.ntp_server != mod.DEFAULT_RUTM_WAN_IP


def test_the_fleet_constant_address_is_what_gets_applied():
    client = run_pipeline(with_network())
    assert client.wan_kwargs["ipaddr"] == mod.DEFAULT_RUTM_WAN_IP
    assert client.wan_kwargs["gateway"] == mod.DEFAULT_RUTM_WAN_GATEWAY


def test_the_forward_is_restricted_to_the_device_upstream():
    # An unrestricted DNAT on the WAN would let anything on that segment reach
    # the site's time server through the router.
    client = run_pipeline(with_network())
    assert client.forward_kwargs["src_ip"] == mod.DEFAULT_RUTM_WAN_GATEWAY
    assert client.forward_kwargs["dest_ip"] == mod.DEFAULT_RUTM_NTP_SERVER


def test_a_station_can_override_the_addresses():
    client = run_pipeline(with_network(
        wan={"enabled": True, "ipaddr": "10.0.0.2", "gateway": "10.0.0.1"},
        ntp_forward={"enabled": True, "dest_ip": "10.0.1.10",
                     "src_ip": "10.0.0.1"}))
    assert client.wan_kwargs["ipaddr"] == "10.0.0.2"
    assert client.forward_kwargs["dest_ip"] == "10.0.1.10"


# ── what an unopted station does ─────────────────────────────────────────────

def test_the_wan_block_is_opt_in():
    # It ends internet on the bench and collides with the TSW202's factory
    # address, so a station that has not asked for it must not get it.
    client = run_pipeline(SETTINGS)
    assert "set_wan_static" not in client.calls
    assert "set_ntp_port_forward" not in client.calls


def test_the_time_source_is_not_opt_in():
    # The whole point of TEC-857 is that the bench applies this by default
    # rather than an operator remembering a runbook.
    assert "set_ntp_client" in run_pipeline(SETTINGS).calls


def test_the_time_source_can_still_be_switched_off():
    client = run_pipeline({**SETTINGS, "ntp": {"enabled": False}})
    assert "set_ntp_client" not in client.calls
    assert "ntp_client_check" not in client.calls


# ── failures are recorded, not swallowed ─────────────────────────────────────

@pytest.mark.parametrize("step,label", [
    ("set_ntp_client", "ntp"),
    ("set_wan_static", "wan-static"),
    ("set_ntp_port_forward", "ntp-forward"),
])
def test_a_failing_step_is_reported_rather_than_completing_the_run(step, label):
    client = StubClient()

    def boom(*_a, **_k):
        raise SystemExit("device said no")

    setattr(client, step, boom)
    result = mod.configure_rutm(client, site_name="haifa", initial_password="pw",
                                settings=with_network())
    assert result["ok"] is False
    assert any(f.startswith(f"{label}:") for f in result["failures"]), result["failures"]
