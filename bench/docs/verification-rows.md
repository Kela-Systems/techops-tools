# Verification rows: what each one actually proves

Every bench tool ends a run by re-reading the device and emitting rows shaped
`{item, expected, actual, ok}`. Those rows are the whole basis for "this unit is
ready to ship" — and since TEC-348 they are also what a mutation-free **Verify**
pass produces on a finished unit, and what TEC-352 will print a label off.

So the interesting question about a row is not whether it is green. It is
**what fact makes it green**.

The reason this document exists: the first TSW202 off the bench shipped running
on a UTC clock with a green timezone row. The check read back
`system.ntp.zoneName`, the same UCI option the tool had just written — and `uci
set` creates an option whether or not the device consumes it. The row could not
fail. Four green rows, one wrong switch.

## The three classes

**Effect-based.** The row reads a consequence the device produced, which the
tool never wrote. `date +%z` for the clock, `hostname` for the running kernel
name, `ps w | grep ntpd` for what the daemon is really polling, `AT+COPS?` for
the radio the modem attached on, an authenticated login for a password, the
address we actually reached the device on. These can fail on a real device that
was written to successfully, which is exactly why they are worth having.

**Read-back.** The row reads the setting the tool wrote, over the device's own
config store or API. It proves the write landed and survived — not nothing, and
sometimes all that is available (a camera's NTP server lives behind a vendor
RPC and nowhere else). But it cannot catch a device that stores a setting and
ignores it, which is the failure that started all of this.

**Known gap.** The row is read-back and the effect is either unobservable from
the bench (a setting that only matters at the next firmware upgrade) or not
worth the run time (waiting for an NTP sync). Listed below with what would
close it, so the next person doesn't have to rediscover the reasoning.

Two rules that follow:

- **A row must be able to fail.** If it can't, it is decoration with the
  authority of a green tick. Convert it or delete it.
- **When the evidence isn't there, say so.** `ok=None` renders as amber
  "skipped" and is the honest answer for "cannot confirm". A row that rounds "I
  couldn't check" up to a pass is the tautology in a new place.

## Teltonika OTD500 and RUTM08

`TeltonikaClient.verify_configuration` in
[`bench-core/src/bench_core/__init__.py`](../bench-core/src/bench_core/__init__.py),
shared by both tools. The RUTM08 has no modem, so `rutm_configure` filters the
SIM row out.

| Row | Evidence | Class |
| --- | --- | --- |
| `admin/root password` | We are authenticated and SSH is answering on the shared password. Neither side of the row carries a password (TEC-349). | effect-based |
| `hostname` | `hostname` (the running kernel name — what the device reports to syslog, DHCP and RMS) **and** the UCI option, which is what survives a reboot. Converted from UCI-only in TEC-348; `test_hostname_applied.py`. | effect-based |
| `timezone` | `date +%z` against what the zone means today, **and** `system.ntp.zoneName` for what the WebUI renders. `test_timezone_applied.py`. | effect-based |
| `SIM 4G-only` | `AT+COPS?` access-technology field — the radio the modem is attached on — **and** the `simcard.@sim[N].service` intent. Amber when the modem is not attached. Converted from UCI-only in TEC-348; `test_sim_4g_effect.py`. | effect-based, degrades |
| `SIM switch slot 1/2` | `uci show sim_switch`: the failover rule options we wrote. | read-back |
| `quota sync script` | `[ -x /sbin/quota-sync ]` and the boot hook: files present and executable on the device's filesystem. | effect-based |
| `quota sync cron` | The exact cron line in `/etc/crontabs/root`. | read-back |
| `quota sync survives upgrade` | The paths listed in `/etc/sysupgrade.conf`. | known gap |
| `RMS` | `ubus call <rms object> status` for a live connection state, then the RMS cloud API as the authority. `enable=1` alone is not a pass. | effect-based |
| `Tailscale` | `tailscale ip -4` returning a `100.x` node address, i.e. the unit joined the tailnet. | effect-based |
| `eSIM profile` | `gsmctl --esim-list` returning anything. | known gap |
| `firmware` | `/etc/version` — the firmware that is running. | effect-based |
| `LAN IP` (verify only) | `lan_ip_check`: the address the session actually reached, **and** the configured address it will come back on after a reboot. | effect-based |
| `prior run` (verify only) | `bench_core/history.py`: whether this unit has a recorded configure run at all. Bookkeeping, not a device check — it fails when nothing is found rather than dropping the checks that needed it. | n/a |

## Teltonika TSW202

`TswClient.verify_configuration` in
[`tsw-config-ui/tsw_configure.py`](../tsw-config-ui/tsw_configure.py). Nothing
about a TSW202's intended state is per-unit, so every expectation comes from the
station config.

| Row | Evidence | Class |
| --- | --- | --- |
| `admin/root password` | As above. | effect-based |
| `timezone` | The shared `timezone_check` — the row this whole document is about. | effect-based |
| `NTP server` | Three facts: every server in the `system` package (not just the option we wrote, so a leftover pool entry is found), the enable flag, and the servers the **running** ntpd was started with, off its command line. | effect-based |
| `firmware` | `/etc/version` against the firmware floor. | effect-based |
| `LAN IP` (verify only) | `lan_ip_check`, as above. | effect-based |

