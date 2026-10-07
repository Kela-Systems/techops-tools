# Cursor task: add an "edge" role to the RUTM08 bench configurator

## Context

`bench/rutm-config-ui` provisions a Teltonika RUTM08 as the **main router of a Gotcha server box**:
LAN `192.168.88.1`, DHCP on, WAN on DHCP from the bench uplink. Call this the **server** role.

Gotcha edge boxes now get their own RUTM08 to put the camera, radars, APUs and speaker behind one address. This is the **edge** role:

- WAN static `192.168.88.20/24`, gateway and DNS `192.168.88.1` (the WAN cable goes into the server-box switch).
- LAN `192.168.89.1/24`, DHCP pool `.200–.249`. The edge devices stay on static IPs below `.100`, with gateway `192.168.89.1`.
- TCP port forwards from WAN to the devices (table below). WebUI and SSH open from WAN.
- Same fleet services as the server role: shared password, timezone, NTP to `192.168.88.10`, firmware pin, RMS + pack, Tailscale.
- Internet reaches the edge router through the server-role RUTM08 and the OTD500.

The reference design is the "Gotcha edge router configuration" page. Its Part 1 SSH block is what this mode must reproduce through the tool.

Edge-role port forwards (all TCP, src zone `wan`, dest zone `lan`):

| name | ext port | dest | dest port |
|---|---|---|---|
| cam-web | 8080 | 192.168.89.30 | 80 |
| cam-rtsp | 554 | 192.168.89.30 | 554 |
| radar1-web | 8050 | 192.168.89.50 | 80 |
| radar2-web | 8051 | 192.168.89.51 | 80 |
| radar3-web | 8052 | 192.168.89.52 | 80 |
| radar4-web | 8053 | 192.168.89.53 | 80 |
| apu1-web | 8060 | 192.168.89.60 | 80 |
| apu2-web | 8061 | 192.168.89.61 | 80 |
| speaker-web | 8070 | 192.168.89.70 | 80 |

## Read these first

- `bench/rutm-config-ui/rutm_configure.py`: the pipeline, `time_rows`, `verify_rutm`. The comments explain why the steps run in this order.
- `bench/rutm-config-ui/rutm_app.py` and `static/rutm.html`: detection loop, routes, UI.
- `bench/rutm-config-ui/config/rutm.config.example.json` and `README.md`.
- `bench/bench-core/src/bench_core/__init__.py`, the `TeltonikaClient` methods:
  - `set_wan_static`, `wan_static_check`
  - `set_dhcp_pool`, `dhcp_pool_check`
  - `set_ntp_port_forward`, `ntp_forward_check`, `firewall_zone_networks`
  - `move_lan`, `lan_ip_check`
  - `_uci_package`, `_uci_sections`, `_uci_add`, `_uci`, `_refuse_mutation`
- `bench/bench-core/src/bench_core/bench_ui.py`: `BenchConfigurator`, especially `_do_run` (how `run_cfg` is built) and `load_config`.
- `bench/bench-core/src/bench_core/ip_mode.py` (`IpModeStore`) and `mountIpModes` in `bench-core/src/bench_core/static/bench.js`. This is the existing pattern for an operator-selected, persisted station switch. Follow it.
- `bench/bench-core/src/bench_core/run_record.py` (the `rutm` device block) and `qa_label.py` (`_content_rutm`).
- Test patterns:
  - `bench/rutm-config-ui/tests/*`
  - `bench-core/tests/test_ntp_path_applied.py`
  - `bench-core/tests/test_verify_only_is_mutation_free.py`
- `bench/docs/verification-rows.md`: why rows for things the bench cannot reach are read-backs.

Before writing code, reply with a short plan that maps each requirement below to the files you will change. Then implement it.

## Decisions already made

1. **One app, a role toggle.** It stays on port 8004 with one launcher tile.
   - The operator picks **Server** or **Edge** in the UI.
   - The choice persists in a small state file beside the tool (`role-state.json`, same idea as `ip-state.json`). It is not stored in the config, so `config_hash` does not change when the operator flips it.
   - Default is `server`.
