"""The identity probe: prober selection, the safety policy, and the patch.

The probers themselves are thin wrappers over the bench tools' own identity
readers and are only exercisable against real hardware. What IS testable —
and is what would hurt if it broke — is the framework around them: that
nothing connects without --confirm, that a credential is never retried, that
a failure leaves a fact unproven rather than guessed, and that the emitted
patch marks only the claims the probe actually establishes.
"""
import textwrap

import pytest
import yaml

import probe
from model import load_site


def site_from(tmp_path, text):
    path = tmp_path / "s.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return load_site(path)


SITE = """
version: 1
site: t
status: provisional
subnet: 192.168.88.0/24
nodes:
  router:
    kind: router
    vendor: Teltonika Networks
    addr: 192.168.88.1
    evidence: arp
  switch_poe:
    kind: poe-switch
    addr: 192.168.88.3
    evidence: arp
  camera_1:
    kind: camera
    addr: 192.168.88.30
    evidence: arp
  operator_station:
    kind: operator-station
    vendor: Dell
    addr: 192.168.88.29
    evidence: arp
  psu:
    kind: psu
    roles: [power-source]
"""


# -- prober selection --------------------------------------------------


def test_kind_selects_the_right_prober():
    assert probe.choose_prober("router", None)[0] is probe.probe_teltonika
    assert probe.choose_prober("poe-switch", None)[0] is probe.probe_planet
    assert probe.choose_prober("camera", None)[0] is probe.probe_raythink
    assert probe.choose_prober("radar", None)[0] is probe.probe_magos_radar


def test_vendor_selects_a_prober_when_the_kind_is_vague():
    found, label = probe.choose_prober("mystery-box", "Magosys Systems")
    assert found is probe.probe_magos_radar
    assert label == "probe_magos_radar"


def test_an_unprobeable_kind_explains_itself_rather_than_guessing():
    found, label = probe.choose_prober("operator-station", "Dell")
    assert found is None
    assert "chassis" in label


def test_an_unknown_kind_and_vendor_gets_no_prober():
    found, label = probe.choose_prober("mystery-box", None)
    assert found is None
    assert "no prober for kind" in label


def test_the_planet_prober_reads_identity_over_http_not_the_cli():
    """Identity over HTTP avoids both the concurrent-CLI-session limit and
    any exposure to the SSH lockout, so this must not regress to the CLI."""
    import inspect
    source = inspect.getsource(probe.probe_planet)
    assert "web_login" in source and "get_identity" in source
    assert ".cli(" not in source


def test_the_teltonika_prober_goes_read_only_before_it_authenticates():
    """The seatbelt is worthless if it is fastened after the first call."""
    import inspect
    source = inspect.getsource(probe.probe_teltonika)
    # Match the statements, not the prose: the docstring mentions
    # get_identity() and would otherwise satisfy the ordering by accident.
    ro = source.index("client.set_read_only")
    assert ro < source.index("client.login(")
    assert ro < source.index("client.get_identity()")


# -- the plan ----------------------------------------------------------


def test_plan_covers_every_node_and_contacts_nothing(tmp_path):
    rows = probe.plan(site_from(tmp_path, SITE))
    assert {r[0] for r in rows} == {
        "router", "switch_poe", "camera_1", "operator_station", "psu"}


def test_a_node_with_no_address_cannot_be_probed(tmp_path):
    rows = {r[0]: r for r in probe.plan(site_from(tmp_path, SITE))}
    assert rows["psu"][2] is None
    assert "no address" in rows["psu"][3]


# -- running -----------------------------------------------------------


def test_each_device_is_contacted_exactly_once(tmp_path, monkeypatch):
    """Never retry a credential: three failures lock a PLANET out, and it
    then fails in a way that looks like the broken-firmware fault."""
    calls = []

    def counting(host, password):
        calls.append(host)
        raise RuntimeError("auth failed")

    monkeypatch.setitem(probe.PROBERS, "router", counting)
    results = probe.run(site_from(tmp_path, SITE), "pw", only={"router"})

    assert calls == ["192.168.88.1"]
    assert results[0].ok is False
    assert "auth failed" in results[0].error


