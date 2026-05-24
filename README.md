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

### `hub-admin/`

Python package that wraps the hub gRPC API for adding/removing radars and ONVIF cameras from a hub. Install with `pip install -e ./hub-admin` (the `hub-admin` script then ends up on your `$PATH`).

Expects a `hub-admin.yaml` next to a `device_config.json` describing the devices; see `hub-admin.example.yaml` and `hub-admin/device_config.example.json`. The real configs are gitignored.

> Note: depends on internal `hub_client` and `proto_py` packages — those are installed separately in your dev venv.

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
| `hub-admin/device_config.json` | Real device IPs / creds — see the `*.example.json` |
| `hub-admin/hub-admin.yaml` | Real defaults — see the `*.example.yaml` |
| `deploy-tracker/service_account.json` | Google service account key (download from GCP IAM) |
| `deploy-tracker/config.yaml` | Sheet ID + path to the service account file |
| `deploy-tracker/deployments/*.yaml` | Per-site deployment manifests (one already shipped: `example.yaml`) |
| `$SLACK_WEBHOOK_URL` (env var) | Webhook for `fs_monitor/alert.sh` |

If you're rotating any of these, do it in the actual source of truth (GCP IAM, Slack, hub UI) — not in git.
