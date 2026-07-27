# techops-tools

A grab-bag of scripts and small tools used during Kela deployments and day-to-day
ops. They fall into two groups: the **bench provisioning tools** (web UIs that
run on the Windows bench PC to configure devices) and a set of **ops/deployment
scripts** (thin wrappers over `gdal`, `kubectl`, `psql`, `gh`, vendor APIs).
Pull out whatever's useful.

## Layout

```
techops-tools/
├── bench/                         ← the Windows bench PC tools (see bench/README.md)
│   ├── Start Bench Tools.bat      Launch all the configurators + open the dashboard
│   ├── launcher/                  The dashboard landing page
│   ├── bench-core/                Shared package: bench-UI base + Teltonika device client
│   ├── magos-config-ui/           Magos AR-300 radar (:8001) + APU (:8002)
│   ├── otd-config-ui/             Teltonika OTD500 (:8003), ad-hoc + optional batch
│   ├── rutm-config-ui/            Teltonika RUTM08 (:8004), no manifest
│   ├── raythink-config-ui/        Raythink thermal camera (:8005)
│   └── speaker-config-ui/         Provision-ISR IP speaker (:8006), DHCP auto-scan
│
├── bench-central/                 Run-record collector (FastAPI + SQLite) on the tailnet host — the
│                                  durable audit trail the bench stations upload to (see its README.md)
├── raster_to_gpkg.py              Convert TIFFs in a folder into an InspireCRS84Quad GeoPackage
├── deployments_scripts/
│   ├── add_inventory.sh           Generate + commit + PR a new on-prem Ansible inventory
│   ├── align_dtm_to_ortho.sh      Reproject a DTM to match an orthophoto's extent + CRS
│   ├── batch_crop.sh              Crop rasters for many sites in one run (calls crop_raster.sh)
│   ├── crop_raster.sh             Crop ortho + DTM around a lat/lon, optionally push to S3
│   ├── crop_raster_bbox.sh        Crop ortho + DTM to an explicit bounding box
│   ├── extract_map_features.sh    Dump the map_features table from a Kela HUB cluster to CSV
│   ├── migrate_map_features.sh    Move map_features between old Platform and Kela HUB
│   └── pg_query.sh                Run psql against a postgres pod via kubectl exec
├── hub-admin/                     CLI for hub device/integration management (Python package)
├── deploy-tracker/                Flask app that tracks deployment progress against Google Sheets
├── scripts/                       Misc ops: PagerDuty session timeouts, SIM audit
└── rugged_operator/               Build runbook + USB image for the Ubuntu operator machine
```

## Conventions

- Every shell script supports `--help`. Start there.
- Anything site- or environment-specific is overridable via env vars or CLI flags.
  The defaults match Kela's typical setup (`kela` namespace, `postgres-0` /
  `postgresql-0` pods, `MapEditor-…` AWS profile, etc.).
