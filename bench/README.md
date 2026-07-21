# Bench provisioning tools

Everything that runs on the **Windows bench PC** to provision devices, one
plug-in at a time. All the tools share the same workflow — *plug a device in →
it's detected → configure → unplug → repeat* — and the same look, so learning
one teaches you all of them.

This `bench/` folder is the **only** thing you need to copy onto the bench PC.
The rest of `techops-tools/` (cluster scripts, GIS tools, etc.) is not used here.

## For the operator

The tools run the same on **Windows** and **macOS** — only the launcher you
double-click differs.

1. Install **Python 3.11+** once:
   - **Windows:** the [python.org installer](https://www.python.org/downloads/)
     — tick *"Add python.exe to PATH"*.
   - **macOS:** the [python.org installer](https://www.python.org/downloads/),
     or `brew install python`.
2. Start everything with one launcher:
   - **Windows:** double-click `Start Bench Tools.bat`
   - **macOS/Linux:** run `./start-bench.sh` from a terminal in this folder

   It pulls the latest tools (`git`), sets up the shared environment, launches
   all the tools, and opens the dashboard in your browser.
3. On the dashboard, click the tool for whatever you're plugging in. A green dot
   means that tool is up; grey means it's still starting (first run installs
   dependencies — give it a moment) or stopped.

> **Operators:** step-by-step instructions, gotchas, and fixes live in the
> **Operator guide** — linked from the dashboard, optionally opened by
> `Start Bench Tools.bat` (it asks, or set `BENCH_OPEN_GUIDE=1` to always open it; drag
> it to the side screen), and in `OPERATOR-GUIDE.md` (the version kept in Notion).

```
double-click launcher  →  dashboard  ┌───────────────────────────────────────────┐
                                     │  ● Magos Radar       :8001   192.168.40.x │
                                     │  ● Magos APU         :8002   192.168.40.x │
                                     │  ● Teltonika OTD500  :8003   192.168.1.x  │
                                     │  ● Teltonika RUTM08  :8004   192.168.1.x  │
                                     │  ● Raythink Camera   :8005   192.168.1.x  │
                                     │  ● ISR Speaker       :8006   DHCP (scan)  │
                                     └───────────────────────────────────────────┘
```

There is a single launcher that starts all the tools together (they run side by
side on fixed, non-colliding ports). To stop everything, close the launcher
window (Windows) or press Ctrl+C in the terminal (macOS/Linux).

> **Frozen bench:** every launch does a best-effort `git pull` to get the latest
> tools. To freeze the version currently on disk (e.g. mid-session), set
> `BENCH_NO_PULL=1` before launching.

> **macOS/Linux first-launch:** if the script won't run, restore the executable
> bit with `chmod +x start-bench.sh` (it can be lost in transit). On macOS, if
> Gatekeeper complains, run it from the terminal rather than Finder.

> **Network adapter:** each tool talks to a device on a specific subnet — the
> dashboard card and the tool's own page tell you which. If a device is plugged
> in but never detected, the adapter is almost always on the wrong subnet.

## The tools

| Folder | Device | Port | Notes |
| --- | --- | --- | --- |
| `magos-config-ui/` | Magos AR-300 **radar** | 8001 | factory subnet `192.168.40.x` |
| `magos-config-ui/` | Magos **APU** | 8002 | factory IP `192.168.40.60` |
| `otd-config-ui/` | Teltonika **OTD500** | 8003 | one device at a time, factory `192.168.1.1` |
| `rutm-config-ui/` | Teltonika **RUTM08** | 8004 | one device at a time, factory `192.168.1.1` |
| `raythink-config-ui/` | Raythink **thermal camera** | 8005 | factory `192.168.1.123`, RPC2 API |
| `speaker-config-ui/` | Provision-ISR **IP speaker** | 8006 | arrives on **DHCP** — auto-scans `192.168.1/2/88.0/24` |

The ports are fixed and don't collide, so all the tools run side by side under
the one launcher.

## For engineers

- **`bench-core/`** is the shared local package (`bench_core`). It holds the
  bench-UI base (`bench_core.bench_ui.BenchConfigurator` — the FastAPI shell,
  detection-loop wrapper, WebSocket state feed, step logging), the Teltonika
  RutOS device client, and the small shared helpers (`load_settings`,
  `make_step_runner`, `tcp_port_open`, the logging context, `format_verification`).
  Every tool installs it editable (`-e ./bench-core[ui]`), which is why the tool
  folders must stay siblings inside `bench/`. The Raythink camera and
  Provision-ISR speaker tools reuse the same bench-UI base but ship their own
  device clients (`raythink_camera.py`, a Dahua-OEM RPC2 JSON client, and
  `speaker_client.py`, a session-cookie CGI client) instead of the Teltonika
  one — proof the base is protocol-agnostic.
- **One shared `.venv`** lives at the `bench/` root and is used by all the
  tools. The launchers (`Start Bench Tools.bat` / `start-bench.sh`) create it on
  first run, `git pull` (unless `BENCH_NO_PULL=1`), and re-sync the single
  `requirements.txt` every run (a near-instant no-op once installed). Only the
  dashboard opens a browser tab; the tools don't auto-open their own (set
  `BENCH_OPEN_BROWSER=1` to opt a single tool back in). On macOS/Linux the tools
  run as background jobs in the launcher's terminal window, and Ctrl+C stops them all.
- **Config** lives per tool under `<tool>/config/` (e.g.
  `otd-config-ui/config/site.config.json`, `raythink-config-ui/config/profiles/`).
  Copy the committed `*.example.*` template alongside it and fill in real values.
- **Run records:** every tool's history entry / per-run JSON follows ONE
  canonical schema, `bench-run-record/1`, defined and documented in
  `bench-core/src/bench_core/run_record.py`. The common core (identity,
  status, verification, steps, timing) is identical across tools; per-family
  fields (site name, channel, profile, radar IP, ...) live under the entry's
  `device` block. All `build_entry` hooks emit it via `build_run_entry()`, and
  consumers (central reporting, label printing) read any record — including
  pre-schema JSONs from old benches — through `parse_run_record()`.
- **Run provenance:** every per-run JSON (and history entry) is stamped with
  `operator`, `station_id` and `bench_version`. The operator name is entered in
  the header of any tool page (badge scan or typed, once at day start) and is
  shared station-wide via `bench/.bench-operator.json` (gitignored). The
  station ID defaults to the machine hostname; set `BENCH_STATION_ID` before
  launching to override it. `bench_version` is the short git revision exported
  by the launcher as `BENCH_VERSION`.
- **Tests** live per tool under `<tool>/tests/` and run with no hardware. From
  the `bench/` root: `.venv/bin/python -m pytest` runs every tool's suite.
- Secrets stay local: real `*.config.json`, exported `profiles/`, and `firmware/`
  are gitignored; only the `*.example.*` templates are committed.
