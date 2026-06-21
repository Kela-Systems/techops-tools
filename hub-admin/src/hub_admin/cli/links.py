"""CLI: hub-admin links [list|assets|add]"""

import grpc
import typer
from rich.console import Console

from hub_admin import display
from hub_admin.client import connect
from hub_admin.config import load_config
from hub_admin.resources.links import LinkResource

app = typer.Typer(help="Manage entity links")
console = Console()


def _guard_unimplemented(e: grpc.RpcError) -> None:
    """Turn a missing-AssetService error into a clean message + exit."""
    if e.code() == grpc.StatusCode.UNIMPLEMENTED:
        console.print(
            "[yellow]This hub's server does not support the asset API "
            "(AssetService is UNIMPLEMENTED). The hub-server is likely older "
            "than this client and needs upgrading.[/yellow]"
        )
        raise typer.Exit(1)
    raise e


@app.command("list")
def list_links(
    context: str = typer.Option("kela-office-01", help="kubectl context"),
):
    """List all entity links on the hub."""
    cfg = load_config(context=context)
    with connect(cfg) as conn:
        res = LinkResource(conn.channel)
        try:
            assets = res.list_assets()
            links = res.list_links(assets)
        except grpc.RpcError as e:
            _guard_unimplemented(e)
        display.links_table(links)


@app.command("assets")
def list_assets(
    context: str = typer.Option("kela-office-01", help="kubectl context"),
):
    """List all assets on the hub."""
    cfg = load_config(context=context)
    with connect(cfg) as conn:
        res = LinkResource(conn.channel)
        try:
            assets = res.list_assets()
        except grpc.RpcError as e:
            _guard_unimplemented(e)
        display.assets_table(assets)


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
        try:
            assets = res.list_assets()
            source_id = _resolve_asset(source, assets)
            target_id = _resolve_asset(target, assets)
        except grpc.RpcError as e:
            if e.code() != grpc.StatusCode.UNIMPLEMENTED:
                raise
            # Older hub without AssetService: skip name resolution and use the
            # arguments as raw asset IDs. The site-config write path below is
            # unchanged across hub versions, so the link can still be persisted.
            console.print(
                "[yellow]Asset discovery unavailable on this hub "
                "(AssetService is UNIMPLEMENTED); treating SOURCE/TARGET as raw "
                "asset IDs.[/yellow]"
            )
            source_id, target_id = source, target

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
