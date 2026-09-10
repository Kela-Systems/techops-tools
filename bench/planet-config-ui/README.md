# PLANET IGS-4215 PoE Switch Configurator

Bench tool for provisioning PLANET **IGS-4215-8UP2T2S** industrial PoE++
switches, one at a time. This is the switch that powers a Magos site — four
AR-300 radars, a speaker and a camera hang off its eight PoE ports — so its
per-port power plan **is** the site's power plan, and it lives in this tool's
config rather than in someone's head.

The most **zero-input** tool of the set: plug the switch in, press Configure.
Nothing is scanned and nothing is typed.

## The factory password is derived from the MAC

PLANET's default is **`sw` + the last 6 hex digits of the switch's MAC**, in
lowercase:

```
arp 192.168.0.100
? (192.168.0.100) at a8:f7:e0:f6:c4:3a on en8 ifscope [ethernet]
                                ^^^^^^^^
                          ->  password  swf6c43a
```

Per-unit like the Teltonika label passwords, but *derivable* rather than
printed — and the bench already reads the MAC off ARP to identify the switch,
so there is still nothing to scan or type.

Login tries the **shared password first** (a re-run of an already-provisioned
switch is the common case, and there the factory one is guaranteed wrong), the
derived one second, and then **stops**. That cap is deliberate: three wrong
attempts start a lockout in which the switch accepts the connection, prints its
banner and hangs up *without prompting* — which is indistinguishable at the
bench from the broken-SSH firmware fault below, and sends whoever is holding the
switch down the wrong path entirely. Set `factory_password` in the config only
to override the derivation for an odd unit.

## What a factory switch does before it will talk to you

Three behaviours, none of them in PLANET's Command Guide, all confirmed on
hardware. The tool handles each; they are documented because they look like
faults when you meet them by hand.

1. **SSH and telnet are both disabled.** Ports 22 and 23 are closed on a
   factory unit and the CGI web UI is the only way in, so the CLI this whole
   pipeline runs on has to be switched on over HTTP first
   (`ensure_ssh_service`). A switch someone configured before usually has SSH
   on already, which is how this stayed hidden until the first genuinely
   factory unit.

2. **SSH refuses to open a CLI until the password is changed.** The shell
   answers with `You are required to change and store a new password`, then
   `(New)Password:` → `Verify (New)Password:` → `Success.` Commands sent into
   that dialogue are swallowed and every one of them waits out its timeout, so
   a tool that does not expect it appears to hang rather than fail. The switch
   also enforces 8-32 characters with upper case, lower case, a numeral and a
   symbol — a shared password that breaks those rules fails here, before
   anything is configured.

3. **That password must be saved immediately.** The switch boots from its
   STARTUP config, so a password living only in the running config reverts to
   the factory one on the next reboot. Seen for real: a run that failed later
   on rebooted and came back on `sw`+MAC, with the operator holding the wrong
   password.

## What it does per device

1. Read identity over the **web UI** (model, firmware, MAC, power inputs).
2. **Firmware floor** — flashed if the switch is below it. See below; this step
   is the reason the tool exists and is **on by default**.
3. Set the admin password to the shared default (`Kelasys123!`).
4. Apply the **PoE plan**: global budget and limit mode, then per-port
   enable/disable, power limit and priority.
5. **Name every port** (`description`), so `gi1` reads `Radar-AR300-1` rather
   than being a number someone has to remember.
6. NTP server → `192.168.88.10`; timezone → `IST` (UTC+3).
7. Verify every setting by reading it back off the switch.
8. Disable telnet — but only *after* SSH has carried the whole run.
9. **Last step:** move the management IP to `192.168.88.3`. The connection drops
   by design; success is confirmed by reaching the switch on the new address.

## The firmware step is load-bearing (read this one)

On the Teltonika tools the firmware floor is housekeeping. Here it is the
difference between a switch that can be configured and one that cannot.

**Firmware at or below `v1.305b251017` has a broken SSH server.** It accepts the
TCP connection, completes key exchange, accepts `none` authentication, refuses
the real password, and then closes the CLI channel immediately after printing
its welcome banner. Telnet behaves identically. The symptom at the bench is a
connection that opens and drops in about a tenth of a second, with **no password
prompt at all** — which reads like a wrong password and is not.

`v1.305b260324` fixes it. PLANET's release notes for that build say
`Bug Fixed: NA` and list only a fault-alarm tweak, so **the notes do not record
this fix — the behaviour does.** Do not conclude from the changelog that the
upgrade is unnecessary.

Consequences baked into this tool:

* `firmware.enabled` defaults to **true**, and stays true when the config omits
  it. A switch below the floor cannot be configured at all, so defaulting off
  would strand it.
* SSH login runs **after** the firmware step, never before.
* A login failure with the broken-SSH signature raises `SshBroken`, whose
  message names the firmware upgrade as the fix — rather than a generic auth
  error that sends someone hunting a password.
* Detection probes **port 80, not 22**. The broken firmware answers on 22, so an
  SSH probe would report a healthy switch that cannot actually be configured.
* The image flashes to the **inactive** partition, which is then marked active
  and the switch rebooted. The known-good image stays put, so a bad flash is one
  partition flip in the web UI away from recovery.