- Real secrets / device configs are **never committed**. The repo only ships
  `*.example.*` templates; copy them to the non-`.example.` filename and fill in
  real values locally. See [Secrets & local config](#secrets--local-config).

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
- `scripts/` — see `scripts/requirements.txt`
- the bench tools manage their own `.venv` per folder (see `bench/README.md`)

## The bench tools

The radar / APU / OTD500 / RUTM08 / Raythink-camera / ISR-speaker configurators
that run on the bench PC live under [`bench/`](bench/), with their own operator
guide in [`bench/README.md`](bench/README.md). Short version: copy the `bench/`
folder to the bench machine and run the master launcher —
**`bench/Start Bench Tools.bat`** on Windows or **`bench/start-bench.sh`** on
macOS/Linux. It starts all six (ports 8001–8006, no collisions) and opens one
dashboard that links to each.

## Ops & deployment scripts

### `raster_to_gpkg.py`

Builds a VRT mosaic from a folder of TIFFs, warps it into a GeoPackage in the
InspireCRS84Quad tiling scheme, and adds overviews. Runs `caffeinate` under
macOS to keep the box awake on long warps.

```bash
python3 raster_to_gpkg.py -i /path/to/tiffs -o out.gpkg --zoom 16
```

### `deployments_scripts/`

- **`add_inventory.sh`** — generate an Ansible inventory for a new on-prem
  customer, commit it on a new branch, push, and open a PR via `gh`.

  ```bash
  KELA_REPO=~/dev/kela ./deployments_scripts/add_inventory.sh \
    --name acme-corp --tags "tag:configuration-management,tag:acme"
  ```

- **`align_dtm_to_ortho.sh`** — reproject + crop a DTM to cover the exact bbox +
  CRS of a reference orthophoto, keeping the DTM's native pixel size.

  ```bash
  ./deployments_scripts/align_dtm_to_ortho.sh -o site/orthophoto.tif -d dtm_hae.tif -t site/dtm.tif
  ```

- **`crop_raster.sh`** / **`crop_raster_bbox.sh`** — crop the canonical ortho +
  DTM to a square around a lat/lon (or an explicit bbox), optionally dropping the
  outputs into a templated site folder and pushing to S3. Source rasters default
  to `$CHATAL_MAPS_DIR` (`~/Documents/chatal-maps`), site templates to
  `$GIS_DATA_DIR` (`~/Documents/kela-gis-data`).

  ```bash
  ./deployments_scripts/crop_raster.sh -c 32.869847,35.698983 -r 1000
  ./deployments_scripts/crop_raster.sh -i --site acme-site-01 --upload
  ```

- **`batch_crop.sh`** — for each site name in a file, fetch its default map
  location from `https://<site>/config-service/...` and call `crop_raster.sh`.

  ```bash
  ./deployments_scripts/batch_crop.sh sites.txt 5000 ./outputs
  ```

- **`extract_map_features.sh`** — export rows from `map_features` in a hub
  postgres pod to a local CSV (`--where`, `--columns`, `--count-only`, `--no-header`).

  ```bash
  ./deployments_scripts/extract_map_features.sh --context prod-cluster \
    --where "classification = 'Alarm'" -o alarms.csv
  ```

- **`migrate_map_features.sh`** — move `map_features` between an old Platform
  cluster and a new Kela HUB, transforming the schema and pulling in alarm zones
  from a magos pod's `zones.yaml`. Has `--dry-run`.

  ```bash
  ./deployments_scripts/migrate_map_features.sh --source-ctx prod-old --dest-ctx prod-new --dry-run
  ```

- **`pg_query.sh`** — run a one-off SQL query (inline, file, or interactive
  psql) against the postgres pod in a cluster.

  ```bash
  ./deployments_scripts/pg_query.sh --context prod --db c2-db -- "SELECT count(*) FROM map_features;"
  ```

### `hub-admin/`

Python package wrapping the hub gRPC API for adding/removing radars and ONVIF
cameras, and capturing/redeploying a whole site as a portable `*.profile.json`
bundle. Install with `pip install -e ./hub-admin`; every command takes
`--context` (the kubectl context to port-forward into).

```bash
hub-admin profile export --context kela-cuas-06 -o cuas-06.profile.json
hub-admin profile apply  cuas-06.profile.json --context kela-cuas-07 --restart
```

Expects a `hub-admin.yaml` + `device_config.json` (see the `*.example.*`); both
are gitignored. Depends on internal `hub_client` and `proto_py` packages,
installed separately in your dev venv.

### `deploy-tracker/`

Tiny Flask app that reads YAML deployment manifests, tracks per-step progress
against a Google Sheet, and renders an HTML dashboard — useful when multiple
installers work a site in parallel.

```bash
cd deploy-tracker
pip install -r requirements.txt
python web.py            # http://localhost:5000
```

Needs `service_account.json` (a Google service account with editor access to the
sheet) and a `config.yaml` pointing at the sheet ID. Both are gitignored.

### `scripts/`

- **`pd_session_timeouts.py`** — view/set PagerDuty account session timeouts
  (API-only; no UI). Needs a PagerDuty API token (see `scripts/.env.example`).
- **`sim_audit.py`** — audit SIM/data usage. See `scripts/requirements.txt`.

### `rugged_operator/`

Build runbook (`setup.md` / `setup.sh`) and a turnkey install-USB builder
(`usb-builder/`) for the Ubuntu 24.04 operator workstation (Dell Latitude 5420
Rugged). Not a bench tool — it builds the customer-facing operator machine.

## Secrets & local config

Files that are **not** in the repo and must be created locally before things work:

| File | What goes in it |
| --- | --- |
| `bench/otd-config-ui/manifest.csv` | Optional Batch-mode device list (site / MAC / label password) — built in the UI or from `manifest.example.csv` |
| `bench/otd-config-ui/site.config.json` | Shared password, RMS token, Tailscale key — see `site.config.example.json` |
| `bench/rutm-config-ui/rutm.config.json` | RMS token, Tailscale key, firmware mode — see `rutm.config.example.json` |
| `bench/raythink-config-ui/config/raythink.config.json` | Camera passwords, NTP, static-IP range — see `raythink.config.example.json` |
| `bench/speaker-config-ui/config/speaker.config.json` | Speaker passwords, NTP, static IP, scan subnets — see `speaker.config.example.json` |
| `bench/speaker-config-ui/config/media/` | The announcement audio file (`.mp3`/`.wav`) the speaker tool uploads — referenced by `media_file` in the config |
| `hub-admin/device_config.json` | Real device IPs / creds — see the `*.example.json` |
| `hub-admin/hub-admin.yaml` | Real defaults — see the `*.example.yaml` |
| `deploy-tracker/service_account.json` | Google service account key (download from GCP IAM) |
| `deploy-tracker/config.yaml` | Sheet ID + path to the service account file |
| `deploy-tracker/deployments/*.yaml` | Per-site deployment manifests (one ships: `example.yaml`) |
| `scripts/.env` | PagerDuty / vendor API tokens — see `scripts/.env.example` |

If you're rotating any of these, do it in the actual source of truth (GCP IAM,
Slack, hub UI) — not in git.
