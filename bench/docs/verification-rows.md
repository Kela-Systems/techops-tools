# Verification rows: what each one actually proves

Every bench tool ends a run by re-reading the device and emitting rows shaped
`{item, expected, actual, ok}`. Those rows are the whole basis for "this unit is
ready to ship" — and since TEC-348 they are also what a mutation-free **Verify**
pass produces on a finished unit, and since TEC-352 what decides whether a QA
label prints at all ([qa-labels.md](qa-labels.md)).

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
| `quota sync script` | Three things: the script and its init script present and executable, **and** the `/etc/rc.d/S99…` symlink that actually runs it at boot. The last one is separate on purpose — a keep-settings upgrade restores the files but regenerates `/etc/rc.d`, so the hook can be installed and disabled at once (TEC-861). | effect-based |
| `quota sync cron` | The exact cron line in `/etc/crontabs/root`. | read-back |
| `quota sync survives upgrade` | The paths listed in `/etc/sysupgrade.conf`. | known gap |
| `RMS` | `ubus call <rms object> status` for a live connection state, then the RMS cloud API as the authority. `enable=1` alone is not a pass. | effect-based |
| `Tailscale` | `tailscale ip -4` returning a `100.x` node address, i.e. the unit joined the tailnet. | effect-based |
| `eSIM profile` | `gsmctl --esim-list` returning anything. | known gap |
| `firmware` | `/etc/version` — the firmware that is running. | effect-based |
| `LAN IP` (verify only) | `lan_ip_check`: the address the session actually reached, **and** the configured address it will come back on after a reboot. | effect-based |
| `NTP client` | Four facts: every server in **both** RutOS time subsystems (`system` and the `ntpclient` package, so a stock `time2.google.com` is found), both enable flags, and the poll interval. See the known gap below for why there is no sync half. `test_ntp_client_applied.py`. | read-back |
| `NTP daemon` | Whether the RUNNING client has read the config the row above checks. RutOS starts it as `ntpclient -s -l`, with no server on the command line, so there is nothing to read the way the `ntpd` row reads `-p <server>` — instead the process start time (the mtime of `/proc/<pid>`) is compared against the config file's. A daemon older than the file is still polling what it read at boot. Amber when the device will not report either timestamp. `test_ntp_client_applied.py`. | effect-based |
| `DHCP pool` (OTD500) | Not the two options read back — that cannot fail. Whether the pool can still lease the address the downstream router holds statically, computed from `start`/`limit`. A pool starting at `.1` fails with both options committed exactly as written. `test_ntp_path_applied.py`. | read-back |
| `WAN address` (RUTM08, opt-in) | The address **and** `proto=static`, so an interface left on `dhcp` with a stale `ipaddr` does not read back correct while taking whatever the site hands it. | read-back |
| `NTP forward` (RUTM08, opt-in) | The redirect's options, **and** whether the wired WAN interface is actually in the `wan` firewall zone the rule matches on — TEC-857's prerequisite, after the demo unit's zone turned out to list only the mobile interfaces. Amber-tolerant on that half: an unreadable zone does not fail the row. | read-back |
| `prior run` (verify only) | `bench_core/history.py`: whether this unit has a recorded configure run at all. Bookkeeping, not a device check — it fails when nothing is found rather than dropping the checks that needed it. | n/a |

## Teltonika TSW202

`TswClient.verify_configuration` in
[`tsw-config-ui/tsw_configure.py`](../tsw-config-ui/tsw_configure.py). Everything
but the address comes from the station config; the address is per-unit since
TEC-848 and is recovered from that switch's own configure record.

| Row | Evidence | Class |
| --- | --- | --- |
| `admin/root password` | As above. | effect-based |
| `timezone` | The shared `timezone_check` — the row this whole document is about. | effect-based |
| `NTP server` | Three facts: every server the device has (not just the option we wrote, so a leftover pool entry is found), the enable flag, and the servers the **running** ntpd was started with, off its command line. Since TEC-857 the server scan covers the `ntpclient` package too — and some TSW202 builds do have it, holding the factory pool where nothing in `system` reaches it (see the second note below). See the note below on what a half-green reading of this row means. | effect-based |
| `firmware` | `/etc/version` against the firmware floor. | effect-based |
| `LAN IP` (verify only, static) | `lan_ip_check`, as above. | effect-based |
| `LAN IP` (verify only, DHCP) | `lan_dhcp_check`: there is no address to hold the switch to, so the answerable question is whether it is configured to ask for one. A leftover static `ipaddr` alongside `proto=dhcp` **fails** — that switch falls back to the bench address when no lease arrives, which is a second device shipped under one record. | effect-based |
| `LAN IP` (configure only, DHCP) | `move_lan_dhcp` finds the switch again by MAC on the configured subnets and waits for it to answer. | effect-based |

