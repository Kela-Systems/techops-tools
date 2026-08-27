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

**The station needs an address on** `192.168.88.x`**.** The RUTM08 renews the
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



## What the first unit on the bench settled

Three device assumptions were taken from Teltonika's docs rather than hardware,
because the TSW2 firmware line is a thinner build than the RUTM/OTD RutOS.
Verified on a real TSW202 (SN 6010620710, `TSW2_R_00.01.07.1`):

- `ubus call mnfinfo get` **is there.** Model, serial and MAC come straight off
it, so the board-info fallback in `TswClient.get_identity` is belt-and-braces
rather than the main path. The model string is `TSW20200XXXX`, which the
prefix match in `assert_device_model` handles.
- **NTP is at** `system.ntp.`*, the same section `set_timezone` writes into.
Confirmed end to end on the bench unit: `system.ntp.server='192.168.88.10'` was
the only server configured, and `ps` showed `/usr/sbin/ntpd -n -N -p
192.168.88.10`, i.e. the daemon really was polling it and nothing else. The
WebUI's *Time servers* table showed four `time*.google.com` rows on a page
loaded around the same time; `uci show system` and the live process are the
ground truth, so treat that table as stale unless a hard refresh still shows it.
- **A committed timezone is not an applied timezone.** The same unit held
`system.system.timezone` and `system.ntp.zoneName` exactly right and ran on a
`+0000` clock. libc reads `/etc/TZ`, and nothing writes it until the system
config is reloaded, so `set_timezone` now runs `/etc/init.d/system reload` and
falls back to writing the TZ file itself if the clock still hasn't moved. This
was never TSW-specific — the routers were missing the same step.
- **The zone name lives in two places, and the WebUI reads the one we weren't
writing.** With the clock fixed and on `+0300`, the Date & Time page still
displayed `UTC`. Setting the zone in the UI and diffing `uci show system`
produced a third key: `system.system.zoneName`. So `set_timezone` now writes all
three — the POSIX string for libc, `system.system.zoneName` for the WebUI, and
`system.ntp.zoneName` for the timeserver section. This matters beyond cosmetics:
an operator opening that page and pressing **Save & Apply** while it displayed
`UTC` would have written UTC straight back over the correct clock, long after
the bench stopped watching.
- **The management address is NOT on** `network.lan`**.** This one was wrong. The
switch answers `uci: Invalid argument` to `uci set network.lan.ipaddr` —
what UCI says when a section doesn't resolve. `move_lan` now asks the device
which `network` section holds the address it was reached on
(`TeltonikaClient.mgmt_section`) instead of hard-coding `lan`, so this is
fixed for the whole family rather than special-cased here. Where `lan` *is*
the section, as on every RutOS router, nothing changes.

Also confirmed: `set_admin_password` works, i.e. this firmware does serve the
RutOS `change_password_firstlogin` endpoint. That was the assumption with no
recoverable fallback, so it was the one worth worrying about.

`probe-tsw.py` re-checks all of the above against any unit, read-only:

```
../.venv/bin/python probe-tsw.py 192.168.1.2
```

Worth running against the first TSW101 or TSW212 if that scope lands (TEC-844),
since the section name is exactly the kind of thing that differs per model.

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