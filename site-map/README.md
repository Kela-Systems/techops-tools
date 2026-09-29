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
.venv/bin/python sitemap.py serve                       # host the UI, sweep a site

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

**Nothing that touches the network is in the path `lint` takes.** That is a
property worth keeping rather than an accident of being unfinished: `lint`,
`show`, `render` and `export` run in CI with no tailnet, no credentials and
no possibility of touching production, and they are what every commit runs.

Three commands do reach out, each opt-in and each read-only:

| Command | What it contacts | What it needs |
| --- | --- | --- |
| `discover` | nothing — it parses an ARP table someone else collected | a file |
| `serve` | pings a subnet from a site server over SSH, then reads that host's neighbour table | ssh to the site server |
| `probe` | the devices' own read-only APIs, one attempt each | `--confirm`, and the bench password |

None of them writes to a device. `probe` is a dry run unless you pass
`--confirm`, because these are production units and a retried credential can
lock a switch out.

## Viewing a site

Three ways out, in increasing effort:

- **`show`** prints the data path as an indented tree with the power feed
  alongside. Fastest way to answer "what is plugged into what".
- **`render`** emits Mermaid, which GitHub draws inline in a README or a PR.
- **`ui/index.html`** served by `sitemap.py serve`, or published as an
  Artifact page: the connection diagram, a
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

## Self-hosting it, and sweeping a site from the page

The published Artifact is a static export and always will be. An artifact
page is sandboxed and its content-security policy blocks every request to a
host that is not allowlisted — silently, so it reads as a hang rather than an
error — and `100.x` and `192.168.88.x` are certainly not allowlisted. A page
that sweeps a site therefore has to be served by a process that is on the
tailnet and has a working `ssh`. That is the whole reason self-hosting is the
answer here, rather than a preference.

```bash
export KELA_BENCH_PASSWORD=...          # or pass --password
.venv/bin/python sitemap.py serve
# site-map on http://localhost:8200
```

### Passwords, and where they are not

Two secrets are involved and they are different: the shared Teltonika
password, and the operator stations' account password. Neither belongs in
this repo, a shell history, a process argument or the page.

**The simplest thing that is still safe: let it ask.** With nothing
configured and a terminal to ask at, it prompts once at startup. Nothing
records what you type — not the shell history, not `ps`, not a file:

```
$ .venv/bin/python sitemap.py serve
the shared Teltonika/router password (not echoed):
site-map on http://localhost:8200
  router password typed at the prompt
```

Add `--ask-host-password` to be asked for the operator stations' password
too. `--no-prompt` turns it off for a systemd unit or CI, where a prompt
would block on a terminal nobody is watching — there it fails on the missing
secret instead.

**For anything long-lived, name a command that produces the secret.** It runs
at the moment the secret is needed, so the value lives wherever it already
lives and this process holds it for the length of one survey:

```bash
# 1Password
sitemap.py serve \
  --password-cmd 'op read "op://TechOps/Teltonika/password"' \
  --host-password-cmd 'op read "op://TechOps/OperatorStation/password"'

# macOS Keychain — store once, with -w reading from stdin so the secret is
# never an argv entry:
security add-generic-password -U -a "$USER" -s kela-router -w
sitemap.py serve --password-cmd 'security find-generic-password -s kela-router -w'
```

The startup banner says where each came from and never any part of it:

```
router password from `security` on demand
host password   not configured
```

Why not the other ways, worst first:

Every way in, strongest first:

| How | Router | Operator stations |
| --- | --- | --- |
| prompt at startup | default, when interactive | `--ask-host-password` |
| a command | `--password-cmd` | `--host-password-cmd` |
| an env var | `$KELA_BENCH_PASSWORD` | `$KELA_HOST_PASSWORD` |
| a literal flag | `--password` | `--host-password` |

And why the last one is last, along with the ways that are not offered at all:

| | Why not |
| --- | --- |
| a field on the page | crosses the network on every survey, sits in browser memory, and the router password opens every router in the fleet |
| a literal flag | visible to every user on the box in `ps`, and recorded in your shell history |
| an env var set inline | same shell history problem |
| a file in the repo | one `git add -A` from being published |

