"""Tests for the label-print gate and transports (bench_core.label_printer).

The gate (`should_print`) is the whole of TEC-352's "no label, it doesn't
ship": a label is a claim that a unit was checked, so the interesting tests
here are the ones asserting the printer stays SILENT. A gate that has never
been shown to fail is not a gate an operator can trust.

The rest covers the half that can fail for reasons unrelated to the device:
discovery order, and the rule that a printer problem never fails a run.
"""
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from bench_core import label_printer as mod
from bench_core.label_printer import (
    LabelPrinter,
    PrinterSettings,
    SpoolerTransport,
    TcpTransport,
    find_spooler_queue,
    printer_settings,
    queue_fault,
    should_print,
)

RECORDS = Path(__file__).parent / "label-records"


def record(name: str = "tsw-static") -> dict:
    return json.loads((RECORDS / f"{name}.json").read_text(encoding="utf-8"))


def printer(tmp_path, **settings) -> LabelPrinter:
    """A printer writing to a file, so `print_run` can be driven end to end."""
    settings.setdefault("sink", str(tmp_path / "labels.zpl"))
    return LabelPrinter(PrinterSettings(**settings))


def printed(tmp_path) -> list[str]:
    sink = tmp_path / "labels.zpl"
    if not sink.exists():
        return []
    return [b for b in sink.read_text(encoding="utf-8").split("^XA") if b.strip()]


# ── the gate ─────────────────────────────────────────────────────────────────

def test_a_verified_ok_run_earns_a_label():
    assert should_print({"status": "ok", "verified": True}) is True


def test_a_failed_verification_prints_nothing():
    assert should_print({"status": "ok", "verified": False}) is False


def test_a_run_that_could_not_be_verified_prints_nothing():
    """The third outcome, and the one a truthiness check would get wrong.
    `None` means the checks were not in scope or could not be run —
    "configured but NOT verified" is a real end state on these tools."""
    assert should_print({"status": "ok", "verified": None}) is False
    assert should_print({"status": "ok"}) is False


def test_a_failed_run_prints_nothing_even_if_something_verified():
    assert should_print({"status": "error", "verified": True}) is False


@pytest.mark.parametrize("verified", [False, None])
def test_the_printer_is_silent_on_a_run_that_did_not_pass(tmp_path, verified):
    entry = dict(record(), verified=verified)
    assert printer(tmp_path).print_run(entry) is None
    assert printed(tmp_path) == []


def test_no_label_key_is_attached_when_no_label_was_due(tmp_path):
    """An absent `label` key means "this run did not earn one". That has to
    stay distinguishable from `{"printed": false}`, which means "it earned one
    and did not get it" — the case someone has to act on."""
    entry = dict(record(), verified=False)
    printer(tmp_path).print_run(entry)
    assert "label" not in entry


def test_a_passing_run_is_printed_once_and_recorded(tmp_path):
    entry = record()
    block = printer(tmp_path).print_run(entry)
    assert block["printed"] is True
    assert block["face"] == "shared-ip"
    assert block["error"] is None
    assert entry["label"] == block          # attached, for the JSON and central
    assert len(printed(tmp_path)) == 1


# ── a printer problem never costs a good provision ───────────────────────────

def test_a_missing_printer_leaves_the_run_ok_and_asks_for_a_hand_label(tmp_path):
    entry = record()
    p = LabelPrinter(PrinterSettings())       # nothing configured, nothing found
    block = p.print_run(entry)
    assert block["printed"] is False
    assert "no label printer" in block["error"]
    assert entry["status"] == "ok"            # the device still passed
    assert entry["verified"] is True
    assert entry["serial"] in p.status()["warning"]
    assert "by hand" in p.status()["warning"]


def test_a_printer_that_raises_mid_send_does_not_raise_into_the_run(tmp_path):
    entry = record()
    p = printer(tmp_path)
    p.discover()
    p._transport.send = lambda data: (_ for _ in ()).throw(OSError("cable"))
    block = p.print_run(entry)
    assert block["printed"] is False
    assert "cable" in block["error"]
    assert entry["label"]["printed"] is False


