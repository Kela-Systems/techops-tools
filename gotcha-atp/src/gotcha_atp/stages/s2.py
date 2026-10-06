"""S2 — Server host. Every read is batched into one SSH round-trip on the server session."""
from __future__ import annotations

import re
from typing import Iterator, Optional

from ..access.exec import ExecResult
from ..context import Context
from ..model import Checks, Row, RowSpec, StageSpec, amber, result
from ._common import guarded

STAGE = StageSpec(
    id="S2", name="Server host", depends_on=("S0.3",),
    vantage="server (ssh exec)",
    rows=(
        RowSpec("S2.1", "Hostname and machine-id", "hostname == site; valid 32-hex id",
                "high", "read-back"),
        RowSpec("S2.2", "Data disk mounted and healthy", "mounted; in fstab; < 80% used", "critical"),
        RowSpec("S2.3", "Root disk and memory", "root < 80%; available > 2048 MB", "high"),
        RowSpec("S2.4", "Host clock synced and serving",
                "leap normal; |offset| < 50 ms; ≥ 1 source; clients include APUs, radars, "
                "camera, router", "high"),
        RowSpec("S2.5", "Timezone", "Asia/Jerusalem", "low", "read-back"),
        RowSpec("S2.6", "Jetson platform version",
                "== release.yaml.server.l4t when set, otherwise informational", "low", "read-back"),
        RowSpec("S2.7", "Tailscale and k3s services", "active; 100.x address", "high"),
        RowSpec("S2.8", "No link flaps or I/O errors since boot", "0 events", "medium"),
    ),
)

DATA_MOUNT = "/mnt/data"
# "SATA link up" is matched only so a later "link down" on the same port can be
# told apart from the boot probe of an empty port (see kernel_events).
KERNEL_EVENTS = (r"link is down|link down|SATA link up|xhci.*(reset|died)|I/O error"
                 r"|exception Emask|hard resetting link|failed command")

COMMANDS = {
    "hostname": "hostname",
    "machine_id": "cat /etc/machine-id",
    "mountpoint": f"mountpoint -q {DATA_MOUNT} && echo mounted || echo not-mounted",
    "findmnt": f"findmnt -no UUID,SOURCE {DATA_MOUNT}",
    "fstab": "grep -v '^[[:space:]]*#' /etc/fstab",
    "df_data": f"df -P {DATA_MOUNT}",
    "df_root": "df -P /",
    "free": "free -m",
    # chronyd may only take commands from root ("506 Cannot talk to daemon"
    # for kela), so all three reads go through sudo.
    "tracking": "sudo -n chronyc -n tracking",
    "sources": "sudo -n chronyc -n sources",
    "clients": "sudo -n chronyc -n clients",
    "tz": "timedatectl show -p Timezone --value",
    "l4t": "head -n 1 /etc/nv_tegra_release",
    "active": "systemctl is-active tailscaled k3s",
    "tsip": "tailscale ip -4",
    "kernel": f"sudo -n journalctl -k -b --no-pager -o short-monotonic | grep -Ei '{KERNEL_EVENTS}' | tail -n 50",
}


# ── parsers (pure; covered by tests/test_s2_parsers.py) ─────────────────────

def df_used_pct(text: str) -> Optional[int]:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    m = re.search(r"(\d+)%", lines[-1])
    return int(m.group(1)) if m else None


def free_available_mb(text: str) -> Optional[int]:
    for line in text.splitlines():
        if line.lower().startswith("mem:"):
            cols = line.split()
            # Mem: total used free shared buff/cache available
            if len(cols) >= 7:
                return int(cols[6])
    return None


def chrony_tracking(text: str) -> dict:
    out: dict = {"leap": None, "offset_ms": None, "stratum": None, "ref": None}
    for line in text.splitlines():
        key, _, val = line.partition(":")
        key, val = key.strip().lower(), val.strip()
        if key == "leap status":
            out["leap"] = val
        elif key == "stratum":
            out["stratum"] = int(val) if val.isdigit() else val
        elif key == "reference id":
            out["ref"] = val
        elif key == "system time":
            m = re.match(r"([\d.]+) seconds (fast|slow)", val)
            if m:
                ms = float(m.group(1)) * 1000
                out["offset_ms"] = ms if m.group(2) == "fast" else -ms
    return out


