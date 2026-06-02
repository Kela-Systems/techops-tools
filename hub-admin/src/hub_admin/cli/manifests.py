"""CLI: hub-admin manifests [list]"""

import typer

from hub_admin import display
from hub_admin.client import connect
from hub_admin.config import load_config
from hub_admin.resources.manifests import ManifestResource

app = typer.Typer(help="Manage integration manifests")


@app.command("list")
def list_manifests(
    context: str = typer.Option("kela-office-01", help="kubectl context"),
):
    """List available manifests on the hub."""
    cfg = load_config(context=context)
    with connect(cfg) as conn:
        res = ManifestResource(conn.channel)
        display.manifests_table(res.list())
