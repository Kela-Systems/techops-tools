"""The factory-password store (collector.py, TEC-845).

One row per device, keyed on the serial, kept forever — the answer to "this
unit came back from the field factory-reset, what does it log in with now".

The interesting half is reconciliation. A device's factory password is printed
on it and cannot change, so two different readings of one serial mean somebody
made a mistake, and the store has to resolve that deliberately instead of
letting the last upload win by accident. Most of what follows is that rule and
its edges.
"""
import pytest
from fastapi.testclient import TestClient

from bench_core.label_record import build_label_record
from bench_core.run_record import build_run_entry

from collector import create_app

SERIAL = "6008219573"
MAC = "20:97:27:2b:00:f7"
SCANNED = "zZ?40*kA"


def label(**over):
    """One device-label record as a station ships it."""
    kwargs = {"serial": SERIAL, "password": SCANNED, "source": "scan",
              "tool": "otd", "mac": MAC, "model": "OTD500",
              "username": "admin", "imei": "864088065513384", "batch": "015"}
    stamps = {"station_id": "bench-1", "operator": "Dana K"}
    for key, value in over.items():
        (kwargs if key in kwargs or key in ("run_id", "captured_at")
         else stamps)[key] = value
    record = build_label_record(**kwargs)
    record.update(stamps)
    return record


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path / "runs.db"))


def post(client, record, expect=200):
    resp = client.post("/api/v1/device-labels", json=record)
    assert resp.status_code == expect, resp.text
    return resp.json()


def stored(client, device=SERIAL):
    return client.get(f"/api/v1/device-labels/{device}").json()


# ── ingest ───────────────────────────────────────────────────────────────────

def test_a_label_is_stored_and_reads_back_whole(client):
    assert post(client, label()) == {"stored": SERIAL, "outcome": "stored"}

    row = stored(client)
    assert row["password"] == SCANNED
    assert row["source"] == "scan"
    assert row["model"] == "OTD500"
    assert row["batch"] == "015"
    assert row["operator"] == "Dana K"
    assert row["conflicts"] == 0
    assert row["first_seen"] == row["last_seen"]


@pytest.mark.parametrize("broken,why", [
    ({"serial": ""}, "nothing to key on"),
    ({"password": ""}, "nothing to keep"),
    ({"source": "shared-fallback"}, "not a factory password"),
])
def test_an_unusable_label_is_400(client, broken, why):
    record = {**label(), **broken}
    assert client.post("/api/v1/device-labels", json=record).status_code == 400, why


def test_the_store_survives_a_restart(tmp_path):
    db = tmp_path / "runs.db"
    post(TestClient(create_app(db)), label())
    # A fresh app on the same file = a service restart.
    assert TestClient(create_app(db)).get(
        f"/api/v1/device-labels/{SERIAL}").json()["password"] == SCANNED


# ── reconciling two readings of one device ───────────────────────────────────

def test_reading_the_same_password_again_only_moves_last_seen(client):
    post(client, label(captured_at="2026-08-01T08:00:00+00:00"))
    assert post(client, label(captured_at="2026-08-20T08:00:00+00:00")) \
        == {"stored": SERIAL, "outcome": "confirmed"}

    row = stored(client)
    assert row["conflicts"] == 0
    assert row["first_seen"] == "2026-08-01T08:00:00+00:00"
    assert row["last_seen"] == "2026-08-20T08:00:00+00:00"


def test_a_corrected_typo_replaces_the_stored_password(client):
    # Two typed readings that disagree: the newer one wins, because the
    # likeliest reason to enter a password again is that the last one was wrong.
    post(client, label(source="typed", password="zZ?40*kb"))
    assert post(client, label(source="typed", password=SCANNED))["outcome"] \
        == "updated"

    row = stored(client)
    assert row["password"] == SCANNED
    assert row["conflicts"] == 1   # flagged, not hidden


def test_a_scan_overrules_a_typed_password(client):
    post(client, label(source="typed", password="zZ?4O*kA"))  # O for 0
    assert post(client, label(source="scan"))["outcome"] == "updated"

    row = stored(client)
    assert row["password"] == SCANNED and row["source"] == "scan"
    assert row["conflicts"] == 1


def test_a_typed_password_does_not_overrule_a_scan(client):
    # The one case where the newest reading loses: it was transcribed by eye
    # and the stored one was machine-read off the QR.
    post(client, label(source="scan", station_id="bench-1"))
    assert post(client, label(source="typed", password="wrong",
                              station_id="bench-2"))["outcome"] == "kept"

    row = stored(client)
    assert row["password"] == SCANNED and row["source"] == "scan"
    assert row["conflicts"] == 1
    # The provenance stays with the reading it describes — the row must not
    # claim bench-2 scanned a password it never saw.
    assert row["station_id"] == "bench-1"


