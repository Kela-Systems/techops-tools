"""CLI: hub-admin server [restart]"""

import typer
from rich.console import Console

from hub_admin.config import load_config
from hub_admin.resources.server import restart_hub_server as _restart

app = typer.Typer(help="Hub server operations")
console = Console()


@app.command("restart")
def restart(
    context: str = typer.Option("kela-office-01", help="kubectl context"),
):
    """Restart the hub-server pod."""
    cfg = load_config(context=context)
    console.print(f"Searching for hub-server pod (context: {cfg.context})...")
    ok, msg = _restart(cfg.context, cfg.namespace)
    style = "green" if ok else "red"
    console.print(f"[{style}]{msg}[/{style}]")
    if not ok:
        raise typer.Exit(1)
