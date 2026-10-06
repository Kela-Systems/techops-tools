"""gotcha-atp — the engine's entry point.

    gotcha-atp [ui] [--port 8190] [--no-browser]     the web UI (default; what Techops uses)
    gotcha-atp run --unit <site|site-operator> [--stages S1,S2] [--soak] [--no-manual]
    gotcha-atp catalogue                             every declared row
    gotcha-atp gen-protos                            (re)generate the hub gRPC stubs
"""
from __future__ import annotations

import argparse
import getpass
import sys
import threading
import webbrowser
from dataclasses import replace

from . import __version__, release as release_mod
from .access import creds as creds_mod
from .access.session import Unit
from .benchcentral import BenchCentral
from .context import Context
from .record import RUNS_DIR
from .runner import Answers, Runner, RunOptions, selection_error
from .stages import STAGES


def _ui(args: argparse.Namespace) -> int:
    import uvicorn

    from .ui.app import create_app
    url = f"http://127.0.0.1:{args.port}"
    print(f"Gotcha ATP {__version__} — {url}")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def _print_event(ev: dict, answers: Answers, manual: bool) -> None:
    t = ev.get("type")
    if t == "stage_started":
        print(f"\n== {ev['stage']} · {ev['name']}")
    elif t == "row":
        r = ev["row"]
        mark = {"pass": "ok  ", "fail": "FAIL", "amber": "amb "}[r["state"]]
        checks = (r.get("detail") or {}).get("checks") or []
        if len(checks) > 1:
            bad = [c for c in checks if c["ok"] is not True]
            print(f"  {mark} {r['id']:<6} {r['item']}: "
                  + (f"{len(bad)} of {len(checks)} not ok" if bad else f"all {len(checks)} ok"))
            for c in bad:
                print(f"              {'✗' if c['ok'] is False else '?'} {c['label']}: {c['actual']}")
        else:
            print(f"  {mark} {r['id']:<6} {r['item']}: {r['actual']}")
        if r.get("hint"):
            print(f"         hint: {r['hint']}")
    elif t == "needs_input" and manual:
        title = "Your turn (the run waits for you)" if ev.get("during") else "Manual steps"
        threading.Thread(target=_ask_stdin, args=(ev["questions"], answers, title), daemon=True).start()
    elif t == "run_finished":
        s = ev.get("summary") or {}
        print(f"\nVerdict {s.get('verdict')} · stamp eligible: {'yes' if s.get('stamp_eligible') else 'no'}"
              f" ({s.get('stamp_reason')})")
        print(f"Record  {ev.get('record')}")
        print(f"Report  {ev.get('pdf') or ev.get('html')}" + (f"  ({ev['pdf_note']})" if ev.get("pdf_note") else ""))
        up = ev.get("upload") or {}
        print(f"bench-central: {up.get('status')} — {up.get('detail')}")


def _ask_stdin(questions: list[dict], answers: Answers, title: str = "Manual steps") -> None:
    print(f"\n== {title}")
    for q in questions:
        allowed = "y/n/s" if q["skippable"] else "y/n"
        while True:
            a = input(f"  {q['id']} {q['prompt']} [{allowed}] ").strip().lower()[:1]
            choice = {"y": "yes", "n": "no", "s": "skip"}.get(a)
            if choice and (choice != "skip" or q["skippable"]):
                break
        note = input("     note (optional): ")
        answers.answer(q["id"], choice, note)


def _run(args: argparse.Namespace) -> int:
    c = creds_mod.load()
    if args.password_stdin:
        c = replace(c, ssh_password=sys.stdin.readline().rstrip("\n"))
    elif not c.ssh_password:
        c = replace(c, ssh_password=getpass.getpass("kela password (empty = key only): "))
    if args.engineer:
        c = replace(c, engineer=args.engineer)
    redact = creds_mod.Redactor(c.secrets())
    site = args.unit.lower().removesuffix("-operator")
    ctx = Context(unit=Unit(site=site, operator_name=(args.operator or "").lower()),
                  creds=c, redact=redact)
    try:
        ctx.release = release_mod.load(args.release)
    except release_mod.ReleaseError as e:
        ctx.release_error = str(e)
    stages = [s.strip().upper() for s in args.stages.split(",") if s.strip()] if args.stages else None
    if (why := selection_error(stages, args.soak)):
        print(f"gotcha-atp: {why}", file=sys.stderr)
        return 2
    answers = Answers()
    manual = not args.no_manual and sys.stdin.isatty()
    runner = Runner(ctx, emit=lambda ev: _print_event(ev, answers, manual), answers=answers,
                    options=RunOptions(stages=stages, soak=args.soak, manual=manual,
                                       engineer=c.engineer),
                    out_dir=RUNS_DIR, central=BenchCentral(c.bench_central_url))
    try:
        result = runner.run()
    except KeyboardInterrupt:
        ctx.cancel.set()
        return 130
    finally:
        if ctx.session is not None:
            ctx.session.close()
    summary = result.get("summary") or {}
    return 0 if summary.get("verdict") == "PASS" else 1


def _catalogue(_: argparse.Namespace) -> int:
    for st in STAGES:
        flag = "" if st.implemented else "  [stub]"
        print(f"{st.id} · {st.name}{flag}" + (f"  (depends on {', '.join(st.depends_on)})" if st.depends_on else ""))
        for r in st.rows:
            print(f"  {r.id:<6} {r.severity:<8} {r.cls:<9} {r.item}")
    return 0


def _gen_protos(_: argparse.Namespace) -> int:
    from .access.grpc import ensure_stubs
    print(f"stubs in {ensure_stubs(force=True)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gotcha-atp", description="Gotcha ATP — Techops acceptance test")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")

    ui = sub.add_parser("ui", help="the web UI (default)")
    ui.add_argument("--port", type=int, default=8190)
    ui.add_argument("--no-browser", action="store_true")
    ui.set_defaults(fn=_ui)

    run = sub.add_parser("run", help="run the ATP from the terminal")
    run.add_argument("--unit", required=True, help="site name (kela-gotcha-07) or its -operator peer")
    run.add_argument("--operator", help="operator station's tailnet name when it is not "
                     "<site>-operator")
    run.add_argument("--stages", help="comma-separated stage ids; S0 always runs; never stamp-eligible")
    run.add_argument("--soak", action="store_true", help="add the S9 power-cycle soak to a full run "
                     "(you will be asked to cut mains to the unit; ~20 min more)")
    run.add_argument("--no-manual", action="store_true", help="skip the manual questions (rows go amber)")
    run.add_argument("--engineer", default="")
    run.add_argument("--release", default=None, help="path to release.yaml (default: the checkout's)")
    run.add_argument("--password-stdin", action="store_true", help="read the kela password from stdin")
    run.set_defaults(fn=_run)

    cat = sub.add_parser("catalogue", help="print every declared row")
    cat.set_defaults(fn=_catalogue)
    gen = sub.add_parser("gen-protos", help="regenerate the hub gRPC stubs from proto/")
    gen.set_defaults(fn=_gen_protos)

    args = p.parse_args(argv)
    if not getattr(args, "fn", None):
        args = p.parse_args(["ui", *(argv or [])])
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
