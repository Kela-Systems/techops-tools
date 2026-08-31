"""The APU tool prints a QA label on a verified-OK run (TEC-352).

MagosBench has its own run orchestration — it is not a BenchConfigurator
subclass — so the hook is wired twice and has to be tested on both sides. The
Teltonika/Raythink/speaker side is covered by
`tsw-config-ui/tests/test_tsw_label.py`.

The ordering assertion is the one worth having: the record is written and
spooled to central by `_save_log`, so a `label` block attached after it would
be missing from the audit trail on disk AND at central, while looking perfectly
fine in the UI's history.
"""
import asyncio
import sys
import threading
import time

import pytest

from bench_core.label_printer import LabelPrinter, PrinterSettings

import apu_app as mod

apu = mod.configurator

HOST = "192.168.40.60"


def fake_result(ip="192.168.88.60", serial="GSAC657352", ok=True,
                verified=True):
    return {
        "ok": ok, "skipped": False, "ip": ip,
        "identity": {"serial": serial, "mac": "ac:3a:e2:7c:42:9f",
                     "model": "AR Processing Unit (APU)"},
        "raw": {}, "firmware": "3.1.2-rc5", "steps": [], "log": "",
        "error": None if ok else "boom",
        "verified": verified, "verify_detail": None if verified else "failed",
        "verification": [{"item": "static IP", "expected": ip, "actual": ip,
                          "ok": bool(verified)}],
    }


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    apu.state.update({
        "phase": "waiting", "detected": False, "active_host": None,
        "busy": False, "on_factory_ip": False, "settled_host": None,
        "message": "", "last_result": None, "history": [],
        "auto": {"enabled": False, "channel": None, "ip": None,
                 "radar_ips": None},
        "cycle": {"enabled": False, "index": 0, "count": 0},
        "last_ok_serial": None, "net_warning": None,
    })
    monkeypatch.setattr(apu, "_save_log", lambda entry, raw: None)
    monkeypatch.setattr(apu, "label_printer",
                        LabelPrinter(PrinterSettings(
                            sink=str(tmp_path / "labels.zpl"))))
    return tmp_path


def labels(tmp_path) -> list[str]:
    sink = tmp_path / "labels.zpl"
    if not sink.exists():
        return []
    return [b for b in sink.read_text(encoding="utf-8").split("^XA") if b.strip()]


def configure(monkeypatch, **kwargs):
    monkeypatch.setattr(apu, "do_configure",
                        lambda target, host, avoid=None: fake_result(**kwargs))
    asyncio.run(apu.run_configuration(apu.resolve_target("0", None), HOST))
    return apu.state["history"][0]


def test_a_verified_configure_run_prints_one_label(monkeypatch, clean_state):
    entry = configure(monkeypatch)
    assert entry["label"]["printed"] is True
    assert entry["label"]["face"] == "pairing"
    assert len(labels(clean_state)) == 1


def test_a_failed_verification_prints_nothing(monkeypatch, clean_state):
    entry = configure(monkeypatch, verified=False)
    assert "label" not in entry
    assert labels(clean_state) == []


def test_a_failed_run_prints_nothing(monkeypatch, clean_state):
    entry = configure(monkeypatch, ok=False, verified=False)
    assert entry["status"] == "error"
    assert "label" not in entry
    assert labels(clean_state) == []


def test_the_label_block_is_attached_before_the_record_is_written(monkeypatch,
                                                                 clean_state):
    """_save_log writes the per-run JSON and spools the record to central. A
    label block attached after it would be absent from both."""
    seen = {}
    monkeypatch.setattr(apu, "_save_log",
                        lambda entry, raw: seen.update(entry) or None)
    configure(monkeypatch)
    assert seen["label"]["printed"] is True


def test_a_printer_failure_does_not_fail_the_run(monkeypatch, clean_state):
    monkeypatch.setattr(apu, "label_printer",
                        LabelPrinter(PrinterSettings(queue="nothing-here")))
    entry = configure(monkeypatch)
    assert entry["status"] == "ok"
    assert entry["verified"] is True
    assert entry["label"]["printed"] is False
    assert apu.state["phase"] == "configured"


# ── hands-free modes must not notice the printer (TEC-352) ───────────────────
#
# Cycle mode is the case that matters: it advances a channel index only on a
# run that came back ok, and it holds `state["busy"]` across the whole run —
# including the print. A printer that failed, or worse blocked, must not stall
# the loop or cost a channel.

def cycle_through(monkeypatch, units=3):
    """Run `units` devices through cycle mode, unplugging between each."""
    from magos_bench import MISS_THRESHOLD

    apu.state["cycle"] = {"enabled": True, "index": 0, "count": 0}
    serials = []

    def device(target, host, avoid=None):
        serial = f"APU-{len(serials):03d}"
        serials.append(serial)
        return fake_result(ip=target["ip"], serial=serial)

    monkeypatch.setattr(apu, "do_configure", device)
    for _ in range(units):
        asyncio.run(apu.poll_step(HOST, on_factory_ip=True))
        for _ in range(MISS_THRESHOLD):
            asyncio.run(apu.poll_step(None))
    return serials


