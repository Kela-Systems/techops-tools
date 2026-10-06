"""The RUTM08's two roles: server (main router of a server box) and edge (the
router inside a Gotcha edge box).

Three properties carry the feature, and most of this file is about them:

* **Server does not move.** With the role on server, the settings a run is
  handed are exactly the config it was handed before roles existed, so every
  write, row, record and label stays as it was.
* **Edge order.** The edge box's network (forwards, WAN access, DHCP pool) is
  pure UCI and lands before the WAN pin, which still comes after everything
  needing the bench uplink; the LAN move to 192.168.89.1 is still last. A guard
  stops a run that would put a second 192.168.88.1 on the server-box subnet
  before anything is touched.
* **A unit is checked as what it is.** Verify takes the role from the unit's
  own configure record, not from the toggle, and starts from the BASE config
  so the record's role can be merged at all.

No hardware: a stub client records calls; detection and the pipelines are
faked the same way `test_rutm_app.py` fakes them.
"""
import asyncio
import json
from pathlib import Path

import pytest

import rutm_app as app_mod
import rutm_configure as mod
from rutm_app import RoleStore

cfg = app_mod.configurator

FACTORY = "192.168.1.1"
SERVER_LAN = "192.168.88.1"
EDGE_LAN = "192.168.89.1"
EXAMPLE = Path(mod.__file__).resolve().parent / "config" / "rutm.config.example.json"

BASE = {
    "host": FACTORY,
    "new_password": "shared-pw",
    "timezone": "Asia/Jerusalem",
    "name_prefix": "rut-",
    "lan_ip": SERVER_LAN,
    "ntp": {"_comment": "server's", "enabled": True, "server": "192.168.88.10",
            "interval": 3600},
    "wan": {"enabled": False, "ipaddr": "192.168.1.2", "netmask": "255.255.255.0",
            "gateway": "192.168.1.1", "dns": ""},
    "ntp_forward": {"enabled": True, "dest_ip": "192.168.88.10", "src_ip": "192.168.1.1"},
    "firmware": {"mode": "none"},
    "rms": {"enabled": False},
    "tailscale": {"enabled": False},
}


def example_config() -> dict:
    """The example as `load_config` reads it: top-level `_` keys dropped."""
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


# ── the merge ────────────────────────────────────────────────────────────────

def test_dicts_merge_and_lists_and_scalars_replace():
    cfg_ = {**BASE, "roles": {"edge": {
        "wan": {"ipaddr": "192.168.88.21"},
        "port_forwards": [{"name": "only", "ext_port": 9, "dest_ip": "192.168.89.9",
                           "dest_port": 9}],
        "timezone": "UTC"}}}
    eff = mod.effective_settings(cfg_, mod.ROLE_EDGE)
    assert eff["wan"]["ipaddr"] == "192.168.88.21"
    # The rest of the block still comes from the edge defaults.
    assert eff["wan"]["gateway"] == "192.168.88.1" and eff["wan"]["enabled"] is True
    assert [r["name"] for r in eff["port_forwards"]] == ["only"]
    assert eff["timezone"] == "UTC"


def test_underscore_keys_in_a_role_block_are_ignored():
    cfg_ = {**BASE, "roles": {"edge": {"_comment": "x", "wan": {"_note": "y"}}}}
    eff = mod.effective_settings(cfg_, mod.ROLE_EDGE)
    assert "_comment" not in eff
    assert "_note" not in eff["wan"]


def test_the_roles_block_is_not_part_of_the_settings():
    assert "roles" not in mod.effective_settings({**BASE, "roles": {}}, mod.ROLE_SERVER)
    assert "roles" not in mod.effective_settings({**BASE, "roles": {}}, mod.ROLE_EDGE)


def test_the_merge_does_not_mutate_the_config():
    cfg_ = json.loads(json.dumps({**BASE, "roles": {"edge": {"wan": {"ipaddr": "x"}}}}))
    snapshot = json.dumps(cfg_, sort_keys=True)
    mod.effective_settings(cfg_, mod.ROLE_EDGE)["wan"]["gateway"] = "changed"
    assert json.dumps(cfg_, sort_keys=True) == snapshot


