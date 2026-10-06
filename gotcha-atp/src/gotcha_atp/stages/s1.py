"""S1 — Network fabric, from the server's vantage (plus the LAN devices' own CLIs).

Two Planet IGS-4215 commands are still "to confirm on the bench" (canvas
revalidation): the port link/speed table (S1.3) and the MAC address table
(S1.4). Their parsers are tolerant; when they cannot read the output the row
goes amber with the raw output recorded — it never passes on a guess. The
RUTM08's RMS state comes from `ubus call rms get_status` (confirmed on RUTM_R_00.07.24.3).
"""
from __future__ import annotations

import base64
import json
import re
from typing import Iterator, Optional

from ..access.exec import AccessError
from ..context import Context
from ..devices import PlanetCli, normalize_mac
from ..model import Checks, Row, RowSpec, StageSpec, amber, result
from ._common import guarded

STAGE = StageSpec(
    id="S1", name="Network fabric", depends_on=("S0.3",),
    vantage="server (ssh exec)",
    rows=(
        RowSpec("S1.1", "Ping matrix from the server",
                "0% loss; avg < 5 ms; jitter < 5 ms — values recorded", "critical"),
        RowSpec("S1.2", "No duplicate IPs on the plan", "exactly one MAC per address", "high"),
        RowSpec("S1.3", "Switch ports link at 1000 Mb/s, no errors",
                "every used port 1000/full; error counters == 0", "high"),
        RowSpec("S1.4", "PoE ports actually drawing power",
                "exactly 4 powered ports in the radar band (20–45 W) and ≥ 1 more > 0 W "
                "(speaker); mapped radars in band, speaker port > 0 W; total < "
                "release.yaml.poe_budget_w; both power inputs present", "high"),
        RowSpec("S1.5", "Router RUTM08 healthy",
                "RMS enabled and connected; 100.x address; ntpclient polls 192.168.88.10 and nothing else", "high"),
        RowSpec("S1.6", "Modem OTD500 attached",
                "LTE attached; physical SIM primary; signal ≥ floor; WAN reachable", "high"),
        RowSpec("S1.7", "Switch TSW202 healthy", "reachable; NTP server == 192.168.88.10", "medium"),
        RowSpec("S1.8", "kela.local resolves on the operator station", "192.168.88.10", "medium"),
        RowSpec("S1.9", "Tailscale MagicDNS off on the server", "CorpDNS false", "medium",
                "read-back"),
    ),
)

# 3GPP TS 27.007 <AcT> values that mean an LTE (E-UTRA) attach.
LTE_ACT = {7: "E-UTRAN (4G)", 9: "E-UTRAN NB-S1 (4G)", 10: "E-UTRA on a 5G core",
           13: "E-UTRA/NR dual connectivity"}

# S1.2 — a raw ARP probe run on the server (python3 is always there; arping
# usually is not, and the ATP installs nothing). Args: iface, own IP,
# comma-separated targets, own IP again for the duplicate-address check.
# Each target gets three "who has" requests; every distinct sender MAC that
# answers is recorded. The own address is asked with sender IP 0.0.0.0
# (RFC 5227 probe), so any answer at all means another host claims it.
ARP_PROBE = r'''
import json, socket, struct, sys, time
iface, me, targets, dad = sys.argv[1], sys.argv[2], sys.argv[3].split(","), sys.argv[4]
s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0806))
s.bind((iface, 0))
mac = s.getsockname()[4]
def who_has(spa, tpa):
    return (b"\xff" * 6 + mac + b"\x08\x06" + struct.pack("!HHBBH", 1, 0x0800, 6, 4, 1)
            + mac + socket.inet_aton(spa) + b"\x00" * 6 + socket.inet_aton(tpa))
seen = {ip: set() for ip in targets + [dad]}
s.settimeout(0.2)
for _ in range(3):
    for ip in targets:
        s.send(who_has(me, ip))
    s.send(who_has("0.0.0.0", dad))
    end = time.time() + 1.0
    while time.time() < end:
        try:
            f = s.recv(2048)
        except socket.timeout:
            continue
        if len(f) < 42 or f[12:14] != b"\x08\x06" or f[20:22] != b"\x00\x02":
            continue
        sha, spa = f[22:28], socket.inet_ntoa(f[28:32])
        if spa in seen and sha != mac:
            seen[spa].add(sha.hex())
print(json.dumps({k: sorted(v) for k, v in seen.items()}))
'''

