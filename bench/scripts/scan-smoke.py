#!/usr/bin/env python3
"""Drive the label-scan flow over real HTTP against both Teltonika tools.

Everything the e2e check on real hardware covers *except* the hardware: the
FastAPI wiring, the Pydantic body, JSON serialisation of the state feed, the
static assets, and both outcomes of the MAC cross-check. Devices are simulated
by writing `state["active_mac"]` directly, which is exactly what the detection
loop would have put there after an ARP read.

    .venv/bin/python scripts/scan-smoke.py

Run it after touching the scan path; it needs no devices and no scanner. It is
not part of the pytest suite because it binds real ports and starts real
servers — the same flow is covered there with the app driven in-process.
"""
from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import requests
import uvicorn

BENCH = Path(__file__).resolve().parent.parent

# Real scans, verbatim, and the MACs their labels carry.
OTD_LABEL = ("SN:6008219573;I:864088065513384;M:2097272B00F7;"
             "U:admin;PW:zZ?40*kA;B:015;")
OTD_MAC, OTD_PW = "20:97:27:2b:00:f7", "zZ?40*kA"
RUTM_LABEL = "SN:6010212527;M:20972732F638;U:admin;PW:mL$7=b6N;B:039;"
RUTM_MAC, RUTM_PW = "20:97:27:32:f6:38", "mL$7=b6N"

# What the scanner really sends: the configured prefix, then a CR.
def wedge(label: str) -> str:
    return "~" + label + "\r"


passed = failed = 0


def check(ok: bool, what: str, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok    {what}")
    else:
        failed += 1
        print(f"  FAIL  {what}" + (f"\n          {detail}" if detail else ""))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Served:
    """One tool's app on a real port, in a background thread."""

    def __init__(self, module_dir: str, module_name: str) -> None:
        sys.path.insert(0, str(BENCH / module_dir))
        self.mod = __import__(module_name)
        self.cfg = self.mod.configurator
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        config = uvicorn.Config(self.mod.app, host="127.0.0.1", port=self.port,
                                log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                requests.get(self.base + "/api/state", timeout=1)
                return self
            except requests.RequestException:
                time.sleep(0.1)
        raise SystemExit(f"{self.base} never came up")

    def __exit__(self, *_exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)

    # The detection loop overwrites active_mac every 2s from a real ARP read,
    # which finds nothing here — so re-assert the simulated device each time.
    def plug_in(self, mac):
        self.cfg.state["active_mac"] = mac
        self.cfg.state["detected"] = True
        self.cfg.state["phase"] = "detected"

    def scan(self, raw):
        return requests.post(self.base + "/api/label-scan",
                             json={"raw": raw}, timeout=5).json()

    def state(self):
        return requests.get(self.base + "/api/state", timeout=5).json()


def exercise(tool, label, mac, password, other_mac, pw_field):
    print(f"\n{tool.cfg.title} on {tool.base}")

    # --- the page and its assets -------------------------------------------
    page = requests.get(tool.base + "/", timeout=5).text
    check("labelPwHint" in page, "the page carries the hint element bench.js updates")
    js = requests.get(tool.base + "/shared/bench.js", timeout=5).text
    check("mountLabelScan" in js and "findLabelPayload" in js,
          "bench.js is served with the scan capture in it")
    css = requests.get(tool.base + "/shared/bench.css", timeout=5).text
    check(".scanwarn" in css and ".scan-sink" in css,
          "bench.css is served with the scan styles in it")

    # --- the happy path ----------------------------------------------------
    tool.plug_in(mac)
    tool.cfg.clear_armed_label()
    result = tool.scan(wedge(label))
    check("error" not in result, "a scan of the plugged-in device is accepted",
          str(result.get("error")))
    armed = (result.get("label_scan") or {}).get("armed") or {}
    check(armed.get("matches_active") is True,
          "the label MAC is confirmed against the device")
    check(armed.get("has_password") is True and armed.get("password_length") == 8,
          "the state reports a password without carrying it")
    check(password not in requests.get(tool.base + "/api/state", timeout=5).text,
          "the password is nowhere in the state feed")
    check(tool.cfg.resolve_label_password("") == (password, "scan"),
          "Configure would use the scanned password")

    # --- typing still wins -------------------------------------------------
    check(tool.cfg.resolve_label_password("typed-instead") == ("typed-instead", "typed"),
          "a typed password overrides the armed scan")

    # --- the mismatch path (the reason the cross-check exists) -------------
    tool.plug_in(other_mac)
    tool.cfg.clear_armed_label()
    result = tool.scan(wedge(label))
    error = result.get("error", "")
    check(bool(error), "scanning a different device's label is refused")
    check(other_mac in error, "the refusal names the device that is plugged in", error)
    check(tool.cfg.armed_label() is None, "nothing is armed after a refusal")
    check(tool.cfg.resolve_label_password("") == ("", "shared-fallback"),
          "Configure falls back rather than using a refused scan")
    problem = (tool.state().get("label_scan") or {}).get("problem") or ""
    check(other_mac in problem, "the refusal is on the state feed for the banner")

    # --- swapping the device clears it -------------------------------------
    tool.plug_in(mac)
    tool.cfg.clear_armed_label()
    tool.scan(wedge(label))
    tool.plug_in(other_mac)                      # a different unit on the bench
    check(tool.cfg.armed_label() is None, "swapping the device drops the armed scan")

    # --- junk --------------------------------------------------------------
    tool.plug_in(mac)
    for junk, why in [("EMP-00417", "an operator badge"),
                      ("~", "a bare prefix"),
                      ("", "an empty scan")]:
        check("error" in tool.scan(junk), f"{why} is refused")

    # --- the field naming asymmetry ---------------------------------------
    tool.cfg.clear_armed_label()
    tool.scan(wedge(label))
    body = {"site_name": "smoke-test", pw_field: ""}
    sent = requests.post(tool.base + "/api/configure", json=body, timeout=5).json()
    # No real device answers, so the run fails — but it must fail in the
    # pipeline, having accepted the body and the armed password.
    check("error" not in sent or "already in progress" not in sent.get("error", ""),
          f"/api/configure accepts a body using {pw_field}", str(sent))
    history = tool.cfg.state["history"]
    check(bool(history) and history[0]["device"].get("password_source") == "scan",
          "the run record says the password came from a scan",
          str(history[0]["device"] if history else "no run recorded"))
    check(bool(history) and password not in str(history[0]),
          "the failed run's record still has no password in it")


def main() -> int:
    print("Label-scan smoke test — no devices, no scanner required.")
    with Served("otd-config-ui", "otd_app") as otd:
        exercise(otd, OTD_LABEL, OTD_MAC, OTD_PW, RUTM_MAC, "label_password")
    with Served("rutm-config-ui", "rutm_app") as rutm:
        exercise(rutm, RUTM_LABEL, RUTM_MAC, RUTM_PW, OTD_MAC, "initial_password")

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
