"""The RUTM08's two label faces: server and edge.

The server face must not move. A record from a server-role run now carries
`role` and `ip`, and the label printed from it has to be byte-for-byte the one
printed before those fields existed — an installer's muscle memory, and the
review loop's diff, both depend on it.

The edge face prints the WAN address instead of the LAN: an edge router's LAN
is inside its own box, and what an installer types is the address it has on
the server box's subnet.
"""
import copy
import json
from pathlib import Path

from bench_core.qa_label import label_content, render_zpl

RECORDS = Path(__file__).parent / "label-records"


def record(name: str) -> dict:
    return json.loads((RECORDS / f"{name}.json").read_text(encoding="utf-8"))


def test_a_server_record_with_role_and_ip_prints_the_identical_label():
    before = record("rutm")
    after = copy.deepcopy(before)
    after["device"].update({"role": "server", "ip": "192.168.88.1"})
    assert render_zpl(after) == render_zpl(before)


def test_the_server_face_is_unchanged():
    content = label_content(record("rutm"))
    assert content.mode == "STATIC"
    assert content.hero_sub == "192.168.88.1"


def test_the_edge_face_says_edge_and_leads_with_the_wan_address():
    content = label_content(record("rutm-edge"))
    assert content.face == "hostname-gateway"
    assert content.mode == "EDGE"
    assert content.hero == "rut-edge-kela-fob-14"
    assert content.hero_sub == "192.168.88.20"


def test_the_edge_label_does_not_print_the_boxs_own_lan():
    # 192.168.89.1 is only reachable from inside the edge box; printing it
    # would send an installer at the server box to an address it cannot reach.
    assert "192.168.89.1" not in render_zpl(record("rutm-edge"))


def test_an_edge_record_without_a_wan_ip_falls_back_to_the_fleet_address():
    entry = record("rutm-edge")
    del entry["device"]["wan_ip"]
    assert label_content(entry).hero_sub == "192.168.88.20"