def test_an_unknown_role_is_refused():
    with pytest.raises(SystemExit, match="Unknown RUTM08 role"):
        mod.effective_settings(BASE, "router")


# ── server identity ──────────────────────────────────────────────────────────

def test_server_settings_are_the_config_unchanged():
    assert mod.effective_settings(BASE, mod.ROLE_SERVER) == BASE


def test_server_settings_from_the_example_are_the_example_minus_roles():
    example = example_config()
    assert "roles" in example
    expected = {k: v for k, v in example.items() if k != "roles"}
    assert mod.effective_settings(example, mod.ROLE_SERVER) == expected


def test_the_configurators_server_run_config_is_todays(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "roles", RoleStore(tmp_path / "role-state.json"))
    monkeypatch.setattr(cfg, "cfg", json.loads(json.dumps(BASE)))
    assert cfg.run_config() == json.loads(json.dumps(BASE))


# ── edge defaults ────────────────────────────────────────────────────────────

def test_edge_works_on_a_config_with_no_roles_block():
    eff = mod.effective_settings(BASE, mod.ROLE_EDGE)
    assert eff["name_prefix"] == "rut-edge-"
    assert eff["lan_ip"] == EDGE_LAN
    assert eff["wan"] == {"enabled": True, "ipaddr": "192.168.88.20",
                          "netmask": "255.255.255.0", "gateway": "192.168.88.1",
                          "dns": "192.168.88.1"}
    assert eff["ntp_forward"]["enabled"] is False
    assert eff["dhcp"] == {"enabled": True, "start": 200, "limit": 50}
    assert eff["wan_access"] == {"webui": True, "ssh": True}
    assert len(eff["port_forwards"]) == 9
    assert mod.edge_settings_problems(eff) == []


def test_the_edge_ntp_interval_is_60_even_when_the_server_config_says_3600():
    # An edge router has no RTC and often boots before the server answers; at
    # 3600 it could sit an hour unable to reach RMS and Tailscale.
    assert BASE["ntp"]["interval"] == 3600
    eff = mod.effective_settings(BASE, mod.ROLE_EDGE)
    assert eff["ntp"]["interval"] == 60
    assert eff["ntp"]["server"] == "192.168.88.10"
    assert eff["ntp"]["enabled"] is True


def test_the_example_edge_block_agrees_with_the_built_in_defaults():
    # Two copies of one intent; if they drift, a station that copies the
    # example provisions something different from one that never did.
    def values(v):
        """Comments come from each base's own top-level blocks; compare values."""
        if isinstance(v, dict):
            return {k: values(x) for k, x in v.items() if not k.startswith("_")}
        return v

    from_example = mod.effective_settings(example_config(), mod.ROLE_EDGE)
    from_defaults = mod.effective_settings(BASE, mod.ROLE_EDGE)
    for key in ("name_prefix", "lan_ip", "ntp", "wan", "ntp_forward", "dhcp",
                "wan_access", "port_forwards"):
        assert values(from_example[key]) == values(from_defaults[key]), key


# ── the edge pipeline ────────────────────────────────────────────────────────

class StubClient:
    """Records what the pipeline calls, with arguments. Every `*_check` returns
    a passing row so the verification block can render."""
    fw_target = ""

    def __init__(self):
        self.calls: list[str] = []
        self.args: dict = {}

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append(name)
            self.args[name] = (args, kwargs)
            if name.endswith("_check"):
                return {"item": name, "expected": "", "actual": "", "ok": True}
            return None
        return record

    def get_identity(self):
        return {"model": "RUTM08", "serial": "SN-1", "mac": "aa:bb:cc:dd:ee:01",
                "firmware": "RUTM_R_00.07.24.3"}

    def verify_configuration(self, **kwargs):
        self.calls.append("verify_configuration")
        self.args["verify_configuration"] = ((), kwargs)
        return []

    def ensure_online(self, *_a, **_k):
        return True

    def ntp_daemon_check(self):
        return {"item": "NTP daemon", "expected": "", "actual": "", "ok": True}

    def move_lan(self, new_ip, **kwargs):
        self.calls.append("move_lan")
        self.args["move_lan"] = ((new_ip,), kwargs)
        return {"item": "LAN IP", "expected": new_ip, "actual": "ok", "ok": True}

    def ssh_exec(self, *_a, **_k):
        return ""


