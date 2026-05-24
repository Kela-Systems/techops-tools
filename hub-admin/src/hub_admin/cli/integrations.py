"""CLI: hub-admin integrations [list|create]"""

import json

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
        res = IntegrationResource(conn.hub_client, conn.channel)
        display.integrations_table(res.list())


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
        res = IntegrationResource(conn.hub_client, conn.channel)
        integration_id = res.create(manifest_id, config)
        console.print(f"[green]Integration created:[/green] {integration_id}")
