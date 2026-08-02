# hub-admin-web

A web UI for [hub-admin](../hub-admin/README.md) — manage Kela hubs from a
browser instead of the CLI. Covers the full feature set: integrations, devices
(including `device_config.json` import with schema filtering), entity links,
site profiles (export / inspect / apply), and hub-server restarts.

The backend is a FastAPI service that reuses `hub_admin.resources` as a
library. It keeps one long-lived `kubectl port-forward` + gRPC channel per
kubectl context (created lazily, reaped after 15 min idle), so it must run
somewhere with `kubectl` and a kubeconfig holding the kela contexts —
typically a shared ops server.

```
browser ──HTTP──> FastAPI backend ──kubectl port-forward + gRPC──> hub-server pods
```

## Layout

```
hub-admin-web/
├── backend/            FastAPI app (pip package: hub-admin-web)
│   └── src/hub_admin_web/
│       ├── main.py         app assembly, error mapping, SPA static serving
│       ├── connections.py  per-context port-forward/channel pool
│       ├── jobs.py         background jobs (profile apply)
│       └── api/            REST routers, one module per CLI command group
├── frontend/           Vite + React + TypeScript SPA
├── profiles/           saved *.profile.json bundles, applyable from the UI
├── device-configs/     editable device_config.json files (gitignored — creds)
├── Dockerfile          multi-stage: frontend build + python runtime + kubectl
└── docker-compose.yaml
```

## Saved profiles

Drop `*.profile.json` bundles (as produced by `profile export`) into the
`profiles/` folder and they show up in the UI's Profiles page, ready to
inspect and apply to any hub — no upload needed. Override the folder with
`HUB_ADMIN_PROFILES_DIR`; in docker it's mounted from `PROFILES_PATH`
(default `./profiles`).

## Device configs

The Device configs page manages `device_config.json` files in the
`device-configs/` folder (`HUB_ADMIN_DEVICE_CONFIGS_DIR`, mounted read-write
from `DEVICE_CONFIGS_PATH` in docker): edit them in the browser, save, and
apply to a hub. On apply, each section (`onvifcams`, `magos`, ...) is matched
to an integration by name and its devices are added — with the same
schema-based field filtering as `hub-admin devices add`. Real configs carry
device credentials, so the folder is gitignored (only `*.example.json` is
committed).

## Development

Backend (needs the internal `hub-client-py` package, same as the CLI):

```bash
cd hub-admin-web
python3.13 -m venv .venv
.venv/bin/pip install -e /path/to/kela/hub/client/hub-client-py -e ../hub-admin -e ./backend
.venv/bin/uvicorn hub_admin_web.main:app --reload --port 8000
```

Frontend (dev server proxies `/api` to `localhost:8000`):

```bash
cd frontend
npm install
npm run dev
```

Or build the frontend once (`npm run build`) and let the backend serve
`frontend/dist` itself — that's the production layout.

## Deploy (EC2 tailnet host, e.g. `techops-automations-host`)

Same host and conventions as bench-central: checkout in ec2-user's home,
python venv, systemd unit, tailnet as the perimeter (no inbound SG rules).
bench-central owns port 8100; hub-admin-web takes **8200**.

Prerequisites beyond bench-central's: `kubectl` + a kubeconfig at
`~/.kube/config` whose contexts are reachable *from the EC2* (the hubs'
API servers must be routable over the tailnet/VPN — verify with
`kubectl --context <ctx> get pods -n kela` before blaming the app), nodejs
for the frontend build, and a checkout of the internal `hub` repo for
`hub-client-py`.

```bash
# as ec2-user
sudo dnf install -y python3.11 nodejs git

# kubectl (arm64 on t4g, amd64 on t3)
ARCH=$(uname -m | sed 's/x86_64/amd64/; s/aarch64/arm64/')
curl -fsSL "https://dl.k8s.io/release/v1.31.4/bin/linux/${ARCH}/kubectl" -o kubectl
sudo install kubectl /usr/local/bin/ && rm kubectl

# code (techops-tools is already there for bench-central)
git -C ~/techops-tools pull || git clone https://github.com/Kela-Systems/techops-tools.git ~/techops-tools

# hub-client-py lives in the kela monorepo — sparse checkout just that path
git clone --depth 1 --filter=blob:none --sparse https://github.com/Kela-Systems/kela.git ~/kela
git -C ~/kela sparse-checkout set hub/client/hub-client-py

# backend venv
cd ~/techops-tools/hub-admin-web
python3.11 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install ~/kela/hub/client/hub-client-py -e ../hub-admin -e ./backend

# frontend build (served by the backend from frontend/dist)
cd frontend && npm ci && npm run build && cd ..

# kubeconfig with the kela contexts
mkdir -p ~/.kube && cp /path/to/your/kubeconfig ~/.kube/config

# service
sudo cp deploy/hub-admin-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hub-admin-web

curl http://127.0.0.1:8200/api/contexts   # {"contexts": [...]}
```