def test_a_failure_records_why_and_claims_nothing(tmp_path, monkeypatch):
    monkeypatch.setitem(probe.PROBERS, "router",
                        lambda h, p: (_ for _ in ()).throw(OSError("no route")))
    [result] = probe.run(site_from(tmp_path, SITE), "pw", only={"router"})
    assert result.model is None and result.firmware is None
    assert "OSError" in result.error


def test_an_unavailable_prober_is_reported_not_raised(tmp_path, monkeypatch):
    def missing(host, password):
        raise probe.ProbeUnavailable("bench_core not importable")

    monkeypatch.setitem(probe.PROBERS, "router", missing)
    [result] = probe.run(site_from(tmp_path, SITE), "pw", only={"router"})
    assert result.ok is False
    assert "not importable" in result.error


def test_a_successful_probe_records_the_identity(tmp_path, monkeypatch):
    monkeypatch.setitem(probe.PROBERS, "router", lambda h, p: {
        "model": "RUTM08", "firmware": "RUTM_R_00.07.22.3",
        "serial": "6010212527", "mac": "20:97:27:36:55:ec"})
    [result] = probe.run(site_from(tmp_path, SITE), "pw", only={"router"})
    assert result.ok
    assert result.model == "RUTM08"
    assert result.learned == ["model", "firmware"]


def test_connecting_but_learning_nothing_is_not_success(tmp_path, monkeypatch):
    monkeypatch.setitem(probe.PROBERS, "router",
                        lambda h, p: {"serial": "x", "mac": "y"})
    [result] = probe.run(site_from(tmp_path, SITE), "pw", only={"router"})
    assert result.ok is False
    assert "no model or firmware" in result.error


def test_one_devices_failure_does_not_stop_the_others(tmp_path, monkeypatch):
    monkeypatch.setitem(probe.PROBERS, "router",
                        lambda h, p: (_ for _ in ()).throw(OSError("down")))
    monkeypatch.setitem(probe.PROBERS, "camera",
                        lambda h, p: {"model": "PC464A1", "firmware": "V1.2.7"})
    results = {r.node: r for r in probe.run(
        site_from(tmp_path, SITE), "pw", only={"router", "camera_1"})}
    assert results["router"].ok is False
    assert results["camera_1"].ok is True


def test_extra_fields_survive(tmp_path, monkeypatch):
    """The PLANET reports whether both DC inputs are live — the one power
    fact any protocol can establish."""
    monkeypatch.setitem(probe.PROBERS, "poe-switch", lambda h, p: {
        "model": "IGS-4215-8UP2T2S", "firmware": "1.305b260324",
        "extra": {"dual_power": True}})
    [result] = probe.run(site_from(tmp_path, SITE), "pw", only={"switch_poe"})
    assert result.extra["dual_power"] is True


# -- the patch ---------------------------------------------------------


def test_the_patch_marks_only_model_and_firmware_as_device_api():
    """A device reporting its model says nothing about its cabling, so the
    patch must not upgrade anything else."""
    results = [probe.ProbeResult(
        node="router", host="192.168.88.1", prober="probe_teltonika",
        ok=True, model="RUTM08", firmware="RUTM_R_00.07.22.3",
        serial="6010212527")]
    doc = yaml.safe_load(probe.as_yaml_patch(results))
    node = doc["nodes"]["router"]
    assert node["model"] == "RUTM08"
    assert node["evidence"]["model"] == "device-api"
    assert node["evidence"]["firmware"] == "device-api"
    assert node["evidence"]["*"] == "arp"
    assert "link" not in node["evidence"]
    assert "power" not in node["evidence"]


def test_the_patch_is_valid_yaml_and_loads_into_a_site(tmp_path):
    results = [probe.ProbeResult(
        node="router", host="192.168.88.1", prober="p", ok=True,
        model="RUTM08", firmware="1.2.3")]
    merged = textwrap.dedent(SITE).replace(
        """  router:
    kind: router
    vendor: Teltonika Networks
    addr: 192.168.88.1
    evidence: arp""",
        """  router:
    kind: router
    vendor: Teltonika Networks
    addr: 192.168.88.1
    model: RUTM08
    firmware: 1.2.3
    evidence:
      "*": arp
      model: device-api
      firmware: device-api""")
    site = site_from(tmp_path, merged)
    assert site.nodes["router"].proves("model")
    assert site.nodes["router"].proves("firmware")
    assert site.nodes["router"].source_for("vendor") == "arp"
    # Sanity: the generated patch parses on its own too.
    assert yaml.safe_load(probe.as_yaml_patch(results))["nodes"]["router"]