PORT_STATUS_COMMANDS = ("show interface status", "show interfaces status", "show port status")
MAC_TABLE_COMMANDS = ("show mac address-table", "show mac-address-table")

_MAC_RE = re.compile(r"\b((?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}|[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4})\b", re.I)
_PORT_RE = re.compile(r"\b(?:gi|gigabitethernet|ge|port)\s*(?:\d+/)*(\d{1,2})\b", re.I)


# ── parsers (pure; covered by tests/test_s1_parsers.py) ─────────────────────

def parse_ping(text: str) -> Optional[dict]:
    m = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received.*?([\d.]+)% packet loss", text)
    if not m:
        return None
    out = {"sent": int(m.group(1)), "received": int(m.group(2)), "loss_pct": float(m.group(3)),
           "min": None, "avg": None, "max": None, "mdev": None}
    r = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms", text)
    if r:
        out.update(min=float(r.group(1)), avg=float(r.group(2)),
                   max=float(r.group(3)), mdev=float(r.group(4)))
    return out


def parallel_script(ips: list[str], command: str, tag: str, setup: str = "") -> str:
    """Run `command` (with $ip set) for every address in parallel, each into its
    own file, then print the results one after another as '@@TAG ip' blocks.
    Parallel jobs writing straight to one pipe can interleave and lose a block."""
    loop = " ".join(ips)
    return (f"{setup}d=$(mktemp -d); "
            f"for ip in {loop}; do ( {command} ) > \"$d/$ip\" 2>&1 & done; wait; "
            f"for ip in {loop}; do printf '@@{tag} %s\\n' \"$ip\"; cat \"$d/$ip\"; "
            "printf '\\n@@END\\n'; done; rm -rf \"$d\"")


def parse_blocks(text: str, tag: str) -> dict[str, str]:
    """'@@TAG <key>\\n...\\n@@END' blocks -> {key: body}."""
    return {m.group(1): m.group(2) for m in
            re.finditer(rf"^@@{tag} (\S+)\n(.*?)\n?@@END$", text, re.S | re.M)}


def parse_poe_draw(text: str) -> tuple[dict[int, float], str]:
    """Per-port delivered watts from `show poe`, found by header name.
    Returns ({port: watts}, header used) or ({}, reason)."""
    lines = text.splitlines()
    header_idx, col = None, None
    for i, line in enumerate(lines):
        if "|" in line and re.search(r"\bport\b", line, re.I):
            cells = [c.strip().lower() for c in line.split("|")]
            for j, c in enumerate(cells):
                if re.search(r"consum|used|actual|output|deliver|power\s*\(w\)|current power", c) \
                        and not re.search(r"limit|max|budget|alloc|class|prio", c):
                    header_idx, col = i, j
                    break
        if col is not None:
            break
    if col is None:
        return {}, "no delivered-power column in `show poe`"
    head = lines[header_idx].split("|")[col].strip()
    milli = bool(re.search(r"\bmw\b|\(mw\)", head, re.I))
    out: dict[int, float] = {}
    for line in lines[header_idx + 1:]:
        m = re.match(r"\s*(?:gi)?0?(\d+)\s*\|", line, re.I)
        if not m:
            continue
        cells = line.split("|")
        if col >= len(cells):
            continue
        v = re.search(r"[\d.]+", cells[col])
        if v:
            w = float(v.group(0))
            out[int(m.group(1))] = w / 1000 if milli else w
    return out, head


def parse_mac_table(text: str) -> dict[str, int]:
    """mac -> switch port from a MAC address table, whatever its column order."""
    out = {}
    for line in text.splitlines():
        mac, port = _MAC_RE.search(line), _PORT_RE.search(line)
        if mac and port:
            nm = normalize_mac(mac.group(1))
            if nm:
                out[nm] = int(port.group(1))
    return out


def parse_port_status(text: str) -> dict[int, dict]:
    """port -> {link, speed, duplex} from a port status table (format to
    confirm on the bench). Rows without a recognisable link state are skipped."""
    out = {}
    for line in text.splitlines():
        port = _PORT_RE.search(line)
        if not port:
            continue
        low = line.lower()
        link = "up" if re.search(r"\b(up|link ?up|connected)\b", low) else \
            "down" if re.search(r"\b(down|link ?down|not ?connected|notconnect)\b", low) else None
        if link is None:
            continue
        speed = re.search(r"\b(10000|1000|100|10)\s*(?:m|mbps|mb/s)?\b|\b(10|1)g\b", low)
        duplex = re.search(r"\b(full|half)\b|\b\d+(f|h)\b", low)
        out[int(port.group(1))] = {
            "link": link,
            "speed": (speed.group(1) or str(int(speed.group(2)) * 1000)) if speed else None,
            "duplex": ({"f": "full", "h": "half"}.get(duplex.group(2)) or duplex.group(1)) if duplex else None,
        }
    return out


