# TSW202 Configurator

Bench tool for provisioning Teltonika TSW202 managed switches, one at a time
(TEC-791). The **zero-input** tool of the set: plug the switch in, scan the
sticker, press Configure. No site name, no hostname — nothing in the switch
baseline is named after a site, so asking would be a field filled in for nothing.

## What it does per device

1. Login with the label password (falls back to the shared password for re-runs).
2. Set the admin/root password to the shared default (`Kelasys123!`).
3. Firmware **floor** — see below.
4. NTP server → `192.168.88.10`, as the switch's *only* time source.
5. Timezone → `Asia/Jerusalem`.
6. Verify every setting by reading it back off the switch.
7. **Last step:** move the management IP to `192.168.88.2`. The connection drops
   by design; success is confirmed by reaching the switch on the new address.

## Two things that differ from the router tools next door

**Firmware is a floor, not a pin.** `firmware.minimum_version` is the *minimum*
acceptable version. A switch below it is flashed from the local image; one at or
above it is left alone, with a note in the run record. This matters because
Teltonika ships a "Stable" and a newer "Latest" for this family, so a switch
arriving *newer* than our pin is routine rather than an edge case — and
`upgrade_firmware(skip_if_version=...)` compares for equality, which would flash
it backwards. `bench_core.fw_version_at_least` is the ordered compare that makes
the floor correct.

**The station needs an address on `192.168.88.x`.** The RUTM08 renews the
laptop's DHCP lease after its LAN move, because the router *serves* DHCP on the
new subnet. A switch does not, so this tool passes `renew_dhcp=False` — on macOS
the renew is `ipconfig set <iface> DHCP`, which would throw away a statically
configured bench adapter. Give the adapter a second static IP on
`192.168.88.x`, or a `/16` covering both. Without it the switch still moves
correctly, but the run reports its final-address check as failed.

The switch's factory address is `192.168.1.**2**` — the `.2`, not the `.1` the
OTD500 and RUTM08 share — so this tool runs alongside them on the same bench
subnet without colliding. The detection loop watches both the factory and the
final address, so an already-provisioned switch can be plugged back in and
re-run: leave the label password empty (it's on the shared password).

## Setup

```
cp config/tsw.config.example.json config/tsw.config.json
python3 tsw_app.py                                        # opens http://127.0.0.1:8007
```

Download the firmware image named in `firmware.bin_path` from
[Teltonika](https://wiki.teltonika-networks.com/view/TSW202_Firmware_Downloads)
into `firmware/` (gitignored). It is only needed for a switch that arrives below
the floor, so the page reports a missing image as a note rather than blocking
Configure.

On the bench PC you don't run this by hand — the top-level launcher
(`Start Bench Tools.bat` / `start-bench.sh`) sets up the shared `.venv` and
starts every tool together.

There is also a single-device CLI:

```
python3 tsw_configure.py --label-password 'Xy7Kp2Lm9Qa'
```

## Before the first real unit

Every device fact here comes from Teltonika's docs rather than a switch on a
bench, and the TSW2 firmware line is a thinner build than the RUTM/OTD RutOS.
`probe-tsw.py` reads back the three things that were assumed and says whether
each holds:

```
../.venv/bin/python probe-tsw.py 192.168.1.2
```

The code is written to survive all three being wrong in the recoverable
direction — the model falls back to the board info when `mnfinfo` is absent, the
NTP section is created when missing, and a wrong interface name fails the LAN
step loudly rather than silently no-opping. What the probe protects against is
the *un*recoverable one: `set_admin_password` posts to the RutOS
`change_password_firstlogin` endpoint, and if this firmware doesn't serve it,
every run fails at that step with a clear error. That is the first thing to
check if the first unit refuses.

## Shared code

This folder must sit next to `bench-core`, the shared local package it installs
(`-e ./bench-core[ui]` from the bench root). That package holds the device
client (`TeltonikaClient` — REST + SSH/UCI, firmware, the LAN move) and the
bench-UI base (`bench_ui`) that drives the detection loop, routes, and
WebSocket. `tsw_configure.py` adds only what a switch needs on top: the NTP
*server* setter (no other tool sets one — the routers get time over their WAN),
the firmware floor, and a `verify_configuration` that doesn't emit the base's
SIM / RMS / Tailscale rows. `tsw_app.py` is a thin `BenchConfigurator` subclass.

Per-device logs land in `logs/`, one JSON per switch named after its **serial**
(there's no hostname to name them after) plus a rolling `tsw-config.log`.

## Not in scope

VLANs, per-port configuration, PoE budget, STP and syslog (TEC-843), TSW101 and
TSW212 (TEC-844), and RMS / Tailscale — a switch has no WAN uplink on the bench,
and neither is part of the TEC-791 baseline.

**PROFINET-enabled units are not supported and have no issue open yet.** They
ship on `0.0.0.0` rather than `192.168.1.2`, expecting to be addressed over
PROFINET DCP, so detection here cannot find them at all — the page simply never
shows a switch. Such a unit needs an address assigned with Teltonika's Windows
configurator first, after which the normal flow works. Worth confirming with
procurement whether that SKU is in the supply chain before building anything for
it.