def test_failed_probes_are_left_out_of_the_patch():
    results = [
        probe.ProbeResult(node="a", host="h", prober="p", ok=True, model="M"),
        probe.ProbeResult(node="b", host="h", prober="p", ok=False,
                          error="unreachable"),
    ]
    doc = yaml.safe_load(probe.as_yaml_patch(results))
    assert "a" in doc["nodes"] and "b" not in doc["nodes"]


# -- the probers themselves -------------------------------------------
#
# Everything above this line monkeypatches `PROBERS` with a lambda, which
# tests the orchestration and the safety policy but never executes a prober
# body. That gap let two probers reference classes that do not exist
# (`PlanetSwitch`, `RaythinkCamera`) for as long as they went unrun: the
# ImportError was caught, converted to ProbeUnavailable and reported as a
# missing *dependency*, so a bug in this repo read as a problem with the
# operator's machine.
#
# These tests import the real bench classes and drive the real prober bodies
# against a stubbed transport. A renamed class now fails the suite instead of
# degrading into "unavailable".

import importlib

import pytest


BENCH_TOOLS = [
    ("bench_core", "TeltonikaClient", ()),
    ("planet_configure", "PlanetClient", ("planet-config-ui",)),
    ("raythink_camera", "RaythinkCameraClient", ("raythink-config-ui",)),
    ("magos_configure", "MagosClient", ("magos-config-ui",)),
]


@pytest.mark.parametrize("module,klass,subdirs", BENCH_TOOLS)
def test_every_prober_names_a_class_that_exists(module, klass, subdirs):
    """The exact failure that hid for a whole build: a prober naming a class
    the bench tool does not define."""
    probe._add_bench_path(*subdirs)
    try:
        mod = importlib.import_module(module)
    except ImportError as exc:
        pytest.skip(f"{module} not importable here: {exc}")
    assert hasattr(mod, klass), (
        f"{module} has no {klass!r} — a prober references it, so that prober "
        f"cannot run. Real candidates: "
        f"{[n for n in dir(mod) if n.endswith(('Client', 'Switch', 'Camera'))]}")


# -- placeholders must never become claims ----------------------------

def test_a_placeholder_is_not_a_reading():
    for placeholder in ("unknown", "UNKNOWN", "", "  ", "n/a", "none", "-", None):
        assert probe._reading(placeholder) is None, placeholder


def test_a_real_value_survives():
    assert probe._reading("  RUTM08 ") == "RUTM08"


def test_a_named_fallback_constant_is_rejected():
    """The PLANET's EXPECTED_MODEL and the Raythink's bare vendor string are
    hardcoded, not read off hardware."""
    assert probe._reading("IGS-4215-8UP2T2S",
                          frozenset({"IGS-4215-8UP2T2S"})) is None
    assert probe._reading("Raythink", frozenset({"Raythink"})) is None


def test_the_planet_never_claims_a_model_from_the_system_name(monkeypatch):
    """`get_identity()` fills `model` from the operator-set System Name. That
    is a site's nickname for the switch, not evidence of what it is."""
    probe._add_bench_path("planet-config-ui")
    planet = pytest.importorskip("planet_configure")

    monkeypatch.setattr(planet.PlanetClient, "web_login",
                        lambda self, password=None: None)
    monkeypatch.setattr(planet.PlanetClient, "get_identity", lambda self: {
        "model": "fob-03-poe",          # someone's hostname
        "firmware": "v1.305b210902",
        "mac": "a8:f7:e0:11:22:33",
        "serial": "a8f7e0112233",
        "dual_power": True,
    })

    out = probe.probe_planet("192.168.88.2", "pw")
    assert out["model"] is None, "a hostname was recorded as a model"
    assert out["firmware"] == "v1.305b210902"
    assert out["extra"]["dual_power"] is True
    # Kept as a note, since it is a genuine reading of *something*.
    assert out["extra"]["system_name"] == "fob-03-poe"


