"""The device-label record schema (bench_core.label_record, TEC-845).

`build_label_record` is the station's side and refuses anything unusable;
`parse_label_record` is the collector's and is forgiving about everything
except the two fields the store cannot work without. Both ends are checked
here because they are the contract between two deployables that ship
separately — a station on last week's release posts to a collector on this
week's.
"""
import pytest

from bench_core.label_record import (
    LABEL_RECORD_SCHEMA,
    build_label_record,
    parse_label_record,
)


def test_a_built_record_carries_the_whole_label():
    record = build_label_record(
        serial="6008219573", password="zZ?40*kA", source="scan", tool="otd",
        mac="20:97:27:2B:00:F7", model="OTD500", username="admin",
        imei="864088065513384", batch="015", run_id="run-1")

    assert record["schema"] == LABEL_RECORD_SCHEMA
    assert record["password"] == "zZ?40*kA"
    assert record["batch"] == "015"
    assert record["run_id"] == "run-1"
    # Stamped when not given, so a record always says when it was read.
    assert record["captured_at"]


def test_the_mac_is_canonicalized_on_the_way_in():
    # The label prints it bare, ARP hands it back colon-separated and macOS
    # drops leading zeros. One shape reaches the store, so one shape is what a
    # lookup has to match.
    shapes = ["2097272B00F7", "20:97:27:2b:00:f7", "20-97-27-2B-00-F7"]
    assert {build_label_record(serial="SN-1", password="pw", source="typed",
                               tool="otd", mac=shape)["mac"]
            for shape in shapes} == {"2097272b00f7"}


@pytest.mark.parametrize("missing", [
    {"serial": ""},              # nothing to key the row on
    {"password": ""},            # nothing to keep
    {"source": "shared-fallback"},  # not a factory password at all
    {"source": "guessed"},
])
def test_an_unusable_record_is_refused_rather_than_built(missing):
    kwargs = {"serial": "SN-1", "password": "pw", "source": "scan",
              "tool": "otd", **missing}
    with pytest.raises(ValueError):
        build_label_record(**kwargs)


def test_parsing_fills_in_every_key():
    # The collector indexes on these columns, so a sparse post must not leave
    # it reading None where it expects a string.
    parsed = parse_label_record({"serial": "SN-1", "password": "pw",
                                 "source": "typed"})
    assert parsed["batch"] == "" and parsed["imei"] == "" and parsed["mac"] == ""
    assert parsed["run_id"] is None


def test_parsing_reports_an_unusable_record_instead_of_raising():
    # The collector turns these into 400s; raising here would be a 500.
    assert parse_label_record({})["serial"] == ""
    assert parse_label_record({"serial": "SN-1"})["password"] == ""
    assert parse_label_record({"serial": "SN-1", "password": "pw",
                               "source": "shared-fallback"})["source"] == ""


def test_a_built_record_survives_the_round_trip():
    record = build_label_record(serial="SN-1", password="pw", source="scan",
                                tool="tsw", mac="20:97:27:2b:00:f7",
                                model="TSW202", batch="001")
    assert parse_label_record(record) == record