Then open `http://techops-automations-host:8200` from any tailnet device.

Saved profiles and device configs live in `/var/lib/hub-admin-web/`
(systemd `StateDirectory`, outside the checkout) — seed it by copying your
`*.profile.json` / `device_config.json` files there.

To update: `git -C ~/techops-tools pull`, rebuild the frontend
(`cd ~/techops-tools/hub-admin-web/frontend && npm ci && npm run build`),
then `sudo systemctl restart hub-admin-web`.

No app-level login, same trade-off as bench-central: reachability over the
tailnet IS the access control. Mind that the mounted kubeconfig defines the
blast radius — anyone on the tailnet can administer every hub in it.

## Deployment (docker alternative)

```bash
cd hub-admin-web
HUB_CLIENT_PY_PATH=/path/to/kela/hub/client/hub-client-py \
KUBECONFIG_PATH=~/.kube/config \
docker compose up -d --build
```

Then open `http://<server>:8000`.

Configuration (all via environment / compose):

| Variable | Default | Meaning |
| --- | --- | --- |
| `KUBECONFIG_PATH` | `~/.kube/config` | kubeconfig mounted into the container (read-only) |
| `HUB_CLIENT_PY_PATH` | `../../kela/hub/client/hub-client-py` | build-time path to the internal hub-client-py checkout |
| `HUB_BASIC_AUTH_USER` / `HUB_BASIC_AUTH_PASSWORD` | `hub-admin-web` | Basic-auth identity recorded in hub audit logs |
| `HUB_API_TOKEN` | unset | use token auth instead of basic auth (hubs that enforce it) |
| `HUB_NAMESPACE` | `kela` | namespace of `svc/hub-server` |
| `HUB_REMOTE_PORT` | `8001` | hub-server service port |
| `HUB_CONN_IDLE_TIMEOUT_S` | `900` | idle seconds before a port-forward is reaped |
| `HUB_ADMIN_PROFILES_DIR` / `PROFILES_PATH` | `profiles/` | folder of saved profile bundles shown in the UI |

There is no app-level login: the service is meant to be reachable only on the
internal network. Anyone who can open it can administer every hub in the
kubeconfig — scope the mounted kubeconfig accordingly.

## API

Interactive docs at `/docs`. Summary:

| Method & path | CLI equivalent |
| --- | --- |
| `GET /api/contexts` | kubeconfig contexts |
| `GET /api/hubs/{ctx}/manifests` | `manifests list` |
| `GET /api/hubs/{ctx}/manifests/{id}/schemas` | — |
| `GET/POST /api/hubs/{ctx}/integrations` | `integrations list` / `create` |
| `GET /api/hubs/{ctx}/integrations/export` | `integrations export` |
| `GET/POST .../integrations/{id}/devices` | `devices list` / `create` |
| `PATCH .../devices/{device_id}` | `devices update` (merge-patch / rename) |
| `POST /api/hubs/{ctx}/devices/import` | `devices add` |
| `GET/PUT /api/device-configs/{name}`, `GET /api/device-configs` | device_config files |
| `POST /api/hubs/{ctx}/device-config/apply` → job | `devices add` for every section |
| `GET /api/hubs/{ctx}/assets`, `GET/POST .../links` | `links assets` / `list` / `add` |
| `GET /api/profiles`, `GET /api/profiles/{name}` | saved profiles folder |
| `POST /api/hubs/{ctx}/profile/export` | `profile export` |
| `POST /api/profile/inspect` | `profile show` (offline) |
| `POST /api/hubs/{ctx}/profile/apply` → `GET /api/jobs/{id}` | `profile apply` (async job) |
| `POST /api/hubs/{ctx}/restart` | `server restart` |

gRPC failures surface as HTTP 502 with the status code and message;
port-forward failures likewise (with kubectl's stderr).