The flags and env vars still work, because a systemd `EnvironmentFile` that
root owns is a reasonable place for this and a script has to come from
somewhere. Stronger sources win where several are set, and both secrets are
resolved once at **startup** rather than per request — so a locked keychain
or a `not signed in` fails when you start the server, not three minutes later
in the middle of someone's survey.

Only the first line of the command's output is used: `op read` and
`security -w` both emit a trailing newline, and a stray one silently becomes
part of the password, which then fails authentication with no clue as to why.

### Why the survey runs from the router

The site's Teltonika is the vantage point, and not only because it is the
internet leg. It is the subnet's **DHCP server, DNS resolver and default
gateway**, so every device at the site has talked to it — where a site
server's neighbour table holds only what the server itself exchanged traffic
with. It is also the one device a FOB certainly has; a server is
site-specific. And its bridge forwarding table is what proved the single
cabling fact in `sites/kela-fob-03.yaml`, so it is the only vantage point
that can ever turn the diagram's dashed lines solid.

**The routers reject keys.** Tailscale SSH is not enabled on them, so:

```
$ ssh -o BatchMode=yes root@100.64.242.104
root@100.64.242.104: Permission denied (publickey,password).
```

They take `root` plus the shared password. Rather than roll keys onto a dozen
production routers to make a read-only tool work, the transport is
`bench_core.TeltonikaClient` — the same client `probe.py` uses — in
`set_read_only(True)` mode, where a command that changes the device is
refused **by construction** rather than by careful reading.

The password is taken server-side, via `--password` or
`KELA_BENCH_PASSWORD`, and **never reaches the browser**. A field on the page
would put the password that opens every router in the fleet into a web form
and across the wire on each survey; `/api/health` reports only whether one is
set.

`--via server` keeps the old path — `ssh` as `kela` with a key, against a site
server — for a site whose router is unreachable. It sees less: no interfaces,
no lease table, and a neighbour table that is only as complete as the server's
own traffic.

### The subnet is read, not asked for

The router states its own LAN — `br-lan 192.168.88.1/24` — so the subnet
field is **optional and normally left empty**. That beats a default typed
into a field: it gets a site on some other range right without anyone
remembering to change it, and it cannot be wrong about the site you are
actually looking at. A value in the field overrides it, for sweeping some
other range from the same vantage point, and the survey says so when the two
disagree.

### Every run leaves a transcript

`runs/` holds one JSON file per survey, written whether it succeeded, was
refused or crashed. Each remote command is recorded with its exit status, how
long it took and what came back:

```
$ sitemap.py runs
ok  2026-09-15T14:46:25+00:00 10.8s  kela-fob-03 192.168.88.0/24 9/254 answered
     20260915T144625+0000-kela-fob-03.json

$ sitemap.py runs --last
  . scan requested: subnet 'to be read from the router', via router
  $ [router] cat /proc/sys/kernel/hostname          rc=0 0.349s out=15b
  $ [router] ip -4 addr show                        rc=0 0.395s out=804b
  . subnet read from the router: 192.168.88.0/24
  $ [router] cat /tmp/dhcp.leases                   rc=0 0.328s out=418b
  $ [router] for a in 192.168.88.1 …                rc=0 2.859s out=0b
  $ [router] ip neigh show                          rc=0 0.425s out=10153b
  . discovered: 9 devices
  $ [kela-fob-03 as kela] read DMI + ip addr        rc=0 out=1129b
  $ [kela-fob-03-operator as kela] read DMI         rc=1 ERR=Permission denied
```

That last line is the point. **Every failure this tool has had was a silent
one** — `hostname` returning rc 127 on BusyBox, an ssh refused for a username
rather than a key, a neighbour table full of k3s pod addresses, a host
answering with the wrong MAC. Each is obvious in a transcript and invisible
in a result.

**Secrets are redacted before anything is written.** Every value the resolver
produced is scrubbed from every command, every output and every traceback, so
a password that turns up inside a command string does not survive into the
file. The marker is plain-ASCII `[redacted]` so it greps the same everywhere.
Outputs are capped at 20 KB and say when they were truncated; the newest 200
runs are kept. The page reports the transcript name with each answer, so a
bug report can name the run.

### What it reads

Four reads, all of them reads:

| Command | What it establishes |
| --- | --- |
| `cat /proc/sys/kernel/hostname` | which site this is. RutOS is BusyBox and has no `hostname` binary — the first attempt died on `rc 127`. |
| `ip -4 addr show` | the router's **own** legs: `br-lan`, `wan` and `tailscale0`. |
| `cat /tmp/dhcp.leases` | MAC → hostname, which is how `.118` gets pinned as a `TSW202`. |
| `ip neigh show` | who answered, and at what MAC. |

Plus one ICMP echo per address in the subnet, and `/sys/class/net/*/address`
for the interface MACs, because `ip -4 addr` prints no `link/ether` line.

### The vantage point has to add itself

**A host has no ARP entry for itself**, so whatever you survey from is the one
device a survey of it cannot see. That is why the server was missing from the
diagram when the server was the vantage point, and it would be the router
now. So the router is added from its own `ip addr`, with `device-api`
evidence — its presence and every one of its addresses come from the box
itself, which outranks an ARP observation.

It is *merged*, not inserted. The sweep pings every address in the subnet
including the router's own, so the router often does end up in its own
neighbour table; inserting blindly emitted two `router:` keys and the
ARP-derived one silently won, taking all three interfaces with it.

### A factory hostname is a model number

An unprovisioned Teltonika still answers to the hostname it shipped with, and
that hostname is a model number. A lease reading `TSW202` therefore becomes a
**candidate** with that as its basis — never a `model`, because nothing has
asked the device. This is how the curated site file pinned `.118` by hand, and
without it a fresh survey has no shortlist at all and the switch the whole
site hangs off is drawn as a plain unknown device.

### Where hostnames come from

Three sources, weakest last, and `nmap` is not one of them — it is not
installed on RutOS, and its hostname discovery is reverse DNS, which the
router will do directly:

| Source | Names |
| --- | --- |
| `/tmp/dhcp.leases` | anything holding an active lease — the device speaking now |
| `uci show dhcp` (`@host` sections) | static reservations, which name a device that holds no lease |
| `nslookup <addr> 127.0.0.1` | whatever the router's own resolver admits to (`expandhosts` is on) |

Reverse DNS is asked only for the addresses that answered, in one command,
not for all 254.

**None of them will name a radar.** `192.168.88.130` has no lease, no
reservation and returns NXDOMAIN. Nothing short of reading the device names
one of those, and the map says `unknown` rather than inventing something.

### When a vendor comes back unknown

nmap's `nmap-mac-prefixes` is a snapshot that ships with the package, so
hardware newer than it resolves to no vendor at all — which on a site map
reads as an unknown device and costs an operator a trip. `oui-overrides.txt`
is merged **over** the system database, longest prefix winning, so a line
there fixes a prefix for every site and every future survey.

One rule, stated in the file: only record a prefix you actually know. A
guessed vendor is worse than no vendor, because it looks like a reading. A
prefix nobody has identified yet belongs in there as a commented line naming
where it was seen, so the next person does not re-derive the dead end.

### Reading a PC's model off the PC

`/sys/class/dmi/id` is world-readable, so an ordinary SSH session as `kela`
is enough — no sudo, no extra credential, nothing written. That is what turns
`vendor Dell` into `Dell Pro Max Tower T2 FCT2250`, and it is the same
reading the curated site file records by hand.

The chain is: **lease hostname → tailnet node → SSH → DMI.** A device's lease
hostname (`kela-fob-03`) is looked up in `tailscale status` to get its tailnet
address, which is how a host on `192.168.88.0/24` becomes reachable from
anywhere. Offline nodes are skipped rather than waited on.

It fills three gaps for each host it can reach:

| Field | From |
| --- | --- |
| `model` | `product_name` |
| `firmware` | `bios_version` — the firmware analogue on a PC. The OS version goes in the notes, because software moves on its own schedule. |
| `interfaces` | the host's own legs, so a site server arrives with its LAN NIC *and* `tailscale0` |

Container plumbing is filtered: the site server runs k3s, so it carries
`cni0` at `10.42.0.1` and `flannel.1`, and both arrived on the diagram as
external interfaces of the server — the same noise as the `10.42.0.x` pod
neighbours the sweep already drops. `tailscale0` is deliberately kept: it is
a real leg, and how the site is reached.

