"""HTML report from a run record — the same document the PDF is printed from.

Cover: site, verdict, stamp eligibility, run_id, engineer, date, release, the
discovered inventory. Then warnings, every stage's rows with expected/actual,
and the matrices (ping, chrony, PoE port map). Built only from the record, so a
stored JSON can be re-rendered later.
"""
from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any

STATE_LABEL = {"pass": "PASS", "fail": "FAIL", "amber": "AMBER"}

CSS = """
@page { size: A4; margin: 14mm 12mm; }
body { font: 10pt/1.4 -apple-system, "Helvetica Neue", Arial, sans-serif; color: #1d1d1f; }
h1 { font-size: 17pt; margin: 0 0 2mm; }
h2 { font-size: 12pt; margin: 7mm 0 2mm; border-bottom: 1px solid #ccc; padding-bottom: 1mm; }
h3 { font-size: 10.5pt; margin: 4mm 0 1.5mm; }
table { border-collapse: collapse; width: 100%; margin: 1mm 0 3mm; }
th, td { text-align: left; vertical-align: top; padding: 1.2mm 1.6mm; border-bottom: 1px solid #e3e3e3; font-size: 8.6pt; }
th { background: #f3f3f3; font-weight: 600; }
tr { page-break-inside: avoid; }
.meta td { border: none; padding: 0.6mm 2mm 0.6mm 0; font-size: 9.5pt; }
.meta td:first-child { color: #666; width: 34mm; }
.badge { display: inline-block; padding: 1mm 3mm; border-radius: 2mm; font-weight: 700; }
.v-PASS { background: #d7f5dd; color: #0b5d1e; }
.v-FAIL { background: #fbd9d9; color: #8a1212; }
.v-INCOMPLETE { background: #fdebc8; color: #7a4b00; }
.s-fail td { background: #fdeeee; }
.s-amber td { background: #fff6e5; }
.state { font-weight: 700; white-space: nowrap; }
.s-pass .state { color: #0b5d1e; } .s-fail .state { color: #8a1212; } .s-amber .state { color: #8a5a00; }
.muted { color: #777; }
.hint { color: #333; font-style: italic; margin-top: 1mm; }
.small { font-size: 8pt; }
.c-bad { color: #8a1212; } .c-unk { color: #8a5a00; }
"""


def _e(v: Any) -> str:
    return escape("" if v is None else str(v))


