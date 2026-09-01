#!/usr/bin/env python3
"""Getting the QA label onto the Zebra ZD421 (TEC-352).

`qa_label.py` decides what a label says; this decides whether one is due,
finds the printer, and sends it. The two are split because the first is pure
and the second is the half that can fail for reasons that have nothing to do
with the device on the bench.

The gate is one line — `should_print()` — and it is the whole point of the
issue: "no label, it doesn't ship" only means something if the printer stays
silent on a run that did not pass. A missing label is the signal, so there is
no REJECT face and no "printed anyway, marked failed".

Printing is never fatal. A verified device is verified whether or not a
printer answered; failing the run would throw away a good provision over a
USB cable. So every failure here ends the same way: the run stays `ok`, the
record carries a `label` block saying what went wrong, and the operator gets a
standing warning telling them to label that unit by hand. What is NOT
acceptable is failing silently — a bench that quietly stopped printing would
put unlabelled units in the done pile, which is the exact hole this closes.

Two transports, discovered in that order, because the station's printer may be
either and the operator should not have to care:

    tcp     a networked ZD421 on port 9100 — raw ZPL, no driver involved
    queue   a USB ZD421 as a Windows print queue — raw passthrough via the
            spooler, which needs the ZDesigner driver (or "Generic / Text
            Only"); a driver that renders the job would print the ZPL as text
    file    BENCH_PRINTER_SINK=<path>, a development escape that appends the
            ZPL to a file so faces can be reviewed with no printer at all

Configured by an optional `printer` block in `.bench-station.json` — a printer
is station hardware, the same category as `station_id`:

    {"station_id": "bench-01", "printer": {"host": "192.168.88.20",
                                           "queue": "ZDesigner ZD421"}}

with BENCH_PRINTER_HOST / BENCH_PRINTER_QUEUE / BENCH_PRINTER_SINK overriding
per process. Nothing is required: with an empty block a USB printer is still
found by name.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bench_core import tcp_port_open
from bench_core.qa_label import MEDIA_COMMAND, label_content, render_content

STATION_FILENAME = ".bench-station.json"
DEFAULT_PORT = 9100                  # the raw-ZPL port on every network Zebra
CONNECT_TIMEOUT_SEC = 3.0
SEND_TIMEOUT_SEC = 10.0

# Hard ceilings on the two spooler calls, which have no timeouts of their own.
# See `_bounded`: without these a wedged spooler blocks the run that is
# printing, and `busy` stays set — which stops a hands-free auto/cycle mode
# dead instead of costing it one label.
SPOOLER_SEND_TIMEOUT_SEC = 10.0
SPOOLER_LIST_TIMEOUT_SEC = 5.0

# How long a failed discovery is remembered. Without it, a station with a
# configured-but-unplugged network printer would spend CONNECT_TIMEOUT_SEC at
# the end of every single run. Short enough that plugging the printer in is
# noticed within a device or two.
PROBE_MISS_TTL_SEC = 20.0

# Which Windows print queue is the label printer, when the station config does
# not name one. Deliberately narrow: picking the wrong queue means ZPL comes
# out of an office laser as a page of gibberish.
_QUEUE_HINT = re.compile(r"zd\s*421|zdesigner|zebra", re.IGNORECASE)

_HAND_LABEL = "label it by hand before shipping"

# Spooler flags meaning "a job handed over now will not come out now". Taken
# from winspool.h rather than read off `win32print`, so the table imports and
# is testable on a machine with no spooler at all.
#
# Ordered by how useful the phrase is to whoever has to go and look at the
# printer — the first match is the one reported.
_WORK_OFFLINE = 0x00000400              # PRINTER_ATTRIBUTE_WORK_OFFLINE
_BLOCKING_STATUS: tuple[tuple[int, str], ...] = (
    (0x00000080, "offline"),            # PRINTER_STATUS_OFFLINE
    (0x00001000, "not available"),      # ..._NOT_AVAILABLE
    (0x00000010, "out of labels"),      # ..._PAPER_OUT
    (0x00000008, "jammed"),             # ..._PAPER_JAM
    (0x00000040, "reporting a media problem"),   # ..._PAPER_PROBLEM
    (0x00400000, "open"),               # ..._DOOR_OPEN
    (0x00100000, "waiting for someone at the printer"),  # ..._USER_INTERVENTION
    (0x00000002, "in an error state"),  # ..._ERROR
    (0x00000001, "paused"),             # ..._PAUSED
    (0x00000004, "being deleted"),      # ..._PENDING_DELETION
    (0x00200000, "out of memory"),      # ..._OUT_OF_MEMORY
)
# Deliberately absent: BUSY, PRINTING, IO_ACTIVE, WAITING, PROCESSING,
# INITIALIZING, WARMING_UP, POWER_SAVE. All of those are a healthy ZD421 either
# idling or working, and refusing on one would stop a printer that is fine.


def queue_fault(win32print, handle) -> Optional[str]:
    """Why this queue will not print right now, in a phrase, or None.

    A USB printer that has been unplugged keeps its Windows queue, so the job
    spools happily, `WritePrinter` succeeds, and the run records a label that
    never came out — then the whole backlog prints at once whenever someone
    reconnects the printer. "No label, it doesn't ship" cannot rest on a send
    that succeeds into a queue nobody is reading, so the queue is asked whether
    it is in a state to print before it is handed anything.

    Fails OPEN, on purpose. An unreadable status is not evidence of a fault,
    and treating it as one would turn every driver that reports nothing into a
    bench that never prints. The failure this guards against is the unsafe
    direction — a label claimed and not produced — not the safe one.
    """
    try:
        info = win32print.GetPrinter(handle, 2)
    except Exception:  # noqa: BLE001 — an unreadable status is not a fault
        return None
    if not isinstance(info, dict):
        return None

    def flag(key: str) -> int:
        try:
            return int(info.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    # The "Use Printer Offline" attribute is the one an unplugged USB device
    # actually sets; the status word alone often still reads as idle.
    if flag("Attributes") & _WORK_OFFLINE:
        return "offline"
    status = flag("Status")
    return next((phrase for bit, phrase in _BLOCKING_STATUS if status & bit),
                None)


def _bounded(work, timeout: float, what: str):
    """Run `work()` on a throwaway thread, giving up after `timeout`.

    Exists for the Windows spooler, whose API takes no timeout and blocks its
    caller for as long as the spooler service is wedged or the queue is paused
    with a full buffer — both real failure modes on a bench station. The
    calling tool holds `state["busy"]` for the whole of a print, so an
    unbounded block does not cost one label, it freezes the run: the page sits
    on "Configuring…" and a hands-free auto/cycle mode stops advancing. A
    printer is not allowed to do that, so the wait has a ceiling.

    A timed-out thread is abandoned rather than killed — a blocked native call
    cannot be interrupted. It is a daemon, so it cannot hold the process open,
    and the bench carries on and reports the label as unprinted.
    """
    done: list = []
    failed: list = []

    def run() -> None:
        try:
            done.append(work())
        except Exception as e:  # noqa: BLE001 — re-raised on the caller
            failed.append(e)

    thread = threading.Thread(target=run, name="label-printer-io", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"{what} did not return within {timeout:.0f}s")
    if failed:
        raise failed[0]
    if not done:
        # The thread ended without recording either outcome, so `work` died on
        # something `except Exception` does not see. Raising keeps the caller's
        # own handler in charge; returning would read as a successful print.
        raise RuntimeError(f"{what} ended without a result")
    return done[0]


def should_print(entry: dict) -> bool:
    """Whether this finished run earned a label.

    `verified is True`, not truthiness: the schema has three outcomes and the
    third one matters. `None` means the checks were not in scope or could not
    be run — "configured but NOT verified" is a real end state on these tools,
    and it is exactly the case a label must not be printed for, because a
    label is a claim that someone checked.
    """
    return entry.get("status") == "ok" and entry.get("verified") is True


# ── configuration ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PrinterSettings:
    host: str = ""
    port: int = DEFAULT_PORT
    queue: str = ""
    sink: str = ""
    # Print quality. Unset means "leave the printer as it is", which is what
    # every station did before these existed — so an unconfigured bench keeps
    # printing exactly as it does today.
    media: str = ""               # "direct" | "transfer"
    darkness: Optional[int] = None
    speed: Optional[int] = None

    def quality(self) -> dict:
        """The print-quality keywords for `render_content`, omitting anything
        this station has not set."""
        out: dict = {}
        if self.media:
            out["media"] = self.media
        if self.darkness is not None:
            out["darkness"] = self.darkness
        if self.speed is not None:
            out["speed"] = self.speed
        return out


def printer_settings(bench_root: Path) -> PrinterSettings:
    """This station's printer settings: the `printer` block of
    `.bench-station.json`, with per-process environment overrides.

    Never raises. A station file that is missing, unreadable or has no printer
    block is the normal case on a bench with a USB printer and nothing
    configured — discovery finds it by name instead.
    """
    block: dict = {}
    with contextlib.suppress(OSError, ValueError, AttributeError):
        # utf-8-sig: setup-station.ps1 writes this file from PowerShell, which
        # leads with a BOM (the updater deals with the same thing).
        raw = json.loads((bench_root / STATION_FILENAME)
                         .read_text(encoding="utf-8-sig"))
        if isinstance(raw.get("printer"), dict):
            block = raw["printer"]

    def setting(env: str, key: str) -> str:
        return str(os.environ.get(env) or block.get(key) or "").strip()

    port = DEFAULT_PORT
    with contextlib.suppress(TypeError, ValueError):
        port = int(setting("BENCH_PRINTER_PORT", "port") or DEFAULT_PORT)

    def number(env: str, key: str) -> Optional[int]:
        """An optional integer setting. A value that is absent and a value that
        is nonsense both mean "leave the printer alone" — a typo in the station
        file must not stop labels printing, and a pale label is a great deal
        easier to notice than a bench that has stopped.

        Deliberately not built on `setting()`: that folds every falsy value to
        "", and 0 is a real darkness — the lightest one. Read through it and a
        station asking for the lightest print silently gets the printer's own
        setting instead.
        """
        raw = os.environ.get(env) or block.get(key)
        if raw is None or raw == "":
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    media = setting("BENCH_PRINTER_MEDIA", "media").lower()
    return PrinterSettings(host=setting("BENCH_PRINTER_HOST", "host"),
                           port=port,
                           queue=setting("BENCH_PRINTER_QUEUE", "queue"),
                           sink=setting("BENCH_PRINTER_SINK", "sink"),
                           media=media if media in MEDIA_COMMAND else "",
                           darkness=number("BENCH_PRINTER_DARKNESS", "darkness"),
                           speed=number("BENCH_PRINTER_SPEED", "speed"))


# ── transports ───────────────────────────────────────────────────────────────

class Transport:
    """Somewhere ZPL can be sent. `target` is what the record and the log
    call it, so a failure names the thing that failed."""

    target = ""

    def send(self, data: bytes) -> None:
        raise NotImplementedError


class TcpTransport(Transport):
    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port
        self.target = f"tcp://{host}:{port}"

    def send(self, data: bytes) -> None:
        with socket.create_connection((self.host, self.port),
                                      timeout=CONNECT_TIMEOUT_SEC) as sock:
            sock.settimeout(SEND_TIMEOUT_SEC)
            sock.sendall(data)


class SpoolerTransport(Transport):
    """A Windows print queue, written as a RAW job.

    RAW is what makes this work: it hands the bytes to the printer untouched
    instead of asking the driver to render them. Anything else turns the label
    into a printout of ZPL source.

    The queue is checked before it is written to — see `queue_fault`. A spooler
    accepts jobs for a printer that is not there, so "the write succeeded" is
    not the same claim as "a label came out".
    """

    def __init__(self, queue: str) -> None:
        self.queue = queue
        self.target = f"queue:{queue}"

    def send(self, data: bytes) -> None:
        _bounded(lambda: self._write(data), SPOOLER_SEND_TIMEOUT_SEC,
                 f"the print spooler ({self.queue})")

    def _write(self, data: bytes) -> None:
        import win32print                       # Windows-only, guarded import

        handle = win32print.OpenPrinter(self.queue)
        try:
            fault = queue_fault(win32print, handle)
            if fault is not None:
                # Refused BEFORE StartDocPrinter. A job accepted here would sit
                # in the queue and print whenever the printer came back, long
                # after the unit it belongs to had been boxed and shipped.
                raise OSError(f"the print queue is {fault}")
            win32print.StartDocPrinter(handle, 1,
                                       ("bench QA label", None, "RAW"))
            try:
                win32print.StartPagePrinter(handle)
                win32print.WritePrinter(handle, data)
                win32print.EndPagePrinter(handle)
            finally:
                win32print.EndDocPrinter(handle)
        finally:
            win32print.ClosePrinter(handle)


class FileTransport(Transport):
    """BENCH_PRINTER_SINK — appends ZPL to a file instead of printing it.

    For working on the faces without hardware: the file is a stack of
    `^XA...^XZ` labels, which is exactly what labelary.com renders.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.target = f"file:{path}"

    def send(self, data: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "ab") as handle:
            handle.write(data)


