"""Put the tool directory on sys.path so tests can `import otd_app`,
`import otd_configure`, etc. when pytest is run from the bench root.

Also holds the OTD500 pipeline harness. `configure_device` is driven by two
suites now — the SIM-switch/data-limit rules (TEC-359) and the time source
(TEC-857) — and both need the same recording stand-in for the device client, so
it lives here rather than in whichever file happened to need it first.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench_core import VERIFIED_SIM_SWITCH_FW  # noqa: E402

# The firmware the SIM-switch UCI options were verified against, so a run does
# not trip the unverified-firmware warning that no test here is about.
FW_VERIFIED = f"OTD5_R_00.{VERIFIED_SIM_SWITCH_FW}"


class _StubClient:
    """Records what the pipeline calls. Enough client surface for a run with
    the network-dependent steps switched off.

    Anything not defined here is answered by `__getattr__` with a recorder, so
    a newly added step shows up in `calls` without this class changing. The
    exceptions are the methods whose RETURN value the pipeline uses — identity
    and the verification rows — which is why those are spelled out.
    """
    fw_target = ""

    def __init__(self):
        self.calls: list[str] = []
        self.verified_with: dict = {}
        self.pool_reserved = ""
        self.ntp_server = ""

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append(name)
        return record

    def set_ntp_client(self, server, **kwargs):
        self.calls.append("set_ntp_client")
        self.ntp_server = server

    def get_identity(self):
        return {"model": "OTD500", "serial": "SN-1", "mac": "aa:bb:cc:dd:ee:01",
                "imei": "350000000000001", "firmware": FW_VERIFIED}

    def verify_identity(self, identity, expected):
        return []

    def verify_configuration(self, **kwargs):
        self.verified_with = kwargs
        return []

    # Real rows rather than the catch-all's None, so the pipeline can render its
    # verification table. What these rows ASSERT is covered by the OTD verify
    # suite and bench-core's; here they only have to exist.
    def ntp_client_check(self, server, **kwargs):
        self.calls.append("ntp_client_check")
        return {"item": "NTP client", "expected": server, "actual": server,
                "ok": True}

    def dhcp_pool_check(self, start, limit, *, reserved="", **kwargs):
        self.calls.append("dhcp_pool_check")
        self.pool_reserved = reserved
        return {"item": "DHCP pool", "expected": f"start {start}",
                "actual": f"start {start}", "ok": True}

    def ntp_daemon_check(self):
        self.calls.append("ntp_daemon_check")
        return {"item": "NTP daemon", "expected": "running", "actual": "running",
                "ok": True}


@pytest.fixture
def stub_client():
    """A fresh recording client, for tests that drive `configure_device`
    themselves — a run expected to raise, or one with a step replaced."""
    return _StubClient()


@pytest.fixture
def run_pipeline():
    """Run `configure_device` against a recording client and return it.

    Asserts the run had no step failures, so a test about ORDERING cannot
    quietly pass on a pipeline where the step it is placing never ran at all.
    """
    import otd_configure

    def run(settings, client=None):
        client = client if client is not None else _StubClient()
        result = otd_configure.configure_device(
            client, label_password="pw", site_name="haifa", settings=settings)
        assert result["failures"] == []
        return client

    return run
