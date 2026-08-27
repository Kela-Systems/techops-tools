"""Firmware-version comparison: equality (fw_versions_match) vs order
(fw_version_at_least).

The ordered compare exists for the TSW202, whose config names a firmware
FLOOR rather than a pin: flash a unit that is older, leave a newer one alone.
Getting the order wrong flashes a switch backwards, and the cases where a
string compare would are all represented below — a release name against a bare
version, differing segment counts, and a segment outgrowing its zero padding.
"""
import pytest

from bench_core import fw_carries_version, fw_version_at_least, fw_versions_match

FLOOR = "TSW2_R_00.01.07.1"
NEWER = "TSW2_R_00.01.10"
OLDER = "TSW2_R_00.01.05"


@pytest.mark.parametrize("device_fw,minimum,expected", [
    (FLOOR, FLOOR, True),        # exactly the floor
    (NEWER, FLOOR, True),        # 00.01.10 is newer than 00.01.07.1
    (OLDER, FLOOR, False),       # 00.01.05 needs the flash
    ("TSW2_R_00.01.07", FLOOR, False),    # 07 predates 07.1
    ("TSW2_R_00.01.07.2", FLOOR, True),   # ...and 07.2 follows it
    ("TSW2_R_00.02.00", FLOOR, True),
    # A release name against the bare version the device reports.
    (NEWER, "00.01.07.1", True),
    ("00.01.05", FLOOR, False),
    # A segment that outgrew its zero padding — where a string compare fails.
    ("TSW2_R_00.01.100", "TSW2_R_00.01.99", True),
    ("TSW2_R_00.01.99", "TSW2_R_00.01.100", False),
    ("OTD5_R_00.07.23.4", "00.07.23", True),
    # No parseable version on either side: "could not tell" must not read as
    # "new enough" and skip a needed upgrade.
    ("", FLOOR, False),
    ("unknown", FLOOR, False),
    (FLOOR, "", False),
])
def test_fw_version_at_least(device_fw, minimum, expected):
    assert fw_version_at_least(device_fw, minimum) is expected


def test_equality_and_order_disagree_where_they_should():
    # The floor semantics in one line: the newer unit is NOT the pinned version
    # but IS acceptable. A tool that only had fw_versions_match would flash it.
    assert fw_versions_match(NEWER, FLOOR) is False
    assert fw_version_at_least(NEWER, FLOOR) is True


def test_fw_versions_match_is_unchanged():
    assert fw_versions_match(FLOOR, "00.01.07.1") is True
    assert fw_versions_match(FLOOR, "00.01.07") is False
    assert fw_carries_version(FLOOR, "01.07.1") is True