def chrony_sources(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        m = re.match(r"^([\^=#])([*+\-?x~ ])\s+(\S+)", line)
        if m:
            out.append({"mode": m.group(1), "state": m.group(2), "name": m.group(3)})
    return out


def chrony_clients(text: str) -> set[str]:
    return {m.group(1) for m in re.finditer(r"^(\d+\.\d+\.\d+\.\d+)\s", text, re.M)}


def kernel_events(text: str) -> tuple[list[str], list[str]]:
    """(events, empty SATA ports). At boot the kernel probes every SATA port
    and logs 'ataN: SATA link down (SStatus 0 …)' for one with nothing plugged
    in — not a fault (the NRU-230S has two ports; a unit with an NVMe data disk
    reports both). A port that came up and then went down is a real event."""
    events, empty, up = [], [], set()
    for line in (x.strip() for x in text.splitlines()):
        if not line:
            continue
        m = re.search(r"\b(ata\d+): SATA link (up|down)", line)
        if m and m.group(2) == "up":
            up.add(m.group(1))
            continue
        if m and "SStatus 0 " in line and m.group(1) not in up:
            empty.append(m.group(1))
            continue
        events.append(line)
    return events, empty


def l4t_version(text: str) -> Optional[str]:
    """'# R36 (release), REVISION: 4.3, ...' -> 'R36.4.3'."""
    m = re.search(r"R(\d+)\s*\(release\),\s*REVISION:\s*([\d.]+)", text)
    return f"R{m.group(1)}.{m.group(2)}" if m else None


# ── rows ────────────────────────────────────────────────────────────────────

def _sec(ctx: Context) -> dict[str, ExecResult]:
    if "_s2" not in ctx.facts:
        ctx.facts["_s2"] = ctx.server_sections(COMMANDS, timeout=60)
    return ctx.facts["_s2"]


def _s21(ctx: Context) -> Row:
    s = _sec(ctx)
    c = Checks()
    c.add("hostname", s["hostname"].text == ctx.unit.site, s["hostname"].text or "—")
    mid = s["machine_id"].text
    c.add("machine-id", bool(re.fullmatch(r"[0-9a-f]{32}", mid)), mid or "missing")
    return c.row(STAGE.spec("S2.1"))


def _s22(ctx: Context) -> Row:
    s = _sec(ctx)
    th = ctx.release.thresholds["disk_used_max_pct"]
    c = Checks()
    mounted = s["mountpoint"].text == "mounted"
    c.add("mounted", mounted, DATA_MOUNT if mounted else "not mounted")
    uuid = (s["findmnt"].text.split() or [""])[0]
    in_fstab = bool(uuid) and (uuid in s["fstab"].out or DATA_MOUNT in s["fstab"].out)
    c.add("fstab", in_fstab, f"UUID={uuid}" if in_fstab else "no fstab entry")
    used = df_used_pct(s["df_data"].out) if mounted else None
    c.add("used", (used < th) if used is not None else (False if not mounted else None),
          f"{used}%" if used is not None else "n/a")
    return c.row(STAGE.spec("S2.2"))


def _s23(ctx: Context) -> Row:
    s = _sec(ctx)
    t = ctx.release.thresholds
    c = Checks()
    used = df_used_pct(s["df_root"].out)
    c.add("root", used < t["disk_used_max_pct"] if used is not None else None,
          f"{used}% used" if used is not None else "df unreadable")
    avail = free_available_mb(s["free"].out)
    c.add("memory", avail > t["mem_available_min_mb"] if avail is not None else None,
          f"{avail} MB available" if avail is not None else "free unreadable")
    return c.row(STAGE.spec("S2.3"))


def _s24(ctx: Context) -> Row:
    s = _sec(ctx)
    plan = ctx.release.plan
    tr = chrony_tracking(s["tracking"].out)
    src = chrony_sources(s["sources"].out)
    c = Checks()
    if not s["tracking"].ok:
        why = ctx.redact(s["tracking"].text.splitlines()[-1][:100] if s["tracking"].text else f"rc {s['tracking'].rc}")
        c.add("chronyc", None, f"tracking unreadable: {why}")
    else:
        c.add("leap", tr["leap"] == "Normal" if tr["leap"] else None, tr["leap"] or "not reported")
        off = tr["offset_ms"]
        limit = ctx.release.thresholds["chrony_offset_max_ms"]
        c.add("offset", abs(off) < limit if off is not None else None,
              f"{off:+.2f} ms (limit {limit} ms)" if off is not None else "not reported")
    if s["sources"].ok:
        selected = [x["name"] for x in src if x["state"] == "*"]
        c.add("sources", len(src) >= 1,
              f"{len(src)} configured, " + (f"syncing to {selected[0]}" if selected else "none selected"))
    else:
        c.add("sources", None, "chronyc sources unreadable")
    if s["clients"].ok:
        clients = chrony_clients(s["clients"].out)
        want = {**{ip: rid for rid, ip in plan.radars.items()},
                **{ip: "APU" for ip in plan.apus}, plan.camera: "camera", plan.router: "router"}
        missing = [f"{name} {ip}" for ip, name in want.items() if ip not in clients]
        c.add("clients", not missing, "all present" if not missing else "missing " + ", ".join(missing))
    else:
        c.add("clients", None, ctx.redact(s["clients"].text[:120] or "chronyc clients failed"))
    ctx.facts["chrony"] = {"tracking": tr, "sources": src}
    return c.row(STAGE.spec("S2.4"), tracking=tr, sources=src)


def _s25(ctx: Context) -> Row:
    s = _sec(ctx)
    want = ctx.release.get("server.timezone", "Asia/Jerusalem")
    got = s["tz"].text
    return result(STAGE.spec("S2.5"), got or "unknown", got == want)


def _s26(ctx: Context) -> Row:
    s = _sec(ctx)
    got = l4t_version(s["l4t"].out)
    want = ctx.release.pin("server.l4t")
    spec = STAGE.spec("S2.6")
    if got is None:
        return amber(spec, "/etc/nv_tegra_release unreadable", actual=s["l4t"].text[:120] or "missing")
    if not want:
        return result(spec, f"{got} (informational — release.yaml.server.l4t not set)", True)
    norm = lambda v: v.upper().lstrip("R").replace(" ", "")  # noqa: E731
    same = norm(got) == norm(want)
    return result(spec, got if same else f"{got} (want {want})", same)


def _s27(ctx: Context) -> Row:
    s = _sec(ctx)
    states = s["active"].text.split()
    c = Checks()
    for unit, state in zip(("tailscaled", "k3s"), states + ["unknown"] * (2 - len(states))):
        c.add(unit, state == "active", state)
    ip = s["tsip"].text.splitlines()[0] if s["tsip"].text else ""
    c.add("tailscale ip", ip.startswith("100."), ip or "none")
    return c.row(STAGE.spec("S2.7"))


def _s28(ctx: Context) -> Row:
    s = _sec(ctx)
    spec = STAGE.spec("S2.8")
    out = s["kernel"].out
    if "a password is required" in out:
        return amber(spec, "sudo on the server asked for a password (journalctl -k needs it)")
    events, empty = kernel_events(out)
    note = f" (empty SATA ports, probed at boot: {', '.join(empty)})" if empty else ""
    if not events:
        return result(spec, "0 events since boot" + note, True, detail={"empty_sata_ports": empty})
    shown = "; ".join(e[:100] for e in events[:3])
    return result(spec, f"{len(events)} events since boot — {shown}{note}", False,
                  detail={"events": events, "empty_sata_ports": empty})


def run(ctx: Context) -> Iterator[Row]:
    try:
        yield from guarded(ctx, STAGE, (_s21, _s22, _s23, _s24, _s25, _s26, _s27, _s28))
    finally:
        ctx.facts.pop("_s2", None)