2. **Server role behaviour must not change.** With role = server, every write, its order, the verify rows, the record and the label stay exactly as today. All existing tests pass without edits.
3. **Edge hostname is `rut-edge-<site>`**, done through the role's `name_prefix`. RMS, Tailscale and the tailnet then never hold two `rut-<site>` devices for one site.
4. **Edge DHCP is on, with pool start 200 and limit 50.** Use `set_dhcp_pool` and `dhcp_pool_check`. The pool follows the LAN move, so `move_lan` can keep renewing the station's lease and a later Verify needs no manual adapter setup.
5. **The tool applies everything for edge.** That means the fleet services, static WAN, port forwards, WAN access for WebUI and SSH, and the DHCP pool, each with a verify row. Nothing is left to hand-run SSH.
6. **`record_tool` stays `"rutm"`.** The role goes into the record's `device` block.

## Config shape

Add a `roles` block to `rutm.config.example.json`. The existing top-level keys stay the base, and each role's block is deep-merged over them:

- Dicts merge.
- Lists and scalars replace.
- Keys starting with `_` are ignored.

```json
"roles": {
  "_comment": "Overrides merged over the top-level settings for the role picked in the UI. server = main router of a server box (today's behaviour). edge = router inside a Gotcha edge box.",
  "server": {},
  "edge": {
    "name_prefix": "rut-edge-",
    "lan_ip": "192.168.89.1",
    "ntp": { "enabled": true, "server": "192.168.88.10", "interval": 60 },
    "wan": { "enabled": true, "ipaddr": "192.168.88.20", "netmask": "255.255.255.0",
             "gateway": "192.168.88.1", "dns": "192.168.88.1" },
    "ntp_forward": { "enabled": false },
    "dhcp": { "enabled": true, "start": 200, "limit": 50 },
    "wan_access": { "webui": true, "ssh": true },
    "port_forwards": [
      { "name": "cam-web", "ext_port": 8080, "dest_ip": "192.168.89.30", "dest_port": 80 }
      // ...the nine rules from the table, proto defaults to tcp
    ]
  }
}
```

- Put the edge defaults in `rutm_configure.py` as constants (`DEFAULT_EDGE_*`, including the nine forwards), next to the existing `DEFAULT_RUTM_*`. The edge role must then work when a station's live config has no `roles` block yet.
- The edge NTP interval is 60. The live server config uses 3600. An edge router has no RTC and often boots before the server answers, so a long interval can leave it unable to reach RMS and Tailscale for up to an hour. Keep the existing clamp-to-60 logic.
- `config_check` will warn about a live config without `roles` once the example has it. That is the intended nudge.

## Implementation

### Role resolution (`rutm_app.py`, plus a small hook in `bench_ui.py`)

- Add a `run_config()` hook to `BenchConfigurator`. It returns `json.loads(json.dumps(self.cfg))`, which is what `_do_run` does inline today. Make `_do_run` call it. The RUTM subclass overrides it to return the role-merged config. No other tool changes behaviour.
- Put the merge in one pure function, e.g. `effective_settings(cfg, role)` in `rutm_configure.py`. The CLI uses it too: add `--role server|edge` to `rutm_configure.py`, default `server`.
- `public_state` exposes:
  - `role`
  - the effective `name_prefix`, `lan_ip`, WAN address and DHCP pool
  - a count of port forwards

  The UI shows these instead of the raw top-level values.

### Pipeline (`configure_rutm`)

Edge order:

1. login, password, hostname, timezone, ntp
2. firmware
3. clock
4. rms-enable, rms-register, rms-set-pack
5. tailscale
6. `port-forwards`, `wan-access`, `dhcp-pool`: pure UCI, before the WAN pin
7. `wan-static`
8. verify
9. `move_lan("192.168.89.1")`, last

Add pipeline tests in the style of `test_rutm_configure.py` (`StubClient`) that pin:

- `wan-static` comes after `tailscale`.
- The three edge steps come before `wan-static`.
- `move_lan` is the last call, with `192.168.89.1`.
- `ntp-forward` never runs in edge.

Add a hard guard at the top of the edge pipeline that raises `SystemExit` before any mutation if any of these hold:

- the effective `lan_ip` is `192.168.88.1`
- `lan_ip` is inside the WAN subnet
- WAN is not enabled

Running the server LAN on an edge unit puts a second `.1` on the server-box subnet.

### New client methods (`bench_core/__init__.py`)