def test_cycle_mode_keeps_advancing_with_no_printer(monkeypatch, clean_state):
    monkeypatch.setattr(apu, "label_printer",
                        LabelPrinter(PrinterSettings(queue="nothing-here")))
    cycle_through(monkeypatch, units=3)
    assert apu.state["cycle"]["count"] == 3
    assert apu.state["cycle"]["index"] == 1        # wrapped after 2 slots
    assert [h["status"] for h in apu.state["history"]] == ["ok"] * 3
    assert all(h["label"]["printed"] is False for h in apu.state["history"])


def test_cycle_mode_is_not_stalled_by_a_printer_that_blocks(monkeypatch,
                                                            clean_state):
    """A wedged spooler used to block its caller forever. `busy` is held for
    the whole print, so that would have stopped the cycle dead rather than
    costing it one label."""
    import bench_core.label_printer as lp

    monkeypatch.setattr(lp, "SPOOLER_SEND_TIMEOUT_SEC", 0.05)
    released = threading.Event()
    p = LabelPrinter(PrinterSettings(sink=str(clean_state / "labels.zpl")))
    p.discover()
    p._transport.send = lambda data: lp._bounded(
        lambda: released.wait(30), lp.SPOOLER_SEND_TIMEOUT_SEC, "a wedged queue")
    monkeypatch.setattr(apu, "label_printer", p)

    started = time.monotonic()
    cycle_through(monkeypatch, units=2)
    elapsed = time.monotonic() - started
    released.set()

    assert elapsed < 10.0, f"the cycle was held up for {elapsed:.1f}s"
    assert apu.state["cycle"]["count"] == 2
    assert apu.state["busy"] is False
    assert apu.state["phase"] in ("waiting", "configured")


class _OfflineSpooler:
    """A Windows queue whose printer is unplugged: it would happily accept
    every job. Enough of `win32print` to drive the real `_write`."""

    def __init__(self) -> None:
        self.written: list[bytes] = []

    def OpenPrinter(self, name):
        return "handle"

    def GetPrinter(self, handle, level):
        return {"Status": 0, "Attributes": 0x00000400}   # WORK_OFFLINE

    def WritePrinter(self, handle, data):
        self.written.append(data)

    def StartDocPrinter(self, handle, level, info):
        return 1

    def StartPagePrinter(self, handle):
        return None

    def EndPagePrinter(self, handle):
        return None

    def EndDocPrinter(self, handle):
        return None

    def ClosePrinter(self, handle):
        return None


def test_cycle_mode_keeps_advancing_with_an_offline_printer(monkeypatch,
                                                            clean_state):
    """The queue-status check added a spooler call to the print path. It runs
    inside the same hands-free loop as everything else, so it has to fail the
    same way: cost the label, not the cycle."""
    import bench_core.label_printer as lp

    fake = _OfflineSpooler()
    monkeypatch.setitem(sys.modules, "win32print", fake)
    monkeypatch.setattr(lp, "spooler_queues", lambda: ["ZDesigner ZD421"])
    monkeypatch.setattr(apu, "label_printer", LabelPrinter(PrinterSettings()))

    started = time.monotonic()
    cycle_through(monkeypatch, units=3)
    elapsed = time.monotonic() - started

    assert elapsed < 10.0, f"the cycle was held up for {elapsed:.1f}s"
    assert apu.state["cycle"]["count"] == 3
    assert apu.state["cycle"]["index"] == 1
    assert apu.state["busy"] is False
    assert [h["status"] for h in apu.state["history"]] == ["ok"] * 3
    assert all(h["verified"] is True for h in apu.state["history"])
    # The point of the check: nothing was handed to a printer that is not there.
    assert fake.written == []
    assert all("offline" in h["label"]["error"] for h in apu.state["history"])
    assert "by hand" in apu.public_state()["printer"]["warning"]


def test_cycle_mode_still_prints_one_label_per_unit(monkeypatch, clean_state):
    cycle_through(monkeypatch, units=3)
    assert apu.state["cycle"]["count"] == 3
    assert len(labels(clean_state)) == 3


def test_a_printer_failure_does_not_leave_the_tool_busy(monkeypatch,
                                                        clean_state):
    """`busy` gates the whole detection loop. Left set, the tool is frozen."""
    monkeypatch.setattr(apu, "label_printer",
                        LabelPrinter(PrinterSettings(queue="nothing-here")))
    configure(monkeypatch)
    assert apu.state["busy"] is False


def test_the_page_is_told_about_the_printer(monkeypatch, clean_state):
    state = apu.public_state()
    assert "printer" in state
    assert set(state["printer"]) == {"available", "target", "warning"}


def test_an_unprinted_pass_warns_the_operator_by_serial(monkeypatch,
                                                       clean_state):
    monkeypatch.setattr(apu, "label_printer",
                        LabelPrinter(PrinterSettings(queue="nothing-here")))
    configure(monkeypatch)
    warning = apu.public_state()["printer"]["warning"]
    assert "GSAC657352" in warning
    assert "by hand" in warning
