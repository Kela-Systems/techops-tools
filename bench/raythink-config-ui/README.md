# Raythink Camera Configurator

Bench tool for provisioning Raythink thermal cameras, one at a time. Same
connect → configure → next flow as the other bench tools, with **no manifest
CSV** — when a camera is detected, you pick a config **profile** (LAN or
Cellular) and how it should be **addressed** (a static IP, or DHCP).

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
6. **Last step:** address the camera — either move it to a static
  `192.168.88.XX` (gateway `192.168.88.1`, mask `255.255.255.0`) or switch it to
   **DHCP**. Either way the connection drops by design; see below for how each
   one is confirmed.
7. Verify every setting by reading it back off the camera — including an
  authenticated **ONVIF** `GetUsers` call to confirm the ONVIF login is
   `admin/Kelafield123!`.

The addressing step runs last because the camera leaves `192.168.1.123` the
moment it applies.

### Choosing how the camera is addressed

Three modes in the UI, chosen any time (even before a camera is plugged in) and
persisted to `ip_state.json` so the choice survives a restart:

- **Manual** — type the last octet of the static IP. `XX` is in the range
`30-50` (configurable).
- **Cycle** — the tool auto-assigns `30`, then `31`, … `50`, then wraps back to
`30`. The counter only advances on a successful run (a failed camera keeps its
slot for a retry).
- **DHCP** — the camera keeps whatever address its own DHCP server gives it, for
a site that addresses its cameras that way. No octet to choose.

Both static modes are confirmed the obvious way: the tool reconnects on the
address it chose (renewing the laptop's DHCP lease so it can follow). **DHCP has
nothing to reconnect to** — nothing on the bench picked the address — so the
tool finds the camera again by the one thing that didn't change, its **MAC**.
Every few seconds, for up to `dhcp.lease_timeout` seconds, it:

1. knocks on every address in `dhcp.scan_subnets`, which makes the PC's **ARP
  cache** learn who is out there — resolution happens before the connection is
   attempted, so a camera that has an address but hasn't finished starting its
   web server still shows up;
2. checks **every** address that MAC is cached at (a camera that just moved is
  often cached at its old address as well as its new one) and takes the first
   that actually answers;
3. re-renews the PC's own DHCP lease as it goes, since the PC may need an
  address on the camera's new subnet before it can see the camera there.

It then verifies everything on whatever address the camera landed on. Progress is
logged while it waits, so the step log shows it is still looking rather than
appearing to hang.

Consequences worth knowing:

- **The PC must hold an address on the subnet the camera lands on.** ARP is
link-local: off-subnet, the PC's cache holds the *router's* MAC, never the
camera's, so the camera can't be found no matter how long the tool waits. That
subnet also needs a **DHCP server**, or there is no lease to find.
- `dhcp.lease_timeout` defaults to **300s**, much longer than the static move's,
because it has to cover the camera rebooting, taking a lease *and* starting its
web server. If it runs out, the two failures are reported differently: the MAC
was **seen** at an address but nothing answered there yet (the tool points at it
anyway, so verification gets a last chance and you get an address to go look at),
or the MAC was **never seen at all** (check the two requirements above). The
camera's previous static address is deliberately left in place as its fallback,
so it never ends up with no address at all.
- A camera whose MAC the bench never read over ARP is **refused** for DHCP up
front, with a message saying so — there would be no way to check the result.
- A DHCP run is named `raythink-dhcp` and its run record carries **no IP**: the
lease is the DHCP server's to change, so recording it as "the address we
assigned" would be a lie. The address it actually landed on is in the run's
verification rows and in the log.



## Setup

```
cp config/raythink.config.example.json config/raythink.config.json   # then fill in real values
python3 raythink_app.py                                              # opens http://127.0.0.1:8005
```

On the bench PC you don't run this by hand — the top-level launcher
(`Start Bench Tools.bat` / `start-bench.sh`) sets up the shared `.venv` and starts
every tool together.

The laptop's adapter must be on the `192.168.1.x` subnet to reach the camera at
`192.168.1.123`; after the run the camera is on `192.168.88.x` (or on a DHCP
lease), so be able to reach that subnet (DHCP or a `192.168.88.x` address) to
confirm the move.

### Config profiles

Each profile is a config JSON exported from a reference camera
(*Setup > System > Export*), stored under `config/profiles/` (the paths in the
config are relative to `config/`). Keep the camera's **Network/IP table out** of
these files — the tool sets the address (static or DHCP) and NTP itself, after
the import. The profile exports ship committed (no secrets); only the real
`raythink.config.json` (copied from the example) is gitignored.

There is also a single-camera CLI (`--ip` and `--dhcp` are the two ways to
address it, exactly one required):

```
python3 raythink_configure.py --profile lan --ip 30
python3 raythink_configure.py --profile lan --dhcp
```



## Shared code

This folder must sit next to `bench-core`, the shared local package it installs
(`-e ./bench-core[ui]` from the bench root). That package provides the bench-UI base (`bench_ui` —
detection loop, run machinery, routes, WebSocket, step logging) and the host-side
network helpers: the DHCP-renew ones, plus `find_ip_by_mac` (sweep a subnet, read
the ARP cache, match a MAC) which is what finds a camera again after it is put on
DHCP. `raythink_camera.py` adds the camera client (RPC2 login, config import,
NTP, the static-IP move and the DHCP switch, verification);
`raythink_configure.py` adds the pipeline + CLI; `raythink_app.py` is the
`BenchConfigurator` subclass (the profile/IP-mode form and the persisted cycle
counter).

Per-camera logs land in `logs/` (one JSON per camera + a rolling
`raythink-config.log`).