"""CLI: hub-admin profile [export|apply|show]

A *profile* is a portable bundle of a site's integrations (manifest +
integration_config + devices) and its SiteConfig. Capture one site's setup and
deploy it onto another context:

    hub-admin profile export --context kela-cuas-06 -o cuas-06.profile.json
    hub-admin profile apply  cuas-06.profile.json --context <dest>
"""

import json
from pathlib import Path

import typer
from rich.console import Console

from hub_admin import display
from hub_admin.client import connect
from hub_admin.config import load_config
from hub_admin.resources.profile import ProfileResource

app = typer.Typer(help="Export/deploy site profiles (integrations + site settings)")
console = Console()


@app.command("export")
def export_profile(
    output: str = typer.Option(
        "site.profile.json",
        "--output",
        "-o",
        help="Where to write the profile bundle",
    ),
    context: str = typer.Option("kela-office-01", help="kubectl context to export from"),
    site_config: bool = typer.Option(
        True,
        "--site-config/--no-site-config",
        help="Include the hub SiteConfig (entity links etc.)",
    ),
    api_token: str = typer.Option(
        None,
        "--api-token",
        envvar="HUB_API_TOKEN",
        help="API token for hubs that enforce auth (or set HUB_API_TOKEN)",
    ),
    force: bool = typer.Option(
        False, "--force", "-f", help="Overwrite the output file if it exists"
    ),
):
    """Export a context's integrations + site settings into a deployable profile."""
    out_path = Path(output)
    if out_path.exists() and not force:
        console.print(
            f"[red]Refusing to overwrite existing file:[/red] {out_path} "
            "(pass --force to overwrite)"
        )
        raise typer.Exit(1)

    cfg = load_config(context=context)
    with connect(cfg, api_token=api_token) as conn:
        res = ProfileResource(conn.channel)
        bundle, summary = res.export(cfg.context, include_site_config=site_config)

    out_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n")
    display.profile_summary(summary, title=f"Exported profile -> {out_path}")
    total_devices = sum(ig.device_count for ig in summary.integrations)
    console.print(
        f"[green]Exported[/green] {len(summary.integrations)} integration(s), "
        f"{total_devices} device(s) -> {out_path}"
    )


@app.command("apply")
def apply_profile(
    profile: str = typer.Argument(..., help="Path to a profile bundle"),
    context: str = typer.Option(..., help="kubectl context to deploy onto"),
    site_config: bool = typer.Option(
        True,
        "--site-config/--no-site-config",
        help="Apply the bundled SiteConfig (fully replaces destination's)",
    ),
    restart: bool = typer.Option(
        False,
        "--restart",
        "-r",
        help="Restart hub-server after applying (needed for SiteConfig changes)",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt"
    ),
    api_token: str = typer.Option(
        None,
        "--api-token",
        envvar="HUB_API_TOKEN",
        help="API token for hubs that enforce auth (or set HUB_API_TOKEN)",
    ),
):
    """Deploy a profile's integrations + site settings onto a target context."""
    bundle_path = Path(profile)
    if not bundle_path.exists():
        console.print(f"[red]Profile not found:[/red] {bundle_path}")
        raise typer.Exit(1)
    bundle = json.loads(bundle_path.read_text())

    summary = ProfileResource.summarize(bundle)
    display.profile_summary(
        summary, title=f"About to deploy {bundle_path} -> {context}"
    )
    if site_config and summary.site_config_keys:
        console.print(
            "[yellow]Note:[/yellow] --site-config fully replaces the "
            "destination SiteConfig. Entity links reference site-specific asset "
            "IDs; pass --no-site-config to skip if deploying to a different site."
        )

    if not yes and not typer.confirm(f"Deploy onto context '{context}'?"):
        raise typer.Exit(1)

    cfg = load_config(context=context)
    with connect(cfg, api_token=api_token) as conn:
        res = ProfileResource(conn.channel)
        report = res.apply(bundle, cfg.context, include_site_config=site_config)

    display.profile_apply_report(report)

    failures = [a for a in report.applied if a.error]
    if restart and report.site_config_applied:
        from hub_admin.resources.server import restart_hub_server

        console.print("Restarting hub-server...")
        ok, msg = restart_hub_server(cfg.context, cfg.namespace)
        style = "green" if ok else "red"
        console.print(f"[{style}]{msg}[/{style}]")

    if failures:
        raise typer.Exit(1)


@app.command("show")
def show_profile(
    profile: str = typer.Argument(..., help="Path to a profile bundle"),
):
    """Inspect a profile bundle offline (no hub connection)."""
    bundle_path = Path(profile)
    if not bundle_path.exists():
        console.print(f"[red]Profile not found:[/red] {bundle_path}")
        raise typer.Exit(1)
    bundle = json.loads(bundle_path.read_text())
    summary = ProfileResource.summarize(bundle)
    display.profile_summary(
        summary, title=f"{bundle_path} (exported {bundle.get('exported_at', '?')})"
    )