def online(**over):
    """Base settings with everything needing the uplink switched on."""
    return {**BASE, "firmware": {"mode": "fota"},
            "rms": {"enabled": True, "auth_code": "x"},
            "tailscale": {"enabled": True, "auth_key": "tskey-x"}, **over}


def run_edge(base=None, **over):
    client = StubClient()
    settings = mod.effective_settings({**(base or online()), **over}, mod.ROLE_EDGE)
    result = mod.configure_rutm(client, site_name="kela-fob-14", initial_password="pw",
                                settings=settings, role=mod.ROLE_EDGE)
    return client, result


def test_the_edge_run_succeeds_and_is_named_rut_edge():
    _, result = run_edge()
    assert result["failures"] == [] and result["ok"] is True
    assert result["name"] == "rut-edge-kela-fob-14"


def test_the_wan_is_pinned_after_tailscale():
    client, _ = run_edge()
    pinned = client.calls.index("set_wan_static")
    for earlier in ("upgrade_firmware", "enable_rms", "join_tailscale"):
        assert client.calls.index(earlier) < pinned, earlier


def test_the_three_edge_steps_land_after_tailscale_and_before_the_wan_pin():
    client, _ = run_edge()
    pinned = client.calls.index("set_wan_static")
    joined = client.calls.index("join_tailscale")
    for step in ("set_port_forwards", "set_wan_access", "set_dhcp_pool"):
        assert joined < client.calls.index(step) < pinned, step


def test_the_lan_move_is_the_last_call_and_goes_to_the_edge_lan():
    client, _ = run_edge()
    assert client.calls[-1] == "move_lan"
    assert client.args["move_lan"][0] == (EDGE_LAN,)


def test_the_ntp_forward_never_runs_on_an_edge_router():
    # Even with a server config that switches it on, and even if a role block
    # tried to: the forward belongs to the server router.
    client, result = run_edge(roles={"edge": {"ntp_forward": {"enabled": True}}})
    assert "set_ntp_port_forward" not in client.calls
    assert "ntp_forward_check" not in client.calls
    assert result["ok"] is True


def test_the_edge_steps_are_given_the_edge_values():
    client, _ = run_edge()
    assert client.args["set_wan_static"][0] == ("192.168.88.20",)
    assert client.args["set_wan_static"][1]["gateway"] == "192.168.88.1"
    assert client.args["set_wan_static"][1]["dns"] == "192.168.88.1"
    assert client.args["set_dhcp_pool"] == ((200, 50), {"serve": True})
    assert client.args["set_wan_access"][1] == {"webui": True, "ssh": True}
    assert len(client.args["set_port_forwards"][0][0]) == 9
    assert client.args["set_ntp_client"][1]["interval"] == 60


def test_the_edge_verify_rows_are_read_backs_including_the_statics():
    client, _ = run_edge()
    for check in ("wan_static_check", "port_forwards_check", "wan_access_check",
                  "dhcp_pool_check"):
        assert check in client.calls, check
    reserved = client.args["dhcp_pool_check"][1]["reserved"]
    assert reserved == ["192.168.89.30", "192.168.89.50", "192.168.89.51",
                        "192.168.89.52", "192.168.89.53", "192.168.89.60",
                        "192.168.89.61", "192.168.89.70"]
    assert client.args["dhcp_pool_check"][1]["require_served"] is True


def test_the_result_says_which_role_and_where_the_router_ended():
    _, result = run_edge()
    assert (result["role"], result["lan_ip"], result["wan_ip"]) == (
        "edge", EDGE_LAN, "192.168.88.20")


def test_the_server_pipeline_runs_none_of_the_edge_steps():
    client = StubClient()
    mod.configure_rutm(client, site_name="haifa", initial_password="pw",
                       settings=mod.effective_settings(online(), mod.ROLE_SERVER))
    for step in ("set_port_forwards", "set_wan_access", "set_dhcp_pool",
                 "port_forwards_check", "wan_access_check", "dhcp_pool_check"):
        assert step not in client.calls, step
    assert client.args["move_lan"][0] == (SERVER_LAN,)