**Every reading is accepted only after the host's own MAC matches the one the
router observed at that address.** `192.168.88.0/24` is the subnet at every
site *and* on the bench, and a bench station carries a `192.168.88.10` alias
of its own — so a session opened to the wrong machine answers entirely
convincingly. A mismatch is discarded and reported, never recorded.

The operator stations take a **different account and a password** —
`KelaAdmin` by default, `--admin-user` to change it — because their sshd
advertises password auth only. That path uses paramiko rather than the `ssh`
binary: the way to feed a password to `ssh` is to turn its prompt back on,
which is how a survey ends up hanging on a prompt nobody can see. It is tried
**only** after a key has actually been refused, so the normal path stays
keys-only, and only when `--host-password-cmd` is set.

Not every host can be read, and one that refuses is a finding rather than a
silence:

```
kela-fob-03-operator (100.120.150.95): kela@100.120.150.95: Permission denied (password).
```

That host advertises password auth only, so a key is never offered. Making it
work is an sshd config change — a write to production — so it is reported and
left alone. `--no-host-identity` skips the whole step.

### A server and an operator station cannot be told apart by MAC

Two real devices from kela-fob-03:

```
e8:cf:83:8d:fc:14   the site server
e8:cf:83:3f:f3:83   the operator station
```

`e8:cf:83` is Dell for both, and the remaining three bytes are Dell's own
allocation sequence — so nothing in either MAC says which is which, and a
rule built on them would be an accident of the purchase order. The same shape
as `8C:1F:64:E7:4` proving Magos while saying nothing about radar-vs-APU.

The lease table settles it, because the devices name themselves:

```
E8CF838DFC14 -> kela-fob-03            ← the site's own name: the server
E8CF833FF383 -> kela-fob-03-operator   ← the suffix: the operator station
```

So `kind` comes from the hostname, which beats the `.10-20` / `.29` address
convention because that is a bench convention a site is free to depart from.
`operator` is matched anywhere in the name, not as a suffix — `afb8-oc-station`
and `fob-91-hamamis-mediaserver` both exist — and it is checked *before* the
server rule, because `kela-fob-03-operator` carries the site name too.
Anything unrecognised leaves the kind alone rather than guessing.

### What a vendor settles, and what it does not

A MAC gives a vendor. A vendor settles a **kind** where it makes only one
kind of thing, and never a **model**:

| OUI | Kind | Why, and what is still unknown |
| --- | --- | --- |
| Magosys | `radar` | Magos makes radars and APUs, but an APU is an NVIDIA board carrying an NVIDIA MAC — so a Magosys OUI is a radar. *Which* radar stays unknown. |
| HangZhou JuRu | `camera` | the site's cameras are on `bc:74:d7` and nothing else at a FOB is. |
| Routerboard / MikroTik / Planet | `switch` | — |
| Teltonika, and not the router | `switch` — as a **lead** | `20:97:27` covers the RUT, the TSW202 and the OTD500 alike, so the OUI cannot confirm it. The router is identified separately and excluded first, and a FOB's second Teltonika is usually its switch. Without this a site whose switch holds no named lease drew as *switch (not seen)* while the switch sat in the device list. |
| Dell | *nothing useful* | a server and an operator station are both Dell on `e8:cf:83`. The hostname decides; the vendor table's answer is only a fallback for a Dell with no name at all. |

**Every site has at least one server.** When none is identified the diagram
says so, and says why a MAC could not have found it: either the lease table
was not read, or the server holds a static address and never appears in it.

Lease hostnames are recorded as notes and candidates but **never used as node
names**: names are referenced by `net:` edges in site files, so deriving them
differently would rewrite identities across every existing model.

Open it and the masthead grows two fields:

| Field | Default | What it is |
| --- | --- | --- |
| **Router Tailscale IP** | none | *Where the survey runs.* A root session on one site's Teltonika over the tailnet. This is what chooses the site. |
| **LAN subnet** | `192.168.88.0/24` | *What it looks at,* from that vantage point. |

