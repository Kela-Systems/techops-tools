# OTD500 Configurator

Bench tool for provisioning Teltonika OTD500 gateways, one at a time. Same
plug-in → configure → unplug flow as the other bench tools, with **two modes you
switch between in the UI**:

- **Ad-hoc (default)** — like `../rutm-config-ui`: plug a device in, the UI reads
  its LAN MAC, you type the **site name** and the **factory label password**, and
  it provisions. Nothing is saved beyond the session history; no manifest needed.
- **Batch** — a saved list (`manifest.csv`) you build and edit in the UI. A
  plugged-in device is matched to a row by its LAN MAC and, with **Auto** on,
  provisioned hands-free. The same manifest feeds `rms_register.py` for bulk RMS
  pre-registration.

The tool opens in **Ad-hoc** mode every time; switch to Batch with the toggle in
the top-right.

## What it does per device

1. Login with the label password (falls back to the shared password for re-runs).
2. Set the admin/root password to the shared default.
3. Hostname / device name → `otd-<site_name>`.
4. Timezone, then SIM 4G-only (if enabled).
5. Firmware upgrade (local `.bin`, FOTA, defer-to-RMS, or none).
6. Enable RMS on-device + register the unit in the RMS cloud (serial+MAC).
7. Join Tailscale (per-device minted key or a static one).
8. Optional eSIM profile load (per-device activation code).
9. Verify every setting by reading it back off the device.

Per-device logs land in `logs/` (one JSON per device + a rolling `otd-config.log`).

## Batch mode

Build the list however suits you:

- **Capture at the bench (simplest).** Add nothing up front. In Batch mode, plug
  a device in; the UI shows it as *unmatched* with its live MAC and lets you
  **Save & configure** (records the row and provisions it now) or **Save to
  batch** (just records it). The device's MAC and serial are written back into
  the row, so each box is handled exactly once.
- **Pre-seed planned rows.** Add rows with only a **site name** (leave MAC/serial
  blank). The first time each device is configured, its MAC/serial are captured
  into that row — so you can plan the batch as a list of sites and let the
  hardware fill in the identifiers.
- **Import a full manifest.** Copy `manifest.example.csv` → `manifest.csv` and
  fill in real values (e.g. from an RMS export), then **Reload**. The only
  required column is `site_name`.

`manifest.csv` is gitignored (it holds per-device label passwords) and is
rewritten whenever you edit the batch in the UI. Passwords are never sent to the
browser — a row only reports whether a password is set.

## Setup

```
cp site.config.example.json site.config.json   # then fill in RMS + Tailscale secrets
pip install -r requirements.txt                # or just double-click run_otd on the bench PC
python3 otd_app.py                             # opens http://127.0.0.1:8003
```

The laptop's Ethernet adapter must be on the `192.168.1.x` subnet and plugged
into the device. Self-test the platform's ARP detection with
`python3 otd_app.py --probe-mac <any-lan-ip>`.

There is also a single-device CLI (no UI, no manifest):

```
python3 otd_configure.py --site haifa-port --label-password 'Xy7Kp2Lm9Qa'
```

And bulk RMS pre-registration from the manifest:

```
python3 rms_register.py            # dry-run from manifest.csv
python3 rms_register.py --apply    # actually create the devices in RMS
```

## Shared code

This folder must sit next to `bench-core`, the shared local package it installs
(`-e ../bench-core[ui]`). That package holds the field-tested device client
(`TeltonikaClient` — REST + SSH/UCI, firmware, RMS, Tailscale) and the bench-UI
base (`bench_ui`) that drives the detection loop, routes, and WebSocket.
`otd_configure.py` adds the OTD500 pipeline; `otd_app.py` is the
`BenchConfigurator` subclass that adds the two modes, manifest editing, and
auto-on-match.
