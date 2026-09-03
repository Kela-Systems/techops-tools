# Raythink Camera Configurator

Bench tool for provisioning Raythink thermal cameras, one at a time. Same
connect → configure → next flow as the other bench tools, with **no manifest
CSV** — when a camera is detected, you pick a config **profile** (LAN or
Cellular) and how it should be **addressed** (a static IP, or DHCP).

A fresh camera of either generation ships on the static IP `192.168.1.123` with
`admin/admin`.

## Two camera generations

Raythink ships two generations of these cameras, and they have **no protocol in
common**. The tool supports both and works out which is on the bench by itself,
so an operator never chooses: everything below is the same on either, except
which config profile file gets sent.

| | firmware version | API |
|---|---|---|
| older | `1.000.General 00.0.T, build: 2025-04-09` | Dahua-OEM **RPC2 JSON** (the protocol its own web UI uses) |
| newer | `B1.2.01.01.15, 2026-05-14` | **REST** under `/v1` with an `X-Token` header |

Detection is a pair of unauthenticated probes — the REST login route, then the
RPC2 login challenge — because reading the firmware version needs a session and
establishing one is the thing that differs. Once logged in, the tool reads the
version anyway and cross-checks it against the API that answered; a disagreement
is logged loudly rather than acted on, since it would mean the version convention
has moved. `--generation rpc2|rest` on the CLI skips the probe.

Two of the newer API's endpoints — the admin password change and the device
identity — appear nowhere in the vendor's 597-page documentation and were
captured from the camera's own web UI. They are marked as such in
`raythink_rest.py`, since a reader checking them against the PDF will not find
them.

## What it does per camera

1. Login with `admin/admin` (falls back to the new password for re-runs). The
  older cameras use a two-step challenge/response; the newer ones send the
   password AES-encrypted under a fixed key and get a token back.
2. Change the admin password to `Kelafield123!` (`new_password`). Neither
  generation takes it in plaintext: the older stores
   `MD5(user:realm:password)` (hex case per firmware), the newer wants the same
   AES form as the login.
3. Import the chosen config profile (LAN or Cellular). On the older cameras that
  is the JSON the web's *Setup > System > Import* accepts, replayed one config
   table at a time via `configManager.setConfig`; on the newer ones the file is
   uploaded whole. Then re-login (an import can drop the session).
4. Date & Time: set NTP to `192.168.88.10` **and** sync the clock to the bench
  PC's current time (the web UI's *Sync to PC time* button).
5. Set the **ONVIF** `admin` password to `Kelafield123!`. ONVIF keeps a
  **separate credential** from the web/system account on both generations, so
   the step 2 password change does **not** touch it — verified on a live unit
   that was still ONVIF `admin/admin` after a normal provisioning run. The older
   cameras have no endpoint for it, so the tool speaks ONVIF `SetUser` over SOAP;
   the newer ones expose it as two plain JSON calls. Idempotent either way.
6. **Last step:** address the camera — either move it to a static
  `192.168.88.XX` (gateway `192.168.88.1`, mask `255.255.255.0`) or switch it to
   **DHCP**. Either way the connection drops by design; see below for how each
   one is confirmed.
7. Verify every setting by reading it back off the camera — including the
  **ONVIF** credential, confirmed with an authenticated `GetUsers` call on the
   older cameras and read back off the user list on the newer ones.

The addressing step runs last because the camera leaves `192.168.1.123` the
moment it applies.

### Choosing how the camera is addressed

Four modes in the UI, chosen any time (even before a camera is plugged in) and
persisted to `ip_state.json` so the choice survives a restart. The picker and
the modes themselves are `bench_core.ip_mode`, shared with the speaker and
switch tools since TEC-848 — this tool is where they started.

