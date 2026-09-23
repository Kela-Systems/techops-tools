"""`sim_inserted`: the OTD500 bench's pre-check that a SIM is in the slot.

Only an explicit "no SIM" from the modem may block a run. Anything else, a modem
still booting included, is None, so a reply we misread never stops a good unit.
"""
import pytest

from bench_core import TeltonikaClient


def client(reply: str) -> TeltonikaClient:
    c = TeltonikaClient(host="192.0.2.1")
    c.ssh_exec = lambda command, check=True, exec_timeout=None: reply
    return c


@pytest.mark.parametrize("reply,expected", [
    ("+CPIN: READY\n\nOK", True),
    ("+CPIN: SIM PIN\n\nOK", True),        # present, just locked
    ("+CME ERROR: SIM not inserted", False),
    ("+CME ERROR: 10", False),              # the numeric form of the same
    ("+CME ERROR: 14", None),               # SIM busy: not proof it is missing
    ("", None),                             # gsmctl absent or modem booting
    ("something else entirely", None),
])
def test_only_an_explicit_no_sim_reads_as_missing(reply, expected):
    assert client(reply).sim_inserted() is expected
