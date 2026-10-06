# gotcha-atp

The Techops final stamp before production: a **mutation-free acceptance test of
an assembled Gotcha unit**, run by a Techops engineer from a laptop web UI over
Tailscale. It logs in to the unit's server over the tailnet (and separately to
the operator station for its own checks), and proves every part is present,
reachable, on the pinned release, in sync and producing data.

Not in scope: detection correctness, FP/FN, drone classification.

Design: canvas *gotcha-atp-design* v1.5. Every stage is implemented: S0–S9
automated (S9, the power-cycle soak, only in an "Extended run"), S10 and the
manual rows of S7/S9 asked as questions. Field-tested on gotcha-rev3-prototype
and gotcha-rev3-dev (S8 on gotcha-rev3-prototype-operator); the S9 power cut
itself still awaits its first run on real hardware.

The PDF needs pango (`brew install pango`); the tool finds Homebrew's libraries
itself, so `uv run gotcha-atp` produces it without the launcher.

Standalone: imports nothing from `bench/`, `bench-central/`, `hub-admin/` or
anywhere else in this repo. What it shares is convention — uv project, the
`bench-run-record/1` JSON shape (bench-central ingests it unchanged), the
FastAPI single-page pattern, the "no verified record, it doesn't ship" policy.

## Quick start

```bash
brew install uv pango            # pango only for the PDF report
cd techops-tools/gotcha-atp
mkdir -p ~/.config/gotcha-atp
cp config.example.toml ~/.config/gotcha-atp/config.toml
chmod 600 ~/.config/gotcha-atp/config.toml   # then fill in the passwords there
uv run gotcha-atp                # = gotcha-atp ui → http://127.0.0.1:8190
```

Or double-click `launcher/Gotcha ATP.command` (macOS) /
`launcher/gotcha-atp.desktop` (Linux). Requirements on the laptop: Tailscale
connected, OpenSSH (any recent macOS/Linux), uv.

1. **Home** lists every tailnet peer named `*-operator`; pick the unit in front
   of you (or type its site name). The kela password is pre-filled from the
   local config — confirm it. *Connect* opens both SSH hops and shows what the
   operator and the server say they are. **Is this the unit in front of you?**
2. **Run** — every stage runs, live. Red rows carry a hint.
3. **Manual steps** — five Yes/No questions at the C2 (S7.5, S10.1–S10.4).
4. **Result** — verdict, stamp eligibility, PDF, bench-central upload; *Re-run
   failed stages* or *Full run*.

