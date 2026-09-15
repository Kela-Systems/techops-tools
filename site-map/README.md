# site-map

What connects to what at a site, what powers what, and who hands out the
addresses — declared in one YAML file per site, checked offline, drawn as
Mermaid.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

.venv/bin/python sitemap.py lint   sites/*.yaml        # offline checks
.venv/bin/python sitemap.py show   sites/my-site.yaml  # tree, in the terminal
.venv/bin/python sitemap.py render sites/my-site.yaml -o docs/my-site.md
.venv/bin/python sitemap.py export sites/*.yaml -o ui/site-data.js   # feed the UI

arp -an | .venv/bin/python sitemap.py identify          # MACs -> vendors
```

Python 3.11+. The only runtime dependency is PyYAML.

## Why it is declared and not discovered

Discovery can only report what *is*. A site map is worth having because it
says what was *meant to be*, so the two can be compared — an address plan
nobody wrote down cannot be violated, only departed from silently.

A Magos site is also cookie-cutter (four AR-300 radars, two APUs, a camera, a
speaker, a PoE switch, a managed switch, a cellular router), so the model is
nearly free to write: copy `sites/_template-magos-site.yaml` and adjust.

**Nothing in this tool contacts a device.** That is a property worth keeping,
not an accident of it being unfinished: `lint` runs in CI with no tailnet, no
credentials and no possibility of touching production. Proving a model against
real hardware is a separate command against read-only endpoints, and is not
built yet — see *Not built yet* below.

## Viewing a site

Three ways out, in increasing effort:

- **`show`** prints the data path as an indented tree with the power feed
  alongside. Fastest way to answer "what is plugged into what".
- **`render`** emits Mermaid, which GitHub draws inline in a README or a PR.
- **`ui/index.html`** is a published Artifact page: the connection flow, a
  drawn faceplate of every switch whose socket layout is known, the whole
  subnet on one ruler with the reserved ranges laid over it, and the PoE
  budget. Click anything to inspect it.

The page reads `ui/site-data.js`, which `export` generates from the same YAML
the linter reads — so the page cannot drift from the model. After changing a
site file, re-export before republishing.

**The page renders ONE site: the first real one in the payload.** `export`
therefore drops any site whose name starts with `_`, because those are schema
templates full of invented devices — and `sites/*.yaml` globs them in with the
real ones. This is not a tidiness rule. The template's name sorts ahead of
every real site, so before the filter existed the dashboard rendered a
fictional 16-device site for several published versions, with prose about a
real site wrapped around it. `--include-templates` keeps them if you actually
want to preview one. `lint` still checks templates; only the UI payload
excludes them.

### Publishing the UI

The page and its data file have to be staged somewhere the Artifact tool can
read (it only reads from the working directory or a scratchpad), then
published together:

```
sitemap.py export sites/*.yaml -o ui/site-data.js
# publish ui/index.html with ui/site-data.js alongside it as `site-data.js`
```

Re-publishing the same file path keeps the same URL.

### Why drawn faceplates and not product photos

An artifact page cannot load an image from an external host — the content
security policy blocks every non-allowlisted origin, silently, so a hotlinked
vendor photo is a gap rather than an error. Images have to be published with
the page; see `ui/images/README.md`.

That constraint pushed the design somewhere better anyway. The question an
installer has is *which socket is gi8*, and a catalogue photo does not answer
it. A drawn faceplate whose ports carry the same ids the site model uses does,
colour-coded by what is plugged in and which sockets supply power. Photos are
supported on top, per model, for the units where seeing the real thing helps.

## Discovery: ARP + bench-central

`discover` joins an ARP sweep to the bench-central archive and writes a site
model in which every fact carries the source that established it.

```bash
ssh <site-router> arp -an > arp.txt

sitemap.py discover --site kela-fob-03 \
    --central http://techops-automations-host:8100 \
    --file arp.txt -o sites/kela-fob-03.yaml
```

The three questions have three different sources, and the output keeps them
apart:

| Question | Source | Evidence recorded |
| --- | --- | --- |
| Is it there, at that address? | the ARP entry | `arp` |
| Who made it? | OUI longest-prefix | `arp` |
| Which model? Which firmware? | bench-central, by MAC | `bench-record` |
| What is it plugged into? | nobody asked yet | `assumed` |
| What powers it? | nobody asked yet | `assumed` |

Neither half can answer the model question alone. ARP gives you
MAC-at-this-address-right-now; a bench record gives the model for that MAC as
the device itself reported it through `get_identity()`. Together they give the
model of the thing at that address now — which is why one node comes out
carrying two sources:

```yaml
  router:
    vendor: Teltonika Networks UAB   # from the OUI
    model: RUTM08                    # from a bench-central run record
    firmware: RUTM_R_00.07.14.3
    evidence:
      "*": arp
      model: bench-record
      firmware: bench-record
```

**The emitted file has no `net:` or `power:` block, ever.** An ARP sweep
proves no cable and no power feed. Writing a plausible-looking one would make
the file confidently wrong, which is the one genuinely damaging thing this
command could do. It is emitted `status: provisional` for the same reason.

Read-only throughout: `bench_central.py` issues GETs and contains no write
path (there is a test asserting the string `"POST"` never appears in it). The
collector has no auth — the tailnet is its perimeter — so a client that could
write would be one that could corrupt the audit trail by accident.

### Before you trust the coverage number

Two limits, both reported at runtime rather than buried here:

- **Central shipping is opt-in per station.** `bench/README.md`: "When the
  variable is unset (the default), nothing is spooled and no uploader runs."
  If no station has `BENCH_CENTRAL_URL` set, the archive is empty and this
  buys nothing — so `discover` prints the archive's row count first, and says
  so loudly when it is zero. An empty archive and an unreachable one look
  identical otherwise.
- **It only knows devices a bench tool configured.** An operator station or a
  third-party box never went through the bench, so it will not be there. A
  miss is reported as unknown and leaves `assumed` in place; it is not
  evidence of anything.

### One MAC cannot settle a model, and a wrong one is invisible

`8C:1F:64:E7:4` proves Magos. It cannot say whether a unit is an AR-300 radar
or an APU, and a site runs four of one and two of the other. `discover` never
guesses: it records the vendor, notes that the vendor makes several devices in
the catalogue, and leaves `model` unset. Device *names* are guessed from the
address plan, because a wrong name is visible to whoever reads it, whereas a
wrong model is not.

## Identifying devices by MAC

`identify` resolves MAC addresses to vendors offline, from nmap's
`nmap-mac-prefixes`. Offline matters: an ARP table from a live site is an
inventory of that site.

**Longest-prefix matching is the point.** Much of this hardware sits behind an
IEEE **MA-S** block, where the vendor is identified by the first 36 bits, not
the usual 24. A 24-bit-only lookup reports the Magos units as "Ieee
Registration Authority" and tells you nothing:

```
8C:1F:64          -> Ieee Registration Authority   (the block's registrar)
8C:1F:64:E7:4     -> Magosys Systems               (the real assignment)
```

Known prefixes seen at sites so far:

| Prefix | Vendor | Turns up as |
| --- | --- | --- |
| `20:97:27` | Teltonika Networks | RUTM08, OTD500, TSW202 |
| `8C:1F:64:E7:4` | Magosys Systems (MA-S) | AR-300 radars, APUs |
| `BC:74:D7` | HangZhou JuRu Technology | Raythink thermal cameras |
| `A8:F7:E0` | Planet Technology | IGS-4215 PoE switch |
| `E8:CF:83` | Dell | operator stations |
| `A0:E0:25` | Provision-ISR | IP speakers |

A MAC identifies a **vendor, never a model**. The model guess comes from the
vendor plus the hostname and the address, and every guess is printed with its
reasoning so a wrong one is visible rather than load-bearing. Where a vendor
makes more than one device in the catalogue — Magos makes both the radar and
the APU — the tool refuses to pick and says so.

Two things `identify` cannot tell you, and says so on every run:

- ARP lists only what that host has **recently talked to**. A device missing
  from the table is not proof it is absent, and a switch that has exchanged
  nothing with the host will not appear at all.
- Nothing in it distinguishes two models from one vendor sharing a prefix.

## How certain is any of this

The tool's original weakness: it checked whether a site model contradicted
*itself*, and said nothing about whether it matched reality. A map that is
confidently wrong is worse than one that is visibly incomplete, so certainty
is now a field on every fact rather than a caveat in prose.

### Evidence proves specific claims, not "confidence"

Each evidence kind declares **what it proves**. The distinction that forced
this design: an OUI lookup settles a device's **vendor** and nothing more.
Teltonika makes the RUTM08, the RUTM51 and the RUTX50; Magos makes both the
AR-300 and the APU. Reading an ARP table as a list of models produces exactly
the kind of wrong that survives review.

| Evidence | Proves | Notes |
| --- | --- | --- |
| `device-api` | present, mac, vendor, model, addr | every bench tool's `get_identity()` already reads this |
| `bench-record` | mac, vendor, model | a bench-central run record; authoritative for identity, silent on whether the box is still plugged in |
| `switch-table` | present, mac, vendor, link | a switch MAC-address table places a MAC on a port |
| `lldp` | present, link | |
| `dhcp-lease` | present, mac, vendor, addr | the router's own lease table — the real answer to "who gives IP to who" |
| `poe-status` | present, link, power, draw | the only network source that proves power, with a measured figure |
| `arp` | present, mac, vendor, addr | **not** model, **not** link |
| `survey` | everything | a person traced it on site; the only thing that can prove a DC or mains path |
| `config` | *nothing* | what a bench tool is set to apply — intent, not observation |
| `doc` | *nothing* | written in a runbook or on a port-map label |
| `assumed` | *nothing* | nobody checked — **the default** |

`assumed` is the default deliberately: an unmarked fact is an unverified fact,
and silence must not read as confirmation.

`lint` reports any fact whose evidence does not cover it
(`model-unverified`, `link-unverified`, `power-unverified`, …) as a warning.
`--require-evidence` promotes those to errors, which is how a site gets gated
once it is meant to be fully verified.

### Coverage

`lint` prints what share of the map is actually proven, per claim, because
the claims are not equally reachable:

```
  verified against hardware:
    vendor 100.0%  [################]  8/8
    model       —   [................]  nothing recorded yet
    addr   100.0%  [################]  8/8
    link        —   [................]  nothing recorded yet
```

A claim with nothing recorded prints `—`, never 100%. Scoring zero facts as
fully verified is the precise false assurance this is built to prevent.

### What can reach 100%, and what cannot

| Question | Authoritative source | Reachable |
| --- | --- | --- |
| Model / serial / firmware | the device's API, or bench-central by MAC | **yes** |
| What is in which socket | switch MAC-address table, LLDP | **yes** |
| Who hands out addresses | the router's DHCP lease table | **yes** |
| Watts per PoE port | the switch's PoE status | **yes** |
| Are both DC inputs live | the switch reports PWR1/PWR2 | **yes** |
| Which PSU, breaker or UPS feeds a device | — | **no** |

**The DC and mains side cannot be machine-verified.** A switch can report that
an input has voltage; nothing can report what is at the other end of that
wire. Those edges raise `power-needs-survey`, which only `survey` evidence
closes — so the tool asks for a person rather than implying a verifier is
coming.

### Provisional sites

`status: provisional` marks a site that is still being surveyed. Gaps
(`unpowered`) become warnings; contradictions (an address collision, a loop)
stay errors at every stage. Without it, the only way to get a half-surveyed
site through CI would be to invent the missing half.

`sites/kela-fob-03.yaml` is the worked example: built from one `arp -an`, it
records eight devices with vendor and address proven, **no** models, and no
network or power edges at all — because none of those are established. Its
notes say what is missing and why ARP cannot settle it.

## Where the facts come from

The site model is the fourth place these facts live, which is the problem it
is meant to end. Today they are spread across:

| Fact | Lives in |
| --- | --- |
| Address plan, per role | `bench/OPERATOR-GUIDE.md` tables, plus each tool's config |
| Physical port → device | `bench/planet-config-ui/config/planet.config.json` (`poe.ports`) |
| Power plan | the same file (`limit_w`, `priority`, and the PWR1/PWR2 note) |
| Per-unit identity (MAC, serial, model) | `bench-central` run records |
| Logical links (radar → camera cueing) | `hub-admin` `site.profile.json` `availableLinks` |

A site file should be *derived* from those, and the linter is what keeps it
honest about drift. Anything in a site file that is not backed by one of them
is marked `unconfirmed:` in the template and needs an eyeball at a real site.

## The model

Two edge sets, deliberately separate:

- **`net`** — `parent.port -> child`: the data path, and each node's
  `addr_source`, which is the "who gives IP to who" answer.
- **`power`** — `source.outlet -> sink`: what feeds what, `via` PoE / DC /
  mains, with `draw_w`.

They are not the same graph. The camera hangs off the PoE switch's `gi9` for
*data*, but `gi9` and `gi10` are the IGS-4215's two **non-PoE** copper
sockets, so the camera's power comes from a separate PSU. A single combined
graph cannot express that, and it is exactly what an installer needs to see.

### `addr_source`

| Value | Meaning |
| --- | --- |
| `static-bench` | assigned by a bench tool as its last provisioning step (the default when an `addr` is given) |
| `static-manual` | typed by hand for this one unit |
| `dhcp-lease` | held on lease from the node with the `dhcp-server` role, and can move |
| `factory` | not provisioned yet; a factory address is legitimately off-subnet |
| `none` | holds no address on this subnet |

### Reservations, and why `dynamic` matters

A reservation is a named slice of the subnet. `dynamic: true` means a tool
*cycles through* the range, so any address in it can land on any device in
turn; without it the range is a documented static plan.

Only a dynamic pool can steal an address, which is what makes this check
worth running. The camera tool cycles its last octet `30 → 50` **inclusive**
(`raythink-config-ui/config/raythink.config.example.json`, `octet_min` /
`octet_max`) while `radar_0` is pinned to `.50` by RF channel
(`bench/OPERATOR-GUIDE.md`). The ranges touch at one address, and the failure
would surface weeks later as a radar that "stopped reporting", nowhere near
whoever configured the camera. `lint` reports it on the template today.

## The checks

`error` is a site that is wrong. `warn` is a site that is fragile or
unprovable. Only errors affect the exit code, so a warning can sit in a file
indefinitely without teaching anyone to skim past red.

| Code | Severity | What it means |
| --- | --- | --- |
| `addr-collision` | error | two nodes on one address |
| `addr-off-subnet` | error / warn | outside the site subnet; only a warning for `factory` |
| `pool-overlap` | error | two reserved ranges overlap |
| `addr-in-foreign-pool` | error | a fixed address sits inside someone else's dynamic pool |
| `unknown-pool` | error | `addr_pool` names a range that is not declared |
| `dhcp-no-server` | error | something takes a lease, but nothing serves DHCP |
| `dhcp-multi-server` | error | two DHCP servers on one subnet |
| `dhcp-pinned-addr` | warn | a lease recorded as though it were fixed |
| `unknown-node` | error | an edge or uplink names a node that does not exist |
| `port-reuse` | error | one socket carrying two devices, at either end |
| `net-multi-parent` | error | a node plugged into two parents |
| `net-cycle` | error | the data path loops |
| `net-orphan` | warn | no data path back to the uplink |
| `unpowered` | error | nothing declared as powering it |
| `power-multi-source` | warn | two feeds — redundant input, or a duplicate edge? |
| `outlet-reuse` | error | one outlet feeding two devices |
| `power-cycle` | error | the power feed loops |
| `poe-overcommit` | error / warn | PoE draw over budget; see below |
| `poe-no-budget` | warn | a PoE switch with no `poe_budget_w`, so draw cannot be checked |
| `poe-unknown-draw` | warn | PoE ports with no `draw_w`, so the total is a floor |
| `power-spof` | warn | a single feed under two or more `critical` nodes |

Two deliberate omissions:

- **The incoming mains is never a `power-spof`.** It is true at every site and
  fixable at none, so reporting it would add one guaranteed warning to every
  map — which is how people learn to skim past the warnings that matter.
- **`power-multi-source` is a warning, not an error.** Two feeds is redundancy
  on a device that takes redundant input (the IGS-4215's PWR1/PWR2) and a
  modelling mistake anywhere else. Telling them apart needs per-model
  knowledge the model does not carry.

### PoE, and `poe_managed`

`poe_managed: false` is the live state of the PLANET IGS-4215 as of
2026-09-14 (commit `c736bfc`): the bench sets no limits, no budget and no
priorities, and the switch negotiates power per port on its own.

So `draw_w` here is what a device actually pulls (~35 W for an AR-300), **not**
the `limit_w` ceiling in `planet.config.json` — that is a reserved ceiling
that nothing is currently applying, and treating the two as the same number is
how a budget silently overcommits. While `poe_managed` is false an overcommit
is a *warning*, to be proven against the switch's own PoE status rather than
settled from the model.

`poe_budget_w: 240` may only be raised if **both** power inputs are wired.

## Not built yet

The verifier: prove a model against the hardware, read-only.

- **PLANET IGS-4215** — MAC address table per port, LLDP, PoE port status.
  The highest-value query available: it answers what is physically in which
  socket and what is actually drawing. The SSH plumbing, the derived-password
  login and the CLI transport already exist in
  `bench/planet-config-ui/planet_configure.py`.
- **RUTM08** — the DHCP lease table, authoritative for who hands out addresses.
- **TSW202** — LLDP and MAC table over RutOS.
- **bench-central** — resolve each discovered MAC to a real unit (serial,
  model, which bench run configured it, when).

Read-only, and it must stay that way: the router at `192.168.88.1` is
production.