- **Same IP every time** (`fixed`) — every camera in the batch gets the one
address the operator sets, starting from `fixed_octet`. For projects whose sites
each take a **single** camera: they all want the same default configuration, and
two of them never meet on a live network.
- **Manual** — type the address for that one camera, as a last octet or a full
`192.168.88.x`. The octet range is `30-50` (configurable).
- **Cycle** — the tool auto-assigns `30`, then `31`, … `50`, then wraps back to
`30`. The counter only advances on a successful run (a failed camera keeps its
slot for a retry).
- **DHCP** — the camera keeps whatever address its own DHCP server gives it, for
a site that addresses its cameras that way. No octet to choose.

The three static modes are confirmed the obvious way: the tool reconnects on the
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
config are relative to `config/`).

The two generations export **incompatible** files — the older one a map of Dahua
config tables replayed one at a time, the newer one a set of sections uploaded
whole, sharing no section names at all — so one profile key names one file per
generation. A plain string still means the older generation only, so a bench
that has not met a new camera yet needs no change:

```json
"profiles": {
  "lan":      { "rpc2": "profiles/lan.json", "rest": "profiles/v2/lan.json" },
  "cellular": "profiles/cellular.json"
}
```

The profile list in the UI is unchanged; a profile with no file for the camera
currently on the bench is shown greyed out, and a run that would need one is
refused before it starts. A profile aimed at the wrong generation is rejected by
name rather than being uploaded for the camera to complain about.

#### Sanitize an export before committing it

Profiles ship committed, because they are needed at runtime. **That is only safe
for a sanitized export**, so run any new one through:

```
python3 raythink_configure.py --sanitize-profile ~/Downloads/export.json \
        -o config/profiles/v2/lan.json
```

A raw export is the reference camera's entire configuration, which is two things
a profile must not be:

- **It carries that camera's address.** The profile is imported in the *middle*
of the pipeline while addressing is deliberately *last*, so an export with a
network section moves the next unit mid-run and the run loses it. This used to
rest on whoever exported the profile deleting it by hand.
- **It carries the station password in plaintext**, on the newer generation —
`OnvifUser.User[].Password` on a bench-provisioned reference camera is the shared
bench password. Committing a raw export puts it in git history, where it stays.
The tool sets the ONVIF password itself, so dropping the section costs nothing.

The sanitizer removes both and **reports** anything else password-shaped it left
alone (a GB28181 SIP password, an SMTP login) — those may be legitimate site
settings, so removing them is a person's call. `import_config` re-runs the same
sanitizer on whatever it is given as a backstop, but that does nothing about a
password already committed. Only the real `raythink.config.json` (copied from the
example) is gitignored.

There is also a single-camera CLI (`--ip` and `--dhcp` are the two ways to
address it, exactly one required):

```
python3 raythink_configure.py --profile lan --ip 30
python3 raythink_configure.py --profile lan --dhcp
python3 raythink_configure.py --profile lan --ip 30 --generation rest
```



## Shared code

This folder must sit next to `bench-core`, the shared local package it installs
(`-e ./bench-core[ui]` from the bench root). That package provides the bench-UI base (`bench_ui` —
detection loop, run machinery, routes, WebSocket, step logging) and the host-side
network helpers: the DHCP-renew ones, plus `find_ip_by_mac` (sweep a subnet, read
the ARP cache, match a MAC) which is what finds a camera again after it is put on
DHCP.

The camera client is split so that the two generations share everything that is
not protocol:

- `raythink_base.py` — the addressing steps, the reachability helpers and the
verification report, over four per-generation hooks. Following a camera through
an IP change is host-side work that is identical whichever protocol answers, and
it is long enough that two copies of it would drift apart.
- `raythink_camera.py` — the older cameras (RPC2 login, table-by-table import,
the hand-rolled ONVIF SOAP).
- `raythink_rest.py` — the newer cameras (AES login and token, whole-file
import, ONVIF as plain JSON).
- `raythink_client.py` — which generation is on the bench, and the client for it.

`raythink_configure.py` adds the pipeline + CLI, written against the shared
client interface so it is one pipeline rather than two; `raythink_app.py` is the
`BenchConfigurator` subclass (the profile/IP-mode form and the persisted cycle
counter).

Per-camera logs land in `logs/` (one JSON per camera + a rolling
`raythink-config.log`).