**Reading a half-green `NTP server` row, and why it is never about the bench.**
A switch off the bench reported `configured 192.168.88.10,
time1..4.google.com (enabled=1); ntpd polling 192.168.88.10`. That was read as
the row demanding something the bench cannot supply, since `192.168.88.10` is on
the assembly network — but **none of this row's three facts leaves the switch**.
All three are local reads over SSH: a `uci show system` parse, a `uci get` and a
`ps` grep for the live daemon's `-p` arguments. Unreachability cannot fail it,
by construction, and a red row here is always a real finding on the device.

The finding that time was a write-path gap. `set_ntp_server` cleared the
`system.ntp.server` *list*, which is what sysntpd builds its command line from —
hence the green daemon half — while the factory `time1-4.google.com` live in a
section **per server**, the shape the WebUI renders. Detection had been widened
to find those (`configured_ntp_servers` scans every `server`/`hostname` option in
the package); the write never was, so every switch kept the stock pool. It now
deletes those sections too, back to front, the way `set_ntp_client` already did
for the routers under TEC-857. Harmless while the daemon ignores them, but they
are one Save & Apply or config migration from being live again, and a failover
timeout each on a site with no internet.

A switch provisioned before that fix still carries them; it is cleared by a
re-run, or by hand with `while uci -q delete system.@ntpserver[0]; do :; done &&
uci commit system && /etc/init.d/sysntpd restart`.

