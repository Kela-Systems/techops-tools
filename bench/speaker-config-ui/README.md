# Provision-ISR Speaker Configurator

Bench tool for provisioning Provision-ISR IP speakers, one at a time. Same
connect → configure → next flow as the other bench tools, with **no per-device
inputs** — every speaker gets the same password, NTP server, media file, and
final static IP, so detection is followed by a single **Configure** click.

Unlike the other devices, a fresh speaker arrives on **DHCP** (`admin/123456`),
so there is no fixed factory IP: the tool sweeps the bench subnets
(`192.168.1.0/24`, `192.168.2.0/24`, `192.168.88.0/24`) for a host answering
HTTP whose landing page is the speaker's (`<title>IP Speaker</title>`). The
device speaks a session-cookie **CGI API** (the same one its own web UI uses —
`POST /cgi-bin/CGI?login`, `?config=<name>.get/.set`), with passwords sent as
plain MD5 hex (the device's design, via its `jquery.md5.js`).

## What it does per speaker

1. Login with `admin/123456` (falls back to the new password for re-runs).
2. Change the admin password to `Kelasys123!` (`new_password`) — via the forced
   first-login endpoint (`changepwd.set`) when the device demands it
   (`changepwd:85` at login), else the normal `security.set`.
3. Set NTP to `192.168.88.10` (`datetime.set`, `timesetmode=0`). The `timezone`
   field uses the device's own encoding: `720 +` the UTC offset in minutes
   (`840` = GMT+2).
4. Upload the announcement media file (`.mp3`/`.wav`) to user slot 0 via
   multipart `POST /cgi-bin/mediaupload?idx=0`, after checking the device's
   free space (it only has ~4 MB for user files).
5. **Last step:** move the speaker from its DHCP address to the static
   `192.168.88.70` (gateway/DNS `192.168.88.1`, mask `255.255.255.0`). The
   connection drops by design; success is confirmed by reaching the speaker on
   the new address (the bench PC's DHCP lease is renewed so it can follow).
6. Verify every setting by reading it back off the speaker on its new address.

The IP move runs last because the speaker leaves its DHCP address the moment it
applies.

## Where the speaker ends up (TEC-848)

`192.168.88.70` is the default, not the only answer. The page's **Address
assignment** box offers all four shared modes:

| Mode | Behaviour |
|---|---|
| `fixed` | Every speaker to the same address — set once, survives units and restarts. Starts on the config's `static.ip`. |
| `cycle` | Alternates `static.cycle_min`/`cycle_max` (`.70`/`.71`), for a site taking two. The number is burned only by a run that succeeds, so a failed speaker keeps its slot for the retry. |
| `manual` | Typed per speaker, as a last octet or a full `192.168.88.x`. One on another subnet is refused — the tool writes its own gateway and netmask alongside. |
| `dhcp` | Left on DHCP; the bench assigns nothing and finds the speaker again by MAC over `scan_subnets`. |

Under any mode but `cycle`, every speaker ends on the **same** final IP, so
provision one at a time — finish and unplug before connecting the next.

The selection lives in `ip-state.json` beside the tool rather than in the
config: it is a switch an operator flips during a shift, and putting it in the
config would change `config_hash` every time they did.

Each run records `device.ip` (what the bench assigned — empty under DHCP, since
the lease is the site's to change), `device.ip_mode`, and `device.reached_at`
(where the speaker actually answered). A verify pass reads the mode back out of
that unit's own configure record, so it checks where *this* speaker was sent
rather than the station default. That triplet is also what a QA label needs in
order to print the real address instead of an assumed one (TEC-352).

## Setup

```
cp config/speaker.config.example.json config/speaker.config.json   # then fill in real values
# drop the announcement audio file into config/media/ and point media_file at it
python3 speaker_app.py                                             # opens http://127.0.0.1:8006
```

On the bench PC you don't run this by hand — the top-level launcher
(`Start Bench Tools.bat` / `start-bench.sh`) sets up the shared `.venv` and starts
every tool together.

Because the speaker is on DHCP, the bench PC just needs an address on a subnet
whose DHCP server also serves the speaker; after the run the speaker moves to
wherever the address mode sent it, so be able to reach that subnet to confirm
the move. Under `dhcp` mode that is the subnet it leased from, since the
rediscovery is over ARP and ARP is link-local.

There is also a single-speaker CLI:

```
python3 speaker_configure.py                       # scan the subnets, then configure
python3 speaker_configure.py --host 192.168.1.57   # skip the scan
python3 speaker_configure.py --ip 192.168.88.71    # somewhere other than the default
python3 speaker_configure.py --dhcp                # assign nothing; re-find by MAC
python3 speaker_configure.py --host 127.0.0.1:8080 # dev port-forward (the final
                                                   # IP-move check can't follow a
                                                   # forward, so it reports unverified)
python3 speaker_configure.py --scan-only           # just print what's found
```

## Shared code

This folder must sit next to `bench-core`, the shared local package it installs
(`-e ./bench-core[ui]` from the bench root). That package provides the bench-UI
base (`bench_ui` — detection loop, run machinery, routes, WebSocket, step
logging) and the host DHCP-renew helpers. `speaker_client.py` adds the speaker
client (MD5 login + session cookie with transparent re-login on `-500`,
password/NTP/media/static-IP calls, verification); `speaker_configure.py` adds
the subnet-scan detection, the pipeline, and the CLI; `speaker_app.py` is the
`BenchConfigurator` subclass (fixed-target configure route).

Per-speaker logs land in `logs/` (one JSON per speaker + a rolling
`speaker-config.log`). The real `speaker.config.json` and anything in
`config/media/` are gitignored; only the `*.example.*` template is committed.