def spooler_queues() -> list[str]:
    """Every print queue this machine can see, or [] where there is no Windows
    spooler (a developer's Mac) or pywin32 is not installed."""
    try:
        import win32print
    except ImportError:
        return []
    try:
        flags = (win32print.PRINTER_ENUM_LOCAL
                 | win32print.PRINTER_ENUM_CONNECTIONS)
        return [p[2] for p in win32print.EnumPrinters(flags)
                if len(p) > 2 and p[2]]
    except Exception:  # noqa: BLE001 — a spooler API error is "no printer"
        return []


def find_spooler_queue(queue: str = "") -> Optional[str]:
    """The label printer's queue name, or None.

    A configured `queue` that is not installed resolves to None rather than
    falling through to the name match: if the station names a printer and that
    printer is gone, quietly using a different one is worse than not printing.
    """
    names = spooler_queues()
    if queue:
        wanted = queue.strip().lower()
        return next((n for n in names if n.strip().lower() == wanted), None)
    return next((n for n in names if _QUEUE_HINT.search(n)), None)


# ── the printer ──────────────────────────────────────────────────────────────

class LabelPrinter:
    """One tool process's view of the station label printer.

    `print_run()` holds all the logic and is synchronous (that is what the
    tests drive); the only thread here is the one-shot startup probe, so the
    page can say "no printer" before the first device rather than after it.
    """

    def __init__(self, settings: PrinterSettings,
                 logger: Optional[logging.Logger] = None) -> None:
        self.settings = settings
        self.log = logger or logging.getLogger("bench-label")
        self._lock = threading.Lock()
        # A separate, never-contended lock for the started-once flag. `_lock`
        # is held across a TCP probe (up to CONNECT_TIMEOUT_SEC), and
        # `status()` runs on the event loop at 1 Hz — sharing one lock would
        # let a background probe stall the whole page for three seconds.
        self._probe_lock = threading.Lock()
        self._transport: Optional[Transport] = None
        self._available: Optional[bool] = None      # None = not probed yet
        self._warning: Optional[str] = None
        self._missing_until = 0.0
        self._probe_started = False
        # Why the printer dropped out, kept for the runs that follow. A failed
        # send starts the negative cache, so the next unit finds no transport
        # at all and would otherwise be told the generic "no printer detected"
        # — which sends an operator looking at the config when the real answer
        # was "the queue is offline, check the cable". Over a batch they would
        # see the useless message far more often than the useful one.
        self._last_fault: Optional[str] = None

    # ── discovery ────────────────────────────────────────────────────────────

    def _find(self) -> Optional[Transport]:
        settings = self.settings
        if settings.sink:
            return FileTransport(Path(settings.sink))
        if settings.host and tcp_port_open(settings.host, settings.port,
                                           CONNECT_TIMEOUT_SEC):
            return TcpTransport(settings.host, settings.port)
        # Bounded for the same reason as the send: EnumPrinters blocks too.
        queue = _bounded(lambda: find_spooler_queue(settings.queue),
                         SPOOLER_LIST_TIMEOUT_SEC, "listing print queues")
        return SpoolerTransport(queue) if queue else None

    def discover(self, *, force: bool = False) -> Optional[Transport]:
        """The printer, or None. Caches both answers — see PROBE_MISS_TTL_SEC."""
        with self._lock:
            if self._transport is not None and not force:
                return self._transport
            if not force and self._available is False \
                    and time.monotonic() < self._missing_until:
                return None
            try:
                transport = self._find()
            except Exception as e:  # noqa: BLE001 — discovery must never raise
                self.log.warning("Label printer discovery failed: %s", e)
                transport = None
            self._transport = transport
            self._available = transport is not None
            if transport is None:
                self._missing_until = time.monotonic() + PROBE_MISS_TTL_SEC
            else:
                self._missing_until = 0.0
                self.log.info("Label printer: %s.", transport.target)
            return transport

    def probe_in_background(self) -> None:
        """Probe once, off whatever thread is asking. Started lazily by
        `status()` rather than in a tool's constructor, so importing an app
        (which every test does) does not go looking for hardware."""
        with self._probe_lock:
            if self._probe_started:
                return
            self._probe_started = True

        def probe() -> None:
            # Only ever ADDS the standing note. A warning already set names a
            # specific unit that passed and did not get its label, which is
            # the actionable one — the generic note must not overwrite it.
            if self.discover() is None and self._warning is None:
                self._warning = (
                    "No label printer detected — units must be labelled by "
                    "hand before they ship.")

        threading.Thread(target=probe, name="label-printer-probe",
                         daemon=True).start()

    # ── state for the page ───────────────────────────────────────────────────

    def status(self) -> dict:
        """What `/api/state` publishes as `printer`. Kicks off the startup
        probe the first time it is asked."""
        self.probe_in_background()
        transport = self._transport
        return {"available": self._available,
                "target": transport.target if transport else None,
                "warning": self._warning}

    # ── printing ─────────────────────────────────────────────────────────────

    def _block(self, *, printed: bool, face: str = "",
               target: Optional[str] = None,
               error: Optional[str] = None) -> dict:
        return {"printed": printed, "face": face, "target": target,
                "error": error, "at": datetime.now(timezone.utc).isoformat()}

    def _failed(self, entry: dict, detail: str) -> None:
        """Record that a unit which passed did not get its label."""
        serial = entry.get("serial") or "this unit"
        self._warning = (f"SN {serial} passed but no label was printed — "
                         f"{_HAND_LABEL}. ({detail})")
        self.log.warning("Label NOT printed for SN %s: %s. The operator has "
                         "been asked to %s.", serial, detail, _HAND_LABEL)

    def print_run(self, entry: dict) -> Optional[dict]:
        """Print `entry`'s QA label if it earned one.

        Returns the `label` block, which it also attaches to `entry` — so the
        record on disk and the copy shipped to central both carry it. Returns
        None, attaching nothing, when no label was due: an absent `label` key
        means "this run did not earn one", which a reader has to be able to
        tell apart from `{"printed": false}` — a run that earned a label and
        did not get it.

        Never raises.
        """
        if not should_print(entry):
            return None
        try:
            content = label_content(entry)
            zpl = render_content(content, **self.settings.quality())
        except Exception as e:  # noqa: BLE001 — a bad face must not fail a run
            self.log.exception("Could not build the QA label.")
            block = self._block(printed=False, error=f"label not built: {e}")
            entry["label"] = block
            self._failed(entry, str(e))
            return block

        transport = self.discover()
        if transport is None:
            detail = self._last_fault or "no printer detected"
            block = self._block(printed=False, face=content.face,
                                error=self._last_fault
                                or "no label printer detected")
            entry["label"] = block
            self._failed(entry, detail)
            return block

        try:
            # ascii, not utf-8: `qa_label._ascii` folds every field, so this
            # encode is that invariant asserted at the one place it becomes
            # bytes. Were a field ever to reach here unfolded, utf-8 would
            # silently emit multibyte and the ZD421 would print mojibake onto a
            # QA document; ascii raises instead, and a raise here is already
            # handled as an unprinted label — which is the right outcome, since
            # a label nobody can read is worse than the missing one the
            # operator is warned about.
            transport.send(zpl.encode("ascii"))
        except Exception as e:  # noqa: BLE001 — any socket/spooler/OS error
            with self._lock:
                # Drop the cached transport: the printer was there and is not
                # now, so the next run should look again rather than keep
                # writing into a dead socket.
                self._transport = None
                self._available = False
                self._missing_until = time.monotonic() + PROBE_MISS_TTL_SEC
            self._last_fault = f"{transport.target}: {e}"
            block = self._block(printed=False, face=content.face,
                                target=transport.target, error=str(e))
            entry["label"] = block
            self._failed(entry, self._last_fault)
            return block

        self._warning = None
        self._last_fault = None
        self._available = True
        self.log.info("QA label printed for SN %s (%s face) on %s.",
                      entry.get("serial"), content.face, transport.target)
        block = self._block(printed=True, face=content.face,
                            target=transport.target)
        entry["label"] = block
        return block


def make_label_printer(bench_root: Path,
                       logger: Optional[logging.Logger] = None) -> LabelPrinter:
    """This station's label printer, ready to use. Does not touch hardware —
    discovery happens on the first `status()` or `print_run()`."""
    return LabelPrinter(printer_settings(bench_root), logger)