# ── the edge guard ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("edge_block,match", [
    ({"lan_ip": SERVER_LAN}, "server LAN"),
    ({"lan_ip": "192.168.88.50"}, "inside the WAN subnet"),
    ({"wan": {"enabled": False}}, "WAN is not enabled"),
    ({"port_forwards": [{"name": "ssh", "ext_port": 22, "dest_ip": "192.168.89.30",
                         "dest_port": 22}]}, "wan_access.ssh"),
    ({"port_forwards": [{"name": "web", "ext_port": 443, "dest_ip": "192.168.89.30",
                         "dest_port": 443}]}, "wan_access.webui"),
    ({"port_forwards": [{"name": "web", "ext_port": 80, "dest_ip": "192.168.89.30",
                         "dest_port": 80}]}, "wan_access.webui"),
])
def test_the_guard_stops_the_run_before_anything_is_touched(edge_block, match):
    client = StubClient()
    settings = mod.effective_settings({**online(), "roles": {"edge": edge_block}},
                                      mod.ROLE_EDGE)
    with pytest.raises(SystemExit, match=match):
        mod.configure_rutm(client, site_name="x", initial_password="pw",
                           settings=settings, role=mod.ROLE_EDGE)
    assert client.calls == []        # not even the login


def test_a_forward_on_a_service_port_is_fine_when_that_access_is_off():
    settings = mod.effective_settings({**BASE, "roles": {"edge": {
        "wan_access": {"webui": True, "ssh": False},
        "port_forwards": [{"name": "cam-ssh", "ext_port": 22,
                           "dest_ip": "192.168.89.30", "dest_port": 22}]}}},
        mod.ROLE_EDGE)
    assert mod.edge_settings_problems(settings) == []


# ── verify: which role a unit is checked as ──────────────────────────────────

def verify(resolve_to, *, role="", fallback_role=mod.ROLE_SERVER, base=None):
    client = StubClient()
    result = mod.verify_rutm(client, settings=base or BASE,
                             resolve=lambda identity: (resolve_to, None),
                             role=role, fallback_role=fallback_role)
    return client, result


def test_a_record_saying_edge_is_checked_as_edge_whatever_the_fallback():
    client, result = verify({"hostname": "rut-edge-x", "role": "edge"})
    assert result["role"] == "edge"
    assert "port_forwards_check" in client.calls
    assert client.args["lan_ip_check"][0] == (EDGE_LAN,)
    assert client.args["verify_configuration"][1]["hostname"] == "rut-edge-x"


def test_a_record_saying_server_is_checked_as_server_with_the_toggle_on_edge():
    client, result = verify({"hostname": "rut-haifa", "role": "server"},
                            fallback_role=mod.ROLE_EDGE)
    assert result["role"] == "server"
    assert "port_forwards_check" not in client.calls
    assert client.args["lan_ip_check"][0] == (SERVER_LAN,)


def test_a_record_from_before_roles_reads_as_server():
    client, result = verify({"hostname": "rut-haifa"}, fallback_role=mod.ROLE_EDGE)
    assert result["role"] == "server"
    assert client.args["lan_ip_check"][0] == (SERVER_LAN,)


def test_with_no_record_the_station_fallback_decides():
    client, result = verify({}, fallback_role=mod.ROLE_EDGE)
    assert result["role"] == "edge"
    assert client.args["lan_ip_check"][0] == (EDGE_LAN,)


def test_an_explicit_role_beats_the_record():
    _, result = verify({"hostname": "rut-haifa", "role": "server"}, role="edge")
    assert result["role"] == "edge"
    # And an operator override, which reaches `expected` ahead of the record.
    _, result = verify({"hostname": "rut-haifa", "role": "edge"})
    assert result["role"] == "edge"


def test_a_site_name_override_uses_the_checked_roles_prefix():
    client = StubClient()
    result = mod.verify_rutm(client, settings=BASE, site_name="kela-fob-14",
                             resolve=lambda identity: ({"hostname": "x", "role": "edge"},
                                                       None))
    assert result["name"] == "rut-edge-kela-fob-14"


