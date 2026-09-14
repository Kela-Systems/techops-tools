"""The read-only bench-central client, against a stub collector.

A real HTTP server on localhost rather than a mocked transport: the failure
this has to rule out is a URL or query built wrong (a MAC sent with colons
matches nothing), and only a real request/response round trip catches that.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

import bench_central

# Two runs for one MAC, newest first, mirroring the collector's real shape
# (_SUMMARY_COLS in bench-central/collector.py). The newest run deliberately
# has no firmware, so the "newest non-empty wins" rule gets exercised.
RUNS = [
    {"run_id": "r2", "timestamp": "2026-09-02T10:00:00Z", "tool": "rutm",
     "kind": "verify", "status": "ok", "serial": "6010212527",
     "mac": "20972736 55ec".replace(" ", ""), "model": "RUTM08",
     "firmware": None, "site": "kela-fob-03", "hostname": "rut-kela-fob-03"},
    {"run_id": "r1", "timestamp": "2026-08-30T09:00:00Z", "tool": "rutm",
     "kind": "configure", "status": "ok", "serial": "6010212527",
     "mac": "2097273655ec", "model": "RUTM08",
     "firmware": "RUTM_R_00.07.14.3", "site": "kela-fob-03",
     "hostname": "rut-kela-fob-03"},
    # A different device that the substring search would also return, because
    # `q` matches across several columns. It must be filtered out.
    {"run_id": "r3", "timestamp": "2026-08-01T09:00:00Z", "tool": "tsw",
     "kind": "configure", "status": "ok", "serial": "x",
     "mac": "aabbccddeeff", "model": "TSW202", "firmware": "1.0",
     "site": "2097273655ec", "hostname": "other"},
]


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/v1/health":
            body = {"status": "ok", "runs": len(RUNS), "device_labels": 0}
        elif parsed.path == "/api/v1/runs":
            q = (parse_qs(parsed.query).get("q") or [""])[0]
            matched = [
                r for r in RUNS
                if q and any(q in str(v) for v in r.values() if v)
            ]
            body = {"count": len(matched), "total": len(matched),
                    "offset": 0, "runs": matched}
        else:
            self.send_response(404)
            self.end_headers()
            return
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):  # pragma: no cover - asserted never to be reached
        self.send_response(500)
        self.end_headers()

    def log_message(self, *args):
        pass


# Module-scoped: the stub is stateless, and standing one up per test made the
# suite spend most of its time in HTTPServer.shutdown().
@pytest.fixture(scope="module")
def collector():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def test_health_reports_row_counts(collector):
    assert bench_central.Client(collector).health()["runs"] == 3


def test_a_mac_with_colons_still_matches(collector):
    """MACs are stored stripped and lowercased, so the client must normalise
    before querying - otherwise this silently returns nothing."""
    runs = bench_central.Client(collector).runs_for_mac("20:97:27:36:55:EC")
    assert len(runs) == 2
    assert {r["run_id"] for r in runs} == {"r1", "r2"}


def test_a_coincidental_substring_match_is_filtered_out(collector):
    """`q` searches several columns; run r3 carries the MAC in its `site`."""
    runs = bench_central.Client(collector).runs_for_mac("2097273655ec")
    assert "r3" not in {r["run_id"] for r in runs}


def test_facts_take_the_newest_non_empty_value_per_field(collector):
    """The newest run has no firmware; the older one does. Both are real
    readings of the same device, so the firmware should survive."""
    facts = bench_central.Client(collector).facts_for_mac("20:97:27:36:55:ec")
    assert facts.model == "RUTM08"
    assert facts.firmware == "RUTM_R_00.07.14.3"
    assert facts.serial == "6010212527"
    assert facts.last_run == "2026-09-02T10:00:00Z"
    assert facts.runs_seen == 2
    assert facts.found and facts.has_identity


def test_an_unknown_mac_is_a_clean_miss(collector):
    facts = bench_central.Client(collector).facts_for_mac("00:00:00:00:00:01")
    assert facts.runs_seen == 0
    assert facts.found is False
    assert facts.model is None


def test_a_bad_mac_is_rejected_before_any_request(collector):
    with pytest.raises(bench_central.CentralError, match="not a 48-bit MAC"):
        bench_central.Client(collector).facts_for_mac("nonsense")


def test_an_unreachable_collector_says_so_and_mentions_the_tailnet():
    client = bench_central.Client("http://127.0.0.1:1", timeout=0.5)
    with pytest.raises(bench_central.CentralError, match="tailnet"):
        client.health()


def test_a_404_is_reported_with_its_status(collector):
    client = bench_central.Client(collector)
    with pytest.raises(bench_central.CentralError, match="HTTP 404"):
        client._get("/api/v1/nope")


def test_the_client_has_no_write_path():
    """Read-only is a property to keep: the collector has no auth, so a client
    that could POST could corrupt the audit trail by accident."""
    source = open("bench_central.py", encoding="utf-8").read()
    for verb in ('"POST"', "'POST'", '"PUT"', '"DELETE"', '"PATCH"'):
        assert verb not in source


@pytest.mark.parametrize("raw,expected", [
    ("20:97:27:36:55:ec", "2097273655ec"),
    ("20-97-27-36-55-EC", "2097273655ec"),
    ("0:1e:42:aa:bb:1", "001e42aabb01"),
    ("2097273655EC", "2097273655ec"),
])
def test_canonical_mac_matches_bench_core(raw, expected):
    assert bench_central.canonical_mac(raw) == expected


# The archive stores MACs in two different spellings depending on which tool
# wrote the row. Confirmed live: rutm/otd/tsw write "20972749801A", while
# magos/speaker write "e0:23:3b:d0:01:d4". A client that queries one spelling
# silently misses every row written in the other.
COLON_ONLY = {"run_id": "colon", "timestamp": "2026-09-01T00:00:00Z",
              "tool": "magos-radar", "mac": "e0:23:3b:d0:07:f4",
              "model": "AR-300", "firmware": None, "serial": "14430001-149"}


class _ColonHandler(_Handler):
    """A collector holding only the colon-separated spelling."""

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/v1/runs":
            q = (parse_qs(parsed.query).get("q") or [""])[0]
            matched = [COLON_ONLY] if q and q in COLON_ONLY["mac"] else []
            payload = json.dumps(
                {"count": len(matched), "total": len(matched), "offset": 0,
                 "runs": matched}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        super().do_GET()


@pytest.fixture(scope="module")
def colon_collector():
    server = HTTPServer(("127.0.0.1", 0), _ColonHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def test_a_colon_separated_row_is_found_by_a_stripped_query(colon_collector):
    """The bug this covers: asking for 'e0233bd007f4' when the row reads
    'e0:23:3b:d0:07:f4' returned nothing, and looked like an empty archive."""
    facts = bench_central.Client(colon_collector).facts_for_mac("e0233bd007f4")
    assert facts.found
    assert facts.model == "AR-300"


def test_both_spellings_are_tried():
    assert bench_central.query_spellings("e0233bd007f4") == [
        "e0:23:3b:d0:07:f4", "e0233bd007f4"]


def test_spellings_of_a_bad_mac_are_empty():
    assert bench_central.query_spellings("nope") == []


def test_results_from_both_queries_are_deduplicated(collector):
    """The stub matches both spellings, so r1/r2 come back twice."""
    runs = bench_central.Client(collector).runs_for_mac("20:97:27:36:55:ec")
    ids = [r["run_id"] for r in runs]
    assert sorted(ids) == ["r1", "r2"]
