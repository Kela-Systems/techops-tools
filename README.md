# techops-tools

A grab-bag of scripts and small tools I use during Kela deployments and day-to-day ops. Most are thin wrappers over `gdal`, `kubectl`, `psql`, and the GitHub CLI. Pull out whatever's useful.

## Layout

```
techops-tools/
├── raster_to_gpkg.py              Convert TIFFs in a folder into an InspireCRS84Quad GeoPackage
├── batch_crop.sh                  Crop rasters for many sites in one run (calls crop_raster.sh)
├── extract_map_features.sh        Dump the map_features table from a Kela HUB cluster to CSV
├── fs_monitor/                    Monit-driven cleanup of old recordings (with Slack alerting)
├── deployments_scripts/
│   ├── add_inventory.sh           Generate + commit + PR a new on-prem Ansible inventory
│   ├── align_dtm_to_ortho.sh      Reproject a DTM to match an orthophoto's extent + CRS
│   ├── crop_raster.sh             Crop ortho + DTM around a lat/lon, optionally push to S3
│   ├── migrate_map_features.sh    Move map_features between old Platform and Kela HUB
│   └── pg_query.sh                Run psql against a postgres pod via kubectl exec
├── magos-config-ui/               Magos AR-300 provisioning: radar + APU CLIs and two FastAPI web UIs
├── otd-config-ui/                 Teltonika OTD500 batch provisioning: CLI + FastAPI bench UI + RMS register
├── hub-admin/                     Small CLI for hub device/integration management (Python package)
└── deploy-tracker/                Flask app that tracks deployment progress against Google Sheets
```

## Conventions

