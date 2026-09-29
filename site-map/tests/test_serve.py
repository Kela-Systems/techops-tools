"""The scan endpoint: that it produces a model `lint` would accept, and that
what it cannot establish stays unestablished.

The pipeline goes the long way round on purpose - sweep, discover, YAML,
load_site, export - so there is only ever one way a site model comes into
being. These tests are mostly about that: the payload the page draws has to
be one the linter has already had a say in.
"""
import json
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

import serve
import sweep

NEIGH = """\
### hostname
kela-fob-03
### neighbours
192.168.88.1 dev enp1 lladdr 20:97:27:36:55:ec REACHABLE
192.168.88.130 dev enp1 lladdr 8c:1f:64:e7:4c:46 REACHABLE
192.168.88.29 dev enp1 lladdr e8:cf:83:3f:f3:83 REACHABLE
"""

DB = """\
209727 Teltonika Networks UAB
8C1F64 Ieee Registration Authority
8C1F64E74 Magosys Systems
E8CF83 Dell
"""


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "nmap-mac-prefixes"
    path.write_text(DB)
    return str(path)


@pytest.fixture
def swept(monkeypatch):
    """A sweep that answers from NEIGH without an ssh anywhere."""
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, NEIGH, "")

    monkeypatch.setattr(sweep.subprocess, "run", run)


def test_a_scan_yields_a_payload_the_page_can_draw(swept, db):
    out = serve.scan("100.1.1.1", "192.168.88.0/24", db=db, via="server")
    site = out["site"]
    assert out["answered"] == 3 and out["swept"] == 254
    assert set(n["addr"] for n in site["nodes"].values()) == {
        "192.168.88.1", "192.168.88.29", "192.168.88.130"}
    assert site["subnet"] == "192.168.88.0/24"


def test_the_site_is_named_from_the_remote_hostname(swept, db):
    # Which is why the page needs two fields and not three.
    assert serve.scan("100.1.1.1", db=db, via="server")["site"]["name"] == "kela-fob-03"


def test_a_server_that_reports_no_hostname_still_gets_a_usable_name(monkeypatch, db):
    monkeypatch.setattr(sweep.subprocess, "run", lambda c, **k:
                        subprocess.CompletedProcess(c, 0, "### neighbours\n" +
                                                    NEIGH.split("### neighbours\n")[1], ""))
    assert serve.scan("100.1.1.1", db=db, via="server")["site"]["name"] == "sweep-100-1-1-1"


def test_no_cabling_and_no_power_is_claimed_for_anything(swept, db):
    # The single most damaging thing this endpoint could do is emit a
    # plausible-looking net: or power: block, so it emits neither, ever.
    site = serve.scan("100.1.1.1", db=db, via="server")["site"]
    assert site["net"] == [] and site["power"] == []
    assert all(not n["net_parents"] and not n["power_in"]
               for n in site["nodes"].values())
    # Checked as a block at column 0, not as a substring: the file's comment
    # header is largely an explanation of why there is no net: block.
    lines = serve.scan("100.1.1.1", db=db, via="server")["yaml"].splitlines()
    assert not [ln for ln in lines if ln.startswith(("net:", "power:"))]


def test_every_fact_carries_arp_as_its_source_and_nothing_stronger(swept, db):
    site = serve.scan("100.1.1.1", db=db, via="server")["site"]
    for node in site["nodes"].values():
        assert node["evidence"]["*"] == "arp"
        assert node["model"] is None, "a MAC cannot establish a model"
        assert node["proven"]["addr"] and node["proven"]["vendor"]


def test_the_model_is_provisional_and_says_so(swept, db):
    assert serve.scan("100.1.1.1", db=db, via="server")["site"]["status"] == "provisional"


def test_the_yaml_handed_back_is_a_real_site_file(swept, db, tmp_path):
    from model import load_site
    path = tmp_path / "swept.yaml"
    path.write_text(serve.scan("100.1.1.1", db=db, via="server")["yaml"])
    assert load_site(path).name == "kela-fob-03"


def test_the_footer_does_not_credit_a_temp_file_that_is_already_gone(swept, db):
    source = serve.scan("100.1.1.1", db=db, via="server")["site"]["source_path"]
    assert "/tmp" not in source and "100.1.1.1" in source


def test_no_vendor_database_is_a_clear_refusal_not_a_vendorless_map(swept, tmp_path):
    with pytest.raises(serve.ScanError) as exc:
        serve.scan("100.1.1.1", db=str(tmp_path / "missing"), via="server")
    assert "vendor" in str(exc.value)