- **`set_port_forwards(rules)` and `port_forwards_check(rules)`**
  - Owned rules are named `kela-fwd-<name>`.
  - Reconcile them: update in place by name, add any that are missing, and delete owned rules that are no longer in config.
  - Never touch a redirect the tool does not own. That includes `kela-ntp` and any operator-made rule.
  - Each rule writes `target DNAT`, `src wan`, `dest lan`, `proto` (default `tcp`), `src_dport`, `dest_ip`, `dest_port` and `enabled 1`, then runs `/etc/init.d/firewall reload`.
  - A re-run must leave exactly one copy of each rule.
  - The check returns one row listing anything missing, wrong or extra.
  - Like `set_ntp_port_forward`, report whether the wired `wan` interface is actually in the `wan` firewall zone. If it is not, the rules are green rows over a dead path.
- **`set_wan_access(webui, ssh)` and `wan_access_check(...)`**
  - RutOS keeps remote access as pre-existing, disabled `rule` sections in the `firewall` package. The tool enables existing rules and never adds new ones.
  - Measured on a RUTM08 by diffing `uci show firewall` before and after WebUI → System → Administration → Access control → remote HTTP/HTTPS:
    - Two sections changed, `firewall.16` (`dest_port='80'`) and `firewall.17` (`dest_port='443'`).
    - The only real change was `enabled='0'` being **deleted**. `dest_port` just moved position in the `uci show` output.
    - "Enabled" therefore means the `enabled` option is absent or `1`.
  - **Find the rules by their option values, never by section name or index.** RutOS numbers these sections, and the numbering is a build detail.
  - Match `rule` sections where:
    - `src='wan'`
    - `target='ACCEPT'`
    - `dest_port` is `80` or `443` for the WebUI, `22` for SSH
    - `name` matches the values in the constants below
  - Write `enabled='1'` rather than deleting the option, so the read-back is explicit. The check accepts absent or `1`.
  - Fill the constants from the on-device dump in "Already captured" below, with a comment naming the firmware it was read from.
  - Raise `SystemExit` if a rule cannot be found. A unit without them is a build the tool does not know.
- **DHCP:** reuse `set_dhcp_pool` and `dhcp_pool_check`.
  - Stock `dhcp.lan` has `start='100'` and `limit='150'` and **no `ignore` option**. Treat absent `ignore` as served. If `ignore` is present, delete it rather than writing `0`.
  - Leave the other options alone: `leasetime 12h`, `dhcpv6 server`, `ra server`, `ra_slaac`, `ra_flags`, `ignore_ipv6 1`.
  - Pass `reserved` so the check also flags the device statics (`.30`, `.50`–`.53`, `.60`, `.61`, `.70`) if they ever fall inside the pool.
- **WAN:** stock `network.wan` is `device='wan'`, `proto='dhcp'`, `metric='1'`, `area_type='wan'`. `set_wan_static` must keep `device`, `metric` and `area_type` and change only `proto`, `ipaddr`, `netmask`, `gateway` and `dns`. Add a test that pins this.

Every new mutating method calls `_refuse_mutation` first. Extend `test_verify_only_is_mutation_free.py` to cover them.

### Verify (`time_rows`, `verify_rutm`)

- Edge adds read-back rows for: WAN static, DHCP pool, port forwards and WAN access, plus `lan_ip_check("192.168.89.1")`.
- No row claims a forward actually reaches a device. The devices are not on the bench, and `docs/verification-rows.md` explains why that makes these read-backs.
- `verify_rutm` takes the role from the unit's configure record (`device.role`) when one exists. An edge router re-checked while the toggle says Server is still checked as edge. An explicit role from the UI overrides the record, the same way the site name does today.
- Records without `role` read as `server`.

### Detection (`_detect_host`, `poll_once`)

- Probe the factory address, the server LAN `192.168.88.1` and the edge LAN `192.168.89.1`. Infer the role a provisioned unit already has from the address it answers on (`detected_role`).
- If a unit answers on the other role's final LAN, do not run Configure. Say which role it looks like and ask the operator to switch the toggle. Verify stays available.
- Do not flip the toggle automatically.

### Run record and QA label

- In `build_entry`, the `device` block for both roles gains:
  - `role`
  - `ip`: the LAN the router ended on
  - `wan_ip`: edge only

  Update the `rutm` line in the `run_record.py` docstring.