def parse_netdev_status(text: str) -> list[dict]:
    """`ubus call network.device status` -> physical ports with carrier."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    ports = []
    for name, d in (data or {}).items():
        if not isinstance(d, dict) or not d.get("carrier"):
            continue
        if d.get("type") in ("bridge", "loopback") or name.startswith(("br-", "lo", "wlan", "tailscale", "wg", "tun", "qmimux", "wwan")):
            continue
        if "." in name:            # VLAN sub-interfaces repeat their parent's link
            continue
        st = d.get("statistics") or {}
        ports.append({"name": name, "speed": str(d.get("speed") or ""),
                      "errors": int(st.get("rx_errors") or 0) + int(st.get("tx_errors") or 0)})
    return ports


def parse_cops_act(text: str) -> Optional[int]:
    m = re.search(r"\+COPS:\s*\d+\s*,\s*\d+\s*,\s*\"[^\"]*\"\s*,\s*(\d+)", text)
    return int(m.group(1)) if m else None


def parse_signal_dbm(text: str) -> Optional[int]:
    m = re.search(r"-\d{2,3}", text)
    return int(m.group(0)) if m else None


def parse_primary_sim(text: str) -> Optional[str]:
    """Position of the primary SIM from `uci show simcard` ('1'/'2' physical,
    '3' eSIM), or None when it cannot be told."""
    sections: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        m = re.match(r"simcard\.([^.=]+)\.(\w+)='?([^']*)'?$", line.strip())
        if m:
            sections.setdefault(m.group(1), {})[m.group(2)] = m.group(3)
    for sec, opts in sections.items():
        if opts.get("primary") == "1":
            if "position" in opts:
                return opts["position"]
            idx = re.search(r"\[(\d+)\]", sec)
            return str(int(idx.group(1)) + 1) if idx else None
    return None


def parse_ntpclient(text: str) -> dict:
    """`uci show ntpclient` (the client RutOS actually polls with, written by
    the WebUI and the bench) → {enabled, servers in section order}."""
    servers = [m.group(1) for m in re.finditer(r"^ntpclient\.[\w@\[\]-]+\.(?:hostname|server)='?([^'\n]+)'?$",
                                               text, re.M)]
    m = re.search(r"^ntpclient\.[\w@\[\]-]+\.enabled='?(\d)'?$", text, re.M)
    return {"enabled": bool(m) and m.group(1) == "1", "servers": servers}


def rms_state(raw: str) -> tuple[Optional[bool], str]:
    """(connected / not / unknown, why) from `ubus call rms get_status`
    (RutOS 07.x; there is no `rms status` method). Seen on the rev3 routers:
      connected      {"connection_status": 0, "error": 0, "error_text": ""}
                     (and an ESTABLISHED socket to the RMS broker)
      not connected  {"connection_status": 1, "error": 1, "error_code": 15,
                      "error_text": "Expired license", "next_retry": …}
    Other shapes fall back to a tolerant key scan."""
    try:
        data = json.loads(raw) if raw.strip() else None
    except ValueError:
        data = None
    if isinstance(data, dict) and "connection_status" in data:
        if data.get("error") or data.get("connection_status") != 0:
            why = str(data.get("error_text") or "").strip() or f"connection_status {data.get('connection_status')}"
            return False, why + (f" (error {data['error_code']})" if data.get("error_code") else "")
        return True, "connected"
    conn = rms_connected(raw)
    return conn, {True: "connected", False: "not connected"}.get(conn, "")


def rms_connected(raw: str) -> Optional[bool]:
    """Connected / not / unknown from an RMS status payload of an unknown
    shape (older firmware) — a tolerant key scan."""
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    hit = {"seen": False, "connected": False}

    def walk(node) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                kl = str(k).lower()
                if ("connect" in kl and "disconnect" not in kl) or kl in ("status", "state"):
                    if not isinstance(v, (dict, list)):
                        hit["seen"] = True
                        if v is True or str(v).strip().lower() in ("1", "true", "connected", "online", "up"):
                            hit["connected"] = True
                walk(v)
        elif isinstance(node, list):
            for it in node:
                walk(it)

    walk(data)
    return hit["connected"] if hit["seen"] else None


# ── rows ────────────────────────────────────────────────────────────────────

def _targets(ctx: Context) -> dict[str, str]:
    t = dict(ctx.release.plan.addresses())
    op_ip = ctx.facts.get("operator_lan_ip")
    if op_ip:
        t[op_ip] = "operator station"
    return t


def _s11(ctx: Context) -> Row:
    spec = STAGE.spec("S1.1")
    th = ctx.release.thresholds
    targets = _targets(ctx)
    n = int(th["ping_count"])
    script = parallel_script(list(targets), f"ping -n -q -c {n} -i 0.2 -W 1 \"$ip\"", "PING")
    r = ctx.server(script, timeout=n * 0.2 + 30)
    blocks = parse_blocks(r.out, "PING")
    matrix, c, raw = {}, Checks(), {}
    for ip, name in targets.items():
        p = parse_ping(blocks.get(ip, ""))
        matrix[ip] = {"name": name, **(p or {})}
        if p is None:
            raw[ip] = blocks.get(ip, "")[-400:] or (r.err or r.out)[-400:]
            c.add(f"{name} .{ip.rsplit('.', 1)[1]}", False,
                  "no ping result" + (f" ({raw[ip].strip().splitlines()[-1][:80]})" if raw[ip].strip() else ""))
            continue
        ok = (p["loss_pct"] <= th["ping_loss_pct"] and p["avg"] is not None
              and p["avg"] < th["ping_avg_ms"] and p["mdev"] < th["ping_jitter_ms"])
        c.add(f"{name} .{ip.rsplit('.', 1)[1]}", ok,
              f"{p['loss_pct']:g}% loss" + (f", avg {p['avg']:.2f} ms, jitter {p['mdev']:.2f} ms"
                                           if p["avg"] is not None else ""))
    if "operator station" not in targets.values():
        c.items.append({"label": "operator station", "ok": True,
                        "actual": "not pinged (LAN address unknown — no operator session)"})
    ctx.facts["ping_matrix"] = matrix
    return c.row(spec, matrix=matrix, **({"raw_missing": raw} if raw else {}))


def arp_probe_command(iface_of: str, targets: list[str], dad: str) -> str:
    """Shell command running ARP_PROBE on the server as root. The script is
    shipped base64-encoded so no quoting can break it; the interface and the
    server's own address come from the route to `iface_of`."""
    b64 = base64.b64encode(ARP_PROBE.encode()).decode()
    return (f"set -- $(ip -4 -o route get {iface_of} | "
            "sed -n 's/.* dev \\([^ ]*\\).* src \\([^ ]*\\).*/\\1 \\2/p'); "
            f"echo {b64} | base64 -d | sudo -n python3 - \"$1\" \"$2\" {','.join(targets)} {dad}")


