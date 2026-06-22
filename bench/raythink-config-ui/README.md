# Raythink Camera Configurator

Bench tool for provisioning Raythink thermal cameras, one at a time. Same
connect → configure → next flow as the other bench tools, with **no manifest
CSV** — when a camera is detected, you pick a config **profile** (LAN or
Cellular) and the **static IP**.

A fresh camera ships on the static IP `192.168.1.123` with `admin/admin` and
speaks the Dahua-OEM **RPC2 JSON API** (the same protocol its own web UI uses).

## What it does per camera

1. Login with `admin/admin` (falls back to the new password for re-runs).
2. Change the admin password to `Kelafield123!` (`new_password`). The device
   stores `MD5(user:realm:password)` (hex case per firmware), not plaintext.
3. Import the chosen config profile (LAN or Cellular) — the same JSON the web's
  *Setup > System > Import* accepts; the tool replays each config table via
   `configManager.setConfig`, then re-logs-in (an import can drop the session).
4. Date & Time: set NTP to `192.168.88.10` **and** sync the clock to the bench
   PC's current time (the web UI's *Sync to PC time* button).
5. Set the **ONVIF** `admin` password to `Kelafield123!`. ONVIF keeps a
   **separate credential** from the web/system account (ONVIF PasswordDigest
   needs a recoverable password, which the system account's `MD5(user:realm:pw)`
   isn't), so the step 2 password change does **not** touch it. The tool sets it
   over the standard ONVIF `SetUser` op, authenticating with the factory ONVIF
   password (the same default as the web login, `admin`). Idempotent.
6. **Last step:** move the camera to a static `192.168.88.XX`
  (gateway `192.168.88.1`, mask `255.255.255.0`). The connection drops by
   design; success is confirmed by reaching the camera on the new address (the
   laptop's DHCP lease is renewed so it can follow).
7. Verify every setting by reading it back off the camera — including an
   authenticated **ONVIF `GetUsers`** call to confirm the ONVIF login is
   `admin/Kelafield123!`.

The IP move runs last because the camera leaves `192.168.1.123` the moment it
applies.

### Choosing the static IP

`XX` is in the range `30-50` (configurable). Two modes in the UI:

- **Manual** — type the last octet.
- **Cycle** — the tool auto-assigns `30`, then `31`, … `50`, then wraps back to
`30`. The counter is persisted to `ip_state.json` so it survives restarts, and
only advances on a successful run (a failed camera keeps its slot for a retry).

## Setup

```
pip install -r requirements.txt   # or just double-click run_raythink on the bench PC
python3 raythink_app.py           # opens http://127.0.0.1:8005
```

The laptop's adapter must be on the `192.168.1.x` subnet to reach the camera at
`192.168.1.123`; after the run the camera moves to `192.168.88.x`, so be able to
reach that subnet (DHCP or a `192.168.88.x` address) to confirm the move.

### Config profiles

Each profile is a config JSON exported from a reference camera
(*Setup > System > Export*). Keep the camera's **Network/IP table out** of these
files — the tool sets the static IP (and NTP) itself, after the import. Real
`profiles/*.json` are gitignored; only the `*.example.json` templates are
committed.

There is also a single-camera CLI:

```
python3 raythink_configure.py --profile lan --ip 30
```

## Shared code

This folder must sit next to `bench-core`, the shared local package it installs
(`-e ../bench-core[ui]`). That package provides the bench-UI base (`bench_ui` —
detection loop, run machinery, routes, WebSocket, step logging) and the host
DHCP-renew helpers. `raythink_camera.py` adds the camera client (RPC2 login,
config import, NTP, static-IP move, verification); `raythink_configure.py` adds
the pipeline + CLI; `raythink_app.py` is the `BenchConfigurator` subclass (the
profile/IP-mode form and the persisted cycle counter).

Per-camera logs land in `logs/` (one JSON per camera + a rolling
`raythink-config.log`).