def test_the_planet_rejects_its_own_expected_model_constant(monkeypatch):
    probe._add_bench_path("planet-config-ui")
    planet = pytest.importorskip("planet_configure")
    monkeypatch.setattr(planet.PlanetClient, "web_login",
                        lambda self, password=None: None)
    monkeypatch.setattr(planet.PlanetClient, "get_identity", lambda self: {
        "model": planet.EXPECTED_MODEL, "firmware": "v1.0", "mac": "", "serial": "",
    })
    assert probe.probe_planet("192.168.88.2", "pw")["model"] is None


def test_the_camera_does_not_claim_the_vendor_string_as_a_model(monkeypatch):
    probe._add_bench_path("raythink-config-ui")
    ray = pytest.importorskip("raythink_camera")
    monkeypatch.setattr(ray.RaythinkCameraClient, "login", lambda self, pw: None)
    monkeypatch.setattr(ray.RaythinkCameraClient, "get_identity", lambda self: {
        "model": "Raythink", "firmware": "unknown",
        "serial": "KC0500PAZ00052", "mac": "bc:74:d7:81:17:b1",
    })
    out = probe.probe_raythink("192.168.88.30", "pw")
    assert out["model"] is None
    assert out["firmware"] is None       # "unknown" is not a reading
    assert out["serial"] == "KC0500PAZ00052"


# -- the login / read-only ordering each client requires --------------

def test_the_camera_is_armed_read_only_after_login(monkeypatch):
    """Order matters and differs per client; this pins the camera's."""
    probe._add_bench_path("raythink-config-ui")
    ray = pytest.importorskip("raythink_camera")
    calls = []
    monkeypatch.setattr(ray.RaythinkCameraClient, "login",
                        lambda self, pw: calls.append("login"))
    monkeypatch.setattr(ray.RaythinkCameraClient, "set_read_only",
                        lambda self: calls.append("read_only"))
    monkeypatch.setattr(ray.RaythinkCameraClient, "get_identity",
                        lambda self: calls.append("identity") or {})
    probe.probe_raythink("192.168.88.30", "pw")
    assert calls == ["login", "read_only", "identity"]


def test_the_radar_logs_in_before_arming_because_its_login_is_a_post(monkeypatch):
    """The Magos guard sits on `_post` and its login IS a POST, so arming the
    guard first would refuse the login itself."""
    probe._add_bench_path("magos-config-ui")
    magos = pytest.importorskip("magos_configure")
    calls = []
    monkeypatch.setattr(magos.MagosClient, "login",
                        lambda self, u, p: calls.append(f"login:{u}"))
    monkeypatch.setattr(magos.MagosClient, "set_read_only",
                        lambda self: calls.append("read_only"))
    monkeypatch.setattr(magos.MagosClient, "get_identity",
                        lambda self: calls.append("identity") or {})
    monkeypatch.setattr(magos.MagosClient, "get_system",
                        lambda self: calls.append("system") or {})
    probe.probe_magos_radar("192.168.88.130", "pw")
    assert calls == ["login:admin", "read_only", "identity", "system"]


def test_the_radar_is_not_probed_without_logging_in(monkeypatch):
    """It used to call get_identity() with no session at all, which reads the
    dashboard API unauthenticated and returns nothing."""
    probe._add_bench_path("magos-config-ui")
    magos = pytest.importorskip("magos_configure")
    logged_in = []
    monkeypatch.setattr(magos.MagosClient, "login",
                        lambda self, u, p: logged_in.append(p))
    monkeypatch.setattr(magos.MagosClient, "set_read_only", lambda self: None)
    monkeypatch.setattr(magos.MagosClient, "get_identity", lambda self: {})
    monkeypatch.setattr(magos.MagosClient, "get_system", lambda self: {})
    probe.probe_magos_radar("192.168.88.130", "secret")
    assert logged_in == ["secret"]


def test_the_teltonika_arms_read_only_first_then_logs_in(monkeypatch):
    """The exception to the ordering rule: this guard screens shell command
    strings and does not gate its own REST login, so it goes on first."""
    probe._add_bench_path()
    core = pytest.importorskip("bench_core")
    calls = []
    monkeypatch.setattr(core.TeltonikaClient, "set_read_only",
                        lambda self, ro=True: calls.append("read_only"))
    monkeypatch.setattr(core.TeltonikaClient, "login",
                        lambda self, pw: calls.append("login"))
    monkeypatch.setattr(core.TeltonikaClient, "get_identity",
                        lambda self: calls.append("identity") or {})
    probe.probe_teltonika("192.168.88.1", "pw")
    assert calls == ["read_only", "login", "identity"]