def parse_arp_probe(text: str) -> Optional[dict[str, list[str]]]:
    """The probe's JSON line -> {ip: [mac, ...]}, or None when it did not run."""
    for line in reversed(text.strip().splitlines()):
        if line.startswith("{"):
            try:
                return {k: [normalize_mac(m) for m in v] for k, v in json.loads(line).items()}
            except ValueError:
                return None
    return None


def _s12(ctx: Context) -> Row:
    spec = STAGE.spec("S1.2")
    plan = ctx.release.plan
    targets = plan.addresses()
    r = ctx.server(arp_probe_command(plan.router, list(targets), plan.server), timeout=40)
    if "a password is required" in r.out:
        return amber(spec, "sudo on the server asked for a password (kela needs passwordless sudo)")
    found = parse_arp_probe(r.out)
    if found is None:
        return amber(spec, "ARP probe did not run on the server: "
                     + ctx.redact((r.out or r.err).strip().splitlines()[-1][:120] if (r.out or r.err).strip()
                                  else f"rc {r.rc}"))
    c, seen = Checks(), {}
    for ip, name in {**targets, plan.server: "server (self, DAD)"}.items():
        macs = set(found.get(ip) or [])
        seen[ip] = sorted(macs)
        if ip == plan.server:
            c.add(name, not macs, "no other host claims it" if not macs else
                  "ALSO answered by " + ", ".join(sorted(macs)))
        elif len(macs) == 1:
            c.add(f"{name} .{ip.rsplit('.', 1)[1]}", True, next(iter(macs)))
        elif not macs:
            c.add(f"{name} .{ip.rsplit('.', 1)[1]}", None, "no ARP reply")
        else:
            c.add(f"{name} .{ip.rsplit('.', 1)[1]}", False, "DUPLICATE: " + ", ".join(sorted(macs)))
    ctx.facts["arp"] = seen
    return c.row(spec, arp=seen)


