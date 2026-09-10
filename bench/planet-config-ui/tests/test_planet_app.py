"""UI-surface tests for the IGS-4215 tool (planet_app.py).

These pin the things an operator sees and acts on: the PoE plan the page
publishes, the address modes offered, and — the one that matters most — that
the firmware step is ON unless someone deliberately turns it off.
"""
import json
from pathlib import Path

import pytest

import planet_app
from planet_app import PlanetConfigurator
from planet_configure import DEFAULT_PLANET_MIN_FIRMWARE

EXAMPLE = Path(__file__).resolve().parent.parent / "config/planet.config.example.json"


@pytest.fixture
def tool(tmp_path):
    """A configurator on the shipped example config, in a scratch directory."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config/planet.config.json").write_text(EXAMPLE.read_text())
    (tmp_path / "static").mkdir()
    (tmp_path / "static/planet.html").write_text("<html></html>")
    return PlanetConfigurator(tmp_path)


# --- the shipped example ------------------------------------------------------

def test_the_example_config_is_valid_json_with_the_site_plan():
    cfg = json.loads(EXAMPLE.read_text())
    ports = cfg["poe"]["ports"]
    assert [p for p, s in ports.items() if s["enabled"]] == ["1", "2", "3", "4", "8"]
    assert all(ports[str(p)]["limit_w"] == 45 for p in (1, 2, 3, 4))
    assert ports["8"]["limit_w"] == 20          # speaker
    assert all(ports[str(p)]["enabled"] is False for p in (5, 6, 7))   # spare


def test_the_example_plan_fits_a_single_supply_budget():
    """4x45 + 20 = 200 W against 240 W, the single-supply ceiling."""
    cfg = json.loads(EXAMPLE.read_text())
    allocated = sum(s["limit_w"] for s in cfg["poe"]["ports"].values() if s["enabled"])
    assert allocated == 200
    assert cfg["poe"]["budget_w"] == 240


def test_every_port_label_fits_the_switchs_32_char_limit():
    cfg = json.loads(EXAMPLE.read_text())
    for port, spec in cfg["poe"]["ports"].items():
        assert len(spec["description"]) <= 32, f"gi{port} label is too long"


def test_the_timezone_acronym_fits_the_switchs_4_char_limit():
    """A longer acronym is accepted and silently ignored, leaving +8."""
    cfg = json.loads(EXAMPLE.read_text())
    assert len(cfg["timezone_acronym"]) <= 4


# --- firmware step ------------------------------------------------------------

def test_the_firmware_step_is_enabled_by_default(tool):
    assert tool._firmware_enabled() is True
    assert tool._min_firmware() == DEFAULT_PLANET_MIN_FIRMWARE


def test_the_firmware_step_stays_on_when_the_config_omits_it(tmp_path):
    """Absent means on: a switch below the floor cannot be configured at all,
    so defaulting to off would strand it at SSH login."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config/planet.config.json").write_text(json.dumps({"firmware": {}}))
    (tmp_path / "static").mkdir()
    (tmp_path / "static/planet.html").write_text("<html></html>")
    assert PlanetConfigurator(tmp_path)._firmware_enabled() is True


def test_a_missing_image_is_reported_not_fatal(tool):
    """The page warns; Configure is not blocked, because a switch that arrives
    already at the floor needs no image."""
    assert tool._firmware_image_found() is False
    assert tool.extra_public_state()["firmware_enabled"] is True


