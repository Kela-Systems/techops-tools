# Bench provisioning tools

Everything that runs on the **Windows bench PC** to provision devices, one
plug-in at a time. All four tools share the same workflow — *plug a device in →
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
2. Start everything with one double-click:
   - **Windows:** `START HERE.bat`
   - **macOS:** `start-bench.command` (it opens in Terminal)

   It launches all four tools and opens the dashboard in your browser.
3. On the dashboard, click the tool for whatever you're plugging in. A green dot
   means that tool is up; grey means it's still starting (first run installs
   dependencies — give it a moment) or stopped.

```
double-click launcher  →  dashboard  ┌──────────────────────────────────────────┐
                                     │  ● Magos Radar       :8001   192.168.40.x │
                                     │  ● Magos APU         :8002   192.168.40.x │
                                     │  ● Teltonika OTD500  :8003   192.168.1.x  │
                                     │  ● Teltonika RUTM08  :8004   192.168.1.x  │
                                     └──────────────────────────────────────────┘
```

To run just one tool, open its folder and double-click its own launcher —
`run_*.bat` on Windows or `run_*.command` on macOS. To stop a tool, close its
console window (Windows) or press Ctrl+C / close the Terminal tab (macOS).

> **macOS first-launch:** if Gatekeeper blocks a `.command` ("unidentified
> developer"), right-click it → **Open** once, or run `chmod +x *.command` in the
> `bench/` folder if the executable bit was lost in transit.

> **Network adapter:** each tool talks to a device on a specific subnet — the
> dashboard card and the tool's own page tell you which. If a device is plugged
> in but never detected, the adapter is almost always on the wrong subnet.

## The tools

| Folder | Device | Port | Launcher (`.bat` = Windows, `.command` = macOS) | Notes |
| --- | --- | --- | --- | --- |
| `magos-config-ui/` | Magos AR-300 **radar** | 8001 | `run_radar` | factory subnet `192.168.40.x` |
| `magos-config-ui/` | Magos **APU** | 8002 | `run_apu` | factory IP `192.168.40.60` |
| `otd-config-ui/` | Teltonika **OTD500** | 8003 | `run_otd` | manifest-driven, factory `192.168.1.1` |
| `rutm-config-ui/` | Teltonika **RUTM08** | 8004 | `run_rutm` | no manifest, factory `192.168.1.1` |

Every launcher ships in two forms — `run_*.bat` (Windows) and `run_*.command`
(macOS) — that do the same thing. The ports are fixed and don't collide, so all
four can run side by side.

## For engineers

- **`bench-core/`** is the shared local package (`bench_core`). It holds the
  bench-UI base (`bench_core.bench_ui.BenchConfigurator` — the FastAPI shell,
  detection-loop wrapper, WebSocket state feed, step logging) and the Teltonika
  RutOS device client (`bench_core` top level). The OTD and RUTM apps install it
  editable (`-e ../bench-core[ui]`), which is why the tool folders must stay
  siblings inside `bench/`.
- Each tool's launcher (`run_*.bat` / `run_*.command`) creates a per-tool `.venv`
  on first run and re-syncs `requirements.txt` every run (a near-instant no-op
  once installed).
- The master launchers (`START HERE.bat` / `start-bench.command`) set
  `BENCH_NO_BROWSER=1` before launching the tools so only the dashboard opens a
  tab; running a single tool's launcher directly still opens its own tab. On
  macOS the four tools run as background jobs in the launcher's Terminal window,
  and Ctrl+C there stops them all.
- Tests: the Magos apps ship `pytest` state-machine tests
  (`magos-config-ui/test_*.py`) that run with no hardware.
- Secrets stay local: real `*.config.json` / `manifest.csv` / `firmware/` are
  gitignored; only the `*.example.*` templates are committed.