The asymmetry is deliberate: there is a sensible default for what to look at
and none at all for where to look from. Press Sweep and the server pings each
address in the subnet from the site server, reads that host's neighbour
table, and runs the result through the same `discover` → YAML → `lint` path
the CLI uses — so the page can only ever draw a model the linter would
accept. The YAML it came from is handed back under the panel, ready to save as
`sites/<name>.yaml`.

The site is named from the remote host's own `hostname`, which is why two
fields are enough and there is no third one to fill in.

Only addresses inside the subnet you asked about are reported. The
neighbour table belongs to the host, not to the sweep, so a site server
running k3s carries one entry per pod on `cni0` — 37 of them on
`kela-tlv-dev-03` — and unfiltered they arrive as vendorless devices that
bury the real ones.

**It is read-only, and the remote commands are the whole of it:** one ICMP
echo per address, then `ip neigh show`. Nothing logs in to a device, no device
credential is needed or accepted, and nothing is written anywhere. What comes
back proves presence, an address, a MAC and — through the OUI — a vendor. It
proves no cable, no power feed and no model, so the emitted model carries
none of those rather than a plausible-looking guess.

Two guards worth knowing about, both of which exist because the failure is
silent otherwise:

- **The subnet is capped at a /22.** The cost is linear in addresses, and a
  mistyped `/8` would otherwise queue 16 million pings against production
  from a web request. It is counted before the address list is built.
- **A LAN address in the server field is flagged.** `192.168.88.0/24` is the
  subnet at every site *and* on the bench, so sweeping it from a laptop finds
  the bench and reports it as a site. That is the same collision every fact
  in `sites/kela-fob-03.yaml` carries a MAC cross-check for. It warns rather
  than refuses — a jump host is legitimate — but it says so on the page.

To resolve models as well as vendors, point it at the archive:

```bash
.venv/bin/python sitemap.py serve --central http://techops-automations-host:8100
```

Without it every device comes back vendor-only, and the page says so.

### Getting to it from somewhere else

**There is no authentication in `serve.py` at all**, which is the same bargain
bench-central's collector makes and for the same reason: a client that could
authenticate would be a client that could be talked into sweeping on someone
else's behalf. So it binds `127.0.0.1` and the tailnet is the perimeter.

The right way to share it is to let Tailscale terminate TLS and do the
identity, which keeps the listener on loopback:

```bash
sitemap.py serve                       # still 127.0.0.1:8200
tailscale serve --bg 8200              # https://<host>.<tailnet>.ts.net/
tailscale serve status
```

Anyone on the tailnet can then open it; nobody off it can reach the port.
`--host 0.0.0.0` also works and publishes an unauthenticated sweep trigger to
whatever LAN the box is on — it prints a warning saying so, and on a site LAN
that is a bad trade.

To keep it up on a server, the unit is unremarkable:

```ini
# /etc/systemd/system/site-map.service
[Unit]
Description=site-map
After=network-online.target tailscaled.service

[Service]
User=techops
WorkingDirectory=/opt/site-map
ExecStart=/opt/site-map/.venv/bin/python sitemap.py serve --central http://techops-automations-host:8100
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

`User=` has to be an account whose `ssh` can reach the site servers
non-interactively: the sweep runs with `BatchMode=yes`, because a passphrase
prompt behind an HTTP request is indistinguishable from a hang. A key with no
passphrase, or an agent that unit can reach, is the requirement — and that
account's key is then a key to every site server, so it belongs on a host you
are willing to treat that way.

Nothing else about the deployment is special: it is stdlib-only, single
process, holds no state and stores no credential, so it can be restarted or
moved at any time and the worst case is a sweep in flight.

## The connection diagram

The page draws the site as four layers, and the whole design rests on one
distinction: **a solid line is a cable something established, a dashed line
is what a FOB does and nobody has checked.** They differ in dash, colour and
weight, so the difference survives greyscale and a projector.

```
OUTSIDE     [ internet / SIM ]
                   |            dashed - nothing has read the WAN side
ROUTER      [ Teltonika RUT ]   mounted high, for signal
                   |            solid where a switch table or LLDP proved it
SWITCH      [ MikroTik / TSW / PSW / Planet ]   in the box
                   |            dashed - which port needs the MAC table
