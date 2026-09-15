#!/usr/bin/env python3
"""site-map — declare a site, check it, draw it.

    ./sitemap.py lint   sites/*.yaml          # offline checks, CI-safe
    ./sitemap.py show   sites/kela-cuas-06.yaml
    ./sitemap.py render sites/kela-cuas-06.yaml -o docs/kela-cuas-06.md

Nothing here contacts a device. That is a property worth keeping: `lint` runs
on every push without a tailnet, without credentials and without any chance
of touching production. Proving a model against real hardware is a separate
command against a read-only path, and it is not built yet.

Exit codes: 0 clean (warnings allowed), 1 lint errors, 2 a file that could
not be loaded at all.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import bench_central
import discover as discover_mod
import probe as probe_mod
import oui
import export
from export import as_js, as_json
from lint import ERROR, WARN, coverage, lint
from model import Site, SiteModelError, load_site
from render import render_markdown, render_network_mermaid, render_power_mermaid, render_tree

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_BAD_MODEL = 2


def _load_all(paths: list[str]) -> tuple[list[Site], int]:
    sites: list[Site] = []
    failed = 0
    for path in paths:
        try:
            sites.append(load_site(path))
        except SiteModelError as exc:
            print(f"cannot load {path}: {exc}", file=sys.stderr)
            failed += 1
    return sites, failed


def cmd_lint(args: argparse.Namespace) -> int:
    sites, failed = _load_all(args.paths)
    worst = EXIT_BAD_MODEL if failed else EXIT_OK

    for site in sites:
        findings = lint(site, require_evidence=args.require_evidence)
        errors = [f for f in findings if f.severity == ERROR]
        warnings = [f for f in findings if f.severity == WARN]

        if args.errors_only:
            findings = errors

        header = f"{site.name} ({site.source_path})"
        print(header)
        print("-" * len(header))
        if not findings:
            print("  clean\n")
        else:
            for finding in findings:
                where = f"{finding.where}: " if finding.where else ""
                print(f"  {finding.severity:<5} {finding.code:<20} {where}{finding.message}")
            print()

        print(
            f"  {len(site.nodes)} nodes, {len(site.net)} data links, "
            f"{len(site.power)} power feeds — "
            f"{len(errors)} error(s), {len(warnings)} warning(s)"
        )

        cov = coverage(site)
        print("  verified against hardware:")
        bar_len = 16
        for claim in ("vendor", "model", "firmware", "addr", "link",
                      "power", "draw"):
            c = cov[claim]
            if c["pct"] is None:
                # Nothing of this kind is on the map. Not verified, not
                # unverified — absent, and said so rather than scored.
                print(f"    {claim:<9}{'—':>6}   "
                      f"[{'.' * bar_len}]  nothing recorded yet")
                continue
            filled = int(round(bar_len * c["pct"] / 100.0))
            detail = f"{c['proven']}/{c['total']}"
            if c["claimed"] != c["total"]:
                # Spell out the gap rather than letting the fraction imply
                # the unrecorded devices do not exist.
                detail += (f"  ({c['total'] - c['claimed']} device(s) have "
                           f"none recorded)")
            print(f"    {claim:<9}{c['pct']:>5.1f}%  "
                  f"[{'#' * filled}{'.' * (bar_len - filled)}]  {detail}")
        if cov["survey_only_feeds"]:
            print(f"    {cov['survey_only_feeds']} power feed(s) can only be "
                  f"closed by an on-site survey — no protocol carries them.")
        print(f"    evidence: " + ", ".join(
            f"{k}={v}" for k, v in cov["by_source"].items()) + "\n")

        if errors and worst == EXIT_OK:
            worst = EXIT_FINDINGS

    return worst


def cmd_show(args: argparse.Namespace) -> int:
    sites, failed = _load_all(args.paths)
    for site in sites:
        print(render_tree(site))
        print()
    return EXIT_BAD_MODEL if failed else EXIT_OK


def cmd_render(args: argparse.Namespace) -> int:
    sites, failed = _load_all(args.paths)
    if failed and not sites:
        return EXIT_BAD_MODEL

    renderers = {
        "markdown": render_markdown,
        "network": lambda s: render_network_mermaid(s) + "\n",
        "power": lambda s: render_power_mermaid(s) + "\n",
    }
    text = "\n".join(renderers[args.format](site) for site in sites)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    else:
        sys.stdout.write(text)

    return EXIT_BAD_MODEL if failed else EXIT_OK


def cmd_export(args: argparse.Namespace) -> int:
    """Emit the payload the UI reads. Same source as the linter, by design."""
    sites, failed = _load_all(args.paths)
    if not sites:
        return EXIT_BAD_MODEL

    # Templates are schema examples with invented devices. The UI renders the
    # first site in the payload, so shipping one is not a harmless extra entry
    # — it silently becomes the site the dashboard shows.
    if not args.include_templates:
        sites, dropped = export.drop_templates(sites)
        for name in dropped:
            print(f"skipping template site '{name}' "
                  f"(pass --include-templates to keep it)", file=sys.stderr)
        if not sites:
            print("nothing to export: every site given was a template",
                  file=sys.stderr)
            return EXIT_BAD_MODEL

    text = as_json(sites) if args.format == "json" else as_js(sites)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out} ({len(sites)} site(s), {len(text)} bytes)", file=sys.stderr)
    else:
        sys.stdout.write(text)

    return EXIT_BAD_MODEL if failed else EXIT_OK


def _addr_sort_key(entry: "oui.Entry") -> tuple:
    if not entry.ip:
        return (1, ())
    return (0, tuple(int(part) for part in entry.ip.split(".")))


def cmd_identify(args: argparse.Namespace) -> int:
    """Resolve MACs to vendors, offline.

    Reads `arp -an` output (a file, or stdin) or bare MACs given as
    arguments. The point of doing this locally is that an ARP table from a
    live site is an inventory of that site.
    """
    try:
        db_path = oui.find_db(args.db)
        table = oui.load_db(db_path)
    except oui.OuiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_MODEL

    entries: list[oui.Entry] = []
    if args.macs:
        for raw in args.macs:
            try:
                digits = oui.normalise(raw)
            except oui.OuiError as exc:
                print(f"skipping: {exc}", file=sys.stderr)
                continue
            entries.append(
                oui.Entry(mac=":".join(
                    digits[i:i + 2] for i in range(0, len(digits), 2)).lower())
            )
    else:
        text = Path(args.file).read_text(encoding="utf-8") if args.file \
            else sys.stdin.read()
        entries = oui.parse_arp(text)

    if not entries:
        print("No MAC addresses found in the input.", file=sys.stderr)
        return EXIT_BAD_MODEL

    entries = oui.resolve(entries, table)
    entries.sort(key=_addr_sort_key)

    print(f"# vendor database: {db_path}")
    print(f"# {len(entries)} device(s)\n")

    width = max(len(e.ip or "") for e in entries) or 4
    for entry in entries:
        vendor = entry.vendor
        name = vendor.name if vendor else "UNKNOWN vendor"
        if vendor and vendor.is_registrar:
            # Be loud about this: it means the assignment is an MA-S block and
            # the real vendor was not found, not that the registrar made it.
            name += " (registrar only - vendor NOT resolved)"
        print(f"{(entry.ip or ''):<{width}}  {entry.mac}  {name}")
        if entry.hostname:
            print(f"{'':<{width}}  {'':<17}  host: {entry.hostname}")
        if vendor:
            print(f"{'':<{width}}  {'':<17}  matched {vendor.prefix_bits}-bit "
                  f"prefix {vendor.matched}")
        if entry.model:
            print(f"{'':<{width}}  {'':<17}  likely: {entry.model}")
        for reason in entry.because:
            print(f"{'':<{width}}  {'':<17}    - {reason}")
        print()

    unresolved = [e for e in entries
                  if not e.vendor or e.vendor.is_registrar]
    if unresolved:
        print(f"{len(unresolved)} of {len(entries)} could not be attributed to a "
              f"vendor. Newer assignments may need a fresher database than "
              f"{db_path.name}.")

    print("A MAC identifies a vendor, never a model - treat 'likely' as a lead "
          "to confirm, not a fact.")
    print("ARP only lists what this host has recently talked to, so a device "
          "missing here is not proof it is absent.")
    return EXIT_OK


def cmd_discover(args: argparse.Namespace) -> int:
    """ARP table -> a provisional site model, with per-fact evidence.

    Read-only throughout: it parses an ARP table someone else collected and,
    if given a collector URL, issues GETs against it. Nothing is written to a
    device or to bench-central.
    """
    try:
        table = oui.load_db(oui.find_db(args.db))
    except oui.OuiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_MODEL

    text = Path(args.file).read_text(encoding="utf-8") if args.file \
        else sys.stdin.read()

    client = None
    if args.central:
        client = bench_central.Client(args.central, timeout=args.timeout)
        try:
            health = client.health()
        except bench_central.CentralError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_BAD_MODEL
        runs = health.get("runs", health.get("run_count", "?"))
        print(f"# bench-central at {args.central}: {runs} run record(s)",
              file=sys.stderr)
        if runs in (0, "0"):
            # Tell an empty archive apart from an unreachable one, loudly:
            # they look identical in the output otherwise.
            print("# WARNING: the archive is EMPTY. Central shipping is "
                  "opt-in per station (BENCH_CENTRAL_URL); if no station has "
                  "it set, no model can be resolved this way.",
                  file=sys.stderr)

    found, warnings = discover_mod.discover(text, table, client)
    if not found:
        print("No MAC addresses found in the input.", file=sys.stderr)
        return EXIT_BAD_MODEL

    for warning in warnings:
        print(f"# note: {warning}", file=sys.stderr)

    resolved = sum(1 for d in found if d.model)
    print(f"# {len(found)} device(s); model resolved for {resolved}",
          file=sys.stderr)

    yaml_text = discover_mod.to_yaml(
        args.site, args.subnet, found, warnings,
        source_note=args.file or "stdin",
    )

    if args.out:
        out = Path(args.out)
        if out.exists() and not args.force:
            # A discovered file is a starting point that someone then edits by
            # hand; silently overwriting that work would be unforgivable.
            print(f"error: {out} exists. Pass --force to overwrite, or write "
                  f"elsewhere and merge.", file=sys.stderr)
            return EXIT_BAD_MODEL
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(yaml_text, encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    else:
        sys.stdout.write(yaml_text)

    return EXIT_OK


def cmd_probe(args: argparse.Namespace) -> int:
    """Read identity off the devices themselves. Read-only, one attempt each.

    Dry run unless --confirm. These are production devices and a retried
    credential can lock a switch out, so connecting is opt-in and the plan is
    printed first either way.
    """
    sites, failed = _load_all([args.site])
    if not sites:
        return EXIT_BAD_MODEL
    site = sites[0]
    only = set(args.only) if args.only else None

    rows = probe_mod.plan(site)
    if only:
        rows = [r for r in rows if r[0] in only]

    print(f"{site.name}: {len(rows)} node(s)\n")
    print(f"  {'node':<20} {'address':<16} plan")
    print(f"  {'-' * 20} {'-' * 16} {'-' * 44}")
    for name, host, prober, label in rows:
        print(f"  {name:<20} {host or '—':<16} {label}")

    probeable = [r for r in rows if r[2] is not None]
    print(f"\n  {len(probeable)} of {len(rows)} node(s) have a prober.")

    if not args.confirm:
        print("\nDRY RUN — nothing was contacted. These are production "
              "devices; re-run with --confirm to connect.")
        print("Policy when you do: read-only enforced at the client, exactly "
              "ONE credential attempt per device (a PLANET locks out after "
              "three and then fails in a way that looks like broken "
              "firmware), identity only — no config, no firmware, no reboot.")
        return EXIT_OK

    if not args.password:
        print("error: --confirm needs --password (the shared bench password).",
              file=sys.stderr)
        return EXIT_BAD_MODEL

    print(f"\nConnecting to {len(probeable)} device(s), read-only...\n")
    results = probe_mod.run(site, args.password, only=only)

    learned = 0
    for result in results:
        if result.ok:
            learned += 1
            bits = []
            if result.model:
                bits.append(f"model={result.model}")
            if result.firmware:
                bits.append(f"firmware={result.firmware}")
            if result.serial:
                bits.append(f"serial={result.serial}")
            for key, value in (result.extra or {}).items():
                bits.append(f"{key}={value}")
            print(f"  OK    {result.node:<20} " + "  ".join(bits))
        else:
            print(f"  MISS  {result.node:<20} {result.error}")

    print(f"\n  {learned}/{len(results)} device(s) reported an identity.")
    print("  Facts that did not come back are left as 'assumed' — a failed "
          "probe is not evidence of anything.")

    if args.write and learned:
        patch = probe_mod.as_yaml_patch(results)
        Path(args.write).write_text(patch, encoding="utf-8")
        print(f"\n  wrote {args.write} — merge these blocks into "
              f"{args.site} to record the facts with 'device-api' evidence.")

    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sitemap",
        description="Declare a site's network and power topology, check it, draw it.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_lint = sub.add_parser("lint", help="offline checks over one or more site files")
    p_lint.add_argument("paths", nargs="+")
    p_lint.add_argument(
        "--errors-only",
        action="store_true",
        help="hide warnings (they never affect the exit code either way)",
    )
    p_lint.add_argument(
        "--require-evidence",
        action="store_true",
        help="treat unproven facts as errors, for gating a site that is "
             "meant to be fully verified",
    )
    p_lint.set_defaults(func=cmd_lint)

    p_show = sub.add_parser("show", help="print the site as an indented tree")
    p_show.add_argument("paths", nargs="+")
    p_show.set_defaults(func=cmd_show)

    p_render = sub.add_parser("render", help="emit Mermaid diagrams / markdown")
    p_render.add_argument("paths", nargs="+")
    p_render.add_argument(
        "-f", "--format", choices=("markdown", "network", "power"), default="markdown"
    )
    p_render.add_argument("-o", "--out", help="write here instead of stdout")
    p_render.set_defaults(func=cmd_render)

    p_export = sub.add_parser("export", help="emit the UI payload")
    p_export.add_argument("paths", nargs="+")
    p_export.add_argument(
        "-f", "--format", choices=("js", "json"), default="js",
        help="js wraps the payload as window.SITE_DATA (default)",
    )
    p_export.add_argument("-o", "--out", help="write here instead of stdout")
    p_export.add_argument(
        "--include-templates", action="store_true",
        help="keep _-prefixed template sites in the payload (they are dropped "
             "by default: the UI shows the first site, so a template ships as "
             "the site the dashboard renders)",
    )
    p_export.set_defaults(func=cmd_export)

    p_id = sub.add_parser(
        "identify",
        help="resolve MACs to vendors from an arp table, offline",
    )
    p_id.add_argument("macs", nargs="*", help="bare MACs; omit to read arp output")
    p_id.add_argument("--file", help="file of `arp -an` output (default: stdin)")
    p_id.add_argument("--db", help="path to nmap-mac-prefixes")
    p_id.set_defaults(func=cmd_identify)

    p_disc = sub.add_parser(
        "discover",
        help="build a provisional site model from an arp table",
    )
    p_disc.add_argument("--site", required=True, help="site slug for the model")
    p_disc.add_argument("--subnet", default="192.168.88.0/24")
    p_disc.add_argument("--file", help="file of `arp -an` output (default: stdin)")
    p_disc.add_argument(
        "--central",
        help="bench-central base URL, e.g. http://techops-automations-host:8100 "
             "- resolves model and firmware by MAC. Read-only (GET only).",
    )
    p_disc.add_argument("--timeout", type=float,
                        default=bench_central.DEFAULT_TIMEOUT)
    p_disc.add_argument("--db", help="path to nmap-mac-prefixes")
    p_disc.add_argument("-o", "--out", help="write here instead of stdout")
    p_disc.add_argument("--force", action="store_true",
                        help="overwrite an existing --out file")
    p_disc.set_defaults(func=cmd_discover)

    p_probe = sub.add_parser(
        "probe",
        help="read model/firmware off the devices themselves (read-only)",
    )
    p_probe.add_argument("--site", required=True, help="site YAML to probe")
    p_probe.add_argument("--only", nargs="*", help="limit to these node names")
    p_probe.add_argument(
        "--confirm", action="store_true",
        help="actually connect. Without this it is a dry run.",
    )
    p_probe.add_argument("--password", help="the shared bench password")
    p_probe.add_argument(
        "--write", help="write a YAML patch of what was learned to this path",
    )
    p_probe.set_defaults(func=cmd_probe)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