def test_a_later_reading_fills_blanks_but_never_blanks_a_field(client):
    # A typed run carries no label, so it knows nothing about the batch or
    # IMEI an earlier scan of the same unit already established.
    post(client, label(source="scan"))
    post(client, label(source="scan", model="", batch="", imei=""))

    row = stored(client)
    assert row["batch"] == "015"
    assert row["imei"] == "864088065513384"
    assert row["model"] == "OTD500"


# ── looking one up ───────────────────────────────────────────────────────────

def test_lookup_by_mac_for_a_unit_whose_sticker_is_gone(client):
    post(client, label())
    # Any MAC shape: the store canonicalizes on the way in and on the way out.
    for shape in (MAC, "2097272B00F7", "20-97-27-2b-00-f7"):
        assert stored(client, shape)["serial"] == SERIAL


def test_an_unknown_device_is_404(client):
    assert client.get("/api/v1/device-labels/SN-nobody").status_code == 404


def test_listing_searches_and_filters(client):
    post(client, label())
    post(client, label(serial="6010212527", mac="20:97:2b:2b:00:f7",
                       model="TSW202", tool="tsw", password="qN4$8xTr",
                       imei="", batch="001"))

    everything = client.get("/api/v1/device-labels").json()
    assert everything["total"] == 2

    assert [l["serial"] for l in
            client.get("/api/v1/device-labels?tool=tsw").json()["labels"]] \
        == ["6010212527"]
    assert [l["serial"] for l in
            client.get("/api/v1/device-labels?q=TSW202").json()["labels"]] \
        == ["6010212527"]
    assert [l["serial"] for l in
            client.get(f"/api/v1/device-labels?mac={MAC}").json()["labels"]] \
        == [SERIAL]
    # A wildcard in the needle is a literal, as in the runs search.
    assert client.get("/api/v1/device-labels?q=%25").json()["total"] == 0


def test_listing_is_newest_reading_first(client):
    post(client, label(serial="SN-old", captured_at="2026-08-01T08:00:00+00:00"))
    post(client, label(serial="SN-new", captured_at="2026-08-20T08:00:00+00:00"))
    assert [l["serial"] for l in
            client.get("/api/v1/device-labels").json()["labels"]] \
        == ["SN-new", "SN-old"]


def test_health_counts_devices_separately_from_runs(client):
    post(client, label())
    health = client.get("/api/v1/health").json()
    assert health["device_labels"] == 1
    assert health["runs"] == 0


# ── the line between the two stores ──────────────────────────────────────────

def test_a_password_never_leaks_into_the_runs_feed(client):
    # The run feed is the read-only dashboard's data and gets one row per
    # visit; the password lives in one row per device, reachable only through
    # its own endpoint. A label posted for a serial must not change that.
    run = build_run_entry(tool="otd", ok=True, serial=SERIAL, mac=MAC,
                          model="OTD500",
                          device={"hostname": "otd-haifa",
                                  "password_source": "scan"})
    assert client.post("/api/v1/runs", json=run).status_code == 201
    post(client, label(run_id=run["run_id"]))

    assert SCANNED not in client.get("/api/v1/runs").text
    assert SCANNED not in client.get(f"/api/v1/runs/{run['run_id']}").text
    # ...and the label still points back at the run it was read during.
    assert stored(client)["run_id"] == run["run_id"]


# ── the dashboard, which is the whole access model ───────────────────────────
#
# TEC-845 settled that the factory password is readable in the dashboard and
# nowhere else — no CLI hand-off, no vault. So the panel being wired up is not
# a cosmetic detail, it is the feature. These are static assertions over the
# one page file, in the same spirit as the bench's per-page audit: crude, and
# the only thing standing between an edit and a store nobody can read.

def dashboard(client) -> str:
    return client.get("/").text


def test_the_page_has_a_factory_password_panel(client):
    page = dashboard(client)
    assert "Factory passwords" in page
    assert 'id="label-rows"' in page
    assert 'id="label-q"' in page          # look one up by serial/MAC/model


def test_the_page_reads_the_store_through_its_own_endpoints(client):
    page = dashboard(client)
    assert "api/v1/device-labels?" in page      # the panel's list
    assert 'fetch("api/v1/device-labels/"' in page  # one device, in run detail


def test_the_page_escapes_the_password_it_prints(client):
    # A factory password is punctuation-heavy by design (`zZ?40*kA`), so it is
    # exactly the kind of value that breaks out of markup if it is interpolated
    # raw. Both places it is printed go through esc().
    page = dashboard(client)
    assert "${esc(l.password)}" in page
    assert "${esc(label.password)}" in page
    assert "${l.password}" not in page
