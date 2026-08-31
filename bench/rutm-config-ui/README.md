# RUTM08 Configurator

Bench tool for provisioning Teltonika RUTM08 routers, one at a time. Same
plug-in → configure → unplug flow as `../otd-config-ui`, but with **no manifest
CSV** — when a router is detected, the UI asks for the **site name** and the
**factory label password**.

## What it does per device

1. Login with the label password (falls back to the shared password for re-runs).
   The label password is also kept on bench-central, keyed to the serial — see
   "Factory passwords" below.
2. Set the admin/root password to the shared default (`Kelasys123!`).
3. Hostname / device name → `rut-<site_name>`.
4. Timezone → `Asia/Jerusalem`.
5. Firmware upgrade (FOTA latest-stable by default; local `.bin` supported).
6. Enable RMS on-device + register the unit in the RMS cloud (serial+MAC),
   then assign the RMS Management pack license (`rms.pack`, the UI's
   "Set pack" action) so the device doesn't run on 30-day credits.
7. Join Tailscale (per-device minted key or a static one).
8. Verify every setting by reading it back off the device.
9. **Last step:** move the LAN to `192.168.88.1`. The connection drops by
   design; success is confirmed by reaching the device on the new address
   (the laptop's DHCP lease is renewed so it follows into the new subnet).

The LAN move runs last because the device leaves `192.168.1.1` the moment it
applies. The detection loop watches both addresses, so an already-moved router
can be plugged back in and re-run — leave the password field empty (it's
already on the shared password).

## Factory passwords (TEC-845)

The label password this unit shipped with is sent to bench-central as the run
finishes, keyed to its serial, and kept there forever. That value is what the
router reverts to on a factory reset, so keeping it is the difference between
recovering a unit reset in the field and losing access to it.

It never enters the run record — it rides its own queue
(`logs/outbox-labels/`) into its own store, because a device has one factory
password however many times it is run. Nothing is kept when the password field
is left empty (a re-run on the shared password), or when what was typed *is*
the shared password. Read them back in the **Factory passwords** panel on the
bench-central dashboard.

## Setup

```
cp config/rutm.config.example.json config/rutm.config.json   # then fill in RMS + Tailscale secrets
python3 rutm_app.py                                           # opens http://127.0.0.1:8004
```

On the bench PC you don't run this by hand — the top-level launcher
(`Start Bench Tools.bat` / `start-bench.sh`) sets up the shared `.venv` and starts
every tool together.

The laptop's Ethernet adapter must be on DHCP and plugged into a LAN port of
the router; the router's WAN port needs a live uplink (FOTA / RMS / Tailscale
need internet). Self-test the platform's ARP detection with
`python3 rutm_app.py --probe-mac <any-lan-ip>`.

There is also a single-device CLI:

```
python3 rutm_configure.py --site haifa-port --label-password 'Xy7Kp2Lm9Qa'
```

## Shared code

This folder must sit next to `bench-core`, the shared local package it
installs (`-e ./bench-core[ui]` from the bench root). That package holds the one
field-tested copy of the device client (`TeltonikaClient` — REST + SSH/UCI,
firmware, RMS, Tailscale) and the bench-UI base (`bench_ui`) that drives the
detection loop, routes, and WebSocket. `rutm_configure.py` adds the RUTM08
specifics on top: wired-WAN internet wait, no SIM/eSIM steps, and the LAN move;
`rutm_app.py` is a thin `BenchConfigurator` subclass.

Per-device logs land in `logs/` (one JSON per device + a rolling
`rutm-config.log`).