- Every shell script supports `--help`. Start there.
- Anything site- or environment-specific is overridable via env vars or CLI flags. The defaults match Kela's typical setup (`kela` namespace, `postgres-0` / `postgresql-0` pods, `MapEditor-…` AWS profile, etc.).
- Real secrets / device configs are **never committed**. The repo only ships `*.example.*` templates; copy them to the non-`.example.` filename and fill in real values locally. See [Secrets & local config](#secrets--local-config) below.

## Prerequisites

Depends on which tool you're running. The common set:

- `bash` 4+, `python3` 3.11+, `curl`, `jq`
- `gdal` (`gdalwarp`, `gdalbuildvrt`, `gdaladdo`, `gdalinfo`, `gdal_edit.py`) — for the raster scripts
- `kubectl` with contexts already set up — for anything that talks to a cluster
- `gh` CLI + `git` — for `add_inventory.sh`
- `aws` CLI — for `crop_raster.sh --upload`

Python packages (only needed where noted):

- `migrate_map_features.sh` needs `pyyaml` (`pip install pyyaml`)
- `hub-admin/` — `pip install -e ./hub-admin` (pulls in `typer`, `rich`, `grpcio`, etc.)
- `deploy-tracker/` — see `deploy-tracker/requirements.txt`

## Tools

### `raster_to_gpkg.py`

Builds a VRT mosaic from a folder of TIFFs, warps it into a GeoPackage in the InspireCRS84Quad tiling scheme, and adds overviews. Runs `caffeinate` under macOS to keep the box awake on long warps.

```bash
python3 raster_to_gpkg.py -i /path/to/tiffs -o out.gpkg --zoom 16
```

### `batch_crop.sh`

For each site name in a file, hits `https://<site>/config-service/system-settings/settings/default`, pulls the default map location, and calls `crop_raster.sh` for it. Useful for re-cropping many customer maps after a base raster update.

```bash
./batch_crop.sh sites.txt 5000 ./outputs
```

### `extract_map_features.sh`

Exports rows from `map_features` in a hub postgres pod to a local CSV. Supports `--where`, `--columns`, `--count-only`, and `--no-header`.

```bash
./deployments_scripts/extract_map_features.sh \
  --context prod-cluster \
  --where "classification = 'Alarm'" \
  -o alarms.csv
```

### `fs_monitor/`

A monit configlet (`fs_monitor`) plus the cleanup script (`fs_cleanup.sh`) and Slack alert (`alert.sh`) it triggers when disk usage crosses a threshold. Drop them into a recording-host's `/etc/monit/` and `/usr/local/bin/`.

`alert.sh` requires `SLACK_WEBHOOK_URL` in the environment — set it in `/etc/default/fs_monitor` or your monit env block, don't commit it.

### `deployments_scripts/add_inventory.sh`

Generates an Ansible inventory file for a new on-prem customer in `$KELA_REPO/deployment/ansible/inventory/onprem/prod/`, commits it on a new branch, pushes, and opens a PR via `gh`.

```bash
KELA_REPO=~/dev/kela \
  ./deployments_scripts/add_inventory.sh \
    --name acme-corp \
    --tags "tag:configuration-management,tag:acme"
```

### `deployments_scripts/align_dtm_to_ortho.sh`

Reproject and crop a DTM so it covers the exact same bbox + CRS as a reference orthophoto, while keeping the DTM's native pixel size.

```bash
./deployments_scripts/align_dtm_to_ortho.sh \
  -o site/orthophoto.tif \
  -d /path/to/dtm_hae.tif \
  -t site/dtm.tif
```

### `deployments_scripts/crop_raster.sh`

Crop the canonical orthophoto + DTM to a square around a lat/lon, optionally drop the outputs into a site folder built from a template and push to S3. Defaults assume your source rasters live under `$CHATAL_MAPS_DIR` (default `~/Documents/chatal-maps`) and your site templates under `$GIS_DATA_DIR` (default `~/Documents/kela-gis-data`); override either via env vars.

```bash
./deployments_scripts/crop_raster.sh -c 32.869847,35.698983 -r 1000
./deployments_scripts/crop_raster.sh -i --site acme-site-01 --upload
```

### `deployments_scripts/migrate_map_features.sh`

Move `map_features` rows between an old Platform cluster and a new Kela HUB cluster, transforming the schema (nested JSON → flat columns), and pull in alarm zones from a magos pod's `zones.yaml`. Has `--dry-run`.

```bash
./deployments_scripts/migrate_map_features.sh \
  --source-ctx prod-old --dest-ctx prod-new --dry-run
```

### `deployments_scripts/pg_query.sh`

Run a one-off SQL query (inline, from a file, or interactive psql) against the postgres pod in a cluster. Useful when you don't want to remember the full `kubectl exec` incantation.

```bash
./deployments_scripts/pg_query.sh --context prod --db c2-db -- "SELECT count(*) FROM map_features;"
./deployments_scripts/pg_query.sh --context prod --db c2-db --interactive
```

### `magos-config-ui/`

Everything for provisioning Magos AR-300 radars lives here — both the CLI and the web UI share one device-talking module (`magos_configure.py`).

**CLI (`magos_configure.py`)** — configures a fresh radar over its dashboard HTTP API: logs in, sets a manual NTP server, and switches it from the factory IP to a static one (IP / gateway / DNS). Factory defaults are baked in (`192.168.40.50`, `admin:password`, gateway/DNS `192.168.88.1`, mask `255.255.255.0`, NTP `192.168.88.10`), so the common case is a one-liner. `--interactive` asks which channel the radar is (0&rarr;`.50`, 1&rarr;`.51`, 2&rarr;`.52`, 3&rarr;`.53`, or a manual IP).

```bash
cd magos-config-ui
python3 magos_configure.py --interactive
# or fully explicit:
python3 magos_configure.py --ip 192.168.88.51 --yes
```

> Changing the IP drops the connection you're talking over — that's expected. Afterwards the radar lives on its new address.

**Web UI (`app.py`)** — a small FastAPI app around the same client for **bulk-provisioning radars one after another**. It polls the factory IPs (`192.168.40.50` and `192.168.40.60` by default — editable in Settings), shows when a radar is plugged in and which IP it answered on, lets you pick the channel, configures it, then loops back to "waiting" for the next one — the app never shuts down between radars. Shared settings (login, NTP, gateway, DNS, mask) are entered once; only the channel changes per radar, and the selection auto-advances after each success.

- **Auto mode:** flip the *Auto-configure on detect* switch with a channel (or manual IP) armed, and each radar that gets plugged in is configured automatically — no clicking. Plug, wait for green, unplug, repeat.
- **Identity + logging:** before configuring, it reads the radar's **model / serial / MAC** and shows them in the UI and history. Every step is timestamped and saved under `magos-config-ui/logs/` — a structured JSON per radar (keyed by serial, including the raw API payloads) plus a rolling `magos-config.log`.

```bash
cd magos-config-ui
pip install -r requirements.txt
python app.py            # http://localhost:8001
```

Your laptop must be on the radar's factory subnet (`192.168.40.x`) to reach it. Live state is pushed over a websocket and a per-session history of configured radars is shown in the UI.

**On a standalone Windows machine:** install [Python 3.11+](https://www.python.org/downloads/) (tick *"Add python.exe to PATH"*), copy the `magos-config-ui/` folder over, and double-click **`run.bat`**. The first run creates a local `.venv` and installs the dependencies (needs internet that once); every run after just launches the app and opens the browser. The PC's network adapter still has to be on the `192.168.40.x` subnet to reach a radar.

#### APU (the AR-300's processing unit)

The same folder also provisions the **APU** (AR Processing Unit), which has a different dashboard API and a fixed factory IP of `192.168.40.60`. It runs **independently** of the radar tool — different CLI, different web app, different port — so you can run both at once.

**CLI (`apu_configure.py`)** — logs in, sets NTP + timezone, points the APU at the radar it controls (`phoenix_ip`), and switches its per-interface networking from the factory IP to a static one. Channels mirror the radar: channel `N` &rarr; APU `192.168.88.6N` controlling radar `192.168.88.5N`. Defaults match a fresh APU (`192.168.40.60`, `admin:password`, gateway/DNS `192.168.88.1`, mask `255.255.255.0`, NTP `192.168.88.10`, timezone `Asia/Jerusalem`).

```bash
cd magos-config-ui
python3 apu_configure.py --interactive
# or fully explicit:
python3 apu_configure.py --ip 192.168.88.61 --radar-ip 192.168.88.51 --yes
```

**Web UI (`apu_app.py`)** — the iterative, auto-mode, identity + logging workflow as the radar UI, but for APUs. It watches `192.168.40.60`, and each channel sets both the APU's own IP **and** the controlled-radar IP automatically. Logs land in `magos-config-ui/logs/` (one JSON per APU plus a rolling `apu-config.log`).

- **Cycle mode:** since APUs are deployed in groups of 4, flip *Cycle mode* and just plug them in one at a time — the 1st becomes channel 0, the 2nd channel 1, … wrapping back to 0 after the 4th. No clicking and no per-unit target selection; the UI shows which channel the next APU will get.

```bash
cd magos-config-ui
pip install -r requirements.txt
python apu_app.py        # http://127.0.0.1:8002
```

On Windows, double-click **`run_apu.bat`** (same first-run venv setup as `run.bat`). Because it's on port `8002`, the radar tool (`run.bat`, port `8001`) and the APU tool can run side by side.

### `otd-config-ui/`

Batch-provisions **Teltonika OTD500** (RutOS) routers. Same iterative bench
workflow as the magos tools, but driven by a **manifest CSV** so deployment is
hands-free: plug a device into the laptop (it boots on `192.168.1.1`), the tool
reads its **LAN MAC over ARP** (no login needed), matches the manifest row, logs
in with that row's unique factory **label password**, and runs the full
pipeline. Devices are done one at a time (all share `192.168.1.1`), but with zero
clicks per device.

Pipeline per device:

```
login(label_pw) → set password "Kelasys123!" → firmware (latest-stable)
  → name/hostname otd-<site> → timezone Asia/Jerusalem → all SIMs "4G only"
  → enable + connect RMS → join Tailscale → [optional] load eSIM profile
  → verify (re-read every setting off the device and report PASS/FAIL/skip)
```

**Verification:** as a final step the tool re-queries the device for each thing
it set (hostname, timezone, per-SIM service, RMS `enable`, Tailscale `100.x` IP,
firmware) and prints a `── Verification ──` table. A run is only reported `ok`
when no step failed **and** every in-scope check passes; the CLI exits non-zero
and the Web UI shows the table (and the per-check PASS/FAIL badges) otherwise.

**Transport:** REST API (`https://192.168.1.1/api`, firmware ≥ 07.06) for auth /
identity / firmware, and **SSH + UCI** for the config settings (uniform across
firmware). Both use the same credentials, so once the password is changed
everything switches over automatically. Firmware-dependent UCI paths are pulled
out as constants at the top of `teltonika_configure.py` — verify the marked `(*)`
ones against your units via the [Teltonika dev portal](https://developers.teltonika-networks.com/).

**Why first-boot must be local:** every OTD500 ships behind a *unique* label
password and forces a change on first login, so RMS zero-touch can't reach it
until something local logs in and sets the shared password. The bench tool does
that; RMS then takes over for fleet-scale management.

**CLI (`teltonika_configure.py`)** — provision a single device:

```bash
cd otd-config-ui
python3 teltonika_configure.py --site haifa-port --label-password 'Xy7Kp2Lm9Qa'
python3 teltonika_configure.py --site eilat --label-password '...' --no-firmware
```

**Web UI (`otd_app.py`)** — the batch bench tool (port `8003`):

```bash
cd otd-config-ui
cp site.config.example.json site.config.json   # fill in password, RMS, Tailscale
cp manifest.example.csv manifest.csv           # mac, label_password, site_name, …
pip install -r requirements.txt
python otd_app.py        # http://127.0.0.1:8003
```

- **Manifest-driven, hands-free:** rows match by MAC; the UI shows a batch
  checklist (pending / done / failed) with a progress bar. Toggle **Auto** off to
  click *Configure* per device instead.
- **Identity verification:** after login it reads the device's real serial / IMEI
  / MAC and warns on any mismatch with the manifest row *before* writing config.
- **Tailscale at scale:** with `mint_per_device`, it mints a fresh ephemeral,
  pre-authorized, tagged auth key per device via the Tailscale API.
- **Firmware:** `local` (pin a `.bin` in `otd-config-ui/firmware/`), `fota`
  (device pulls latest-stable), or `rms` (defer — the registered device upgrades
  itself off the bench). Logs land in `otd-config-ui/logs/` (one JSON per device
  + a rolling `otd-config.log`).
- **Waits for mobile data:** FOTA, Tailscale and eSIM need the SIM to have a data
  connection, which can take a minute+ to attach. With `wait_for_internet` (on by
  default), the tool polls the modem — logging signal/registration — until it's
  online before those steps, up to `internet_timeout` seconds. Local-`.bin`
  firmware and enabling RMS need no internet (RMS connects itself later); if data
  never comes up, Tailscale's key is still stored in UCI so the device joins on
  its own once it's online, and eSIM is skipped with a warning.

On Windows, double-click **`run_otd.bat`** (same first-run venv setup as the
other tools; port `8003`, so all three can run side by side).

**RMS — two halves.** Enabling RMS on the device (UCI `enable=1`) only makes it
*dial out*; the unit only appears/connects in your account once it's **registered
there by serial + MAC**. If `rms.api_token` + `rms.company_id` are set in
`site.config.json`, the bench run now does this automatically as a host-side API
call (works even before the SIM has data — the device connects on its own once
online). Registration is idempotent (an "already exists" response counts as OK).

**RMS register (`rms_register.py`)** — the same registration as a standalone
batch step: bulk-register the whole manifest up front (and optionally attach a
pending firmware upgrade) so devices self-upgrade the moment they connect.

```bash
python3 rms_register.py                 # dry-run from manifest.csv
python3 rms_register.py --apply         # create the devices in RMS
```

> The PC's adapter must be on the `192.168.1.x` subnet to reach a device. Real
> `manifest.csv` (holds label passwords), `site.config.json`, and the `firmware/`
> folder are gitignored — commit only the `*.example.*` templates.

### `hub-admin/`

Python package that wraps the hub gRPC API for adding/removing radars and ONVIF cameras from a hub. Install with `pip install -e ./hub-admin` (the `hub-admin` script then ends up on your `$PATH`).

Expects a `hub-admin.yaml` next to a `device_config.json` describing the devices; see `hub-admin.example.yaml` and `hub-admin/device_config.example.json`. The real configs are gitignored. Every command takes `--context` (the kubectl context to port-forward into).

> Note: depends on internal `hub_client` and `proto_py` packages — those are installed separately in your dev venv.

**Site profiles (`profile` subcommand)** — capture a whole site's setup as one portable bundle and redeploy it. A *profile* contains every integration (its manifest, `integration_config`, and devices) plus the hub's `SiteConfig` (entity links etc.). This is the fast path for "stand up site B exactly like site A".

```bash
# 1. Export everything from a reference site
hub-admin profile export --context kela-cuas-06 -o cuas-06.profile.json

# 2. Inspect it offline (no hub connection)
hub-admin profile show cuas-06.profile.json

# 3. Deploy it onto another context
hub-admin profile apply cuas-06.profile.json --context kela-cuas-07 --restart
```

- **Portable across hubs:** on apply, each manifest is matched by *name* on the destination (falling back to the stored manifest ID), so differing per-hub manifest IDs don't matter.
- **Schema-safe devices:** device `setup_info` is filtered to the destination manifest's schema (same transform as `devices add`), so a profile replays even when manifests differ slightly.
- **Site settings:** `--site-config` (default on) fully replaces the destination `SiteConfig` and needs a hub restart (`--restart`, or `hub-admin server restart`). Entity links reference site-specific asset IDs — pass `--no-site-config` when deploying to a genuinely different site.
- Exported `*.profile.json` files can hold device IPs/creds, so they're gitignored; commit only `*.profile.example.json` (see `hub-admin/site.profile.example.json`).

### `deploy-tracker/`

Tiny Flask app that reads YAML deployment manifests, tracks per-step progress against a Google Sheet, and renders an HTML dashboard. Useful when multiple installers are working a site in parallel.

```bash
cd deploy-tracker
pip install -r requirements.txt
python web.py            # http://localhost:5000
```

Needs `service_account.json` (a Google service account with editor access to the target sheet) and a `config.yaml` pointing at the sheet ID. Both are gitignored — start from the comments in `deploy_tracker.py`.

## Secrets & local config

Files that are **not** in the repo and need to be created locally before things work:

| File | What goes in it |
| --- | --- |
| `otd-config-ui/manifest.csv` | Per-device MAC / label password / site — see `manifest.example.csv` |
| `otd-config-ui/site.config.json` | Shared password, RMS token, Tailscale key — see `site.config.example.json` |
| `cam-config-ui/manifest.csv` | Per-camera MAC / serial / name / target IP — see `manifest.example.csv` |
| `cam-config-ui/site.config.json` | Shared camera creds + gateway/DNS/NTP/timezone — see `site.config.example.json` |
| `hub-admin/device_config.json` | Real device IPs / creds — see the `*.example.json` |
| `hub-admin/hub-admin.yaml` | Real defaults — see the `*.example.yaml` |
| `deploy-tracker/service_account.json` | Google service account key (download from GCP IAM) |
| `deploy-tracker/config.yaml` | Sheet ID + path to the service account file |
| `deploy-tracker/deployments/*.yaml` | Per-site deployment manifests (one already shipped: `example.yaml`) |
| `$SLACK_WEBHOOK_URL` (env var) | Webhook for `fs_monitor/alert.sh` |

If you're rotating any of these, do it in the actual source of truth (GCP IAM, Slack, hub UI) — not in git.
