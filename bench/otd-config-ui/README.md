# OTD500 Configurator

Bench tool for provisioning Teltonika OTD500 gateways, **one at a time**. Same
plug-in → configure → unplug flow as the other bench tools: plug a device in, the
UI reads its LAN MAC, you type the **site name** and the **factory label
password**, and it provisions. Nothing is saved beyond the session history.

## What it does per device

1. Login with the label password (falls back to the shared password for re-runs).
2. Set the admin/root password to the shared default.
3. Hostname / device name → `otd-<site_name>`.
4. Timezone, then SIM 4G-only (if enabled).
5. Firmware upgrade (local `.bin`, FOTA, defer-to-RMS, or none).
6. Enable RMS on-device + register the unit in the RMS cloud (serial+MAC) when
   `rms.api_token` + `rms.company_id` are set in the config.
7. Join Tailscale (per-device minted key or a static one).
8. Verify every setting by reading it back off the device.

Per-device logs land in `logs/` (one JSON per device + a rolling `otd-config.log`).

## Setup

```
cp config/site.config.example.json config/site.config.json   # then fill in RMS + Tailscale secrets
python3 otd_app.py                                            # opens http://127.0.0.1:8003
```

On the bench PC you don't run these by hand — the top-level launcher
(`Start Bench Tools.bat` / `start-bench.command`) sets up the shared `.venv` and starts
every tool together.

The laptop's Ethernet adapter must be on the `192.168.1.x` subnet and plugged
into the device. Self-test the platform's ARP detection with
`python3 otd_app.py --probe-mac <any-lan-ip>`.

There is also a single-device CLI (no UI):

```
python3 otd_configure.py --site haifa-port --label-password 'Xy7Kp2Lm9Qa'
```

## Shared code

This folder must sit next to `bench-core`, the shared local package it installs
(`-e ./bench-core[ui]` from the bench root). That package holds the field-tested
device client (`TeltonikaClient` — REST + SSH/UCI, firmware, RMS, Tailscale) and
the bench-UI base (`bench_ui`) that drives the detection loop, routes, and
WebSocket. `otd_configure.py` adds the OTD500 pipeline; `otd_app.py` is the
`BenchConfigurator` subclass that wires it to the UI. Tests live in `tests/`.