def test_the_app_verifies_from_the_base_config_not_the_toggles(monkeypatch, tmp_path):
    # The case that a run_cfg already merged for the toggle would get wrong:
    # toggle on edge, record says server -> server rows, server LAN.
    store = RoleStore(tmp_path / "role-state.json")
    store.set(mod.ROLE_EDGE)
    monkeypatch.setattr(cfg, "roles", store)
    monkeypatch.setattr(cfg, "cfg", json.loads(json.dumps(BASE)))
    monkeypatch.setattr(cfg, "verify_resolver", lambda inputs: (
        lambda identity: ({"hostname": "rut-haifa", "role": "server"}, None)))
    client = StubClient()
    result = cfg.verify_pipeline(client, cfg.run_config(), {"host": SERVER_LAN})
    assert result["role"] == "server"
    assert "port_forwards_check" not in client.calls
    assert client.args["lan_ip_check"][0] == (SERVER_LAN,)


# ── the app: store, detection, route, record ─────────────────────────────────

@pytest.fixture
def station(monkeypatch, tmp_path):
    """The configurator on a known config, with its role file in tmp_path."""
    store = RoleStore(tmp_path / "role-state.json")
    monkeypatch.setattr(cfg, "roles", store)
    monkeypatch.setattr(cfg, "cfg", json.loads(json.dumps(BASE)))
    monkeypatch.setattr(cfg, "_save_log", lambda entry: None)
    cfg.state.update(cfg.initial_state())
    cfg.state["config_loaded"] = True
    yield store
    cfg.state.update(cfg.initial_state())


def set_detection(monkeypatch, host, mac="aa:bb:cc:dd:ee:02"):
    monkeypatch.setattr(cfg, "_detect_host", lambda *a, **k: host)
    monkeypatch.setattr(app_mod, "read_device_mac", lambda *a, **k: mac)


def poll():
    async def run():
        await cfg.poll_once(asyncio.get_running_loop())
    asyncio.run(run())


def route(path):
    return next(r for r in app_mod.app.routes if getattr(r, "path", None) == path)


def post_role(role):
    return asyncio.run(route("/api/role").endpoint(app_mod.RoleBody(role=role)))


def post_configure(site="kela-fob-14"):
    return asyncio.run(route("/api/configure").endpoint(
        app_mod.ConfigureBody(site_name=site, initial_password="pw")))


def test_the_store_defaults_to_server_and_persists(tmp_path):
    path = tmp_path / "role-state.json"
    assert RoleStore(path).role == "server"
    RoleStore(path).set("edge")
    assert RoleStore(path).role == "edge"
    assert json.loads(path.read_text()) == {"role": "edge"}


def test_an_unreadable_or_unknown_state_file_reads_as_server(tmp_path):
    path = tmp_path / "role-state.json"
    path.write_text("{not json")
    assert RoleStore(path).role == "server"
    path.write_text(json.dumps({"role": "router"}))
    assert RoleStore(path).role == "server"


def test_the_role_route_switches_and_persists(station):
    state = post_role("edge")
    assert state["role"] == "edge"
    assert station.role == "edge"
    assert state["name_prefix"] == "rut-edge-"
    assert state["final_lan_ip"] == EDGE_LAN
    assert state["wan_summary"] == "192.168.88.20/255.255.255.0 via 192.168.88.1"
    assert state["dhcp_pool"] == ".200-.249"
    assert state["port_forward_count"] == 9


def test_the_role_does_not_touch_the_config_or_its_hash(station):
    before = (json.dumps(cfg.cfg, sort_keys=True), cfg.config_hash)
    post_role("edge")
    assert (json.dumps(cfg.cfg, sort_keys=True), cfg.config_hash) == before


def test_an_unknown_role_is_refused_by_the_route(station):
    assert "error" in post_role("router")
    assert station.role == "server"


def test_the_role_cannot_change_mid_run(station):
    cfg.state["busy"] = True
    answer = post_role("edge")
    assert "error" in answer and "in progress" in answer["error"]
    assert station.role == "server"


def test_detection_probes_the_factory_address_then_both_final_lans(station, monkeypatch):
    tried = []

    def refuse(addr, timeout):
        tried.append(addr[0])
        raise OSError("nothing there")

    monkeypatch.setattr(app_mod.socket, "create_connection", refuse)
    assert cfg._detect_host() is None
    assert tried == [FACTORY, SERVER_LAN, EDGE_LAN]


