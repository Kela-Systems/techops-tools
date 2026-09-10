# Bench provisioning tools

Everything that runs on the **Windows bench PC** to provision devices, one
plug-in at a time. All the tools share the same workflow — *plug a device in →
it's detected → configure → unplug → repeat* — and the same look, so learning
one teaches you all of them.

This `bench/` folder is the **only** thing that runs on the bench PC — and it
gets there via **bench-central**, not by hand: stations install and update
from the release pinned there (see "Station setup" below). The rest of
`techops-tools/` (cluster scripts, GIS tools, etc.) is not used here.

## Station setup (new bench PC)

The PC must reach bench-central (tailnet or LAN) — that's the only
prerequisite. Then, in a PowerShell window (Windows) or terminal (macOS/Linux):

```powershell
# Windows
irm http://techops-automations-host:8100/setup.ps1 -OutFile setup.ps1
.\setup.ps1 -CentralUrl http://techops-automations-host:8100 -StationId bench-3
```

```bash
# macOS / Linux
curl -fsS http://techops-automations-host:8100/setup.sh -o setup.sh
bash setup.sh --central-url http://techops-automations-host:8100 --station-id bench-3
```

The installer finds/installs Python, downloads the pinned bench release,
writes the station identity (`.bench-station.json`), seeds each tool's config
from its committed example template, builds the shared `.venv`, drops a
desktop shortcut (Windows), verifies, and checks the station in — it appears
on bench-central's **Fleet** page immediately. Pass `-StationId bench-3` /
`--station-id bench-3` to name the station (or set `BENCH_STATION_ID` in the
environment before running; default: the hostname).

**Existing machine missing its station identity?** (installed by hand before
the fleet installer existed, or `.bench-station.json` is absent/unreadable so
launches say "skipping central update"): just re-run the installer above on
that machine — it is idempotent over an existing install. It re-extracts the
pinned release, rewrites `.bench-station.json`, and leaves local state
(configs, `.venv`, logs, the operator file) alone. If the install doesn't
live in the default folder (`%USERPROFILE%\kela-bench` / `~/kela-bench`),
point at it with `-InstallDir C:\path` / `--dir /path`.

## For the operator

The tools run the same on **Windows** and **macOS** — only the launcher you
double-click differs.

1. Start everything with one launcher:
  - **Windows:** double-click **Kela Bench Tools** on the desktop
   (`Start Bench Tools.bat`)
  - **macOS/Linux:** run `./start-bench.sh` from a terminal in this folder
   It updates to the release pinned on bench-central, sets up the shared
   environment, launches all the tools, and opens the dashboard in your browser.
2. On the dashboard, click the tool for whatever you're plugging in. A green dot
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
                                     │  ● Teltonika TSW202  :8007   192.168.1.x  │
                                     │  ● PLANET IGS-4215   :8008   192.168.0.x  │
                                     └───────────────────────────────────────────┘