def test_a_send_failure_drops_the_cached_printer_so_the_next_run_looks_again(tmp_path):
    p = printer(tmp_path)
    p.discover()
    p._transport.send = lambda data: (_ for _ in ()).throw(OSError("gone"))
    p.print_run(record())
    assert p._transport is None
    assert p.status()["available"] is False


def test_a_hung_printer_is_abandoned_rather_than_holding_the_run(tmp_path,
                                                                monkeypatch):
    """The Windows spooler API takes no timeout, and a wedged spooler blocks
    its caller indefinitely. The calling tool holds `state["busy"]` for the
    whole print, so an unbounded block would not cost one label — it would
    freeze the run and stop a hands-free auto/cycle mode advancing."""
    monkeypatch.setattr(mod, "SPOOLER_SEND_TIMEOUT_SEC", 0.05)
    released = threading.Event()
    p = printer(tmp_path)
    p.discover()
    p._transport.send = lambda data: mod._bounded(
        lambda: released.wait(30), mod.SPOOLER_SEND_TIMEOUT_SEC, "a wedged queue")

    started = time.monotonic()
    block = p.print_run(record())
    elapsed = time.monotonic() - started
    released.set()

    assert elapsed < 5.0, f"the run was held for {elapsed:.1f}s"
    assert block["printed"] is False
    assert "did not return within" in block["error"]


def test_a_hung_queue_listing_does_not_wedge_discovery(monkeypatch):
    monkeypatch.setattr(mod, "SPOOLER_LIST_TIMEOUT_SEC", 0.05)
    released = threading.Event()
    monkeypatch.setattr(mod, "find_spooler_queue",
                        lambda queue="": released.wait(30) or None)
    started = time.monotonic()
    assert LabelPrinter(PrinterSettings()).discover() is None
    elapsed = time.monotonic() - started
    released.set()
    assert elapsed < 5.0, f"discovery blocked for {elapsed:.1f}s"


def test_bounded_returns_the_value_and_re_raises_the_error():
    assert mod._bounded(lambda: 7, 5, "fine") == 7
    with pytest.raises(ValueError):
        mod._bounded(lambda: (_ for _ in ()).throw(ValueError("boom")),
                     5, "broken")


def test_a_face_that_cannot_be_built_is_reported_not_raised(tmp_path):
    entry = dict(record(), tool="toaster")
    p = printer(tmp_path)
    block = p.print_run(entry)
    assert block["printed"] is False
    assert "label not built" in block["error"]


def test_a_label_that_would_print_as_mojibake_is_not_printed(monkeypatch,
                                                             tmp_path):
    """`qa_label` folds every field to ASCII, so this cannot happen today — the
    encode on the way out is that invariant asserted where it turns into bytes.
    Should a face ever leak a non-ASCII field, the choice locked in here is to
    print nothing and warn: an operator chases a missing label, but a garbled
    one gets stuck on a box and shipped."""
    monkeypatch.setattr(mod, "render_content", lambda c: "^XA^FD\u20ac^FS^XZ")
    p = printer(tmp_path)
    entry = record()
    block = p.print_run(entry)
    assert block["printed"] is False
    assert entry["status"] == "ok"                 # the run itself is untouched
    assert not (tmp_path / "labels.zpl").exists()  # nothing reached the printer
    assert entry["serial"] in p.status()["warning"]