From a terminal (the engine's entry point; the UI is what v1 is for):

```bash
uv run gotcha-atp run --unit kela-gotcha-07              # full run, asks the questions on stdin
uv run gotcha-atp run --unit kela-gotcha-07 --stages S1,S2 --no-manual
uv run gotcha-atp catalogue                              # every declared row
uv run pytest
```

## Access model

The laptop never joins the unit LAN; everything goes through SSH over the tailnet.

| Path | Used for |
| --- | --- |
| server · `ssh kela@<site>` over the tailnet (ControlMaster) — **first choice** | everything except the operator's own checks: ping/arping from the hub's vantage, chronyc, journalctl, kubectl (`sudo -n k3s kubectl`), psql via kubectl exec. Logs in with Tailscale SSH, the laptop key or the kela password |
| `-D` SOCKS on the server session | laptop HTTP / WebSocket → radars .50–.53, APUs .60/.61, camera .30, speaker .70, PoE switch .3 web |
| `-L` on the server session, on demand | hub gRPC ClusterIP:8001, MediaMTX RTSP 8554 (cluster-internal) |
| SSH tunnelled through the server session | RUTM08 .1, TSW202 .2, OTD500 192.168.1.1 (`root`), IGS-4215 .3 CLI |
| operator · `ssh kela@<site>-operator` over the tailnet | only the operator's own checks (getent kela.local, kela-verify, timedatectl, curl). Without it (no peer, offline, login refused) those rows are amber and the run is never stamp-eligible; everything else still runs |
| backup · `kela@192.168.88.10` through the operator | only when the direct server login fails: the operator session then carries the SOCKS forward and the device tunnels too. S0.3 records which path the server session took and why |
| tailnet direct | Fleet admin API (S0.5, S4), bench-central :8100 (S4.9, upload) |

Every master is plain OpenSSH; later commands are multiplexed over them, and
reads are batched so a stage costs about one round-trip. Device SSH host keys
are accepted and not persisted — every unit's devices sit on the same LAN
addresses. The server session uses `HostKeyAlias=<site>` on both paths, so its
key is checked under its own name, not under the shared 192.168.88.10. A
`ControlPersist` / `ControlMaster` in the user's `~/.ssh/config` is overridden
for the tool's own sessions.

## Credentials

`~/.config/gotcha-atp/config.toml` on the laptop (override with
`GOTCHA_ATP_CONFIG`) — see `config.example.toml`. **Never in the repo.** The kela
password is fleet-wide, so Home pre-fills it and the engineer confirms once per
run; device logins (Magos radars, APUs — `[devices.apu]`, falls back to the radar login; an APU that refuses the dashboard login is read with HTTP Basic auth, as the driver does — Raythink, speaker, Teltonika, Planet) and
the optional Fleet token live in the same file.

Passwords are held in memory only. SSH gets them through an askpass helper that
reads the master's environment (never a command line or a file). Every event,
log line, record and report passes through a redactor that masks each secret
and the MD5/AES forms the devices put on the wire. The UI never receives a
password, only whether one is set. When the Techops ATP key is in kela's
`authorized_keys` on both hosts (`init_gotcha_server.sh --ssh-pubkey` and an
operator-setup step, to be added), the key is tried first and the password is
not used.

## Stages

| Stage | Name | Depends on | Phase 1 |
| --- | --- | --- | --- |
| S0 | Pre-flight & discovery (release, tailnet, hops, identity, Fleet, S0.6 device discovery) | — | implemented |
| S1 | Network fabric (ping matrix, duplicate IPs, switch links, PoE draw, RUTM08, OTD500, TSW202, kela.local, MagicDNS) | S0.3 | implemented |
| S2 | Server host (hostname, data disk, memory, chrony, timezone, L4T, services, kernel events) | S0.3 | implemented |
| S3 | Cluster & hub (node + converge, pods, stuck pods, required workloads, hub gRPC health + Gotcha profile, int-gotcha health, C2 from the operator) | S2.7 | implemented |
| S4 | Versions vs release.yaml (hub release + Fleet running/desired, staged and running integration images, magos-agent tag, node-controller and k3s vs pin and Fleet, APU / radar / camera / network gear / speaker firmware from S0.6, operator build, bench record per serial, default detector and every mounted copy) | S0.3 (S3.5 for the hub rows) | implemented |
| S5 | Hub config vs plan (integration + device, radar array, APU assignment and filters, credentials, pan quadrants, calibration, pose, camera/speaker, map center + DEM radius, integration config) | S3.5 | implemented |
| S6 | Sensor liveness (hub radar health, driver session, track history, 15 s sweep sample per radar through its APU, raw mode, probes, latency, video registered / received / decoded, PTZ telemetry, speaker, radar + APU self-status, link stability) | S3.5, S5.1 | implemented |
| S7 | Time & latency (NTP offset matrix vs the server clock, chrony clients and their recency, hub gRPC p95 + kela.local TTFB, ping matrix while both camera streams flow; S7.5 manual) | S0.3 | implemented; S7.5 asked |
| S8 | Operator station (kela-verify, kiosk + Chrome's NSS pin vs the served cert, timesyncd server, kela egress lock + AnyDesk) | S0.3 (operator session) | implemented |
| S9 | Power-cycle soak (optional, "Extended run"): engineer cuts mains → unit back on its own (SSH, cold boot, no hands) → devices rebooted and back → every S1–S8 row that passed before the cut passes again within 15 min → no stuck pods, restarts settled | S0.3 | implemented; S9.0 asked |
| S10 | Operator attestations (manual) | — | asked |

`uv run gotcha-atp catalogue` prints every row with its severity and class.

**Everything runs, amber is not green.** No stage is skipped because an earlier
one failed. A stage whose prerequisite row did not pass reports its rows amber
"prerequisite Sx.y failed"; amber never counts as a pass. Manual questions are
asked whatever the automated stages found.

**Per-unit facts are discovered, never typed.** Unit identity comes from
`/etc/kela/build-info` on both hosts; serials, models, firmware and MACs are read
off every device in S0.6 and printed on the PDF cover. There is no site.yaml.

### Still to confirm on the bench

The parsers for these are tolerant and go amber with the raw output recorded
when they cannot read it — they never pass on a guess:

- IGS-4215 port link/speed table (S1.3) — tries `show interface status`,
  `show interfaces status`, `show port status`; error counters not read yet.
- IGS-4215 `show poe` delivered-power column (S1.4) — found by header name.
- IGS-4215 MAC address table (S1.4 port→device map) — `show mac address-table`.
- RUTM08 live RMS state (S1.5) — `ubus call rms status`.

## Stamp rule

- a **critical or high fail** blocks the stamp;
- a **critical amber** blocks the stamp (so in Phase 1 no run is stamp-eligible
  — the stubbed critical rows are amber by design);
- a **medium or low fail** is a warning: listed on Result and in the PDF, does
  not block;
- only a **full run** can be stamp-eligible; a re-run of failed stages never is.

Verdict: `FAIL` (any critical/high fail) · `INCOMPLETE` (no such fail, but a
critical amber) · `PASS`.

## release.yaml

The only config file: fleet-wide expected versions, thresholds and the LAN plan
(.1 .2 .3 .10 .30 .50–.53 .60 .61 .70, plus the OTD500 at 192.168.1.1),
schema `gotcha-release/1`, PR-reviewed. A pin that is empty or starts with
`TODO` is "not pinned": rows comparing against it record the value and go
amber.

Open: `hub.image_tag` stays `gotcha-rc-23.9` until there is an official Gotcha
release tag (CI sets IMAGE_TAG to the tag name; the newest tag is gotcha-rc-28.9).

Agreed on 2026-10-06:

- integration image digests are not pinned: they follow from the hub release.
  S4.2 checks the node staged its images from the bundle of Fleet's desired
  release, and that the digests it published match its Fleet images report;
- `speaker.outvolume_min: 1` — only a muted speaker fails S6.13: the operator
  sets the volume from the C2, and S10.3 asks whether it is audible;
- `server.l4t: R36.4.3` (S2.6, low) — the prototype's R36.3.0 is flagged;
- `server.k3s_floor` / `server.node_controller_floor` are floors, not pins: the
  node installs exactly what Fleet's system configuration for its hardware model
  says (S4.3 requires that match), and models differ — on 2026-10-06 the
  prototype's NRU-230S_2025-06-17_1734 got node-controller 0.3.60, rev3-dev's
  NRU-230S_2026-01-30_1427 got 0.3.56 (both k3s v1.36.2+k3s1).

Thresholds used by S0–S2: `ping_*` (S1.1), `poe_*` (S1.4),
`modem_signal_min_dbm` (S1.6), `chrony_offset_max_ms` (S2.4),
`disk_used_max_pct` / `mem_available_min_mb` (S2.2/S2.3). S3.2 wants every container running for at least
`pod_stable_min_minutes` (30); earlier restarts are recorded, grouped by when they
happened, and not judged — they cluster at boot and at k3s restarts. Fleet check-in must be
under 5 minutes old (S0.5).

S7 compares every clock against the server's: `ntp_offset_s` for the devices
the laptop reads (each offset carries its measurement uncertainty and is "not
checked" when the read cannot decide), `magos_time_sync_offset_s` for the
radars' and APUs' own measurement. `hub_api_p95_ms` / `hub_ttfb_ms` bound S7.3.

S8 runs `sudo kela-verify` on the operator station. When sudo there wants a
password, the kela password from `config.toml` is passed on the SSH session's
stdin (`sudo -S`) — never on a command line, never in the output.

## Outputs

- `runs/<site>/<UTC>.json` — `bench-run-record/1` (`tool: gotcha-atp`,
  `kind: verify`, `serial` = site, `verified` = stamp eligible) plus `stages[]`,
  `summary` and `device.inventory`.
- `runs/<site>/<UTC>.html` and `.pdf` — cover (site, verdict, run_id, engineer,
  release, discovered inventory), blocking failures, warnings, every row,
  ping matrix, server clock, PoE port map.
- POST to bench-central `/api/v1/runs` over the tailnet; when it is unreachable
  the record waits in `runs/outbox/` and is retried at the next start or with
  *Retry upload* on Result.

`runs/` is gitignored.

## Hub gRPC

The hub is reached on hub-server's ClusterIP:8001 through a `-L` tunnel on the server
session. Kela hubs run with `HUB_BASIC_AUTH=true` and accept any non-empty Basic
credentials; the ATP sends `gotcha-atp`, which is what the hub's audit log records
as the caller. Only health answers without it.

### Stubs

`proto/` is vendored from the kela repo (see `proto/VENDORED.md` for the source
revision and how to refresh it). Stubs are generated into `src/gotcha_atp/_pb/`
(gitignored) on first use, or explicitly with `uv run python scripts/gen_protos.py`.

## Layout

```
gotcha-atp/
├── README.md, release.yaml, pyproject.toml, config.example.toml
├── proto/                     vendored kela protos + grpc health
├── launcher/                  Gotcha ATP.command (macOS), gotcha-atp.desktop (Linux)
├── scripts/gen_protos.py
├── tests/
└── src/gotcha_atp/
    ├── cli.py                 gotcha-atp ui (default) | run | catalogue | gen-protos
    ├── runner.py              runs every stage, prerequisites → amber, manual questions, events
    ├── model.py               RowSpec / Row / StageSpec, Checks, the stamp rule
    ├── context.py             what a stage gets: release, session, creds, facts, rows
    ├── release.py             release.yaml loader + validation (S0.1)
    ├── record.py              bench-run-record/1 writer + stages[]
    ├── hints.yaml             plain-language hint per row id
    ├── access/                session.py (server + operator sessions, backup route), exec.py, socks_http.py,
    │                          socks_ws.py, grpc.py, creds.py, tailnet.py
    ├── devices.py             read-only device clients (login + GETs only)
    ├── discovery.py           S0.6
    ├── stages/                s0 … s10, one module per stage
    ├── report/                html.py, pdf.py
    ├── benchcentral.py        GET per serial, POST record, outbox
    └── ui/                    FastAPI + SSE single page: Home / Run / Manual steps / Result
```

## Deferred

Recorded here so they are found later:

- Zebra certificate label for a passed unit.
- On-server `/etc/kela/atp-passed` stamp.
- Mandatory power-cycle soak (S9 is optional in v1).
- Remote re-verification mode for fielded units (the tailnet-only runner already
  makes it possible; the manual steps would be skipped).
- Open question: should the radar sensor-to-asset sweep calibration be mandatory
  before the stamp? v1: no — S5.10 is amber "not calibrated" and recorded.
- Suggested cleanup outside this tool: retire or rename
  `hub-admin-web/profiles/gotcha.profile.json` (legacy split layout).