def test_a_unit_on_the_edge_lan_is_recognised_as_edge(station, monkeypatch):
    set_detection(monkeypatch, EDGE_LAN)
    poll()
    assert cfg.state["detected_role"] == "edge"
    assert cfg.state["at_final_lan"] is False       # not the SELECTED role's LAN
    assert "Edge" in cfg.state["message"]
    assert station.role == "server"                 # never flipped for the operator


def test_configure_is_refused_on_the_other_roles_lan_but_verify_is_not(station, monkeypatch):
    set_detection(monkeypatch, EDGE_LAN)
    poll()
    answer = post_configure()
    assert "error" in answer and "Switch the role to Edge" in answer["error"]
    seen = {}
    monkeypatch.setattr(cfg, "_do_verify", lambda inputs: seen.update(inputs) or {
        "ok": True, "name": "rut-edge-x", "hostname": "x", "role": "edge",
        "lan_ip": EDGE_LAN, "wan_ip": "192.168.88.20",
        "identity": {"serial": "SN-1", "mac": "aa", "model": "RUTM08",
                     "firmware": "f"},
        "warnings": [], "error": None, "steps": [], "verification": [], "log": ""})
    from bench_core.bench_ui import VerifyBody
    assert "error" not in asyncio.run(route("/api/verify").endpoint(VerifyBody()))
    # With no record, the unit would be checked as what it answered as.
    assert seen["fallback_role"] == "edge"


def test_on_the_selected_roles_lan_it_is_already_provisioned(station, monkeypatch):
    station.set("edge")
    set_detection(monkeypatch, EDGE_LAN)
    poll()
    assert cfg.state["detected_role"] == "edge"
    assert cfg.state["at_final_lan"] is True
    set_detection(monkeypatch, SERVER_LAN)
    poll()
    assert cfg.state["detected_role"] == "server"
    assert cfg.state["at_final_lan"] is False


def test_the_factory_address_is_no_role(station, monkeypatch):
    set_detection(monkeypatch, FACTORY)
    poll()
    assert cfg.state["detected_role"] is None
    assert cfg.state["at_final_lan"] is False


def test_switching_the_role_reclassifies_the_unit_on_the_bench(station, monkeypatch):
    set_detection(monkeypatch, EDGE_LAN)
    poll()
    state = post_role("edge")
    assert state["at_final_lan"] is True


def test_the_hostname_uses_the_selected_roles_prefix(station):
    station.set("edge")
    assert cfg.hostname_for({"site_name": "Kela FOB 14"}) == "rut-edge-kela-fob-14"


def fake_result(**extra):
    return {"ok": True, "hostname": "x", "name": "rut-x",
            "identity": {"serial": "SN-1", "mac": "aa:bb", "model": "RUTM08",
                         "firmware": "RUTM_R_00.07.24.3"},
            "warnings": [], "error": None, "steps": [], "verification": [], "log": "",
            **extra}


def test_an_edge_record_carries_role_lan_and_wan(station, monkeypatch):
    station.set("edge")
    set_detection(monkeypatch, FACTORY)
    poll()
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result(
        **mod._role_fields(cfg.run_config(), inputs["role"])))
    assert "error" not in post_configure()
    device = cfg.state["history"][0]["device"]
    assert device["role"] == "edge"
    assert device["ip"] == EDGE_LAN
    assert device["wan_ip"] == "192.168.88.20"


def test_a_server_record_carries_role_and_lan_but_no_wan(station, monkeypatch):
    set_detection(monkeypatch, FACTORY)
    poll()
    monkeypatch.setattr(cfg, "_do_configure", lambda inputs: fake_result())
    post_configure()
    device = cfg.state["history"][0]["device"]
    assert device["role"] == "server"
    assert device["ip"] == SERVER_LAN
    assert "wan_ip" not in device


def test_a_run_records_the_role_it_was_given_not_the_one_selected_after(station, monkeypatch):
    seen = {}
    station.set("edge")
    set_detection(monkeypatch, FACTORY)
    poll()
    monkeypatch.setattr(cfg, "_do_configure",
                        lambda inputs: seen.update(inputs) or fake_result())
    post_configure()
    assert seen["role"] == "edge"
    assert cfg.state["history"][0]["device"]["role"] == "edge"
