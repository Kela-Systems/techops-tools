"""S7 helpers against what gotcha-rev3-dev returned (2026-10-05)."""
from datetime import datetime, timezone

from gotcha_atp.stages import s7

CLIENTS = """\
Hostname                      NTP   Drop Int IntL Last     Cmd   Drop Int  Last
===============================================================================
192.168.88.51              140961      0   1   -     0       0      0   -     -
192.168.88.61               97401      0   4   -    15       0      0   -     -
192.168.88.139                925      0  11   -   27m       0      0   -     -
192.168.88.47                   2      0   5   -  488m       0      0   -     -
"""


def test_judge_is_decisive_only_outside_the_uncertainty():
    assert s7.judge(0.2, 0.5, 1.0) is True
    assert s7.judge(-1.8, 0.5, 1.0) is False
    assert s7.judge(1.2, 0.5, 1.0) is None     # could be 0.7 or 1.7
    assert s7.fmt_offset(-0.28, 0.57) == "-0.28 s (±0.57)"


def test_bound_offset_narrows_at_the_tick():
    # device runs 0.3 s ahead; reads every 0.2 s with a 0.1 s round-trip
    off, reads = 0.3, []
    for i in range(7):
        a = 100.0 + i * 0.2
        reads.append((a, a + 0.1, float(int(a + 0.05 + off))))
    est, half = s7.bound_offset(reads, 1.0)
    assert abs(est - off) <= half and half <= 0.15
    assert s7.bound_offset([(100.0, 100.1, 50.0), (100.2, 100.3, 99.0)], 1.0) is None   # contradictory
    est, half = s7.bound_offset([(10.0, 10.2, 10.4)], 0.0)                                # %N clock
    assert round(est, 2) == 0.3 and round(half, 2) == 0.1


def test_camera_epoch_uses_its_timezone():
    t = {"Year": 2026, "Month": 10, "Day": 5, "Hour": 18, "Minute": 51, "Second": 10, "Timezone": "GMT+03:00"}
    assert s7.camera_epoch(t) == datetime(2026, 10, 5, 15, 51, 10, tzinfo=timezone.utc).timestamp()
    assert s7.camera_epoch({"Year": 2026}) is None


def test_http_date():
    assert s7.http_date_epoch("Mon, 05 Oct 2026 15:51:10 GMT") == \
        datetime(2026, 10, 5, 15, 51, 10, tzinfo=timezone.utc).timestamp()
    assert s7.http_date_epoch(None) is None and s7.http_date_epoch("garbage") is None


def test_chrony_clients_and_freshness():
    c = s7.parse_chrony_clients(CLIENTS)
    assert set(c) == {"192.168.88.51", "192.168.88.61", "192.168.88.139", "192.168.88.47"}
    assert c["192.168.88.139"] == {"ntp": 925, "int_log2": 11, "last_s": 1620}
    assert s7.client_fresh(c["192.168.88.51"]) and s7.client_fresh(c["192.168.88.139"])  # 27 min < 2×2048 s
    assert not s7.client_fresh(c["192.168.88.47"])                                       # 488 min ≫ 2×32 s
    assert s7.chrony_last_s("4h") == 14400 and s7.chrony_last_s("-") is None