def _planet(ctx: Context) -> PlanetCli:
    """One CLI session for S1.3 and S1.4. A failed login is remembered and not
    retried: the switch locks out after 3 wrong attempts, and the lockout looks
    exactly like its broken-SSH firmware fault."""
    if "_planet_error" in ctx.facts:
        raise AccessError(ctx.facts["_planet_error"])
    cli = ctx.facts.get("_planet_cli")
    if cli is None:
        cli = PlanetCli(ctx.session.proxy_command(ctx.release.plan.poe_switch, 22),
                        ctx.release.plan.poe_switch, ctx.creds.device("planet"))
        try:
            cli.open()
        except AccessError as e:
            ctx.facts["_planet_error"] = str(e)
            raise
        ctx.facts["_planet_cli"] = cli
    return cli


def _first_parsable(cli: PlanetCli, commands: tuple[str, ...], parse) -> tuple[str, str, object]:
    raw = ""
    for cmd in commands:
        out = cli.cli(cmd)
        raw += f"$ {cmd}\n{out}\n"
        parsed = parse(out)
        if parsed:
            return cmd, raw, parsed
    return "", raw, None


def _s13(ctx: Context) -> Row:
    spec = STAGE.spec("S1.3")
    c, detail = Checks(), {}
    try:
        cmd, raw, ports = _first_parsable(_planet(ctx), PORT_STATUS_COMMANDS, parse_port_status)
        detail["igs4215_raw"] = raw[-6000:]
        if not ports:
            c.add("IGS-4215", None, "port-status command not confirmed on this firmware (raw output recorded)")
        else:
            up = {p: v for p, v in ports.items() if v["link"] == "up"}
            bad = [f"gi{p} {v['speed'] or '?'}/{v['duplex'] or '?'}" for p, v in sorted(up.items())
                   if v["speed"] != "1000" or (v["duplex"] and v["duplex"] != "full")]
            c.add("IGS-4215", not bad, f"{len(up)} ports up" + (", not 1000/full: " + ", ".join(bad) if bad else
                                                               " at 1000/full") + f" (`{cmd}`); error counters not read")
            detail["igs4215_ports"] = ports
    except AccessError as e:
        c.add("IGS-4215", None, ctx.redact(str(e)))
    r = ctx.device_ssh(ctx.release.plan.switch).run("ubus call network.device status")
    if not r.ok:
        c.add("TSW202", None, ctx.redact(r.err.strip() or f"rc {r.rc}"))
    else:
        ports = parse_netdev_status(r.out)
        detail["tsw202_ports"] = ports
        bad = [f"{p['name']} {p['speed'] or '?'}" for p in ports if not re.fullmatch(r"1000F", p["speed"])]
        errs = [f"{p['name']} {p['errors']}" for p in ports if p["errors"]]
        c.add("TSW202", (not bad and not errs) if ports else None,
              (f"{len(ports)} ports linked" + (", not 1000F: " + ", ".join(bad) if bad else " at 1000F")
               + (", errors: " + ", ".join(errs) if errs else ", 0 errors")) if ports else "no linked ports parsed")
    return c.row(spec, **detail)