def _table(headers: list[str], rows: list[list[Any]], cls: list[str] | None = None) -> str:
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = []
    for i, r in enumerate(rows):
        c = f' class="{cls[i]}"' if cls and cls[i] else ""
        body.append(f"<tr{c}>" + "".join(f"<td>{cell}</td>" for cell in r) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _actual(r: dict) -> str:
    """A row's actual: for several sub-checks, the verdict and what failed one
    per line, the passing ones listed small underneath."""
    checks = (r.get("detail") or {}).get("checks") or []
    if len(checks) < 2:
        return _e(r["actual"])
    bad = [c for c in checks if c["ok"] is False]
    unk = [c for c in checks if c["ok"] is None]
    good = [c for c in checks if c["ok"] is True]
    n = len(checks)
    verdict = (f"{len(bad)} of {n} failed" + (f", {len(unk)} not evaluated" if unk else "") if bad
               else f"{len(unk)} of {n} not evaluated" if unk else f"all {n} ok")
    out = [f"<div><b>{verdict}</b></div>"]
    for c, cls, mk in [*[(c, "c-bad", "✗") for c in bad], *[(c, "c-unk", "?") for c in unk]]:
        out.append(f"<div class='{cls}'>{mk} <b>{_e(c['label'])}</b> — {_e(c['actual'])}</div>")
    if good:
        out.append("<div class='muted small'>ok: " + "; ".join(
            f"{_e(c['label'])} ({_e(c['actual'])})" for c in good) + "</div>")
    return "".join(out)


def _failed_text(r: dict) -> str:
    checks = (r.get("detail") or {}).get("checks") or []
    bad = [c for c in checks if c["ok"] is not True]
    return "; ".join(f"{c['label']}: {c['actual']}" for c in bad) if len(checks) > 1 and bad else r["actual"]


def _row_detail(rows: dict, row_id: str) -> dict:
    return (rows.get(row_id) or {}).get("detail") or {}


def render(rec: dict) -> str:
    dev = rec.get("device") or {}
    summary = rec.get("summary") or {}
    rel = dev.get("release") or {}
    verdict = summary.get("verdict", "?")
    when = rec.get("timestamp", "")
    try:
        when = datetime.fromisoformat(when).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        pass
    all_rows = {r["id"]: r for st in rec.get("stages") or [] for r in st.get("rows") or []}

    parts = [f"<!doctype html><html><head><meta charset='utf-8'><title>Gotcha ATP — {_e(rec.get('serial'))}</title>"
             f"<style>{CSS}</style></head><body>"]
    parts.append(f"<h1>Gotcha ATP — {_e(rec.get('serial'))}</h1>")
    parts.append(f"<p><span class='badge v-{_e(verdict)}'>{_e(verdict)}</span> &nbsp; "
                 f"Stamp eligible: <b>{'Yes' if summary.get('stamp_eligible') else 'No'}</b> "
                 f"<span class='muted'>— {_e(summary.get('stamp_reason'))}</span></p>")
    meta = [
        ("Run", rec.get("run_id")), ("Date", when), ("Engineer", rec.get("operator") or "—"),
        ("Release", f"{rel.get('name', '?')} · commit {rel.get('commit', '?')}"),
        ("Route", dev.get("route")), ("Run type", "full run" if summary.get("full_run") else
                                      f"partial ({', '.join(dev.get('stages_requested') or [])})"),
        ("Duration", f"{rec.get('duration_s')} s"), ("Tool", rec.get("bench_version")),
        ("Rows", ", ".join(f"{k} {v}" for k, v in (summary.get("counts") or {}).items())),
    ]
    parts.append("<table class='meta'>" + "".join(
        f"<tr><td>{_e(k)}</td><td>{_e(v)}</td></tr>" for k, v in meta) + "</table>")
    if rec.get("error"):
        parts.append(f"<p class='v-FAIL badge'>Run error: {_e(rec['error'])}</p>")

    inv = dev.get("inventory") or []
    ident = dev.get("identity") or {}
    srv, op = ident.get("server") or {}, ident.get("operator") or {}
    parts.append("<h2>Inventory (discovered)</h2>")
    inv_rows = []
    if srv:
        bi = srv.get("build_info") or {}
        inv_rows.append(["server", "192.168.88.10", _e(bi.get("model")), _e(srv.get("board_serial") or "—"),
                         _e(bi.get("setup_version")), _e(srv.get("machine_id")), "build-info"])
    if op:
        bi = op.get("build_info") or {}
        inv_rows.append(["operator", _e(op.get("lan_ip")), "Toughbook", "—", _e(bi.get("setup_version")),
                         "—", "build-info"])
    for p in inv:
        inv_rows.append([_e(p["role"]), _e(p["ip"]), _e(p["model"]), _e(p["serial"]), _e(p["firmware"]),
                         _e(p["mac"]), "ok" if p.get("identified") else f"<b>{_e(p.get('error') or 'incomplete')}</b>"])
    parts.append(_table(["Part", "Address", "Model", "Serial", "Firmware", "MAC / id", "Identity"], inv_rows)
                 if inv_rows else "<p class='muted'>Not discovered (S0.6 did not run).</p>")

    warn = [all_rows[i] for i in summary.get("warnings") or [] if i in all_rows]
    failed = [all_rows[i] for i in summary.get("failed") or [] if i in all_rows]
    parts.append("<h2>Blocking failures</h2>")
    parts.append(_table(["#", "Check", "What failed", "Hint"],
                        [[_e(r["id"]), _e(r["item"]), _e(_failed_text(r)), _e(r.get("hint"))] for r in failed])
                 if failed else "<p>None.</p>")
    parts.append("<h2>Warnings (medium/low — do not block)</h2>")
    parts.append(_table(["#", "Check", "What failed"], [[_e(r["id"]), _e(r["item"]), _e(_failed_text(r))] for r in warn])
                 if warn else "<p>None.</p>")

    parts.append("<h2>Results by stage</h2>")
    for st in rec.get("stages") or []:
        label = f"{st['id']} — {st['name']}" + ("" if st.get("implemented", True) else " (not implemented yet)")
        parts.append(f"<h3>{_e(label)} · {_e(st.get('state'))}</h3>")
        body, cls = [], []
        for r in st.get("rows") or []:
            note = ""
            if r.get("reason") and r["state"] == "amber":
                note = f"<div class='muted small'>{_e(r['reason'])}</div>" if r["reason"] != r["actual"] else ""
            if r.get("hint"):
                note += f"<div class='hint'>{_e(r['hint'])}</div>"
            body.append([_e(r["id"]), _e(r["item"]), _e(r["expected"]), _actual(r) + note,
                         f"<span class='state'>{STATE_LABEL.get(r['state'], r['state'])}</span>",
                         _e(r["severity"])])
            cls.append(f"s-{r['state']}")
        parts.append(_table(["#", "Check", "Expected", "Actual", "State", "Severity"], body, cls))

    matrix = _row_detail(all_rows, "S1.1").get("matrix")
    if matrix:
        parts.append("<h2>Ping matrix (S1.1, from the server)</h2>")
        parts.append(_table(["Address", "Device", "Loss %", "Min ms", "Avg ms", "Max ms", "Jitter ms"], [
            [_e(ip), _e(m.get("name")), _e(m.get("loss_pct")), _e(m.get("min")), _e(m.get("avg")),
             _e(m.get("max")), _e(m.get("mdev"))] for ip, m in matrix.items()]))
    chrony = _row_detail(all_rows, "S2.4")
    if chrony.get("tracking"):
        tr = chrony["tracking"]
        parts.append("<h2>Server clock (S2.4)</h2>")
        parts.append(f"<p>Leap {_e(tr.get('leap'))} · offset {_e(tr.get('offset_ms'))} ms · stratum "
                     f"{_e(tr.get('stratum'))} · reference {_e(tr.get('ref'))}</p>")
        parts.append("<p class='muted small'>Per-device NTP offset matrix: S7.1 (not implemented yet).</p>")
    ports = _row_detail(all_rows, "S1.4").get("ports")
    if ports:
        parts.append("<h2>PoE port map (S1.4, discovered — never configured)</h2>")
        parts.append(_table(["Port", "Watts", "Devices (from MAC table)"], [
            [f"gi{_e(p)}", f"{v.get('watts', 0):.1f}", _e(", ".join(v.get("devices") or []) or "—")]
            for p, v in ports.items()]))
    parts.append("</body></html>")
    return "".join(parts)
