# OTD500 Configurator

Bench tool for provisioning Teltonika OTD500 gateways, **one at a time**. Same
plug-in → configure → unplug flow as the other bench tools: plug a device in, the
UI reads its LAN MAC, you type the **site name** and the **factory label
password**, and it provisions. Nothing is saved beyond the session history.

## What it does per device

1. Login with the label password (falls back to the shared password for re-runs).
   The label password is also kept on bench-central, keyed to the serial — see
   "Factory passwords" below.
2. Set the admin/root password to the shared default.
3. Hostname / device name → `otd-<site_name>`.
4. Timezone, SIM failover rules (if enabled), then SIM 4G-only (if enabled).
5. Firmware upgrade (local `.bin`, FOTA, defer-to-RMS, or none).
6. Per-operator data limits: deploy the on-device quota-sync script (after the
  firmware step — a flash in progress would wipe it).
7. Enable RMS on-device + register the unit in the RMS cloud (serial+MAC) when
  `rms.api_token` + `rms.company_id` are set in the config, then assign the
   RMS Management pack license (`rms.pack`, the UI's "Set pack" action) so the
   device doesn't run on 30-day credits.
8. Join Tailscale (per-device minted key or a static one).
9. Verify every setting by reading it back off the device.

Per-device logs land in `logs/` (one JSON per device + a rolling `otd-config.log`).

## SIM failover and per-operator data limits

Both come from the `sim_switch` block in `site.config.json` (TEC-359). Set
`"enabled": false` to skip the `sim-switch` and `quota-sync` steps entirely — the
verification table then shows their rows as skipped rather than passing.

The whole block is validated *before* the run logs in, so a typo fails the device
with a message naming the key rather than leaving it half-configured — the
failover rules would otherwise be committed several steps before the operator
table is even parsed.

### Settings

| Key | Default | Meaning |
|---|---|---|
| `enabled` | — | Run the failover + data-limit steps at all |
| `check_interval` | 30 | Seconds between checks of the active SIM |
| `check_count` | 5 | Consecutive failed checks before switching |
| `weak_signal_dbm` | -105 | RSSI threshold; below this counts as a failed check |
| `icmp_host` | 8.8.8.8 | Ping target for the data check |
| `operators[]` | — | ICCID prefix → data limit table (below) |
| `unknown_operator` | 1330000 MB, day 1, on | Fallback for a SIM matching no prefix |

`check_interval`, `check_count`, `weak_signal_dbm` and `icmp_host` are the only
per-site tuning; every other option in the rule set is fixed policy in
`bench_core.sim_switch_options()`. An unset or empty value falls back to the
default above. The three numeric ones must be whole numbers, and `icmp_host` must
be an IP address or a hostname — a typo there would otherwise land in
`data_fail_host` and leave a check that can never succeed, so it is rejected
before the run starts.

Each `operators` entry needs a `name` (a label, echoed in the device's log line),
`iccid_prefixes` (one or more digit strings — each becomes a `prefix*` match) and
`data_limit_mb`. `reset_day` is the operator's billing reset day and defaults to
1; `enabled` defaults to true, and setting it false means *no limit at all*, for a
practically-unlimited plan. `data_limit_mb` is still required to be a real value
on those rows, so enabling one later can't hand a SIM a 0 MB limit. `mccmnc` is
optional and only ends up as a comment in the generated script.

`data_limit_mb` is in **device MB, which are binary** — 3000000 shows up in the
WebUI as 2.86 TB, so the configured values keep real margin against a decimal-TB
plan.

### Failover logic

The device's own `sim_switch` service does the switching; the bench only writes
the rules, as one `config sim` section per slot. That makes the step pure UCI plus
a service restart: it needs **no SIM inserted** (the normal bench state), never
reads modem state, and never touches `simcard.@sim[].primary`. Restarting the
service does not bounce the modem or drop back to the primary SIM.

Slots 1 and 2 each get the same rule set — failover is symmetric, so it doesn't
matter which SIM is active. A switch is triggered by weak signal (`on_signal`,
`weak_signal`), no network (`no_network`), the network refusing the SIM
(`denied`), no SIM in the slot (`sim_not_ready`), the slot's data limit being
reached (`data_limit`), or the ICMP check failing (`data_fail=2`,
`data_fail_host`, `data_fail_timeout=3`). SMS limit and roaming are deliberately
*not* triggers. `order` is the slot number, so slot 1 is tried first.

Failover is **sticky**: `enable_back=0`, so a device that moves to slot 2 stays
there instead of flapping back to a marginal SIM. Going back to slot 1 is a
deliberate act by an operator, not something the device decides on its own.

Slot 3 (the eSIM) gets a section with `enabled=0` — present, so the device's
config is complete, but never switched to.

Two details matter when re-provisioning a device that was configured before.
Sections are matched to slots by their `position` option rather than file
order, so a re-run updates the existing rules instead of appending a second set.
Anything left unclaimed — a duplicate position, or a position beyond the slots
this device has — is treated as an orphan and disabled with a warning, since a
leftover enabled section would keep failing over on stale rules. The modem id is
read from the device's config (existing `sim_switch` sections first, then
`simcard`, then `2-1` as a last resort) rather than from the modem, so it is
correct with no SIM in.

The `sim_switch` option names are undocumented and were verified on **firmware
07.22.3**. On any other version the run logs a warning and continues, because
Teltonika renames these options between releases and `uci set` silently accepts
a name the service ignores. Re-check with `uci export sim_switch` before trusting
a new firmware.

### Data-limit logic

Which limit a slot needs depends on whose SIM is in it, and SIMs are inserted in
the field and swapped between slots — so the decision can't be made on the bench.
The bench installs a script that the device runs itself instead, at boot and
every 10 minutes, re-deriving the limits from what is actually in the slots.

Per run, for each of slots 1 and 2, it reads that slot's ICCID from
`simcard.@sim[N].iccid`, finding the section by its `position` rather than by file
order. That value is readable for the *inactive* slots too — unlike the IMSI,
which only the active SIM reports — and that is what makes a slot-by-slot
decision possible at all. It then matches the prefix against the operator table
and writes `enabled`, `data_limit`, `period` (monthly) and `reset_day` to that
slot's `quota_limit` section. A slot with no section yet gets one created with the
same fields the WebUI writes. The eSIM slot's limit is forced off if it has a
section, and `event_sent` — `quota_limit`'s runtime state — is never touched.

A SIM matching no prefix, and an empty slot, both get `unknown_operator`:
deliberately the smallest plan's limit, so a SIM we can't identify can never run
up the biggest plan's worth of data.

The script only writes when a value actually differs, commits and restarts
`quota_limit` only if something changed, and logs every change plus a summary
line, so the 10-minute tick is silent until a SIM is swapped:

```
ssh root@192.168.1.1 'logread | grep kela-quota-sync'
... kela-quota-sync: mob1s1a1.data_limit: '' -> '2660000'
... kela-quota-sync: applied: slot1=pelephone slot2=unknown
```

A lock directory under `/tmp` keeps the boot run and a cron tick from
interleaving two `uci set`/`commit` sequences.

The script is generated from the config, so change the bench config and
re-provision rather than editing the copy on the device.

### On the device

The script lands as `/usr/local/bin/kela-quota-sync`, the boot hook as
`/etc/init.d/kela-quota-sync` (`START=99`, and the run is backgrounded behind a
90 s sleep so it never holds up boot and the modem has time to publish a new
card's ICCID), and the schedule in `/etc/crontabs/root`. It is not in `/usr/bin`:
the OTD500's rootfs is a read-only squashfs and the writable UBI volume is
overlaid onto `/etc` and `/usr/local` only, so a write to `/usr/bin` fails with
"Read-only file system".

A keep-settings firmware upgrade would otherwise wipe all three: RutOS collects
what to preserve from `/etc/sysupgrade.conf` plus `/lib/upgrade/keep.d/*`, and
those cover `/etc/config/` and `/etc/crontabs/` but neither `/usr/local` nor
`/etc/init.d` — so the cron entry would survive and go on calling a script that
is no longer there, with limits silently frozen at their last values. The install
step therefore adds its files to `/etc/sysupgrade.conf`, including that file
*itself*: it isn't in `keep.d` either, so without that line the block would
survive one upgrade and be gone for the next. Confirm on a device with

```
ssh root@192.168.1.1 'sysupgrade -b /tmp/b.tar.gz && tar -tzf /tmp/b.tar.gz | grep kela'
```

which builds the keep archive without flashing anything.

The pipeline still installs quota-sync *after* the firmware step, since a device
being flashed on the bench has nothing to preserve yet.

Verification reads all of it back: one row per slot comparing the conditions that
decide *whether* it fails over (`enabled`, `interval`, `retry_count`,
`weak_signal`, `enable_back`, `data_fail_host`), plus rows for the script and boot
hook being installed, the cron entry, and the keep list — that last one naming
any path a firmware upgrade would drop.

## Factory passwords (TEC-845)

The label password this unit shipped with is sent to bench-central as the run
finishes, keyed to its serial, and kept there forever. That value is what the
device reverts to on a factory reset, so keeping it is the difference between
recovering a unit reset in the field and losing access to it.

It never enters the run record — it rides its own queue
(`logs/outbox-labels/`) into its own store, because a device has one factory
password however many times it is run. Nothing is kept when the password field
is left empty (a re-run on the shared password), or when what was typed *is*
the shared password. Read them back in the **Factory passwords** panel on the
bench-central dashboard.

## Setup

```
cp config/site.config.example.json config/site.config.json   # then fill in RMS + Tailscale secrets
python3 otd_app.py                                            # opens http://127.0.0.1:8003
```

On the bench PC you don't run these by hand — the top-level launcher
(`Start Bench Tools.bat` / `start-bench.sh`) sets up the shared `.venv` and starts
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