class DummyClient:
    """Stands in for PlanetClient: run_pipeline closes the session in a finally,
    so a leaked slot can't refuse the next run."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_skip_firmware_turns_the_step_off_for_one_run(tool, monkeypatch):
    seen = {}

    def fake_configure(client, **kwargs):
        seen.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(planet_app, "configure_planet", fake_configure)
    tool.run_pipeline(DummyClient(), {"firmware": {"enabled": True}},
                      {"skip_firmware": True, "host": "192.168.0.100"})
    assert seen["settings"]["firmware"]["enabled"] is False


def test_a_normal_run_leaves_the_firmware_step_alone(tool, monkeypatch):
    seen = {}

    def fake_configure(client, **kwargs):
        seen.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(planet_app, "configure_planet", fake_configure)
    client = DummyClient()
    tool.run_pipeline(client, {"firmware": {"enabled": True}},
                      {"skip_firmware": False, "host": "192.168.0.100"})
    assert seen["settings"]["firmware"]["enabled"] is True
    assert client.closed, "the CLI session must be closed when a run ends"


# --- published state ----------------------------------------------------------

def test_the_poe_plan_is_published_for_the_page(tool):
    plan = tool.extra_public_state()["poe_plan"]
    assert [row["port"] for row in plan] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert plan[0]["description"] == "Radar-AR300-1"
    assert plan[0]["limit_w"] == 45.0


def test_the_data_only_ports_are_flagged_as_having_no_poe(tool):
    """gi9-10 carry the camera and the management drop. The page has to show
    them as "no PoE" rather than "off", which reads as a reversible choice."""
    plan = {row["port"]: row for row in tool.extra_public_state()["poe_plan"]}
    assert all(plan[p]["poe"] for p in range(1, 9))
    assert plan[9]["poe"] is False and plan[10]["poe"] is False
    assert plan[9]["description"] == "Camera-RAYTHINK-PC464A1"
    assert plan[10]["description"] == "Management"


def test_allocated_and_budget_are_published_so_the_page_can_flag_overrun(tool):
    state = tool.extra_public_state()
    assert state["poe_allocated_w"] == 200
    assert state["poe_budget_w"] == 240


def test_no_dhcp_mode_is_offered(tool):
    """The switch is configured with no gateway; a unit on a lease would be
    findable only by guesswork, so the mode is not offered at all."""
    assert "dhcp" not in tool.ip_mode_policy().modes


def test_the_default_address_is_the_third_host_on_the_management_subnet(tool):
    """.1 gateway, .2 TSW202, .3 this switch."""
    policy = tool.ip_mode_policy()
    assert policy.prefix == "192.168.88"
    assert policy.default_fixed_octet == 3


def test_nothing_is_scanned(tool):
    """The factory password is derived from the MAC the bench already reads off
    ARP, so there is no sticker to scan and no label step."""
    assert tool.label_scan_enabled is False


def test_the_example_leaves_the_factory_password_derived(tmp_path):
    """An explicit factory_password overrides the MAC derivation; the shipped
    config must not pin one, or every switch would be tried with the wrong."""
    cfg = json.loads(EXAMPLE.read_text())
    assert cfg["factory_password"] == ""


def test_the_run_label_falls_back_to_the_mac(tool):
    """There is no serial on this device — the MAC is the only handle."""
    assert tool.hostname_for({"mac": "A8:F7:E0:F6:C4:3A"}) == "planet-c43a"
    assert tool.hostname_for({"mac": ""}) == "planet-unknown"


def test_the_printed_port_map_names_every_socket_with_its_power(tool):
    """What goes on the switch itself. The wattage comes from limit_w rather
    than from the text, so the label and the switch cannot disagree."""
    rows = dict(tool._port_map_rows())
    assert rows["1"] == "RADAR 1 45W"
    assert rows["8"] == "SPEAKER 20W"
    assert rows["5"] == "SPARE"          # no wattage on a port with PoE off
    assert rows["9"] == "CAMERA" and rows["10"] == "MGMT"
    assert len(rows) == 10


def test_the_port_map_reaches_the_run_record(tool):
    """The label is printed from the record, so a plan that never got there is
    a switch that ships with no port map."""
    entry = tool.build_entry(
        {"identity": {"mac": "a8:f7:e0:f6:c4:3a"}, "ok": True, "error": None,
         "steps": [], "log": "", "verification": []}, {}, 30)
    assert entry["device"]["port_map"][0] == ["1", "RADAR 1 45W"]
