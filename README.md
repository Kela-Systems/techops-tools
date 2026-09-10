# techops-tools

A grab-bag of tools used during Kela deployments and day-to-day ops. The bulk
is the **bench provisioning tools** (web UIs on the Windows bench PC) and the
pieces around them — the run-record collector, hub-admin, the offline site
installer, and the operator-machine USB builder. A couple of leftover ops
scripts live under `scripts/`. Pull out whatever's useful.

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
│   ├── speaker-config-ui/         Provision-ISR IP speaker (:8006), DHCP auto-scan
│   └── tsw-config-ui/             Teltonika TSW202 switch (:8007), zero-input
│
├── bench-central/                 Run-record collector (FastAPI + SQLite) on the tailnet host — the
│                                  durable audit trail the bench stations upload to (see its README.md)
├── hub-admin/                     CLI for hub device/integration management (Python package)
├── hub-admin-web/                 Browser UI over the same hub-admin library
├── kela-offline-installer/        USB stick that takes a bare amd64 box to a converged Kela system
├── rugged_operator/               Build runbook + USB image for the Ubuntu operator machine
└── scripts/
    ├── init_gotcha_server.sh      Provision a Gotcha NRU-230S: hostname, machine-id, kela user, data SSD, Tailscale
    └── pd_session_timeouts.py     View/set PagerDuty account session timeouts
```

## Conventions

- Every shell script supports `--help`. Start there.
- Anything site- or environment-specific is overridable via env vars or CLI flags.
  The defaults match Kela's typical setup (`kela` namespace, `postgres-0` /
  `postgresql-0` pods, etc.).
- Real secrets / device configs are **never committed**. The repo only ships
  `*.example.*` templates; copy them to the non-`.example.` filename and fill in
  real values locally. See [Secrets & local config](#secrets--local-config).

## Prerequisites

Depends on which tool you're running. The common set:

- `bash` 4+, `python3` 3.11+, `curl`, `jq`
- `kubectl` with contexts already set up — for hub-admin
- `xorriso` — for `kela-offline-installer/` and `rugged_operator/usb-builder/`

Python packages (only needed where noted):

- `hub-admin/` — `pip install -e ./hub-admin` (pulls in `typer`, `rich`, `grpcio`, etc.)
- `scripts/pd_session_timeouts.py` — `pip install requests python-dotenv`
- the bench tools manage their own `.venv` per folder (see `bench/README.md`)

## The bench tools

The radar / APU / OTD500 / RUTM08 / Raythink-camera / ISR-speaker / TSW202
configurators that run on the bench PC live under [`bench/`](bench/), with their
own operator guide in [`bench/README.md`](bench/README.md). Short version: copy
the `bench/` folder to the bench machine and run the master launcher —
**`bench/Start Bench Tools.bat`** on Windows or **`bench/start-bench.sh`** on
macOS/Linux. It starts all seven (ports 8001–8007, no collisions) and opens one
dashboard that links to each.

## Ops tools

### `scripts/init_gotcha_server.sh`

Initial provisioning for a Gotcha **NRU-230S** (Jetson AGX Orin 32GB, arm64).
Sets the hostname, gives the box a unique `machine-id`, creates `kela` with
passwordless sudo, formats the SSD and mounts it at `/mnt/data`, points
`/var/lib/rancher` at it so k3s data lands off the 64GB eMMC, and joins the
tailnet. Meant to be run over SSH on the LAN, and safe to re-run: it only
formats a disk that has no `KELADATA` filesystem yet, and refuses outright if
the target disk backs `/`, `/boot` or `/boot/firmware`.

```bash
scp scripts/init_gotcha_server.sh kela@192.168.88.20:/tmp/
ssh -t kela@192.168.88.20 'sudo bash /tmp/init_gotcha_server.sh \
  --site kela-gotcha-01 --data-disk /dev/nvme0n1 \
  --ts-authkey tskey-auth-XXXX --ts-tags tag:gotcha \
  --static-ip 192.168.88.10/24'
