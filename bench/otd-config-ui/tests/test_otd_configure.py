"""The OTD500 configure pipeline: the time source, and where it sits (TEC-857).

The counterpart to `rutm-config-ui/tests/test_rutm_configure.py`, and about the
same thing — what runs, in what order, and what each step is handed. The
SIM-switch half of this pipeline has its own ordering tests in
`test_sim_switch.py`; the harness both share lives in `conftest.py`.

Two placements are load-bearing here:

* **Before the firmware flash.** The NTP and DHCP steps are pure UCI, so they
  need nothing online and a keep-settings sysupgrade preserves them. Below the
  flash they would still work, right up until a run whose firmware step reboots
  into a device nobody has re-pointed at a time server.
* **The pool and the server are not independent settings.** The address kept
  out of the DHCP pool IS the NTP server: the downstream router's static WAN
  address, which it holds without defending. Configuring one and not the other
  is a collision that surfaces when the second device boots.

What the steps WRITE is pinned in bench-core's `test_ntp_client_applied.py` and
`test_ntp_path_applied.py`; this file is only about the pipeline.
"""
import otd_configure as mod

SETTINGS = {
    "timezone": "Asia/Jerusalem",
    "name_prefix": "otd-",
    "sim_4g_only": False,
    "sim_switch": {"enabled": False},
    "firmware": {"mode": "none"},
    "rms": {"enabled": False},
    "tailscale": {"enabled": False},
    "esim": {"enabled": False},
}


# ── ordering ─────────────────────────────────────────────────────────────────

def test_the_time_source_is_set_before_a_firmware_flash(run_pipeline):
    client = run_pipeline({**SETTINGS, "firmware": {"mode": "local"}})
    assert client.calls.index("set_ntp_client") \
        < client.calls.index("upgrade_firmware")
    assert client.calls.index("set_dhcp_pool") \
        < client.calls.index("upgrade_firmware")


def test_the_time_source_needs_nothing_online(run_pipeline):
    # An OTD500 on the bench usually has no data connection at all (no outdoor
    # antenna), so a step that waited for one would fail on most units.
    client = run_pipeline(SETTINGS)
    assert "set_ntp_client" in client.calls
    assert "ensure_online" not in client.calls


# ── what the steps are handed ────────────────────────────────────────────────

def test_the_device_aims_at_the_routers_wan_not_at_the_server(run_pipeline,
                                                              stub_client):
    # The asymmetry TEC-857 turns on. The OTD500 is UPSTREAM of the router, so
    # the server on that router's LAN is behind its NAT and unreachable —
    # RutOS fails that silently and falls back to the modem clock, which is why
    # aiming at the server directly looks fine and drifts.
    run_pipeline(SETTINGS, stub_client)
    assert stub_client.ntp_server == mod.DEFAULT_OTD_NTP_SERVER == "192.168.1.2"
    # Not the time server itself: that is the config that looks right, is
    # unreachable from here, and leaves the unit running on the modem clock.
    assert stub_client.ntp_server != "192.168.88.10"


def test_the_pool_is_kept_clear_of_the_address_the_router_holds(run_pipeline,
                                                                stub_client):
    run_pipeline(SETTINGS, stub_client)
    assert stub_client.pool_reserved == mod.DEFAULT_OTD_NTP_SERVER


def test_a_station_can_reserve_a_different_address(run_pipeline, stub_client):
    run_pipeline({**SETTINGS, "dhcp_pool": {"reserved": "192.168.1.9"}},
                 stub_client)
    assert stub_client.pool_reserved == "192.168.1.9"


# ── opting out ───────────────────────────────────────────────────────────────

def test_the_time_source_is_applied_by_default(run_pipeline):
    # The point of TEC-857 is that the bench does this rather than an operator
    # remembering a runbook, so an absent config block must not mean "skip".
    client = run_pipeline(SETTINGS)
    assert "set_ntp_client" in client.calls
    assert "set_dhcp_pool" in client.calls


def test_the_steps_can_still_be_switched_off(run_pipeline):
    client = run_pipeline({**SETTINGS, "ntp": {"enabled": False},
                           "dhcp_pool": {"enabled": False}})
    assert "set_ntp_client" not in client.calls
    assert "set_dhcp_pool" not in client.calls
    # And their rows go with them, rather than showing as skipped on every unit.
    assert "ntp_client_check" not in client.calls
    assert "dhcp_pool_check" not in client.calls


# ── failures are recorded, not swallowed ─────────────────────────────────────

def test_a_device_that_refuses_the_ntp_write_fails_one_step_not_the_run(
        stub_client):
    def refuse(*_a, **_k):
        stub_client.calls.append("set_ntp_client")
        raise SystemExit("This device has no 'ntpclient' package")

    stub_client.set_ntp_client = refuse
    result = mod.configure_device(stub_client, label_password="pw",
                                  site_name="haifa", settings=SETTINGS)
    assert result["ok"] is False
    assert len(result["failures"]) == 1
    assert result["failures"][0].startswith("ntp: ")
    # The run carried on: the operator gets the rest of the steps and the
    # verification table, not a run that stopped at the first refusal.
    assert "set_dhcp_pool" in stub_client.calls