**The factory pool has a second hiding place.**
A later switch failed this row with a `system` package that was clean:
`system.ntp.server` was ours alone, `enabled=1`, and `ntpd` polled ours — but
`uci show ntpclient` held `time1-4.google.com` in a section each, plus its own
enabled client on an 86400s interval. The row reads both packages, so it was
right; `set_ntp_server` only wrote one, so a re-run never cleared them. It now
points `ntpclient` at the same server when the package exists (reusing the
routers' `set_ntp_client`), and leaves a build without one untouched.

The general lesson is the one above, twice: detection was widened to find the
stock pool before the write was, so the row went red on a switch that no number
of re-runs could fix. When a check reads more places than the setter writes,
the gap surfaces as an unfixable red row rather than a silent pass.

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
| `DHCP` (verify only, DHCP mode) | The speaker is set to take a lease, and the address it currently holds is reported alongside as a finding. There is no `reached at` row under this mode: no address was ever promised, so there is nothing to hold it to. | read-back |
| `DHCP` (configure only, DHCP mode) | `set_dhcp` finds the speaker again by MAC on the scan subnets and waits for it to answer. | effect-based |

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

## Magos AR-300 radar

[`magos-config-ui/magos_verify.py`](../magos-config-ui/magos_verify.py) — the row
builders both the configure run's re-read and the Verify pass are made of
(TEC-851). Before that this tool ended a run with a single reachability probe
and no rows at all.

There is deliberately **no password row**. This tool never changes the radar's
credentials — the station password *is* the factory password unless an operator
changed it in settings — so a row saying "we authenticated" could only ever be
green, which rule 1 below rules out. A login that fails aborts the pass with an
error naming the credentials instead, which is louder than a red row.

| Row | Evidence | Class |
| --- | --- | --- |
| `reached at` | The address the session got an answer on. On a configure run `verify_device_at` waits for the radar to come back at its new address; on a verify pass it is where the sweep found it. With no recorded address the row **fails** rather than comparing the radar to itself. | effect-based |
| `NTP server` | `/dshb/v1/system`: `ntpServer`, **and** `ntpAutomatic` being off, so a unit that went back to picking its own server is not passed on a field it no longer reads. | read-back |
| `timezone` | `/dshb/v1/system`'s `timezone`. | read-back |
| `RF channel` | `current_variant`: the `variant` in the `radar_settings` frame the `/radar/v1/detections` WebSocket pushes on connect — the same socket `set_channel` wrote it over — trusted only when the radar also lists it in `listVariants`. Nothing is sent on the socket. Amber on firmware that lists variants but pushes no such frame, or when the socket cannot be opened. | read-back |
| `static IP` | `/dshb/v1/networking`: the address **and** `ip4Method == "manual"`, so a lease that happens to match today isn't mistaken for the assignment. Corroborated by `reached at`. Handles both networking schemas (flat CIDR, per-interface). | read-back |
| `netmask`, `gateway`, `DNS` | The same networking object. DNS is a list on the device and the tool writes one server, so the row checks the primary and shows them all. | read-back |
| `prior run` (verify only) | As the Teltonika table: whether this radar has a recorded configure run at all. | n/a |
| `re-read after configuring` (configure only) | Not a check — the amber row a configure run emits *instead of* the rows below when the radar could not be re-read on its new address. By then it has already been provisioned, so a re-read that could not happen must not fail the run. | n/a |

## Magos APU

The same builders, plus the things only an APU has. Its firmware, timezone,
NTP, clock and networking rows are the radar's, read off the same dashboard API.

| Row | Evidence | Class |
| --- | --- | --- |
| `firmware` | `/systemStatus`'s `softwareVersion` against the 3.1.2 floor the multi-radar assignment needs. A **row** on a verify pass, where a configure run refuses the unit outright: a finished APU on old firmware is a QA finding, and declining to look at it is not reporting it. | effect-based |
| `controlled radars` | `GET /apu/v1/settings` — the assignment `set_radars` wrote — compared as a set against the radars in the configure record, since the firmware may hand the array back in any order. Confirmed answering on 3.1.2-rc5; amber on firmware where it doesn't. | read-back, degrades |
| `range gates / detector threshold` | The same `GET /apu/v1/settings`: `range_gates` and `detector_threshold` must be `null` (disabled — what `set_radars` writes) on every radar entry; the row names each radar and field still set. Amber when the settings cannot be read or no radars were assigned. | read-back, degrades |
| `reached at`, `NTP server`, `timezone`, `static IP`, `netmask`, `gateway`, `DNS` | As the radar table above. | see above |

## PLANET IGS-4215 PoE switch

`PlanetClient.verify_configuration` in
[`planet-config-ui/planet_configure.py`](../planet-config-ui/planet_configure.py),
plus the `lan-ip` row `configure_planet` adds when it moves the switch.

| Row | Evidence | Class |
| --- | --- | --- |
| `firmware` | The web UI's system page against the floor, compared with this tool's own version parse — `bench_core.fw_version_at_least` reads `1.305b251017` and `1.305b260324` as the same `(1, 305)` and would pass a switch whose SSH server cannot open a CLI. The only row read over HTTP, so it is also the only one an expired web session can break; the session is renewed rather than parsed. | read-back |
| `ntp-server` | `show sntp`. The switch cannot reach `192.168.88.10` from the bench subnet, so what is provable here is the configuration, not a synced clock. | read-back |
| `timezone` | `show clock`, which prints the *offset* the switch is running on (`UTC+3`) rather than the acronym it was given. That matters: the acronym field silently ignores anything over 4 characters, and a rejected `local` leaves the factory `+8` — which this row catches. | effect-based |
| `poe-port-N` (only with `poe.managed`) | **Not emitted since 2026-09-14**: the bench stopped setting PoE, so there is nothing of ours to read back and the switch is not asked. With `poe.managed` on: `show poe`, the switch's own PSE table: enable state, and the limit in the deci-watts the hardware holds. A plan sent in watts by mistake reads back as 4.5 W here, not 45 W. Only gi1–gi8 are checked — the switch has no PSE on the rest and never reports them. | effect-based |
| `port-name-N` | `show running-config` (there is no `show interface description` on this firmware). Covers gi9–gi10 too, which are named but never powered. | read-back |
| `telnet` | The absence of `ip telnet` in the running config. | read-back |
| `lan-ip` (configure only) | The switch answered on `192.168.88.3` after the move — the session dies mid-command by design, so reaching it again is the evidence. | effect-based |

**No password row, because the login is one.** The run authenticates with the
shared password before any of the above can be read, and a switch still on its
derived factory password refuses. That is the same argument the speaker's
`admin password` row makes explicitly; here it is structural.

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

Before writing that clock read, though, read the Magos entry below: on a bench
whose units cannot reach the NTP server until assembly, the clock is not
evidence of anything, and the row it produces is worse than the gap.

**`config profile` (camera).** A profile is an exported blob of vendor config
tables; there is no per-field expectation to check on a verify pass, so the row
names the profile from the configure record and stays amber. Closing it means
diffing the camera's live tables against the exported profile — feasible, and
its own piece of work.

**`RF channel` (Magos radar) — closed 2026-10-08.** This was the worst-shaped
gap on the bench, because the fault it catches — two neighbouring radars
transmitting on one frequency — is invisible everywhere else and only shows up
as degraded detection on site. The read was on the socket all along: on
connect, `/radar/v1/detections` pushes a `radar_settings` frame whose payload
carries `variant` (confirmed on an AR-300 running 3.1.0, where no REST payload
has it — `/systemStatus` lacks the field, `/radar/v1/sensors` is 403 in Raw
mode, `/radar/v1/remoteProductInfo` is 404). `current_variant` now listens for
that frame, sends nothing, and still trusts only a value the unit lists in
`listVariants`. The row stays amber, not green, on firmware that lists variants
but pushes no settings frame — a guessed pass here would be worse than no row,
because somebody would ship on it.

**`NTP client` (OTD500, RUTM08), and why the bench cannot tell you a pair
synced.** TEC-857 asks for a verify mode that reports *synced* rather than
merely configured, distinguishes NTP-sourced time from the GSM modem clock, and
confirms `date -u` agrees across the OTD500, the router and the time server.
None of that is a bench row, for a reason that is structural rather than
temporary: the OTD500 is **upstream** of the router and reaches the time server
only through a port forward on it, and this bench provisions **one device at a
time** — an OTD500 is never cabled behind the router that would carry it there.
There is no path to the server on this bench, so an `ntpclient -d` probe here
could only ever report "no reply", on a device that is perfectly configured.

Recorded so it is not re-proposed: the tempting version is to run the probe
anyway and mark the row amber when it finds nothing. That fails rule 1 in the
same way the Magos clock row did — a fresh OTD500 and one whose forward is
misconfigured are indistinguishable from the bench, so the row is amber on every
unit and teaches operators that skips are furniture.

What the bench *can* answer, and now does, is the half of the question that
needs no reachable server: whether the running daemon has read the config at
all. That is the `NTP daemon` row, added after a bench OTD500 (2026-08-31)
showed the client is started as `ntpclient -s -l` and reads its servers from
`/etc/config/ntpclient` **once, at startup** — so the best-effort restart in
`set_ntp_client` is what makes a commit live, and a restart that quietly failed
leaves a correct file, a green `NTP client` row and a daemon still polling what
it read at boot. It does not prove sync; it removes one way of failing silently.

Two things do close the rest, neither a bench row:

* **The sync check belongs to assembly**, where the pair is cabled together and
  the server is reachable. That check must use `ntpclient -d` reading
  `/etc/config/ntpclient` — this RutOS build **ignores a positional hostname
  argument**, so a probe passing the server on the command line silently tests
  the configured target instead and tells you nothing.
* **The competing time source has to be named.** RutOS falls back to the
  cellular modem clock (`get_time_from_modem`) whenever NTP samples are
  rejected, silently, so "the clock looks right" is not evidence of sync. Any
  assembly-side check has to separate the two.

Note also that as of TEC-846 a correctly configured pair still does **not**
sync: the round trip measured 2252 ms, samples reach the server and are
discarded, and the root cause is unfound. TEC-846 attributes the rejection to
ntpclient's `min_delay 800`; that does not hold up, and is recorded here so the
next person does not spend a day on it. Upstream documents `-q min_delay` as
**microseconds** (800 = 0.8 ms, so the comparison is off by 1000x) and as the
*shortest* possible round trip — a floor the `-l` lock algorithm uses, not a
ceiling that discards samples. Raising it is what upstream warns against; the
recommended direction is *down*. What does reject packets is the RFC-4330
cross-check set (`cross_check 1` in the debug output), whose candidates include
`abs(DELAY)>65536` and `LI==3`. Which one fires is not worth guessing:
`ntpclient -d` prints `rejected packet: <reason>` verbatim, and nobody has yet
run it against a reachable server.

Two constraints on any fix, both from the OTD500 bench unit (2026-08-31). The
build's usage line is `[-d] [-f frequency] [-g goodness] [-l] [-p port]
[-q min_delay] [-s]` — Teltonika dropped upstream's `-h`, `-c`, `-i` **and the
`-t`/`-x` switches that turn the cross-checks off**, so they cannot be disabled
from the command line. And `/etc/init.d/ntpclient` runs a fixed
`/usr/sbin/ntpclient -s -l`, passing nothing from the config, so `min_delay` is
always the compiled-in 800 and no UCI option can reach it. Changing either means
editing the init script, which a sysupgrade wipes — the same constraint that
makes quota-sync install after the firmware step. The binary parses
`/etc/config/ntpclient` itself, and `strings /usr/sbin/ntpclient` gives its
option surface: `hostname`, `interval`, `freq`, `tmz_sync_enabled` — and
**`force`**, which is the config-side face of the WebUI's "Force Servers"
(there is no `-t` flag on this build). So the cross-checks *are* reachable, by
UCI rather than by command line. `interval` is validated to 60..2147483647 and
silently falls back to **10 minutes**, not to 86400, when it cannot be read.

That validation range is why `NTP_CLIENT_INTERVAL` now sits on **60**, its floor.
On this fleet the interval is not an accuracy knob but a *retry latency*: a unit
cannot reach its time server from the bench, so its early polls all fail, and the
interval is how long it then sits on the firmware image's build date after it
finally reaches one (see `ensure_clock_sane`, which is what stops that costing a
provisioning run). It also buys attempts against the rejection above — 60 tries
an hour is 60 chances for a sample to pass the cross-checks where there was one.
Polling costs nothing here because every target is on the local network; the
OTD500 reaches the same server through the router's UDP 123 forward, so none of
it crosses the cellular link. `set_ntp_client` **clamps** below-floor requests
and warns, because the silent substitution above means asking for 30 would buy a
poll ten times slower than the default it was trying to beat.

The per-server **failover timeout** — the cost the deleted stock servers 2-4 were
about — is not tunable. It appears nowhere in the binary's option surface
(`hostname`, `interval`, `freq`, `tmz_sync_enabled`, `force`), and the init script
passes nothing from the config, so there is no UCI or command-line route to it.
Keeping the server list to one entry remains the only lever on that cost.

Two things in that binary matter more than the tuning, and both are worth
checking before anyone touches `force`:

* `[...] Parsed time is older than the current system time. Not syncing.` —
  Teltonika added a guard that **refuses to step the clock backwards**. A unit
  whose GSM modem clock runs ahead of real time therefore never syncs, no matter
  how correct its config, and says so only in debug output. That is an untested
  candidate root cause for TEC-846 and a distinct failure from any sanity check.
* `[...] Delay=%.1f  Dispersion=%.1f ...` and `LI=%d VN=%d Mode=%d Stratum=%d` —
  the debug output prints the actual values, so a single `ntpclient -d` against a
  reachable server settles which check fires and with what numbers, rather than
  anyone reasoning about it from a threshold. The
bench rows above deliberately do not encode that — they say what was
configured, which is true and useful, and they would otherwise fail every unit
for a fault that is not the unit's.

**`NTP server` (Magos pair), and why there is no clock row at all.** The NTP
server the tools point a unit at (`192.168.88.10`) is on the **assembly**
network; a unit on this bench cannot reach it and will not sync until it leaves.
So the row is a read-back of the server name and the `ntpAutomatic` flag, and
the effect that would normally corroborate it is out of the bench's reach by
construction. This is the one gap here that no amount of row-writing closes,
because the evidence is not present to be read.

The tempting fix is a `device clock` row off the HTTP `Date` header, and it was
written and then removed, which is worth recording so it is not re-proposed:

* As an NTP check it is meaningless. An unsynced unit drifts for as long as it
  is powered — the first APU verified read 90s out — so a window tight enough to
  mean "synced" fails every unit the bench provisions.
* Widened into a "was the clock ever set" check it still fails rule 1, because
  it cannot separate the fault from the expected state. Nothing on the bench
  sets these clocks, so a fresh unit reads whatever its factory image left and
  however long it sat in a box. A dead RTC and a perfectly good unit awaiting
  its first sync are indistinguishable from one reading, and telling them apart
  means power-cycling the unit to see whether the clock survives — which a
  verify pass must not do.

An always-amber row was the other option and is worse than none: it would add a
skip to every single run and teach operators that skips are furniture, which is
exactly how the `RF channel` gap stops being noticed. So the clock is not read.

Closing this properly means checking sync where the server is reachable, which
is an assembly-side check and not a bench row at all. A documented NTP-status
read on the dashboard API would at least let the bench report what the unit
thinks its own state is — the same ask as the RF channel, to the same vendor.

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