## Provision-ISR IP speaker

`SpeakerClient.verify_configuration` in
[`speaker-config-ui/speaker_client.py`](../speaker-config-ui/speaker_client.py),
plus the rows `verify_speaker` adds.

| Row | Evidence | Class |
| --- | --- | --- |
| `reached at` (verify only) | The speaker answered at the target static address — the same evidence the configure run's IP-move row waits for. | effect-based |
| `admin password` | The login is the check: a speaker that still answers to the factory password fails, and the verify path says which password it is on. | effect-based |
| `NTP server` | The device's own settings page (`ntpserverstr`, `timesetmode`). | known gap |
| `media file` | The speaker's inventory of what is stored in the slot, which the upload produced — an empty slot after a failed upload shows up. Playback is not exercised. | effect-based |
| `static IP` | The network table (`netip`, `dhcp=0`), corroborated by `reached at` / the configure run's move. | read-back |
| `netmask`, `gateway` | The network table. | read-back |
| `static IP` (configure only) | `set_static_ip` follows the speaker to its new address and waits for the port to open. | effect-based |

## Raythink thermal camera

`RaythinkCameraClient.verify_configuration` in
[`raythink-config-ui/raythink_camera.py`](../raythink-config-ui/raythink_camera.py),
plus the rows `verify_camera` adds.

| Row | Evidence | Class |
| --- | --- | --- |
| `reached at` (verify only) | The camera answered at the address its configure run assigned. With no recorded address the row **fails** — reading the camera's own address and calling it expected would pass by construction. | effect-based |
| `config profile` | A profile is a full config export; on a configure run the row counts the tables the camera accepted, on a verify pass it is amber and names the profile from the record. | known gap |
| `admin password` | The RPC2 login is the check, with the factory password tried second so the row can say which one it is on. | effect-based |
| `ONVIF login (admin)` | An authenticated ONVIF call — the credential the VMS will actually use, which is a separate account from the web login. | effect-based |
| `NTP server` | `configManager.getConfig` for the NTP table (`Address`, `Enable`). | known gap |
| `static IP` | The network table: the address **and** `DhcpEnable` off, so a lease that happens to match today isn't mistaken for the assignment. Corroborated by `reached at`. | read-back |
| `subnet mask`, `gateway` | The network table. | read-back |
| `DHCP` (DHCP mode) | The camera holds a lease and has an address. | effect-based |
| `subnet mask`, `gateway` (DHCP mode) | Reported, never asserted: the DHCP server chose them, so there is nothing of ours to compare against (`ok=None` by design). | n/a |

## The known gaps, and what would close each

**`quota sync survives upgrade` (OTD500).** The effect — the script still being
there after a keep-settings upgrade — cannot be observed without performing an
upgrade, which a verify pass must not do. The listed paths are the best evidence
available. Closing it would mean a firmware-upgrade soak test, not a bench row.

**`eSIM profile` (OTD500).** The row passes on `gsmctl --esim-list` returning
anything at all; it does not compare the ICCID against the activation code the
run used, and does not check the profile is enabled. Closing it needs the
`--esim-list` output format pinned against a real OTD500 with a loaded profile.
Worth doing when eSIM stations become common.

**`NTP server` (speaker, camera).** Both devices expose the server and an enable
flag over their own API and nothing else — no daemon to inspect, no offset to
read. The effect equivalent would be reading the device clock and comparing it
to the station's, which needs a clock read on each API that has not been found
yet. Both devices *are* checked for reachability at their assigned address,
which is an effect, so a wholly unconfigured unit does not slip through.

**`config profile` (camera).** A profile is an exported blob of vendor config
tables; there is no per-field expectation to check on a verify pass, so the row
names the profile from the configure record and stays amber. Closing it means
diffing the camera's live tables against the exported profile — feasible, and
its own piece of work.

## Adding or changing a row

1. Ask what fact makes it green, and whether that fact could be false on a
   device the tool wrote to successfully. If it couldn't, the row is a
   tautology.
2. Prefer a consequence over a setting. Running process, running clock, radio
   state, filesystem, an address that answered, a credential that
   authenticated.
3. Read the setting **as well**, when it says something different: the running
   value is today, the stored value is after the next reboot. Both rows above
   that check two things do it for that reason.
4. If the evidence isn't available, emit `ok=None` with a reason. Never a pass.
5. Write the negative test first, driving a fake device whose *effect* is wrong
   while its *config* is right —
   [`test_timezone_applied.py`](../bench-core/tests/test_timezone_applied.py) is
   the pattern, and
   [`test_hostname_applied.py`](../bench-core/tests/test_hostname_applied.py)
   and [`test_sim_4g_effect.py`](../bench-core/tests/test_sim_4g_effect.py)
   follow it. A converted row with no test that fails on the old code has not
   been converted.
6. On the verify path, the row must be a read. `test_verify_only_is_mutation_free.py`
   asserts no verify pipeline issues a mutating command, and the read-only
   client refuses one at runtime — a new read that trips the deny-list belongs
   in that file's `ALLOWED_READS`.
7. Update the table here.
