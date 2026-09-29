#!/usr/bin/env python3
"""Serve the dashboard, and sweep a site for it. Self-hosted, necessarily.

The published Artifact build of `ui/index.html` is a static export and can
never scan anything. An artifact page is sandboxed and its content-security
policy blocks every request to a host that is not allowlisted - silently, so
it reads as a hang rather than an error - and 100.x and 192.168.88.x are
certainly not allowlisted. Two address fields therefore need a process on a
machine that is genuinely on the tailnet, with an `ssh` that works. That is
this file, and that is the whole reason self-hosting is the answer here.

The page still works without it. `GET /api/health` is how it finds out:
answered, it shows the sweep panel; unanswered, it stays exactly the static
export it is published as, with the panel saying why it is inert.

There is no authentication here at all, so the tailnet - or an SSH tunnel -
is the perimeter, which is the same bargain bench-central's collector makes
and for the same reason: a client that could authenticate would be a client
that could be made to sweep on someone else's behalf. Hence 127.0.0.1 as the
default bind, and a printed warning when that is widened.

Stdlib only. PyYAML is still the one runtime dependency of the whole tool.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bench_central
import discover as discover_mod
import hosts as hosts_mod
import router as router_mod
import runlog
import secrets_store as secrets
import export
import oui
import sweep as sweep_mod
from model import SiteModelError, load_site

UI_DIR = Path(__file__).resolve().parent / "ui"
DEFAULT_SUBNET = "192.168.88.0/24"
DEFAULT_SSH_USER = sweep_mod.DEFAULT_SSH_USER
MAX_BODY = 64 * 1024


class ScanError(Exception):
    pass


def scan(server: str, subnet: str | None = None, *,
         central: str | None = None, user: str | None = DEFAULT_SSH_USER,
         db: str | None = None, timeout: float = 180.0,
         password: str | None = None, via: str = "router",
         read_hosts: bool = True, host_password: str | None = None,
         admin_user: str = "KelaAdmin", listen: bool = False,
         log=None) -> dict:
    """Sweep a site and return the payload the page already knows how to draw.

    Deliberately the long way round - sweep, then `discover`, then the same
    YAML the CLI writes, then `load_site`, then `export`. A shortcut straight
    to the payload would be a second code path for producing a site model,
    free to disagree with the one `lint` checks. Going through the YAML means
    the page can only ever show a model the linter would accept, and the text
    it came from is handed back so it can be saved as a real site file.
    """
    # The router is the vantage point by default: it is the site's DHCP
    # server, DNS resolver and gateway, so everything has talked to it, and
    # it is the one device every FOB has. The server path stays for a site
    # whose router is unreachable.
    if log:
        log.step("scan requested", server=server,
                 subnet=subnet or "to be read from the router",
                 via=via, read_hosts=read_hosts, central=central)

    if via == "router":
        if not password:
            raise ScanError(
                "No router password. The Teltonikas take root plus the "
                "shared password and reject keys, so start serve.py with "
                "--password (or set KELA_BENCH_PASSWORD)."
            )
        try:
            result = router_mod.survey(server, password, subnet, log=log,
                                       listen=listen)
        except (router_mod.RouterError, sweep_mod.SweepError) as exc:
            raise ScanError(str(exc))
    else:
        result = sweep_mod.run(server, subnet or DEFAULT_SUBNET,
                               timeout=timeout, user=user)
        subnet = result.subnet

    try:
        table = oui.load_db(oui.find_db(db))
    except oui.OuiError as exc:
        raise ScanError(
            f"{exc} Without it a MAC resolves to no vendor at all, which is "
            f"most of what a sweep can tell you."
        )

    client = None
    fans: list = []
    net_edges: list = []
    warnings = list(result.warnings)
    if central:
        client = bench_central.Client(central, timeout=15.0)
        try:
            health = client.health()
        except bench_central.CentralError as exc:
            client = None
            warnings.append(f"bench-central at {central} is unreachable: {exc}")
        else:
            runs = health.get("runs", health.get("run_count", 0))
            if runs in (0, "0"):
                # An empty archive and an unreachable one look identical in
                # the output otherwise, and the archive is opt-in per station.
                warnings.append(
                    "bench-central answered but holds 0 run records, so no "
                    "model can be resolved by MAC. Central shipping is opt-in "
                    "per station (BENCH_CENTRAL_URL)."
                )

    found, notes = discover_mod.discover(result.arp_text, table, client)
    warnings.extend(notes)
    if log:
        log.step("discovered", devices=len(found),
                 addresses=[d.entry.ip for d in found])

    if via == "router":
        import ipaddress
        # The router told us, unless the caller insisted.
        subnet = result.subnet or subnet
        net = ipaddress.ip_network(subnet, strict=False)
        # The lease table names devices the ARP table only numbers.
        router_mod.lease_notes(
            found, result.leases,
            router_mod.site_name_from(result.hostname, result.host),
            by_addr=result.by_addr)
        # And the vantage point adds itself - or enriches the entry it
        # already has, since the sweep pings its own address too.
        found = router_mod.merge_vantage_point(found, result, net, table)

        # Before the fan-out, because a BPDU is the only thing that can turn
        # a `network-device` into a switch by reading rather than by vendor
        # hunch - and the fan-out decides what it can claim by counting the
        # switches on a port.
        stp_edges: list = []
        if listen:
            warnings.extend(router_mod.mndp_facts(found, result))
            stp_edges, stp_notes = router_mod.stp_facts(
                found, result, router_mod.vantage_name(found, result))
            warnings.extend(stp_notes)
            if log:
                log.step("heard neighbour-discovery broadcasts",
                         ports=sorted(result.mndp),
                         boards={p: h.get("board")
                                 for p, h in (result.mndp or {}).items()})
                log.step("listened for spanning-tree BPDUs",
                         ports=sorted(result.bpdu),
                         bridges={p: f.get("bridge_mac")
                                  for p, f in (result.bpdu or {}).items()})

        # The forwarding database, read as ports. This is what turns "ten
        # devices are here somewhere" into "ten devices behind one cable",
        # and it is the only thing at this vantage point that can see a
        # switch with no address at all.
        vantage = router_mod.vantage_name(found, result)
        fans = router_mod.fan_out(found, result, vantage)
        net_edges = list(stp_edges)
        seen = {(e["from"], e["to"]) for e in net_edges}
        for edge in router_mod.fdb_edges(fans, vantage):
            # A BPDU names the nearest bridge on the port; the forwarding
            # table only says the MAC was learned there. Where both speak
            # about the same pair, the stronger claim stands and the weaker
            # one would just draw a second line between the same two boxes.
            if (edge["from"], edge["to"]) not in seen:
                net_edges.append(edge)
        warnings.extend(router_mod.fan_out_notes(fans))
        note = router_mod.upstream_note(result, table)
        if note:
            warnings.append(note)
        if log:
            log.step("fan-out read from the router's bridge",
                     ports={f.port: len(f.macs) for f in fans},
                     proven_edges=len(net_edges),
                     hidden_switches=[f.port for f in fans if f.hidden_switch])

        # And, for the hosts whose lease name places them on the tailnet,
        # read identity off the host itself. This is what turns "vendor
        # Dell" into a model, and it gives a site server its second leg.
        if read_hosts:
            warnings.extend(
                hosts_mod.enrich(
                    found, result.leases, net, user=user,
                    admin_user=admin_user, password=host_password, log=log,
                )
            )
    if not found:
        raise ScanError(
            f"{result.swept} addresses swept in {subnet} and no MAC "
            f"came back. " + (
                "; ".join(result.warnings) if result.warnings
                else "The neighbour table was empty."
            )
        )

    if via == "router":
        name = router_mod.site_name_from(result.hostname, result.host)
        reached = result.host
    else:
        name = result.hostname or f"sweep-{result.server.replace('.', '-')}"
        reached = result.server
    yaml_text = discover_mod.to_yaml(
        name, subnet, found, warnings,
        source_note=f"live survey from {reached}",
        net_edges=net_edges,
    )

    # load_site wants a path, and the round trip is the point (see above).
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", encoding="utf-8", delete=False
    ) as handle:
        handle.write(yaml_text)
        temp = Path(handle.name)
    try:
        site = load_site(temp)
    except SiteModelError as exc:
        raise ScanError(f"the sweep produced a model that will not load: {exc}")
    finally:
        temp.unlink(missing_ok=True)

    # Otherwise the page's footer credits a file in /tmp that is already gone.
    site.source_path = f"live survey of {subnet} from {reached}"

    # Marked live in the payload itself, not only by the page that receives
    # it: the same payload can be written to a data file and loaded later,
    # and the page gates the hand-written kela-fob-03 findings on this flag.
    payload = export.site_payload(site, fans)
    payload["live"] = True

    return {
        "site": payload,
        "yaml": yaml_text,
        "server": reached,
        "subnet": subnet,
        "hostname": result.hostname,
        "swept": result.swept,
        "answered": result.answered,
        "warnings": warnings,
    }


class Handler(SimpleHTTPRequestHandler):
    """Static files out of ui/, plus /api/health and /api/scan."""

    def __init__(self, *args, options=None, **kwargs):
        self.options = options
        super().__init__(*args, directory=str(UI_DIR), **kwargs)

    # Every text file here is UTF-8 and the page is full of em-dashes. Python
    # serves "text/html" bare, the browser then falls back to its locale
    # default, and every — arrives as â€”. The page carries a <meta charset>
    # as well; published as an artifact the injected skeleton supplied one,
    # which is why this only showed up once it was self-hosted.
    TEXT_TYPES = ("text/", "application/javascript", "application/json",
                  "image/svg+xml")

    def guess_type(self, path):
        kind = super().guess_type(path)
        if kind.startswith(self.TEXT_TYPES) and "charset=" not in kind:
            return kind + "; charset=utf-8"
        return kind

    def log_message(self, fmt, *args):
        # One line per request, without the default's noisy timestamp block.
        print(f"{self.address_string()} {fmt % args}")

    def _json(self, code: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.split("?")[0] == "/api/health":
            try:
                db = str(oui.find_db(self.options.db))
            except oui.OuiError:
                db = None
            return self._json(200, {
                "ok": True,
                "oui_db": db,
                "central": self.options.central,
                "default_subnet": DEFAULT_SUBNET,
                "ssh_user": self.options.ssh_user,
                "via": self.options.via,
                "has_password": bool(self.options.router_password),
            })
        return super().do_GET()

    def do_POST(self):
        if self.path.split("?")[0] != "/api/scan":
            return self._json(404, {"error": "no such endpoint"})

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._json(413, {"error": "request body too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json(400, {"error": "body was not JSON"})
        if not isinstance(body, dict):
            return self._json(400, {"error": "body was not a JSON object"})

        asked = str(body.get("subnet") or "").strip()
        # One log per request, written whatever happens - a refusal and a
        # crash are exactly the runs worth having a transcript of.
        log = runlog.RunLog(
            request={"server": str(body.get("server", "")),
                     "subnet": asked or None,
                     "via": self.options.via},
            secrets=tuple(x for x in (self.options.router_password,
                                      self.options.host_password) if x),
        )
        try:
            result = scan(
                str(body.get("server", "")),
                asked or None,
                central=self.options.central,
                user=self.options.ssh_user,
                password=self.options.router_password,
                via=self.options.via,
                read_hosts=not self.options.no_host_identity,
                listen=self.options.listen,
                host_password=self.options.host_password,
                admin_user=self.options.admin_user,
                db=self.options.db,
                timeout=self.options.timeout,
                log=log,
            )
        except (sweep_mod.SweepError, ScanError) as exc:
            # These are all "you asked for something that cannot be done, and
            # here is which part" - the page shows the text verbatim.
            path = log.finish(error=exc)
            self.log_message("scan refused: %s [log %s]", exc, path)
            return self._json(400, {"error": str(exc), "log": str(path or "")})
        except Exception as exc:  # noqa: BLE001 - a 500 must still say something
            path = log.finish(error=exc)
            self.log_message("scan failed: %r [log %s]", exc, path)
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}",
                                    "log": str(path or "")})
        path = log.finish(site=result.get("site"),
                          warnings=result.get("warnings"),
                          answered=result.get("answered"),
                          swept=result.get("swept"))
        self.log_message("scan ok: %s [log %s]",
                         (result.get("site") or {}).get("name"), path)
        result["log"] = str(path or "")
        return self._json(200, result)


def resolve_secrets(options) -> int:
    """Turn the password options into values, once, at startup.

    Done here rather than per request so a broken `op read` or a locked
    keychain fails when you start the server, not three minutes later in the
    middle of someone's survey.
    """
    interactive = not options.no_prompt
    # The router's password is prompted for by default: without it every
    # survey refuses, so asking beats failing. The station's is only asked
    # for when requested, since a site with no operator station to read
    # should not be made to answer a question about one.
    plan = (
        ("router_password", secrets.ROUTER, options.password_cmd,
         options.password,
         interactive and options.via == "router"),
        ("host_password", secrets.HOST, options.host_password_cmd,
         options.host_password,
         interactive and options.ask_host_password),
    )
    for attr, which, command, literal, prompt in plan:
        try:
            value, how = secrets.resolve(which, command, literal=literal,
                                         prompt=prompt)
        except secrets.SecretError as exc:
            print(f"error: {exc}")
            return 2
        setattr(options, attr, value)
        setattr(options, attr + "_typed", "prompt" in how)
    return 0


def serve(options) -> int:
    if not (UI_DIR / "index.html").is_file():
        print(f"error: no page at {UI_DIR / 'index.html'}")
        return 2
    failed = resolve_secrets(options)
    if failed:
        return failed
    # Line-buffered, because stdout block-buffers the moment it is not a
    # terminal: under the systemd unit in the README the startup banner and
    # every request line would sit in a buffer instead of reaching the
    # journal, and the service would look like it had never started.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, OSError):
        pass
    handler = partial(Handler, options=options)
    server = ThreadingHTTPServer((options.host, options.port), handler)
    shown = "localhost" if options.host in ("127.0.0.1", "::1") else options.host
    print(f"site-map on http://{shown}:{options.port}")
    print(f"  serving {UI_DIR}")
    print(f"  sweeps run over ssh, read-only; default subnet {DEFAULT_SUBNET}")
    if options.central:
        print(f"  bench-central {options.central}")
    print(f"  surveying via the {options.via}"
          + (" (root + shared password)" if options.via == "router"
             else f" (ssh {options.ssh_user})"))
    # Never the secret, only where it came from.
    print(f"  router password "
          f"{secrets.describe(options.password_cmd, options.password, secrets.ROUTER[0], options.router_password_typed)}")
    print(f"  host password   "
          f"{secrets.describe(options.host_password_cmd, options.host_password, secrets.HOST[0], options.host_password_typed)}")
    if options.via == "router" and not options.router_password:
        print("  WARNING: no router password, so every survey will refuse. "
              "The routers reject keys. Use --password-cmd.")
    if options.host not in ("127.0.0.1", "::1"):
        print(f"  WARNING: bound to {options.host} with no authentication. "
              f"Anyone who can reach this port can trigger a sweep of any "
              f"subnet from any host you can ssh to. Prefer 127.0.0.1 plus "
              f"`tailscale serve`.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8200)
    parser.add_argument("--central",
                        help="bench-central base URL, to resolve models by MAC")
    parser.add_argument(
        "--ssh-user", default=DEFAULT_SSH_USER,
        help=f"the account to log in to site servers as, when the address "
             f"field does not name one (default: {DEFAULT_SSH_USER}). Under "
             f"Tailscale SSH the tailnet policy decides which accounts are "
             f"permitted, and your own login name is not one of them - which "
             f"is why this has a default and ssh's own behaviour is wrong "
             f"here.",
    )
    parser.add_argument(
        "--password-cmd",
        help="a command that prints the shared router password, run when a "
             "survey needs it. THE PREFERRED WAY: the secret stays in "
             "whatever already holds it and never reaches a flag, a shell "
             "history or this repo. e.g. "
             "--password-cmd 'op read \"op://TechOps/Teltonika/password\"'",
    )
    parser.add_argument(
        "--host-password-cmd",
        help="the same, for the operator-station password (a different "
             "account and a different secret from the router's).",
    )
    parser.add_argument(
        "--admin-user", default=hosts_mod.ADMIN_USER,
        help=f"the account on the operator stations, which take a password "
             f"rather than a key (default: {hosts_mod.ADMIN_USER})",
    )
    parser.add_argument(
        "--password",
        help="the shared router password as a literal. Visible in `ps` to "
             "every user on this machine and recorded in your shell history "
             "- prefer --password-cmd, or just let it prompt. Kept for "
             "scripts, and $KELA_BENCH_PASSWORD is still read.",
    )
    parser.add_argument(
        "--host-password",
        help="the operator-station password as a literal, with the same "
             "caveats as --password. $KELA_HOST_PASSWORD is also read.",
    )
    parser.add_argument(
        "--ask-host-password", action="store_true",
        help="prompt for the operator-station password at startup. The "
             "router's is prompted for automatically when nothing else "
             "supplies it and there is a terminal to ask at.",
    )
    parser.add_argument(
        "--no-prompt", action="store_true",
        help="never prompt. For a systemd unit or a CI run, where a prompt "
             "would block on a terminal nobody is watching.",
    )
    parser.add_argument(
        "--no-host-identity", action="store_true",
        help="skip reading /sys/class/dmi/id off the Linux hosts over the "
             "tailnet. That read is what settles a PC's model and firmware, "
             "and it needs an ssh key that works; without it they stay "
             "vendor-only.",
    )
    parser.add_argument(
        "--listen", action="store_true",
        help="capture one spanning-tree BPDU per live router port (about 12s "
             "each, passive - nothing is sent). A BPDU is consumed by the "
             "next bridge along rather than forwarded, so it names the "
             "switch the cable actually lands on, proves that switch IS a "
             "switch, and its path cost to the root proves whether a further "
             "bridge sits beyond it. Off by default only because of the time.",
    )
    parser.add_argument(
        "--via", choices=("router", "server"), default="router",
        help="which host to survey from (default: router)",
    )
    parser.add_argument("--db", help="path to nmap-mac-prefixes")
    parser.add_argument("--timeout", type=float, default=180.0,
                        help="seconds a single sweep may take (default: 180)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_arguments(parser)
    return serve(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