```

Install k3s afterwards; it follows the symlink, so no `--data-dir` is needed.

`--static-ip` is optional and off by default. It pins the address the rest of
the fleet expects the site server on (operator stations resolve `kela.local`
there; cameras, radars and APUs use it for NTP), so the box still answers on
`.10` if the router's DHCP reservation is ever lost. It is added as an
*additional* address on the existing DHCP profile — no gateway on the static,
the lease keeps providing the default route and DNS, and the interface is
never reactivated, so it won't drop the SSH session you're running it over.
It refuses to claim the address if something else already answers there.

The same step sets `ipv4.dhcp-timeout infinity` on the profile, because
NetworkManager otherwise fails a `method=auto` profile outright when no lease
arrives and tears the manual address down with it — losing the address in
exactly the case it exists for. `ipv4.required-timeout` bounds how long boot
waits on a lease (needs NM 1.34+, skipped with a warning below that).

It then pins the k3s node IP to that address via a
`/etc/rancher/k3s/config.yaml.d/` drop-in. k3s otherwise takes the first
global address on the default-route interface, so on a box holding both a
lease and the static it depends on which one NetworkManager applied first. A
drop-in is used because `kela-node-controller` owns `config.yaml` and rewrites
it; the drop-in deliberately never mentions the `*-arg` keys, since k3s
replaces lists rather than merging them.

Every Jetson flashed from the same JetPack image ships with an identical
`/etc/machine-id`, and systemd derives the DHCP DUID from it — so two
un-fixed boxes on one LAN present the same DHCP identity and fight over
leases, which is exactly what the `.10` reservation depends on. The script
regenerates it once, recording a stamp at
`/var/lib/kela/.machine-id-regenerated` so re-runs don't churn the box's DHCP
identity or journal lineage. Both `/etc/machine-id` and
`/var/lib/dbus/machine-id` have to be removed first: on JetPack the latter is a
regular file holding the same factory ID, and `systemd-machine-id-setup`
prefers it as a seed, so clearing only `/etc/machine-id` hands the duplicate
straight back (`Initializing machine ID from D-Bus machine ID`). It is then
restored as a symlink to `/etc/machine-id`, and the step fails loudly if the ID
comes back unchanged. The new ID becomes the DHCP identity on the next boot.
Use `--keep-machine-id` to opt out.

`kela-node-controller` also derives the k3s `node-name` from the machine-id,
which is the strongest reason to make it unique — a fleet sharing one ID would
share one k3s node name. It equally means regenerating it on a box where k3s
has already registered renames the node and orphans the old object, so the
step refuses outright when k3s is installed. Pass `--keep-machine-id` if the
ID is already unique, or `--force-machine-id` to accept the re-registration.

### `scripts/pd_session_timeouts.py`

View/set PagerDuty account session timeouts (API-only; no UI). Needs a
PagerDuty API token in `scripts/.env` (`PAGERDUTY_API_TOKEN=...`).

```bash
cd scripts
python3 pd_session_timeouts.py                          # show current
python3 pd_session_timeouts.py --web-idle 30d --web-absolute 90d
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
installed separately in your dev venv. See [`hub-admin/README.md`](hub-admin/README.md).
The same surface is also available as a browser UI in
[`hub-admin-web/`](hub-admin-web/README.md).

### `kela-offline-installer/`

USB builder that takes a bare amd64 box to a converged Kela system with zero
network and one prompt (the hostname). See
[`kela-offline-installer/README.md`](kela-offline-installer/README.md).

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
| `bench/tsw-config-ui/config/tsw.config.json` | Shared password, NTP, timezone, management IP, firmware floor — see `tsw.config.example.json` |
| `bench/tsw-config-ui/firmware/` | The TSW202 image, needed only for a switch that arrives below the configured firmware floor |
| `hub-admin/device_config.json` | Real device IPs / creds — see the `*.example.json` |
| `hub-admin/hub-admin.yaml` | Real defaults — see the `*.example.yaml` |
| `scripts/.env` | `PAGERDUTY_API_TOKEN=...` for `pd_session_timeouts.py` |

If you're rotating any of these, do it in the actual source of truth (GCP IAM,
Slack, hub UI) — not in git.
