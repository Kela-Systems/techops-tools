"""CLI: hub-admin devices [add|create]"""

import json

import typer
from rich.console import Console

from hub_admin import display
from hub_admin.client import connect
from hub_admin.config import load_config
from hub_admin.resources.devices import DeviceResource
from hub_admin.resources.integrations import IntegrationResource

app = typer.Typer(help="Manage devices")
console = Console()


@app.command("add")
def add_from_config(
    integration_id: str = typer.Argument(..., help="Integration ID"),
    config_path: str = typer.Option(
        ..., "--config", "-c", help="Path to device config JSON"
    ),
    section: str = typer.Option(
        None, "--section", "-s", help="Config section key"
    ),
    manifest_name: str = typer.Option(
        None, "--manifest-name", help="Manifest name for auto-matching section"
    ),
    context: str = typer.Option("kela-office-01", help="kubectl context"),
):
    """Add devices from a config file."""
    cfg = load_config(context=context)
    with connect(cfg) as conn:
        res = DeviceResource(conn.channel)

        if not section and manifest_name:
            section = res.find_matching_section(config_path, manifest_name)

        if not section:
            sections = res.list_config_sections(config_path)
            if not sections:
                console.print("[red]No sections found in config.[/red]")
                raise typer.Exit(1)
            console.print(f"Available sections: {', '.join(sections)}")
            section = typer.prompt("Pick a section")

        schema = next(
            (
                ig.device_setup_info_schema
                for ig in IntegrationResource(conn.channel).list()
                if ig.id == integration_id
            ),
            None,
        )
        allowed = res.allowed_keys_from_schema(schema)

        def _on_drop(dev_name: str, dropped: list[str]):
            console.print(
                f"  [yellow]Dropped {len(dropped)} field(s) not in target "
                f"schema for {dev_name}:[/yellow] {', '.join(dropped)}"
            )

        results = res.add_from_config(
            integration_id, section, config_path,
            allowed_keys=allowed, on_drop=_on_drop,
        )
        for name, device_id in results:
            console.print(f"  Device created: {name} -> {device_id}")


@app.command("list")
def list_devices(
    integration_id: str = typer.Argument(..., help="Integration ID"),
    context: str = typer.Option("kela-office-01", help="kubectl context"),
):
    """List devices belonging to an integration."""
    cfg = load_config(context=context)
    with connect(cfg) as conn:
        res = DeviceResource(conn.channel)
        display.devices_table(res.list(integration_id))


@app.command("update")
def update_device(
    integration_id: str = typer.Argument(..., help="Integration ID"),
    device_id: str = typer.Argument(..., help="Device ID"),
    patch: str = typer.Option(
        None,
        "--patch",
        "-p",
        help=(
            "JSON merge-patch for setup_info (RFC 7396): top-level keys "
            "overwrite, a null value deletes the key, untouched keys are kept."
        ),
    ),
    name: str = typer.Option(None, "--name", help="New device name (rename)"),
    context: str = typer.Option("kela-office-01", help="kubectl context"),
):
    """Modify an existing device's setup_info (merge-patch) and/or name."""
    if patch is None and name is None:
        console.print(
            "[red]Nothing to update: pass --patch and/or --name.[/red]"
        )
        raise typer.Exit(1)

    cfg = load_config(context=context)
    with connect(cfg) as conn:
        res = DeviceResource(conn.channel)
        if patch is not None:
            patch_dict = json.loads(patch)
            res.update_setup_info(integration_id, device_id, patch_dict)
            console.print(
                f"[green]setup_info patched:[/green] {device_id} <- {patch}"
            )
        if name is not None:
            res.rename(integration_id, device_id, name)
            console.print(f"[green]Renamed:[/green] {device_id} -> {name}")


@app.command("create")
def create_device(
    integration_id: str = typer.Argument(..., help="Integration ID"),
    name: str = typer.Argument(..., help="Device name"),
    setup_info: str = typer.Option(
        "{}", "--setup-info", help="Setup info as JSON string"
    ),
    context: str = typer.Option("kela-office-01", help="kubectl context"),
):
    """Create a single device."""
    cfg = load_config(context=context)
    with connect(cfg) as conn:
        res = DeviceResource(conn.channel)
        info = json.loads(setup_info)
        device_id = res.create(integration_id, name, info)
        console.print(f"[green]Device created:[/green] {name} -> {device_id}")