def test_a_sweep_that_finds_nothing_says_how_many_it_tried(monkeypatch, db):
    monkeypatch.setattr(sweep.subprocess, "run", lambda c, **k:
                        subprocess.CompletedProcess(c, 0, "### neighbours\n", ""))
    with pytest.raises(serve.ScanError) as exc:
        serve.scan("100.1.1.1", db=db, via="server")
    assert "254 addresses" in str(exc.value)


def test_an_unreachable_bench_central_is_a_warning_and_not_a_failed_scan(swept, db):
    out = serve.scan("100.1.1.1", db=db, central="http://127.0.0.1:1", via="server")
    assert out["site"]["nodes"], "the sweep's own findings still stand"
    assert any("unreachable" in w for w in out["warnings"])


def test_the_warnings_from_the_sweep_survive_into_the_payload(swept, db):
    # A LAN address in the server field is the bench collision, and the page
    # must be told even though the scan succeeds.
    out = serve.scan("192.168.88.10", db=db, via="server")
    assert any("bench" in w for w in out["warnings"])


# -- the HTTP surface ----------------------------------------------------

class Options:
    host = "127.0.0.1"
    port = 0
    central = None
    timeout = 5.0
    ssh_user = "kela"

    no_host_identity = True
    password_cmd = None
    host_password_cmd = None
    host_password = None
    admin_user = "KelaAdmin"
    ask_host_password = False
    no_prompt = True
    listen = False          # the BPDU capture is opt-in and takes real time

    def __init__(self, db=None, via="server", password=None):
        self.db = db
        self.via = via
        self.password = password
        self.router_password = password


@pytest.fixture
def server(db):
    from functools import partial
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(serve.Handler, options=Options(db)))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def post(base, path, body):
    request = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_health_is_how_the_page_learns_a_backend_exists(server):
    with urllib.request.urlopen(server + "/api/health", timeout=10) as response:
        body = json.loads(response.read())
    assert body["ok"] and body["default_subnet"] == "192.168.88.0/24"
    assert body["oui_db"]
    # The page shows this in the field's placeholder, so a reader can see
    # which account a bare address will be tried as.
    assert body["ssh_user"] == "kela"


def test_the_configured_ssh_user_is_used_for_a_bare_address(server, swept):
    status, body = post(server, "/api/scan", {"server": "100.1.1.1"})
    assert status == 200
    assert not any("--ssh-user" in w for w in body["warnings"])


def test_the_page_itself_is_served(server):
    with urllib.request.urlopen(server + "/", timeout=10) as response:
        assert b"Sweep a site" in response.read()


def test_text_is_served_as_utf8_and_the_page_says_so_itself():
    """Both halves, because either one alone leaves the page wrong somewhere.

    Served as bare "text/html" the browser falls back to its locale default
    and every em-dash on the page arrives as "â€”". Published as an artifact
    the injected skeleton supplied a charset, so this was invisible until the
    page was self-hosted.
    """
    import pathlib
    head = pathlib.Path(__file__).resolve().parent.parent / "ui" / "index.html"
    text = head.read_text(encoding="utf-8")
    assert '<meta charset="utf-8">' in text[:1024], "must be in the first 1024 bytes"


def test_the_server_sends_a_charset_for_text(server):
    for path, kind in (("/", "text/html"), ("/site-data.js", "javascript")):
        with urllib.request.urlopen(server + path, timeout=10) as response:
            header = response.headers["Content-Type"].lower()
        assert "charset=utf-8" in header, (path, header)


def test_em_dashes_survive_the_round_trip(server):
    with urllib.request.urlopen(server + "/", timeout=10) as response:
        body = response.read().decode("utf-8")
    assert "\u2014" in body and "â€”" not in body


def test_a_scan_over_http_returns_the_payload(server, swept):
    status, body = post(server, "/api/scan",
                        {"server": "100.1.1.1", "subnet": "192.168.88.0/24"})
    assert status == 200
    assert body["site"]["name"] == "kela-fob-03"


def test_a_missing_server_field_is_a_400_the_page_can_show_verbatim(server):
    status, body = post(server, "/api/scan", {"subnet": "192.168.88.0/24"})
    assert status == 400
    assert "no default" in body["error"]


def test_an_oversized_subnet_is_refused_before_anything_is_pinged(server):
    status, body = post(server, "/api/scan",
                        {"server": "100.1.1.1", "subnet": "10.0.0.0/8"})
    assert status == 400 and "cap" in body["error"]


def test_the_subnet_falls_back_to_the_default_when_left_empty(server, swept):
    status, body = post(server, "/api/scan", {"server": "100.1.1.1", "subnet": ""})
    assert status == 200 and body["subnet"] == "192.168.88.0/24"


