"""CLI: hub-admin integrations [list|create|export]"""

import json
from pathlib import Path

import typer
from rich.console import Console

from hub_admin import display
from hub_admin.client import connect
from hub_admin.config import load_config
from hub_admin.resources.integrations import IntegrationResource

app = typer.Typer(help="Manage integrations")
console = Console()


@app.command("list")
def list_integrations(
    context: str = typer.Option("kela-office-01", help="kubectl context"),
):
    """List existing integrations with device counts."""
    cfg = load_config(context=context)
    with connect(cfg) as conn:
        res = IntegrationResource(conn.channel)
        display.integrations_table(res.list())


@app.command("export")
def export_integrations(
    output: str = typer.Option(
        "device_config.exported.json",
        "--output",
        "-o",
        help="Where to write the device_config.json",
    ),
    context: str = typer.Option("kela-office-01", help="kubectl context"),
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
    """Download all integrations + their devices into a reusable device_config.json.

    Inverse of `devices add`: produces a file you can replay onto another hub
    with `hub-admin devices add <integration_id> -c <file> -s <section>`.
    """
    out_path = Path(output)
    if out_path.exists() and not force:
        console.print(
            f"[red]Refusing to overwrite existing file:[/red] {out_path} "
            "(pass --force to overwrite)"
        )
        raise typer.Exit(1)

    cfg = load_config(context=context)
    with connect(cfg, api_token=api_token) as conn:
        res = IntegrationResource(conn.channel)
        config, summary = res.export()

    out_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    display.export_summary(summary, str(out_path))
    total_devices = sum(e.device_count for e in summary)
    console.print(
        f"[green]Exported[/green] {len(summary)} integration(s), "
        f"{total_devices} device(s) -> {out_path}"
    )


@app.command("create")
def create_integration(
    manifest_id: str = typer.Argument(..., help="Manifest ID to create from"),
    config_json: str = typer.Option(
        None, "--config", "-c", help="Integration config as JSON string"
    ),
    context: str = typer.Option("kela-office-01", help="kubectl context"),
):
    """Create a new integration from a manifest."""
    cfg = load_config(context=context)
    config = json.loads(config_json) if config_json else None
    with connect(cfg) as conn:
        res = IntegrationResource(conn.channel)
        integration_id = res.create(manifest_id, config)
        console.print(f"[green]Integration created:[/green] {integration_id}")