- In `qa_label._content_rutm`:
  - Edge: `mode="EDGE"`, and `hero_sub` is the WAN address `192.168.88.20`, since that is what an installer types from the server box.
  - Server: the label stays byte-identical. Add a test for both faces.
- bench-central needs no change beyond showing `device.role` if its record view lists device fields. Check `bench-central/static/index.html` and leave it alone if it renders the block generically.

### UI (`static/rutm.html`, `bench.js` only if a shared helper is cleaner)

- Add a Server / Edge segmented control above the site-name field, posting to `POST /api/role`.
  - It is disabled while a run is in flight, and the route refuses with a clear error during a run.
  - The current role shows in the status line and the form title, e.g. "New device · Edge".
- The name preview uses the effective prefix: "Will be named rut-edge-kela-fob-14".
- The settings summary shows the effective final LAN, WAN, DHCP pool and port-forward count for the selected role.
- Add a role column to the history table.
- Edge only: a single-line notice. "The WAN is pinned to 192.168.88.20 after Tailscale. On the bench, plug WAN into a LAN port of a provisioned server-role RUTM08 so the unit keeps internet."

### Docs

Update the following, keeping the existing tone (short, reasons stated):

- `rutm-config-ui/README.md`: add a "Roles" section. Cover what each role writes, the order, and the bench uplink for edge.
- The RUTM08 section of `bench/OPERATOR-GUIDE.md`.
- The `rutm_app.py` module docstring.
- `print_banner`: show the role.

## Don'ts

- Don't change server-role behaviour, the port, `record_tool`, or anything in the other tools beyond the `run_config()` hook.
- Don't put the role in `rutm.config.json` or in `config_hash`.
- Don't invent UCI option or rule names. Use the existing ones (they are in the code) or the ones from the on-device diff.
- Don't add a "did the forward reach the camera" row.
- Don't use `--force_feeds` or change the Tailscale and RMS flows. Edge uses them as-is.

## Done when

- `pytest` from `bench/` passes. All existing RUTM, bench-core and central tests pass unchanged.
- New tests cover:
  - role merge
  - server-role config identity (the merged `run_cfg` for server equals today's)
  - edge step order
  - the edge guard
  - forward reconciliation: idempotent, stale owned rules removed, foreign rules untouched
  - WAN access and DHCP pool read-backs
  - mutation-free verify for the new methods
  - role taken from the record on verify
  - detection role inference
  - the `role` and `ip` fields in the record
  - both label faces
  - the `/api/role` route, including refusal mid-run
- `python3 rutm_configure.py --role edge --site test-edge --label-password …` provisions a real unit, and `--verify --role edge --site test-edge` passes on it.

## Already captured on a RUTM08

```
# diff of `uci show firewall`, before vs after enabling remote HTTP/HTTPS in Access control
113d112
< firewall.16.dest_port='80'
118d116
< firewall.16.enabled='0'
119a118
> firewall.16.dest_port='80'
121d119
< firewall.17.dest_port='443'
126d123
< firewall.17.enabled='0'
127a125
> firewall.17.dest_port='443'

# stock LAN DHCP and WAN
dhcp.lan=dhcp
dhcp.lan.interface='lan'
dhcp.lan.start='100'
dhcp.lan.limit='150'
dhcp.lan.leasetime='12h'
dhcp.lan.dhcpv6='server'
dhcp.lan.ra='server'
dhcp.lan.ra_slaac='1'
dhcp.lan.ra_flags='managed-config' 'other-config'
dhcp.lan.ignore_ipv6='1'
network.wan=interface
network.wan.device='wan'
network.wan.proto='dhcp'
network.wan.metric='1'
network.wan.area_type='wan'
```

The full rule sections are pasted below. They include the names and the SSH rule, which the diff did not touch:

```
<paste the output of the grep in the next block here>
```

Command (run on the same unit):

```sh
ssh root@<router> 'uci show firewall | grep -E "^firewall\.(16|17)\."; \
  for s in $(uci show firewall | sed -n "s/^firewall\.\([^.]*\)\.dest_port=.22.$/\1/p"); do \
    uci show firewall.$s; done; cat /etc/version'
```
