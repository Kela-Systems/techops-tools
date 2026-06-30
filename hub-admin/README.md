# hub-admin

A production-grade CLI for managing devices, integrations, and entity links on a
Kela hub over its gRPC API. It handles the `kubectl` port-forward for you, then
lets you add radars/cameras from a config file, list what's on a hub, wire up
entity links, and — most usefully — capture a whole site as a portable
`*.profile.json` bundle and redeploy it onto another cluster.

## Install

```bash
pip install -e ./hub-admin
```

This pulls in `typer`, `rich`, `grpcio`, `protobuf`, `pyyaml`, and `tenacity`.
It also depends on the internal `hub_client` and `proto_py` packages, which are
installed separately in your dev venv.

Requires Python 3.11+ and a working `kubectl` with the relevant contexts already
configured (every command port-forwards into `svc/hub-server`).

## Configuration

Commands read defaults from `~/.hub-admin.yaml` (copy from `hub-admin.example.yaml`).
Anything there is overridable per-command with `--context`.

```yaml
defaults:
  context: kela-office-01
  namespace: kela
  port: 8001

device_configs:
  - ./device_config.json
```

Device data lives in a `device_config.json` (copy from `device_config.example.json`).
It groups devices by integration type — `onvifcams`, `magos`, etc. — with the
per-device fields the hub's manifest schema expects. Real configs hold device
IPs and credentials and are **gitignored**; only the `*.example.*` templates are
committed.

Auth: hubs running with `HUB_BASIC_AUTH=true` accept any non-empty HTTP Basic
credentials (resolved from `HUB_BASIC_AUTH_USER` / `HUB_BASIC_AUTH_PASSWORD`, or a
default). Hubs that enforce token auth take `--api-token` or the `HUB_API_TOKEN`
env var.

## Commands

Run `hub-admin --help` (or `hub-admin <group> --help`) for full options. Every
command accepts `--context` to choose the kubectl context to port-forward into.

### `interactive`

Guided setup wizard: walks through existing integrations, lets you create new
ones from manifests, add devices (from `device_config.json` or by hand), edit
the setup_info / name of existing devices, and configure entity links — then
offers to restart the hub-server.

```bash
hub-admin interactive --context kela-office-01 -d ./device_config.json
```

### `manifests list`

List the integration manifests available on a hub.

```bash
hub-admin manifests list --context kela-office-01
```

### `integrations`

```bash
hub-admin integrations list                        # existing integrations + device counts
hub-admin integrations create <manifest_id> -c '{"poll_interval_ms": 500}'
hub-admin integrations export -o device_config.exported.json   # dump all integrations + devices to a replayable config
```

`export` is the inverse of `devices add`: it produces a `device_config.json` you
can replay onto another hub.

### `devices`

```bash
# Add devices from a config section (auto-matches the section by manifest name)
hub-admin devices add <integration_id> -c ./device_config.json --manifest-name magos

# Or create a single device directly
hub-admin devices create <integration_id> "Radar 1000" --setup-info '{"IpAddress": "192.168.1.20"}'

# List the devices on an integration (with their IDs and setup_info)
hub-admin devices list <integration_id>

# Modify an existing device: merge-patch its setup_info and/or rename it
hub-admin devices update <integration_id> <device_id> -p '{"IpAddress": "192.168.1.30"}'
hub-admin devices update <integration_id> <device_id> --name "Radar 2000"
```

Fields not present in the target integration's schema are dropped (and reported)
on `add`.

`update` applies an RFC 7396 JSON merge-patch to `setup_info`: top-level keys in
`--patch` overwrite the current value (nested objects are deep-merged), a key set
to `null` is deleted, and any key you don't mention is preserved — so partial
edits never clobber background-written keys such as `intrinsic_calibration`.
`--name` renames the device without touching its `setup_info`. On older hubs that
predate the `UpdateDeviceSetupInfo` RPC (returning `UNIMPLEMENTED`), the patch is
applied client-side over the device's current `setup_info` and written back via
the wholesale `UpdateDevice`, preserving the same merge semantics.

### `links`

Manage entity links between assets (e.g. point a radar at a camera).

```bash
hub-admin links assets                  # list assets on the hub
hub-admin links list                    # list configured links
hub-admin links add <source> <target> --restart   # link by name or ID, then restart
```

Link changes need a hub-server restart to take effect (`--restart` does it for
you). On older hubs without `AssetService`, source/target are treated as raw
asset IDs.

### `profile`

Capture a site's integrations + SiteConfig as a portable bundle and deploy it
elsewhere.

```bash
hub-admin profile export --context kela-cuas-06 -o cuas-06.profile.json
hub-admin profile show   cuas-06.profile.json          # inspect offline, no hub connection
hub-admin profile apply  cuas-06.profile.json --context kela-cuas-07 --restart
```

`--site-config` (default on) **fully replaces** the destination's SiteConfig.
Entity links reference site-specific asset IDs, so pass `--no-site-config` when
deploying to a genuinely different site.

### `server restart`

Restart the hub-server pod.

```bash
hub-admin server restart --context kela-office-01
```

## Typical workflows

Clone one site's setup onto a fresh hub:

```bash
hub-admin profile export --context kela-cuas-06 -o cuas-06.profile.json
hub-admin profile apply  cuas-06.profile.json --context kela-cuas-07 --restart
```

Add a batch of cameras to an existing integration:

```bash
hub-admin integrations list --context kela-office-01
hub-admin devices add <integration_id> -c ./device_config.json --manifest-name onvifcams
```

## Development

```bash
pip install -e './hub-admin[dev]'   # adds pytest, pytest-mock
pytest hub-admin
```

Layout:

```
src/hub_admin/
├── cli/          Typer commands (one module per command group)
├── resources/    gRPC resource layer (manifests, integrations, devices, links, profile, server)
├── client.py     Port-forward + gRPC channel lifecycle
├── config.py     YAML config loading
├── display.py    Rich tables / summaries
├── models.py     Dataclasses
└── auth.py       HTTP Basic auth interceptor
```