def _s14(ctx: Context) -> Row:
    spec = STAGE.spec("S1.4")
    th = ctx.release.thresholds
    lo, hi = th["poe_radar_w"]
    try:
        cli = _planet(ctx)
        poe_raw = cli.cli("show poe")
        draw, head = parse_poe_draw(poe_raw)
        _, mac_raw, mac_table = _first_parsable(cli, MAC_TABLE_COMMANDS, parse_mac_table)
    except AccessError as e:
        return amber(spec, ctx.redact(str(e)))
    detail: dict = {"show_poe": poe_raw[-6000:], "mac_table_raw": mac_raw[-6000:]}
    if not draw:
        return amber(spec, f"{head} — confirm the command on the bench (raw output recorded)",
                     detail=detail)
    roles = ctx.facts.get("mac_to_role") or {}
    port_roles: dict[int, list[str]] = {}
    for mac, port in (mac_table or {}).items():
        if mac in roles:
            port_roles.setdefault(port, []).append(roles[mac])
    ports = {p: {"watts": w, "devices": port_roles.get(p, [])} for p, w in sorted(draw.items())}
    detail.update(ports=ports, draw_column=head)
    ctx.facts["poe_port_map"] = ports

    in_band = [p for p, w in draw.items() if lo <= w <= hi]
    other = [p for p, w in draw.items() if w > 0 and p not in in_band]
    total = sum(draw.values())
    c = Checks()
    c.add("radar band", len(in_band) == 4, f"{len(in_band)} ports at {lo}–{hi} W "
          f"({', '.join(f'gi{p} {draw[p]:.1f} W' for p in sorted(in_band))})")
    c.add("other powered", len(other) >= 1, f"{len(other)} ports > 0 W "
          f"({', '.join(f'gi{p} {draw[p]:.1f} W' for p in sorted(other))})" if other else "none")
    c.add("total", total < th["poe_budget_w"], f"{total:.1f} W (budget {th['poe_budget_w']} W)")
    pwr = ctx.facts.get("poe_power_inputs")
    c.add("power inputs", (pwr.get("PWR1") and pwr.get("PWR2")) if pwr else None,
          ", ".join(f"{k} {'on' if v else 'off'}" for k, v in pwr.items()) if pwr else "not read (S0.6)")
    if port_roles:
        for port, names in sorted(port_roles.items()):
            for name in names:
                w = draw.get(port, 0.0)
                if name.startswith("radar_"):
                    c.add(name, lo <= w <= hi, f"gi{port} {w:.1f} W")
                elif name == "speaker":
                    c.add("speaker", w > 0, f"gi{port} {w:.1f} W")
    else:
        c.items.append({"label": "port map", "ok": True,
                        "actual": "MAC table not resolved — layout recorded by draw only"})
    return c.row(spec, **detail)


def _s15(ctx: Context) -> Row:
    spec = STAGE.spec("S1.5")
    s = ctx.device_ssh(ctx.release.plan.router).sections({
        "rms_enable": "uci -q get rms_mqtt.rms_connect_mqtt.enable",
        "rms_status": "ubus call rms get_status 2>/dev/null",
        # The broker link itself: rms_mqtt keeps one TCP session to rms_ip:rms_port.
        "rms_link": "p=$(uci -q get rms_mqtt.rms_mqtt.rms_port); "
                    "[ -n \"$p\" ] && netstat -tn 2>/dev/null | grep ESTABLISHED | grep -c \":$p \"",
        "ts": "tailscale ip -4 2>/dev/null",
        "ntpclient": "uci -q show ntpclient",
        "sysntp": "uci -q get system.ntp.enabled; uci -q get system.ntp.server",
    })
    if all(r.rc == 255 for r in s.values()):
        return result(spec, ctx.redact(s["ntpclient"].err.strip() or "router unreachable over SSH"), False)
    c = Checks()
    c.add("RMS enabled", s["rms_enable"].text == "1", s["rms_enable"].text or "unset")
    conn, why = rms_state(s["rms_status"].out)
    link = s["rms_link"].text
    if conn and link == "0":
        conn, why = False, "reports connected, but holds no connection to the RMS broker"
    elif conn and link.isdigit():
        why += " (broker link established)"
    c.add("RMS connected", conn, why or "rms get_status returned nothing usable")
    ts = s["ts"].text.splitlines()[0] if s["ts"].text else ""
    c.add("tailscale", ts.startswith("100."), ts or "no address")
    want = ctx.release.plan.server
    client = parse_ntpclient(s["ntpclient"].out)
    sys_lines = s["sysntp"].text.splitlines()
    sys_enabled = bool(sys_lines) and sys_lines[0] == "1"
    sys_servers = sys_lines[1].split() if len(sys_lines) > 1 else []
    if client["servers"]:
        servers = client["servers"]
        c.add("NTP client", client["enabled"] and servers[0] == want,
              f"ntpclient {'enabled' if client['enabled'] else 'DISABLED'}, first server {servers[0]}"
              + ("" if servers[0] == want else f" (want {want})"))
    else:
        servers = sys_servers
        c.add("NTP client", sys_enabled and want in servers,
              f"no ntpclient servers; system.ntp {'enabled' if sys_enabled else 'disabled'}: "
              + (" ".join(servers) or "unset"))
    # system.ntp is RutOS's dormant OpenWrt timeserver list (stock pool.ntp.org);
    # it only counts when it is switched on.
    active = servers + ([x for x in sys_servers if x not in servers] if sys_enabled and client["servers"] else [])
    extra = [x for x in active if x != want]
    c.add("other NTP servers", not extra, "none" if not extra else
          ", ".join(extra) + " — stock leftovers: each costs a failover timeout offline and "
                             "lets the router drift to internet time (the bench removes them)")
    return c.row(spec, rms_status=s["rms_status"].text[:2000],
                 ntp={"ntpclient": client, "system_ntp": {"enabled": sys_enabled, "servers": sys_servers}})