class _ImmediateThread:
    """Runs the target on start(), so the startup probe is synchronous and the
    test is not a race."""

    def __init__(self, target=None, name=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


def test_the_startup_note_never_overwrites_a_named_unit(monkeypatch):
    """The standing "no printer" note is generic; a warning naming a serial is
    something an operator has to go and act on. The startup probe runs on its
    own thread and used to clobber the second with the first."""
    monkeypatch.setattr(mod, "spooler_queues", lambda: [])
    monkeypatch.setattr(mod.threading, "Thread", _ImmediateThread)
    p = LabelPrinter(PrinterSettings())
    p.print_run(record())
    warning = p.status()["warning"]            # triggers the probe, inline
    assert record()["serial"] in warning
    assert "No label printer detected" not in warning


def test_the_startup_note_appears_when_nothing_has_been_printed(monkeypatch):
    monkeypatch.setattr(mod, "spooler_queues", lambda: [])
    monkeypatch.setattr(mod.threading, "Thread", _ImmediateThread)
    p = LabelPrinter(PrinterSettings())
    assert "No label printer detected" in p.status()["warning"]


def test_a_successful_print_clears_an_earlier_warning(tmp_path):
    p = printer(tmp_path)
    p._warning = "stale"
    p.print_run(record())
    assert p.status()["warning"] is None


# ── a queue that accepts jobs it will not print ──────────────────────────────
#
# The gap these close: a USB ZD421 that has been unplugged keeps its Windows
# queue, so `WritePrinter` succeeds, the run records `printed: true`, and the
# label prints days later when someone reconnects the printer. That is the one
# way this feature can fail in the UNSAFE direction — claiming a label that
# does not exist — so the queue is asked before it is written to.

OFFLINE = 0x00000080          # PRINTER_STATUS_*
PAPER_OUT = 0x00000010
PAUSED = 0x00000001
DOOR_OPEN = 0x00400000
BUSY = 0x00000200
PRINTING = 0x00000400
WARMING_UP = 0x00010000
POWER_SAVE = 0x01000000
WORK_OFFLINE = 0x00000400     # PRINTER_ATTRIBUTE_WORK_OFFLINE (a different word)


class _FakeSpooler:
    """Enough of `win32print` to drive `SpoolerTransport._write` for real."""

    def __init__(self, status: int = 0, attributes: int = 0,
                 info=..., fail_status: bool = False) -> None:
        self._status = status
        self._attributes = attributes
        self._info = info
        self._fail_status = fail_status
        self.written: list[bytes] = []
        self.docs = 0
        self.closed = 0

    def OpenPrinter(self, name):
        return f"handle:{name}"

    def GetPrinter(self, handle, level):
        if self._fail_status:
            raise RuntimeError("RPC server unavailable")
        if self._info is not ...:
            return self._info
        return {"pPrinterName": "ZDesigner ZD421", "Status": self._status,
                "Attributes": self._attributes}

    def StartDocPrinter(self, handle, level, info):
        self.docs += 1
        return 1

    def StartPagePrinter(self, handle):
        return None

    def WritePrinter(self, handle, data):
        self.written.append(data)
        return len(data)

    def EndPagePrinter(self, handle):
        return None

    def EndDocPrinter(self, handle):
        return None

    def ClosePrinter(self, handle):
        self.closed += 1


def spooler(monkeypatch, **kwargs) -> _FakeSpooler:
    fake = _FakeSpooler(**kwargs)
    monkeypatch.setitem(sys.modules, "win32print", fake)
    return fake


@pytest.mark.parametrize("kwargs,phrase", [
    ({"attributes": WORK_OFFLINE}, "offline"),   # the unplugged-USB case
    ({"status": OFFLINE}, "offline"),
    ({"status": PAPER_OUT}, "out of labels"),
    ({"status": PAUSED}, "paused"),
    ({"status": DOOR_OPEN}, "open"),
])
def test_a_queue_that_will_not_print_is_refused_the_job(monkeypatch, kwargs,
                                                       phrase):
    fake = spooler(monkeypatch, **kwargs)
    with pytest.raises(OSError, match=phrase):
        SpoolerTransport("ZDesigner ZD421").send(b"^XA^XZ")
    assert fake.written == [], "the job was spooled to a printer that is not there"
    assert fake.docs == 0
    assert fake.closed == 1, "the printer handle was leaked"


@pytest.mark.parametrize("status", [0, PRINTING, WARMING_UP, BUSY, POWER_SAVE])
def test_a_healthy_or_merely_busy_printer_still_gets_the_job(monkeypatch, status):
    """The bias runs one way only. Refusing on `PRINTING` or `POWER_SAVE` —
    both normal for a ZD421 — would be a bench that never prints."""
    fake = spooler(monkeypatch, status=status)
    SpoolerTransport("ZDesigner ZD421").send(b"^XA^XZ")
    assert fake.written == [b"^XA^XZ"]


def test_an_unreadable_status_fails_open(monkeypatch):
    """A driver that will not answer is not evidence of a fault. Treating it as
    one would stop a working printer on every run."""
    fake = spooler(monkeypatch, fail_status=True)
    SpoolerTransport("ZDesigner ZD421").send(b"^XA^XZ")
    assert fake.written == [b"^XA^XZ"]


@pytest.mark.parametrize("info", [None, "not a dict", {},
                                  {"Status": None, "Attributes": None},
                                  {"Status": "weird"}])
def test_a_status_shaped_wrong_fails_open(monkeypatch, info):
    fake = spooler(monkeypatch, info=info)
    SpoolerTransport("ZDesigner ZD421").send(b"^XA^XZ")
    assert fake.written == [b"^XA^XZ"]


def test_an_offline_queue_reads_as_an_unprinted_label_end_to_end(monkeypatch):
    """What the operator ends up with: the run still passes, the record says
    the label did not print, and the standing warning names the unit."""
    spooler(monkeypatch, attributes=WORK_OFFLINE)
    monkeypatch.setattr(mod, "spooler_queues", lambda: ["ZDesigner ZD421"])
    p = LabelPrinter(PrinterSettings())
    entry = record()
    block = p.print_run(entry)

    assert block["printed"] is False
    assert "offline" in block["error"]
    assert block["target"] == "queue:ZDesigner ZD421"
    assert entry["status"] == "ok" and entry["verified"] is True
    assert entry["serial"] in p.status()["warning"]
    assert mod._HAND_LABEL in p.status()["warning"]
    # And the printer is looked for again next run rather than written into.
    assert p._transport is None


def test_the_reason_survives_into_the_runs_that_follow(monkeypatch):
    """A failed send starts the 20 s negative cache, so the next unit finds no
    transport at all. Without carrying the reason forward it was told the
    generic "no printer detected" — which sends an operator to check the config
    when the answer was "the queue is offline, check the cable". Over a batch
    they would see the useless message far more often than the useful one."""
    spooler(monkeypatch, attributes=WORK_OFFLINE)
    monkeypatch.setattr(mod, "spooler_queues", lambda: ["ZDesigner ZD421"])
    p = LabelPrinter(PrinterSettings())

    first = p.print_run(record())
    second = p.print_run(record())

    assert "offline" in first["error"]
    assert "offline" in second["error"], "the second unit lost the reason"
    assert "offline" in p.status()["warning"]


def test_a_successful_print_forgets_the_last_fault(tmp_path):
    p = printer(tmp_path)
    p._last_fault = "queue:X: the print queue is offline"
    p.print_run(record())
    assert p._last_fault is None
    # And a later miss with no history reads as plainly missing again.
    p._transport, p._available, p._missing_until = None, False, 0.0
    p.settings = PrinterSettings(queue="nothing-here")
    assert "no label printer detected" in p.print_run(record())["error"]


def test_the_fault_check_is_inside_the_timeout(monkeypatch):
    """`GetPrinter` blocks like every other spooler call, so it has to be under
    the same ceiling — a wedged status read must not freeze the run."""
    monkeypatch.setattr(mod, "SPOOLER_SEND_TIMEOUT_SEC", 0.05)
    released = threading.Event()

    class _Hanging(_FakeSpooler):
        def GetPrinter(self, handle, level):
            released.wait(30)
            return {}

    monkeypatch.setitem(sys.modules, "win32print", _Hanging())
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        SpoolerTransport("ZDesigner ZD421").send(b"^XA^XZ")
    elapsed = time.monotonic() - started
    released.set()
    assert elapsed < 5.0, f"the run was held for {elapsed:.1f}s"


def test_queue_fault_reads_the_attribute_and_the_status(monkeypatch):
    assert queue_fault(_FakeSpooler(), "h") is None
    assert queue_fault(_FakeSpooler(status=OFFLINE), "h") == "offline"
    assert queue_fault(_FakeSpooler(attributes=WORK_OFFLINE), "h") == "offline"


# ── discovery ────────────────────────────────────────────────────────────────

def test_the_sink_wins_so_a_developer_never_reaches_the_station_printer(tmp_path):
    p = printer(tmp_path, host="192.0.2.1", queue="ZDesigner ZD421")
    assert p.discover().target.startswith("file:")


def test_a_configured_host_is_tried_on_9100(monkeypatch, tmp_path):
    seen = {}

    def fake_open(host, port, timeout=2.0):
        seen["host"], seen["port"] = host, port
        return True

    monkeypatch.setattr(mod, "tcp_port_open", fake_open)
    transport = LabelPrinter(PrinterSettings(host="192.0.2.1")).discover()
    assert isinstance(transport, TcpTransport)
    assert seen == {"host": "192.0.2.1", "port": 9100}
    assert transport.target == "tcp://192.0.2.1:9100"


def test_an_unreachable_host_falls_back_to_the_windows_spooler(monkeypatch):
    monkeypatch.setattr(mod, "tcp_port_open", lambda *a, **k: False)
    monkeypatch.setattr(mod, "spooler_queues",
                        lambda: ["Microsoft Print to PDF", "ZDesigner ZD421"])
    transport = LabelPrinter(PrinterSettings(host="192.0.2.1")).discover()
    assert isinstance(transport, SpoolerTransport)
    assert transport.target == "queue:ZDesigner ZD421"


def test_a_usb_printer_is_found_by_name_with_nothing_configured(monkeypatch):
    monkeypatch.setattr(mod, "spooler_queues", lambda: ["ZD421", "OfficeJet"])
    assert find_spooler_queue() == "ZD421"


def test_an_unrelated_queue_is_never_picked(monkeypatch):
    """Sending ZPL to an office laser prints a page of source, so guessing
    wrong is worse than not printing."""
    monkeypatch.setattr(mod, "spooler_queues",
                        lambda: ["Microsoft Print to PDF", "OfficeJet Pro"])
    assert find_spooler_queue() is None


def test_a_named_queue_that_is_gone_does_not_resolve_to_a_different_one(monkeypatch):
    """If the station names a printer and that printer is missing, quietly
    using another one would put labels somewhere nobody is looking."""
    monkeypatch.setattr(mod, "spooler_queues", lambda: ["ZDesigner ZD421"])
    assert find_spooler_queue("Bench Label Printer") is None


def test_a_named_queue_matches_case_insensitively(monkeypatch):
    monkeypatch.setattr(mod, "spooler_queues", lambda: ["ZDesigner ZD421"])
    assert find_spooler_queue("zdesigner zd421") == "ZDesigner ZD421"


def test_no_spooler_at_all_is_not_an_error(monkeypatch):
    """A developer's Mac has no win32print; that is "no printer here", not a
    crash on import."""
    monkeypatch.setattr(mod, "spooler_queues", lambda: [])
    assert find_spooler_queue() is None


def test_discovery_failure_is_swallowed(monkeypatch):
    monkeypatch.setattr(mod, "spooler_queues",
                        lambda: (_ for _ in ()).throw(RuntimeError("spooler")))
    assert LabelPrinter(PrinterSettings()).discover() is None


# ── probe caching ────────────────────────────────────────────────────────────

def test_a_missing_printer_is_not_re_probed_on_every_run(monkeypatch):
    """A configured-but-unplugged network printer would otherwise cost the
    connect timeout at the end of every run in the batch."""
    calls = []
    monkeypatch.setattr(mod, "tcp_port_open",
                        lambda *a, **k: calls.append(1) or False)
    monkeypatch.setattr(mod, "spooler_queues", lambda: [])
    p = LabelPrinter(PrinterSettings(host="192.0.2.1"))
    p.discover()
    p.discover()
    assert len(calls) == 1


def test_the_negative_cache_expires(monkeypatch):
    monkeypatch.setattr(mod, "tcp_port_open", lambda *a, **k: False)
    monkeypatch.setattr(mod, "spooler_queues", lambda: [])
    p = LabelPrinter(PrinterSettings(host="192.0.2.1"))
    p.discover()
    p._missing_until = 0.0                    # as if PROBE_MISS_TTL_SEC passed
    monkeypatch.setattr(mod, "tcp_port_open", lambda *a, **k: True)
    assert p.discover() is not None


def test_status_does_not_wait_for_a_probe_in_flight(monkeypatch):
    """`status()` is called at 1 Hz on the event loop, and a TCP probe can take
    the full connect timeout. Sharing one lock between them stalled the whole
    page for three seconds every time discovery ran."""
    probing = threading.Event()
    release = threading.Event()

    def slow_open(*a, **k):
        probing.set()
        release.wait(5)
        return False

    monkeypatch.setattr(mod, "tcp_port_open", slow_open)
    monkeypatch.setattr(mod, "spooler_queues", lambda: [])
    p = LabelPrinter(PrinterSettings(host="192.0.2.1"))
    threading.Thread(target=p.discover, daemon=True).start()
    assert probing.wait(5), "the probe never started"

    started = time.monotonic()
    p.status()                                # must not block on the probe
    elapsed = time.monotonic() - started
    release.set()
    assert elapsed < 1.0, f"status() blocked for {elapsed:.2f}s"


def test_a_found_printer_is_reused_without_probing_again(monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "tcp_port_open",
                        lambda *a, **k: calls.append(1) or True)
    p = LabelPrinter(PrinterSettings(host="192.0.2.1"))
    p.discover()
    p.discover()
    assert len(calls) == 1


# ── settings ─────────────────────────────────────────────────────────────────

def test_settings_read_the_printer_block_of_the_station_file(tmp_path):
    (tmp_path / ".bench-station.json").write_text(json.dumps(
        {"station_id": "bench-01",
         "printer": {"host": "192.168.88.20", "port": 6101,
                     "queue": "Bench Labels"}}), encoding="utf-8")
    settings = printer_settings(tmp_path)
    assert settings.host == "192.168.88.20"
    assert settings.port == 6101
    assert settings.queue == "Bench Labels"


def test_settings_tolerate_a_powershell_written_byte_order_mark(tmp_path):
    """setup-station.ps1 writes this file from PowerShell, which leads with a
    BOM — the updater already has to deal with the same thing."""
    (tmp_path / ".bench-station.json").write_text(
        "\ufeff" + json.dumps({"printer": {"host": "192.168.88.20"}}),
        encoding="utf-8")
    assert printer_settings(tmp_path).host == "192.168.88.20"


def test_no_station_file_is_the_normal_case_not_an_error(tmp_path):
    settings = printer_settings(tmp_path)
    assert settings == PrinterSettings()
    assert settings.port == 9100


def test_a_station_file_with_no_printer_block_still_auto_detects(tmp_path):
    (tmp_path / ".bench-station.json").write_text(
        json.dumps({"station_id": "bench-01"}), encoding="utf-8")
    assert printer_settings(tmp_path).host == ""


def test_a_corrupt_station_file_does_not_stop_a_tool_starting(tmp_path):
    (tmp_path / ".bench-station.json").write_text("{not json",
                                                  encoding="utf-8")
    assert printer_settings(tmp_path) == PrinterSettings()


def test_the_environment_overrides_the_station_file(tmp_path, monkeypatch):
    (tmp_path / ".bench-station.json").write_text(json.dumps(
        {"printer": {"host": "192.168.88.20", "queue": "Old"}}),
        encoding="utf-8")
    monkeypatch.setenv("BENCH_PRINTER_HOST", "10.0.0.9")
    monkeypatch.setenv("BENCH_PRINTER_QUEUE", "New")
    monkeypatch.setenv("BENCH_PRINTER_SINK", "/tmp/x.zpl")
    settings = printer_settings(tmp_path)
    assert (settings.host, settings.queue, settings.sink) == \
        ("10.0.0.9", "New", "/tmp/x.zpl")


def test_a_junk_port_falls_back_to_9100(tmp_path):
    (tmp_path / ".bench-station.json").write_text(json.dumps(
        {"printer": {"port": "not-a-port"}}), encoding="utf-8")
    assert printer_settings(tmp_path).port == 9100


# ── print quality from the station file ──────────────────────────────────────

def test_print_quality_is_read_from_the_station_file(tmp_path):
    (tmp_path / ".bench-station.json").write_text(json.dumps(
        {"printer": {"media": "direct", "darkness": 24, "speed": 3}}),
        encoding="utf-8")
    settings = printer_settings(tmp_path)
    assert settings.quality() == {"media": "direct", "darkness": 24,
                                  "speed": 3}


def test_a_station_that_configures_no_quality_asks_for_none(tmp_path):
    """An empty dict, not zeroes — the difference between "leave the printer
    alone" and "print at darkness 0", which is the lightest setting there is."""
    assert printer_settings(tmp_path).quality() == {}


def test_darkness_zero_survives_into_the_quality(tmp_path):
    (tmp_path / ".bench-station.json").write_text(json.dumps(
        {"printer": {"darkness": 0}}), encoding="utf-8")
    assert printer_settings(tmp_path).quality() == {"darkness": 0}


@pytest.mark.parametrize("block", [{"darkness": "dark"}, {"speed": ""},
                                   {"media": "thermal"}, {"darkness": None}])
def test_a_typo_in_the_quality_block_leaves_the_printer_alone(tmp_path, block):
    """A pale label is a great deal easier to notice than a bench that has
    stopped, so a bad value here degrades to the printer's own settings rather
    than raising on the way to a print."""
    (tmp_path / ".bench-station.json").write_text(json.dumps(
        {"printer": block}), encoding="utf-8")
    assert printer_settings(tmp_path).quality() == {}


def test_the_environment_overrides_the_quality_too(tmp_path, monkeypatch):
    (tmp_path / ".bench-station.json").write_text(json.dumps(
        {"printer": {"darkness": 10}}), encoding="utf-8")
    monkeypatch.setenv("BENCH_PRINTER_DARKNESS", "27")
    monkeypatch.setenv("BENCH_PRINTER_MEDIA", "TRANSFER")
    settings = printer_settings(tmp_path)
    assert settings.darkness == 27
    assert settings.media == "transfer"       # case-folded, not rejected


def test_the_configured_quality_reaches_the_printed_label(tmp_path):
    """The wiring, end to end: a station file with a darkness in it has to come
    out as a `~SD` in front of the ZPL that gets sent."""
    p = printer(tmp_path, media="direct", darkness=26, speed=2)
    entry = record()
    p.print_run(entry)
    assert entry["label"]["printed"] is True
    written = Path(p.settings.sink).read_text(encoding="ascii")
    assert written.split("^XA", 1)[0].strip().endswith("^PR2")
    assert "^MTD" in written and "~SD26" in written


def test_an_unconfigured_station_still_prints_exactly_as_before(tmp_path):
    p = printer(tmp_path)
    p.print_run(record())
    written = Path(p.settings.sink).read_text(encoding="ascii")
    assert written.startswith("^XA")


# ── how many labels a run produces ───────────────────────────────────────────

def test_a_station_that_says_nothing_prints_the_pair(tmp_path):
    """Unlike the quality settings, this one has an opinion by default: a unit
    and its box both need labelling, and that is true of every bench."""
    assert printer_settings(tmp_path).copies == 2


@pytest.mark.parametrize("configured,expected", [
    (1, 1), (3, 3), (5, 5),
    (99, 5),        # clamped before it reaches the printer, not after
    (0, 1),
])
def test_a_station_can_say_how_many_it_wants(tmp_path, configured, expected):
    (tmp_path / ".bench-station.json").write_text(json.dumps(
        {"printer": {"copies": configured}}), encoding="utf-8")
    assert printer_settings(tmp_path).copies == expected


def test_a_typo_in_the_count_falls_back_to_the_pair(tmp_path):
    """Same reasoning as the quality block: a bad value must not be the thing
    that stops a bench printing."""
    (tmp_path / ".bench-station.json").write_text(json.dumps(
        {"printer": {"copies": "two"}}), encoding="utf-8")
    assert printer_settings(tmp_path).copies == 2


def test_the_environment_overrides_the_count(tmp_path, monkeypatch):
    (tmp_path / ".bench-station.json").write_text(json.dumps(
        {"printer": {"copies": 2}}), encoding="utf-8")
    monkeypatch.setenv("BENCH_PRINTER_COPIES", "1")
    assert printer_settings(tmp_path).copies == 1


def test_the_configured_count_reaches_the_printed_label(tmp_path):
    p = printer(tmp_path, copies=3)
    p.print_run(record())
    assert "^PQ3" in Path(p.settings.sink).read_text(encoding="ascii")


def test_a_run_sends_one_job_however_many_labels_it_wants(tmp_path):
    """The pair is the printer replicating one format. Two formats would mean a
    failure could leave the unit labelled and the box bare, which reads as a
    finished unit and is exactly the state this must not produce."""
    p = printer(tmp_path)
    p.print_run(record())
    written = Path(p.settings.sink).read_text(encoding="ascii")
    assert written.count("^XA") == 1
    assert "^PQ2" in written


def test_the_record_says_how_many_labels_exist(tmp_path):
    entry = record()
    printer(tmp_path, copies=2).print_run(entry)
    assert entry["label"]["copies"] == 2


def test_a_run_that_printed_nothing_claims_no_labels(tmp_path):
    """`copies` answers "how many are on the bench", not "how many were meant"
    — someone reconciling a batch against a pile of labels needs the former."""
    entry = record()
    p = LabelPrinter(PrinterSettings(host="", queue="", sink=""))
    p.print_run(entry)
    assert entry["label"]["printed"] is False
    assert entry["label"]["copies"] == 0


# ── the transports themselves ────────────────────────────────────────────────

def test_the_tcp_transport_sends_the_raw_zpl():
    """Raw ZPL on 9100, no driver and no framing — what a network Zebra
    expects."""
    received = []
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve():
        conn, _ = server.accept()
        with conn:
            received.append(conn.recv(65536))

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    TcpTransport("127.0.0.1", port).send(b"^XA^FDhi^FS^XZ")
    thread.join(timeout=5)
    server.close()
    assert received == [b"^XA^FDhi^FS^XZ"]


def test_the_file_transport_appends_so_a_session_stacks_up(tmp_path):
    p = printer(tmp_path)
    p.print_run(record("tsw-static"))
    p.print_run(record("otd"))
    assert len(printed(tmp_path)) == 2


def test_the_sink_directory_is_created_if_it_does_not_exist(tmp_path):
    p = printer(tmp_path, sink=str(tmp_path / "deep" / "nested" / "labels.zpl"))
    assert p.print_run(record())["printed"] is True


# ── what the page is told ────────────────────────────────────────────────────

def test_status_reports_the_target_once_a_printer_is_found(tmp_path):
    p = printer(tmp_path)
    p.discover()
    status = p.status()
    assert status["available"] is True
    assert status["target"].startswith("file:")
    assert status["warning"] is None


def test_status_before_any_probe_says_unknown_rather_than_broken():
    p = LabelPrinter(PrinterSettings(sink="/dev/null"))
    assert p._available is None


# ── the switch's extra label ─────────────────────────────────────────────────

def test_the_switch_gets_its_port_map_in_the_same_job(tmp_path):
    """QA label first, port map second, one send. Split across two jobs, a
    printer that died between them could put a port map on a switch whose QA
    label never came out."""
    p = printer(tmp_path, copies=1)
    p.print_run(record("planet"))
    written = Path(p.settings.sink).read_text(encoding="ascii")
    assert written.count("^XA") == 2
    assert written.index("PORT MAP") > written.index("^BC")   # QA label first


def test_the_port_map_is_counted_as_a_label_that_came_out(tmp_path):
    """`copies` is what someone reconciling a pile of labels counts, so the
    extra face is one more label, not a footnote."""
    entry = record("planet")
    block = printer(tmp_path, copies=2).print_run(entry)
    assert block["extra_faces"] == ["port-map"]
    assert block["copies"] == 3
    assert entry["label"] is block


def test_no_other_tool_grows_an_extra_label(tmp_path):
    entry = record("tsw-static")
    block = printer(tmp_path, copies=2).print_run(entry)
    assert "extra_faces" not in block
    assert block["copies"] == 2