Download the `.bix` from
<https://www.planet.com.tw/en/support/downloads?method=keyword&keyword=IGS-4215-8UP2T2S>
into `firmware/` (gitignored) and set `firmware.bix_path`. It is only needed for
a switch that arrives below the floor, so a missing image warns on the page
rather than blocking Configure.

## The PoE plan

Ports are configured in **watts** in `planet.config.json`. The switch's CLI
takes **deci-watts** (`poe power-limit 450` is 45.0 W) and the conversion
happens once, in code — do not pre-multiply.

| Port | State | Limit | Priority | Label |
|---|---|---|---|---|
| gi1–gi4 | on | 45 W | critical | `Radar-AR300-1` … `-4` |
| gi5 | off | — | — | `Camera-RAYTHINK-PC464A1` |
| gi6 | on | 20 W | high | `Speaker-PR-HS15W-IP` |
| gi7 | off | — | — | `Unused` |
| gi8 | off | — | — | `Management` |

**Why 45 W for a 35 W radar.** The AR-300 draws 35 W *at the radar*; the
switch's limit is measured *at its own port*, with up to 100 m of copper in
between. IEEE 802.3bt builds that gap into the standard — a Type 3 Class 5 PSE
sources 45 W to guarantee 40 W at the PD. A 35 W cap browns the radar out on a
long run or a cold start, which presents as an intermittent port flap and gets
diagnosed as a radar fault. The limit is a **ceiling, not a draw**: the radars
still pull ~35 W each.

**Budget.** In `allocation` mode each enabled port's limit is reserved against
`budget_w`, so the shipped plan reserves 4×45 + 20 = **200 W of 240 W**. The tool
warns when a plan over-allocates, and when `budget_w` exceeds 240 W on a switch
reporting only one live power input — **360 W requires both PWR1 and PWR2**, and
a site planned at 360 W on one supply browns out under load.

**Priorities** decide who survives a shortfall: radars `critical`, speaker
`high`.

## Where the switch ends up

`192.168.88.3` by default — `.1` is the site gateway, `.2` is the TSW202, `.3`
is this switch. The **Address assignment** box offers two of the shared modes:

| Mode | Behaviour |
|---|---|
| `fixed` | Every switch to the same address; survives units and restarts. |
| `manual` | The address typed for that one switch, as a last octet or a full `192.168.88.x`. |

No `dhcp` and no `cycle`. A site takes one switch, and this switch is configured
with **no default gateway** — a unit that wandered off to a lease would be
reachable only by whoever guessed the subnet, and it serves no DHCP itself to
help anyone find it.

## Station requirements

The factory address is `192.168.0.100` — a different `/24` from every other
bench tool (the Teltonikas live on `192.168.1.x`). The station therefore needs
**two addresses**: one on `192.168.0.x` to reach a fresh switch, and one on
`192.168.88.x` to confirm the final move. On macOS:

```bash
sudo ifconfig en8 alias 192.168.88.50 255.255.255.0
```

## Four device quirks that fail silently

All four are absent from PLANET's Command Guide and were found on the device:

* **`poe power-limit` is in deci-watts.** The Guide's own example
  (`poe power-limit 95 all`) reads as watts and is wrong. Sending `45` caps a
  radar at 4.5 W; the port is accepted and the radar never comes up.
* **`clock timezone <ACRONYM> <hours>` takes 1–4 characters.** A longer acronym
  is accepted and silently ignored, leaving the switch on its factory +8 with
  every log timestamped wrong.

* **`username admin privilege 15 password …` is rejected.** The guide documents
  a numeric privilege level; this firmware accepts only `privilege admin` or
  `privilege user`. Worse, the command is **interactive** — it prompts `Old
  password:` before accepting the new one, so fired blindly it eats whatever
  command comes next. `set_password` handles both. (The tool normally skips
  this step entirely: the forced change at login has already set the password.)
* **The switch has a concurrent-CLI-session limit.** With no slot free it
  accepts the connection, prints `Close after 2 seconds` with a countdown and
  hangs up — which looks exactly like the broken-SSH firmware fault above but
  is transient, and is reported as its own error so nobody reflashes a current
  switch over it. Sessions are closed in a `finally` for the same reason.

A third, less silent: `show interface description` does not exist on this
firmware, so port labels are read back out of `show running-config`.

## Config

Copy `config/planet.config.example.json` to `config/planet.config.json`
(gitignored). The example is commented field by field.

## Verify

**Verify** re-checks a finished switch and changes nothing: firmware floor, NTP
server, timezone, every PoE port's state and limit, every port label, and that
telnet is off. Nothing about this switch's intended state is per-unit, so there
is no history lookup — the config is the whole expectation.

## CLI (without the UI)

```bash
python3 planet_configure.py --password 'Kelasys123!'
python3 planet_configure.py --verify          # change nothing
python3 planet_configure.py --no-firmware     # skip the floor for one run
```

## Tests

```bash
../.venv/bin/python -m pytest tests -q
```

No hardware needed. The suite pins the deci-watt conversion, the 32-character
label limit, the 4-character timezone acronym, the budget warnings, and the
firmware decision — including a test asserting that `bench_core`'s shared
`fw_version_at_least` gets PLANET's version format **wrong**, which is why this
module carries its own comparison. If that test ever fails, the shared helper
learned the format and the local one can go.