def test_a_body_that_is_not_json_does_not_take_the_server_down(server):
    request = urllib.request.Request(
        server + "/api/scan", data=b"{oh no", method="POST",
        headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=10)
    assert exc.value.code == 400


def test_an_unknown_endpoint_is_a_404(server):
    status, _ = post(server, "/api/anything", {})
    assert status == 404


# -- the router vantage point --------------------------------------------

ROUTER_ADDRS = """\
3: wan: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500
    inet 192.168.1.164/24 brd 192.168.1.255 scope global wan
12: br-lan: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500
    inet 192.168.88.1/24 brd 192.168.88.255 scope global br-lan
18: tailscale0: <POINTOPOINT,MULTICAST,NOARP,UP,LOWER_UP> mtu 1280
    inet 100.64.242.104/32 scope global tailscale0
"""


@pytest.fixture
def surveyed(monkeypatch):
    """A router survey that answers without a router."""
    import router as router_mod
    import sweep as sweep_mod

    # **rest so a new survey option (listen=..., say) does not turn every
    # test in this file into a 500 that says nothing about the option.
    def fake(host, password, subnet=None, log=None, **rest):
        # The real one reads the subnet off br-lan when none is given.
        subnet = subnet or "192.168.88.0/24"
        addrs, net = sweep_mod.hosts(subnet)
        ifaces = router_mod.parse_addrs(ROUTER_ADDRS)
        for iface in ifaces:
            iface.mac = {"br-lan": "20:97:27:36:55:ec"}.get(iface.name)
        return router_mod.Survey(
            host=host, hostname="rut-kela-fob-03", subnet=str(net),
            interfaces=ifaces,
            leases={"209727A0EC70": "TSW202", "E8CF838DFC14": "kela-fob-03"},
            arp_text=sweep_mod.as_arp(NEIGH, net),
            swept=len(addrs), answered=3,
        )

    monkeypatch.setattr(router_mod, "survey", fake)


def test_the_router_survey_produces_a_site_including_the_router_itself(surveyed, db):
    out = serve.scan("100.64.242.104", db=db, password="x", read_hosts=False)
    nodes = out["site"]["nodes"]
    assert "router" in nodes, "the vantage point is the device a sweep cannot see"
    assert out["site"]["name"] == "kela-fob-03", "site name minus the rut- prefix"


def test_the_router_arrives_with_every_leg_and_the_lan_one_is_primary(surveyed, db):
    router_node = serve.scan("100.64.242.104", db=db, password="x", read_hosts=False)["site"]["nodes"]["router"]
    by_name = {i["name"]: i for i in router_node["interfaces"]}
    assert set(by_name) == {"wan", "br-lan", "tailscale0"}
    assert by_name["br-lan"]["scope"] == "internal"
    assert by_name["wan"]["scope"] == "external"
    assert by_name["tailscale0"]["scope"] == "external"
    assert router_node["addr"] == "192.168.88.1"
    assert router_node["interfaces_read"] is True


def test_a_swept_device_reports_one_leg_and_is_marked_unread(surveyed, db):
    # One address is what an ARP entry establishes. The page has to be able
    # to say that is a limit of the reading, not a claim about the NIC count.
    nodes = serve.scan("100.64.242.104", db=db, password="x", read_hosts=False)["site"]["nodes"]
    other = next(n for k, n in nodes.items() if k != "router")
    assert len(other["interfaces"]) == 1
    assert other["interfaces_read"] is False
    assert other["interfaces"][0]["scope"] == "internal"


def test_the_router_is_the_diagram_s_root_without_being_dot_one(surveyed, db):
    topo = serve.scan("100.64.242.104", db=db, password="x", read_hosts=False)["site"]["topology"]
    assert topo["router"] == "router"
    assert any(e["parent"] == "__internet__" and e["child"] == "router"
               for e in topo["edges"])


def test_no_password_is_refused_before_anything_is_contacted(db):
    with pytest.raises(serve.ScanError) as exc:
        serve.scan("100.64.242.104", db=db, read_hosts=False)
    assert "--password" in str(exc.value) and "reject keys" in str(exc.value)


def test_the_password_never_reaches_the_browser(server):
    # It opens every router in the fleet; health says only whether one is set.
    with urllib.request.urlopen(server + "/api/health", timeout=10) as response:
        body = json.loads(response.read())
    assert "password" not in json.dumps(body).lower() or "has_password" in body
    assert body.get("has_password") in (True, False)
    assert "x" not in [v for v in body.values() if isinstance(v, str)]