ON THE LAN  [ cameras  radars  lidars  server  operator station ... ]
```

`topology.py` builds it and `export` puts it in the payload, so the page
draws the topology the CLI would draw rather than working out one of its own.

**The bottom row never wraps,** however many devices there are; the panel
scrolls sideways instead. Wrapping was actively misleading: an edge from the
switch to a device on a second row had to pass the first row, so the diagram
read as *switch → server → radar* when those two are only neighbours on the
same switch. One row is also what the hardware looks like — a switch with its
ports in a line — and nothing crosses anything.

**Nothing is identified by address.** `.1` being the router is a bench
convention, and a site where it is untrue is exactly the site where a diagram
built on the convention misleads. The tells, strongest first:

| Thing | How it is recognised |
| --- | --- |
| router | an evidence-backed `gateway` role; else the declared `uplink`; else a model read as a `RUT*`; else the only Teltonika that is not a switch — and that last one says it is the vendor talking |
| switch | a recorded `switch`/`poe-switch` kind; a model read as `TSW`/`PSW`/`IGS-`/`CRS`/`CSS`; a shortlist naming one; or a MikroTik/Routerboard/Planet OUI |

A pattern edge is a question, not an answer, and it is **never written into a
site file's `net:` block** — the same rule `discover.to_yaml` follows, for the
same reason: someone will wire to a diagram.

### Where it refuses to guess

These are the cases that make the diagram worth trusting:

- **Two switches.** A FOB runs a second when one has too few ports, and which
  device is on which is then the exact fact a MAC-address table exists to
  answer. No leaf is attached to either; the note says so.
- **A switch nobody saw.** A FOB has one. An ARP sweep lists only what the
  host has recently exchanged traffic with, so a quiet switch never appears —
  an expected absence, not a missing device. It is drawn as a dashed empty box
  labelled *switch (not seen)*, with the LAN hanging off it, because the
  alternative is pretending the devices plug into the router.
- **No router.** Nothing is rooted, and the note says what would settle it —
  naming the actual unread Teltonika where there is one, and saying plainly
  that there is none where there is not.
- **A declared cable.** Wins outright. The pattern does not also propose a
  different parent for that device, and the edge carries its own evidence
  rather than its parent's.

### Two legs, and the E/I badges

The router carries `br-lan`, `wan` and `tailscale0`; a site server carries its
LAN NIC and `tailscale0`. One `addr` per node cannot say that, and the one
address it does hold is whichever side you happened to come in on — so
`Node.interfaces` is a list, and each entry carries a scope:

- **I** — internal: an address on the site's own subnet.
- **E** — external: a tailnet or WAN leg, reachable off site.

A letter rather than colour alone, on the same principle as the certainty
glyphs: the distinction has to survive greyscale and colour blindness. Scope
comes from the **interface name**, not the address — this router's `wan`
holds `192.168.1.164`, a private address that is emphatically not the site
LAN, and an "is it RFC1918" test would put the site's uplink on the wrong side
of the diagram.

**A device showing one interface is not a device with one NIC.** An ARP entry
can only ever establish the leg facing the host that swept, so
`interfaces_read` records whether anyone actually looked, and the inspector
says so in as many words. Two legs show up for the router (read on the box)
and for anything `probe` has read; everything else shows the one address that
was established.

### Power

Not drawn yet, and the reason is that there is nothing to draw: no site model
carries a single `power:` edge. The switch's PoE port status would prove the
PoE half in one read, and the mains half — which PSU, which breaker, which UPS
— is the half no protocol can answer and the model only accepts `survey`
evidence for. The layers and the solid/dashed rule carry over unchanged when
the data exists.

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

## Seeing a switch that has no address

A ping sweep finds things that hold an address. An unmanaged switch holds
none, so it is not merely missed — it cannot be looked for. `traceroute`
cannot help either: a switch forwards at layer 2 and never decrements TTL, so
it is never a hop, and every device at a FOB is one hop from the router
whether there are zero switches or four in series.

The router's own forwarding database can. `bridge fdb show` reports which MAC
was learned on which physical port, so several MACs on one port means several
devices behind one cable — which is a switch, named or not. The survey reads
it after the sweep on purpose: the database is learned from traffic and ages
out in about five minutes, so the sweep is what makes everything speak.

What that licenses is deliberately narrow, because people wire to diagrams:

| what the port shows | what is claimed |
| --- | --- |
| one MAC | that device is on the far end of that cable |
| several, one of them a switch | that switch's uplink is this port |
| several, none a switch | an unmanaged switch is **on that port**, with those clients |
| several, two of them switches | nothing — their own tables settle it |

The third row is the interesting one. An unmanaged switch is in no table
anywhere, so its model, serial and firmware are unknowable by any protocol —
but its *position* is proven and so is its client list. The page draws it
solid-outlined and hollow rather than dashed, because dashed means "expected"
and this was read.

A port with no carrier is skipped: a dark port carries nothing, and saying so
about a socket with no cable in it is noise rather than a finding.

### Listening is worth more than asking (`--listen`)

Two protocols the devices speak to nobody in particular, captured passively
with `tcpdump` (already installed on RutOS, under `/usr/local`). Opt-in only
because of the time: about 12s plus 35s per live port.

**Spanning-tree BPDUs.** A BPDU is generated by a bridge and *consumed* by
the next bridge along rather than forwarded, so one arriving on a router port
names the nearest bridge on that cable — the thing the router's own
forwarding table cannot say. Three separate facts come out of one frame:

- the sender **is** a switch, because only bridges speak STP. That is the
  first evidence-backed `kind` available for a device whose model nobody has
  read, and it upgraded two devices that had been sitting on a vendor hunch.
- **which** switch the cable lands on, and which of *its* ports, from the
  bridge-id. This is the chain order at a multi-switch site.
- the **root** and the **path cost to it**. A non-zero cost proves a further
  bridge beyond the nearest one, whether or not it holds an address — a
  second hidden-switch detector, independent of the fan-out count. The cost
  also names the link speed, and 200000 is 802.1D's figure for 100 Mb, which
  is how a fast-ethernet bottleneck inside a fabric carrying cameras became
  visible from the router.

The claim is *nearest bridge*, never *directly attached*: an unmanaged switch
forwards BPDUs and leaves no trace. But see the path cost above — that is
often what catches it.

**MikroTik neighbour discovery (MNDP, UDP 5678).** A MikroTik broadcasts its
model, RouterOS version and serial number every 30 seconds, unauthenticated
and unsolicited. Nobody has RouterOS credentials for the field switches, so
this is the only read that will ever settle what they are — and it settled
three of them (`CRS112-8P-4S`, with serials). Parsed as TLVs from a hex
capture rather than scraped out of tcpdump's printable output, because the
strings sit between binary uptime and address fields.

Both are reads, and both get their own evidence source — `stp` and
`discovery` — rather than being filed under `lldp`, which they are not.

### The upstream leg is a hop further out than the diagram's root

Every FOB surveyed routes its default through a **second** Teltonika — the
outdoor SIM unit, on the WAN subnet, invisible to any sweep of the LAN. The
survey records it from `ip route` plus the neighbour entry, so the map says
what the internet leg actually is instead of implying the indoor router is
the end of the line.

### The address plan is a convention, and four sites in seven break it

`ADDR_ROLES` calls `.1` the router. At three of seven sites `.1` is a MikroTik
switch and the Teltonika's `br-lan` is `.2` — and because names were deduped
before the vantage point was merged in, both wanted the node name `router`,
the YAML got two `router:` keys and the later one won. **The device the survey
ran from was silently absent from its own map**, taking its model, firmware
and three interfaces with it. The reading now wins: the vantage point keeps
the name, whatever held that address is renamed from its vendor, and it stops
claiming to be a router — which had the diagram drawing two of them.

### A hyphen cost five switches their model

Lease hostnames name the switch at five sites, spelled `TSW202` or
`Teltonika-TSW202`. The catalogue spells it `Teltonika TSW202`, and a plain
substring test says yes to the first and no to the second. Matching now
squashes separators and consults `aliases`, and `tsw`/`psw` in a hostname
settles `kind` — a switch still on its factory hostname is telling you what
it is.

## Not built yet

The verifier's remaining half: what is in which socket, and what powers it.
`serve` establishes presence and `probe` reads identity; neither can see a
cable.

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