def test_the_teltonika_login_happens_or_firmware_is_silently_lost(monkeypatch):
    """Firmware comes ONLY from the REST endpoint, which needs the bearer token
    `login()` sets. No login, no firmware — and no error either."""
    probe._add_bench_path()
    core = pytest.importorskip("bench_core")
    tokens = []
    monkeypatch.setattr(core.TeltonikaClient, "set_read_only", lambda self, ro=True: None)
    monkeypatch.setattr(core.TeltonikaClient, "login", lambda self, pw: tokens.append(pw))
    monkeypatch.setattr(core.TeltonikaClient, "get_identity", lambda self: {})
    probe.probe_teltonika("192.168.88.1", "shared-pw")
    assert tokens == ["shared-pw"]


def test_the_radar_reads_firmware_from_the_component_list(monkeypatch):
    """`get_identity()` carries no firmware key; `GET /system` does, as four
    separately-versioned components."""
    probe._add_bench_path("magos-config-ui")
    magos = pytest.importorskip("magos_configure")
    monkeypatch.setattr(magos.MagosClient, "login", lambda self, u, p: None)
    monkeypatch.setattr(magos.MagosClient, "set_read_only", lambda self: None)
    monkeypatch.setattr(magos.MagosClient, "get_identity", lambda self: {
        "model": "SR1000-I", "serial": "12510000-082",
        "mac": "8c:1f:64:e7:4c:46",
    })
    monkeypatch.setattr(magos.MagosClient, "get_system", lambda self: {
        "swComponents": [
            {"name": "System Software", "version": "1.1.4"},
            {"name": "Dashboard", "version": "2.4.3"},
            {"name": "DSP", "version": "1.2.4"},
            {"name": "FPGA", "version": "1.2.6"},
        ]})
    out = probe.probe_magos_radar("192.168.88.130", "pw")
    assert out["model"] == "SR1000-I"
    assert out["firmware"] == "1.1.4"           # System Software
    # The others are kept, not flattened away — a moved FPGA version matters.
    assert out["extra"]["fpga version"] == "1.2.6"
    assert out["extra"]["dsp version"] == "1.2.4"


def test_a_radar_that_serves_no_system_endpoint_still_reports_its_model(monkeypatch):
    probe._add_bench_path("magos-config-ui")
    magos = pytest.importorskip("magos_configure")
    monkeypatch.setattr(magos.MagosClient, "login", lambda self, u, p: None)
    monkeypatch.setattr(magos.MagosClient, "set_read_only", lambda self: None)
    monkeypatch.setattr(magos.MagosClient, "get_identity",
                        lambda self: {"model": "SR1000-I"})
    def boom(self):
        raise RuntimeError("no /system on this firmware")
    monkeypatch.setattr(magos.MagosClient, "get_system", boom)
    out = probe.probe_magos_radar("192.168.88.130", "pw")
    assert out["model"] == "SR1000-I"
    assert out["firmware"] is None


def test_no_prober_reaches_the_network_when_its_client_is_stubbed(monkeypatch):
    """A guard on the suite itself. The radar firmware step was added after
    these tests were written and fell straight through to a live HTTP call —
    with an SSH tunnel open, that meant a unit test quietly contacting a
    production radar. Any socket attempt from a fully-stubbed prober fails
    here instead."""
    import socket

    def no_sockets(*a, **k):
        raise AssertionError("a stubbed prober tried to open a socket")

    probe._add_bench_path("magos-config-ui")
    magos = pytest.importorskip("magos_configure")
    monkeypatch.setattr(magos.MagosClient, "login", lambda self, u, p: None)
    monkeypatch.setattr(magos.MagosClient, "set_read_only", lambda self: None)
    monkeypatch.setattr(magos.MagosClient, "get_identity",
                        lambda self: {"model": "SR1000-I"})
    monkeypatch.setattr(magos.MagosClient, "get_system", lambda self: {})
    monkeypatch.setattr(socket.socket, "connect", no_sockets)
    monkeypatch.setattr(socket.socket, "connect_ex", no_sockets)
    assert probe.probe_magos_radar("192.168.88.130", "pw")["model"] == "SR1000-I"
