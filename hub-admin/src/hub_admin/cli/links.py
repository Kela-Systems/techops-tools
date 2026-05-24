"""CLI: hub-admin links [list|assets|add]"""

import typer
from rich.console import Console

from hub_admin import display
from hub_admin.client import connect
from hub_admin.config import load_config
from hub_admin.resources.links import LinkResource

app = typer.Typer(help="Manage entity links")
console = Console()


@app.command("list")
def list_links(
    context: str = typer.Option("kela-office-01", help="kubectl context"),
):
    """List all entity links on the hub."""
    cfg = load_config(context=context)
    with connect(cfg) as conn:
        res = LinkResource(conn.channel)
        assets = res.list_assets()
        links = res.list_links(assets)
        display.links_table(links)


@app.command("assets")
def list_assets(
    context: str = typer.Option("kela-office-01", help="kubectl context"),
):
    """List all assets on the hub."""
    cfg = load_config(context=context)
    with connect(cfg) as conn:
        res = LinkResource(conn.channel)
        display.assets_table(res.list_assets())


@app.command("add")
def add_link(
    source: str = typer.Argument(..., help="Source asset ID or name"),
    target: str = typer.Argument(..., help="Target asset ID or name"),
    context: str = typer.Option("kela-office-01", help="kubectl context"),
    restart: bool = typer.Option(
        False, "--restart", "-r", help="Restart hub-server after adding"
    ),
):
    """Add an available entity link between two assets."""
    from hub_admin.resources.server import restart_hub_server

    cfg = load_config(context=context)
    with connect(cfg) as conn:
        res = LinkResource(conn.channel)
        assets = res.list_assets()

        source_id = _resolve_asset(source, assets)
        target_id = _resolve_asset(target, assets)

        added = res.add_available_link(source_id, target_id)
        if added:
            console.print(f"[green]Configured:[/green] {source} -> {target}")
            if restart:
                console.print("Restarting hub-server...")
                ok, msg = restart_hub_server(cfg.context, cfg.namespace)
                style = "green" if ok else "red"
                console.print(f"[{style}]{msg}[/{style}]")
        else:
            console.print(f"[yellow]Already configured:[/yellow] {source} -> {target}")


def _resolve_asset(name_or_id: str, assets) -> str:
    for a in assets:
        if a.id == name_or_id or a.name == name_or_id:
            return a.id
    console.print(f"[red]Asset not found: {name_or_id}[/red]")
    raise typer.Exit(1)