```

There is a single launcher that starts all the tools together (they run side by
side on fixed, non-colliding ports). To stop everything, close the launcher
window (Windows) or press Ctrl+C in the terminal (macOS/Linux).

> **Frozen bench:** every launch converges (best-effort) to the release pinned
> on bench-central. To freeze the version currently on disk (e.g. mid-session),
> set `BENCH_NO_PULL=1` before launching. An offline bench just launches
> what's on disk — an update is never required to work.

> **macOS/Linux first-launch:** if the script won't run, restore the executable
> bit with `chmod +x start-bench.sh` (it can be lost in transit). On macOS, if
> Gatekeeper complains, run it from the terminal rather than Finder.

> **Network adapter:** each tool talks to a device on a specific subnet — the
> dashboard card and the tool's own page tell you which. If a device is plugged
> in but never detected, the adapter is almost always on the wrong subnet.

## The tools


| Folder                | Device                       | Port | Notes                                                  |
| --------------------- | ---------------------------- | ---- | ------------------------------------------------------ |
| `magos-config-ui/`    | Magos AR-300 **radar**       | 8001 | factory subnet `192.168.40.x`                          |
| `magos-config-ui/`    | Magos **APU**                | 8002 | factory IP `192.168.40.60`                             |
| `otd-config-ui/`      | Teltonika **OTD500**         | 8003 | one device at a time, factory `192.168.1.1`; label scan |
| `rutm-config-ui/`     | Teltonika **RUTM08**         | 8004 | one device at a time, factory `192.168.1.1`; label scan |
| `raythink-config-ui/` | Raythink **thermal camera**  | 8005 | factory `192.168.1.123`, RPC2 API                      |
| `speaker-config-ui/`  | Provision-ISR **IP speaker** | 8006 | arrives on **DHCP** — auto-scans `192.168.1/2/88.0/24` |
| `tsw-config-ui/`      | Teltonika **TSW202** switch  | 8007 | factory `192.168.1.**2**`; label scan                  |
| `planet-config-ui/`   | PLANET **IGS-4215** PoE switch | 8008 | factory `192.168.**0**.100`; no scan; PoE plan + firmware floor |


The ports are fixed and don't collide, so all the tools run side by side under
the one launcher. The Teltonika subnet is shared without conflict: the OTD500 and
RUTM08 arrive on `192.168.1.1`, the TSW202 on `192.168.1.2`. The PLANET IGS-4215
is the one device that arrives on a *different* `/24` — `192.168.0.100` — so the
bench adapter needs an address on `192.168.0.x` as well to reach a fresh one.

> **One exception, if the RUTM08's `wan` block is switched on** (TEC-857): that
> step pins the router's WAN port to a static `192.168.1.2` — the same address a
> TSW202 arrives on. The two must not be on one bench segment during that step.
> The step also **ends the router's internet**, since the WAN port stops taking
> a lease and starts waiting for a gateway that only exists at the site, so it
> runs last, after FOTA, RMS registration and Tailscale. It ships disabled in
> the committed template; read the comments there before turning it on.

> **Devices that end up on `192.168.88.x`.** The RUTM08 (`.1`), TSW202 (`.2`),
> PLANET IGS-4215 (`.3`), camera and speaker (`.70`) all move to that subnet as
> their last step, and the
> tool confirms the move by reaching the device on its new address. The RUTM08
> case works anywhere because the router *serves* DHCP there and the tool renews
> the station's lease. **A switch, a camera and a speaker do not**, so the bench
> adapter has to carry an address on `192.168.88.x` itself (a second static IP,
> or a /16 over both `192.168.1.x` and `192.168.88.x`). Without it the device
> still moves correctly, but the run reports its final-address check as failed.

### Where a device ends up: the four address modes

The camera, speaker and switch pages each carry an **Address assignment** box
(TEC-848). Before it, each of them hardcoded one answer — the camera cycled an
octet, the speaker always landed on `.70`, the switch always on `.2` — and an
operator who needed something else re-addressed the device by hand afterwards,
which left no record anywhere.

| Mode | What each device gets |
|---|---|
| `fixed` | The same address every time. For a batch destined for sites that take a single device of that kind, which all want the same default configuration; two of them never meet on a live network. |
| `cycle` | The next address in a range, wrapping at the top — for kinds that land several to a site. The counter is burned only by a run that succeeds. |
| `manual` | The address the operator types for that one unit, as a last octet or a full address. |
| `dhcp` | Nothing. The device is left asking the site's DHCP server, and is found again by MAC to be checked. |

A tool declares which subset it offers, so the switch has no `cycle` (a site
takes one switch) and no mode appears that means nothing for the device in front
of the operator. The machinery is `bench_core.ip_mode` plus the shared
`mountIpModes()` widget — a tool opts in with `ip_modes_enabled = True` and an
`ip_mode_policy()`, and gets the picker, the `/api/ip-mode` route and the
persisted selection without writing any of them. The selection lives in
`ip-state.json` beside each tool, not in its config, because it is a switch an
operator flips during a shift and `config_hash` should not move when they do.

Every run records `device.ip` (what the bench assigned — empty under `dhcp`),
`device.ip_mode` and `device.reached_at` (where the device actually answered),
so a verify pass checks a unit against the address *it* was given rather than
the station default, and a QA label can print the real address instead of an
assumed one (TEC-352).

### Verify: checking a finished device without changing it

Every tool has a **Verify** button next to Configure. It runs the same
checks a normal run ends with — against a device that was already provisioned — and **mutates
nothing**: no password, no IP, no reboot, not even a `uci commit`. It needs nothing typed: the
tool recovers what this unit was supposed to be from its own configure record (this station's
`logs/`, then bench-central), so an operator can sweep a finished batch knowing nothing about any
individual unit.

Use it as the pre-ship gate at the end of a batch, or on any unit whose history is unclear. The
operator-facing procedure is section 9 of the [Operator guide](OPERATOR-GUIDE.md).

Three things make the result trustworthy rather than decorative:

- **A missing record fails.** If neither the station nor bench-central has a configure run for the
  unit, the pass emits a red `prior run` row instead of quietly dropping the checks that needed
  it. A verify pass that skips what it can't check is a green tick that means nothing.
- **Rows have to be able to fail.** Which row proves what — effect-based, read-back or a known gap
  — is written down per tool in [`docs/verification-rows.md`](docs/verification-rows.md), and is
  the first thing to read before adding or changing a check.
- **Mutation-freedom is tested, not intended.** `test_verify_only_is_mutation_free.py` runs the
  verify paths against a fake device and asserts nothing mutating was ever sent; the read-only
  client refuses one at runtime as a seatbelt.

Verify runs are stamped `kind: "verify"` in the run record and counted separately in the session
(their own pills, their own history badge), so a QA sweep never inflates the day's device count or
gets mistaken for a provisioning run centrally. On the CLI it is `--verify` on each tool's script.

### Scanning the label instead of typing the password

On the three Teltonika tools, the factory password can be read off the device's QR
label with a barcode scanner rather than retyped from the sticker. Plug in the
device, scan its label, and the password field fills itself — the site name (where
the tool asks for one) and Configure are unchanged.

The label also carries the device's MAC, so the tool checks it against the MAC it
read off the plugged-in device itself: **scanning the wrong box is refused rather
than provisioned.** The password stays on the server, never appears on the page,
and is never written into a run record.

One-time scanner setup (Zebra **DS2278** in its cradle), the self-test barcodes
that prove a station's keyboard layout isn't mangling passwords, and the
troubleshooting table are in
[`docs/scanner-ds2278.md`](docs/scanner-ds2278.md).

## For engineers

- `**bench-core/**` is the shared local package (`bench_core`). It holds the
bench-UI base (`bench_core.bench_ui.BenchConfigurator` — the FastAPI shell,
detection-loop wrapper, WebSocket state feed, step logging), the Teltonika
RutOS device client, and the small shared helpers (`load_settings`,
`make_step_runner`, `tcp_port_open`, the host-side network helpers —
DHCP renew, `arp_table`, `find_ip_by_mac` for locating a device that moved to
an address nothing here chose — the logging context, `format_verification`).
Every tool installs it editable (`-e ./bench-core[ui]`), which is why the tool
folders must stay siblings inside `bench/`. The Raythink camera and
Provision-ISR speaker tools reuse the same bench-UI base but ship their own
device clients (`raythink_camera.py`, a Dahua-OEM RPC2 JSON client, and
`speaker_client.py`, a session-cookie CGI client) instead of the Teltonika
one — proof the base is protocol-agnostic.
- **Layout:** the bench root holds only the two double-click entry points
(`Start Bench Tools.bat` / `start-bench.sh`), the docs and
`requirements.txt`; everything they delegate to — `bench-launch.bat`,
`_lib.sh`, `updater.py`, the `setup-station.*` installers — lives in
`scripts/`. The tools and `bench-core/` are siblings at the root. Reference
material that isn't a runbook lives in `docs/` (currently the scanner setup and
its committed self-test barcodes, verification-row notes, and
[`qa-labels.md`](docs/qa-labels.md)).
- **Scanner diagnostics** (not wired into the launcher, no hardware needed to
run the second one):
  - `scripts/scan-probe.html` — open it in a browser on any host and scan into
  it. Reports which browser event carried the characters, the inter-character
  timing, and a character-by-character diff against an expected payload. This is
  what you reach for when a station mangles passwords.
  - `scripts/scan-smoke.py` — drives the whole scan flow over real HTTP against
  all three Teltonika tools with simulated devices, including the deliberate
  wrong-device case. Run it after touching the scan path.
- **Updates are pushed from bench-central** (`bench-central/README.md`,
"Fleet"): an admin pins a release (any git ref, resolved to a SHA) and every
station converges to it at launch. The station half is `scripts/updater.py`
— run by the launchers before anything starts — which compares the local
`.bench-build.json` stamp with the pin, downloads the bundle (`git archive`
of `bench/`, built by bench-central) when they differ, extracts it over this
folder, and checks the station in. Stations need **no git and no GitHub
credentials**. It is fail-open end to end (offline → launch what's on disk),
never touches gitignored local state (configs, `.venv`, logs, the
`.bench-*.json` files — they're never in a bundle), and skips a folder that
is a git checkout, so an engineer's working copy never self-updates. One
trade-off: files *deleted* from the repo are not deleted on stations
(harmless orphans).
- **Station identity** lives in `.bench-station.json` (gitignored, written
once by the installer): `station_id` and `central_url`. The launchers export
`BENCH_STATION_ID` / `BENCH_CENTRAL_URL` from it, so nothing needs env vars
set by hand on a station; values already in the environment still win.
- **One shared `.venv`** lives at the `bench/` root and is used by all the
tools. The launchers (`Start Bench Tools.bat` / `start-bench.sh`) create it on
first run, update from bench-central (unless `BENCH_NO_PULL=1`), and re-sync
the single `requirements.txt` every run (a near-instant no-op once installed). Only the
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
`device` block. All `build_entry` hooks emit it via `build_run_entry()`,
which also mints a unique `run_id` and a full ISO UTC `timestamp` per run,
and every per-run JSON is written through one shared writer,
`bench_core.bench_ui.save_run_record()`. Consumers (central reporting,
  label printing) read any record — including pre-schema JSONs from old
benches — through `parse_run_record()`. Records carry `kind`
(`configure` / `verify`), defaulting to `configure` when absent so every
record written before the verify mode existed still reads correctly.
- **Verify mode (TEC-348, TEC-851)** is one shared path: `verify_supported`
and the `verify_pipeline` hook on `BenchConfigurator`, the `POST /api/verify`
route registered once in `build_app()`, the `verifying`/`verified` phases and
their separate counts, and the expected-value lookup in
`bench-core/src/bench_core/history.py` (operator override → this station's
`logs/` → bench-central → a failing `prior run` row). A tool opts in by
setting the flag and implementing the hook, which calls its `verify_*()`
function; those set `client.set_read_only()` right after the login so the
client itself refuses a write rather than trusting the pipeline to stay a
read. What each check actually proves is catalogued in
[`docs/verification-rows.md`](docs/verification-rows.md).
The Magos pair reaches the same behaviour from its own engine: `MagosBench` is a
full parallel implementation (own poll loop, auto/cycle state machine, settings
persistence), so TEC-851 mirrored the orchestration onto it rather than porting
the two tools. Everything that ships centrally *is* shared — the row schema, the
`/api/verify` body, the record schema, the expected-value lookup and all of
`bench.js` — and `tests/test_verify_ui.py` audits both bases' pages together so
they cannot drift.
- **Run provenance:** every per-run JSON (and history entry) is stamped with
`operator`, `station_id`, `bench_version` and `config_hash`. The operator
name is entered in the header of any tool page (badge scan or typed, once at
day start) and is shared station-wide via `bench/.bench-operator.json`
(gitignored). The station ID comes from `.bench-station.json` (falling back
to the machine hostname); `BENCH_STATION_ID` in the environment overrides
it. `bench_version` is the `.bench-build.json` bundle stamp (short git
revision for engineer checkouts), exported by the launcher as
`BENCH_VERSION`.
- **Config self-check + hash:** at startup (and on every config reload) each
tool validates its config — placeholder values (`xxxxx`, `example.com`, ...),
expired or soon-to-expire API tokens, and fields missing vs the committed
example template (`bench_core/src/bench_core/config_check.py`). Warnings show
in an amber banner on the tool page, in `/api/state` (`config_warnings`) and
in the launch console, so a mis-configured station announces itself at launch
instead of failing (or silently falling back) mid-run. `config_hash` — a
short fingerprint of the redacted config, also in `/api/state` — is stamped
into every run record, so config drift across stations is visible centrally.
- **Central shipping (opt-in per station):** with `BENCH_CENTRAL_URL` set —
the launcher exports it from `.bench-station.json`, or set it by hand to the
collector's base URL (e.g. `http://techops-automations-host:8100`, reachable
over Tailscale) before launching — every completed run record is also
queued under `<tool>/logs/outbox/` and uploaded in the background
(`bench_core/src/bench_core/central.py`). Offline benches just queue —
records upload when connectivity returns, with backoff, and a run never
blocks or fails because the network is down. Raw Magos device payloads stay
in the local per-run JSON only; they are never shipped. When the variable is
unset (the default), nothing is spooled and no uploader runs.
- **Factory passwords are kept (Teltonika only):** the OTD500, RUTM08 and
TSW202 tools ship each unit's factory-label password to bench-central, keyed
to its serial, so a device factory-reset in the field is still reachable
(it reverts to that password, and nobody used to have it). It rides its own
queue (`<tool>/logs/outbox-labels/`) into its own store, **not** the run
record — a device has one factory password however many times it is run, and
run records feed the read-only dashboard. Nothing is kept when the operator
leaves the password field empty, or types the station's shared password. Read
them back in the dashboard's **Factory passwords** panel; the whole design is
in `bench_core/src/bench_core/label_record.py`.
- **Tests** live per tool under `<tool>/tests/` (plus `tests/` at this root
for `scripts/updater.py`) and run with no hardware. From the `bench/` root:
`.venv/bin/python -m pytest` runs every suite.
- Secrets stay local: real `*.config.json`, exported `profiles/`, and `firmware/`
are gitignored; only the `*.example.*` templates are committed.