def _s16(ctx: Context) -> Row:
    spec = STAGE.spec("S1.6")
    s = ctx.device_ssh(ctx.release.plan.modem).sections({
        "cops": "gsmctl -A 'AT+COPS?'",
        "signal": "gsmctl -q",
        "sim": "uci show simcard",
        "wan": "ping -c 3 -W 3 8.8.8.8",
    }, timeout=40)
    if all(r.rc == 255 for r in s.values()):
        return result(spec, ctx.redact(s["cops"].err.strip() or "modem unreachable over SSH"), False)
    c = Checks()
    act = parse_cops_act(s["cops"].out)
    c.add("access tech", act in LTE_ACT if act is not None else False,
          LTE_ACT.get(act, f"AcT {act}") if act is not None else (s["cops"].text or "not attached"))
    pos = parse_primary_sim(s["sim"].out)
    c.add("primary SIM", pos in ("1", "2") if pos else None,
          {"1": "physical slot 1", "2": "physical slot 2", "3": "eSIM"}.get(pos or "", pos or "unknown"))
    dbm = parse_signal_dbm(s["signal"].out)
    floor = ctx.release.thresholds["modem_signal_min_dbm"]
    c.add("signal", dbm >= floor if dbm is not None else None,
          f"{dbm} dBm (floor {floor})" if dbm is not None else "not read")
    wan = parse_ping(s["wan"].out)
    c.add("WAN", bool(wan and wan["received"] > 0),
          f"8.8.8.8 {wan['received']}/{wan['sent']}" if wan else "ping failed")
    return c.row(spec)


def _s17(ctx: Context) -> Row:
    spec = STAGE.spec("S1.7")
    s = ctx.device_ssh(ctx.release.plan.switch).sections({
        "ntp": "uci -q get system.ntp.server", "board": "ubus call system board"})
    if all(r.rc == 255 for r in s.values()):
        return result(spec, ctx.redact(s["ntp"].err.strip() or "switch unreachable over SSH"), False)
    c = Checks()
    try:
        board = json.loads(s["board"].out)
    except ValueError:
        board = {}
    c.add("reachable", bool(board), board.get("model") or s["board"].text[:80] or "no board info")
    c.add("NTP server", ctx.release.plan.server in s["ntp"].text.split(), s["ntp"].text or "unset")
    return c.row(spec)


def _s18(ctx: Context) -> Row:
    spec = STAGE.spec("S1.8")
    if not ctx.session.has_operator:
        return amber(spec, "no operator session — " + ctx.redact(ctx.session.operator_error))
    r = ctx.operator("getent hosts kela.local", timeout=15)
    got = r.text.split()[0] if r.text else ""
    return result(spec, got or "does not resolve", got == ctx.release.plan.server)


def _s19(ctx: Context) -> Row:
    spec = STAGE.spec("S1.9")
    r = ctx.server("sudo -n tailscale debug prefs", timeout=15)
    try:
        prefs = json.loads(r.out)
    except ValueError:
        return amber(spec, ctx.redact(r.text[:160] or r.err.strip() or "no prefs"))
    corp = prefs.get("CorpDNS")
    return result(spec, f"CorpDNS {str(corp).lower()}", corp is False)


def run(ctx: Context) -> Iterator[Row]:
    try:
        yield from guarded(ctx, STAGE, (_s11, _s12, _s13, _s14, _s15, _s16, _s17, _s18, _s19))
    finally:
        cli = ctx.facts.pop("_planet_cli", None)
        ctx.facts.pop("_planet_error", None)
        if cli is not None:
            cli.close()
