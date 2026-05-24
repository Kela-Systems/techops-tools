"""Rich table formatters — display layer, never imported by resources."""

from rich.console import Console
from rich.table import Table

from hub_admin.models import Asset, EntityLink, Integration, Manifest

console = Console()


def manifests_table(manifests: list[Manifest]) -> None:
    table = Table(title="Manifests")
    table.add_column("#", style="dim", width=4)
    table.add_column("Name", style="cyan")
    table.add_column("Version")
    table.add_column("Description", max_width=40)
    table.add_column("Manifest ID", style="dim")
    for i, m in enumerate(manifests, 1):
        table.add_row(str(i), m.name, m.version, m.description, m.manifest_id)
    console.print(table)


def integrations_table(integrations: list[Integration]) -> None:
    table = Table(title="Integrations")
    table.add_column("#", style="dim", width=4)
    table.add_column("Name", style="cyan")
    table.add_column("Devices", justify="right")
    table.add_column("Integration ID", style="dim")
    for i, integ in enumerate(integrations, 1):
        table.add_row(str(i), integ.name, str(integ.device_count), integ.id)
    console.print(table)


def assets_table(assets: list[Asset]) -> None:
    table = Table(title="Assets")
    table.add_column("#", style="dim", width=4)
    table.add_column("Name", style="cyan")
    table.add_column("Type")
    table.add_column("Sensors")
    table.add_column("Asset ID", style="dim")
    for i, a in enumerate(assets, 1):
        sensors = ", ".join(a.sensors) or "-"
        table.add_row(str(i), a.name, a.asset_type, sensors, a.id)
    console.print(table)


def links_table(links: list[EntityLink]) -> None:
    if not links:
        console.print("[dim]No existing entity links.[/dim]")
        return
    table = Table(title="Entity Links")
    table.add_column("Source", style="cyan")
    table.add_column("Target", style="green")
    table.add_column("Link Type")
    for link in links:
        table.add_row(link.source_name, link.target_name, link.link_type or "-")
    console.print(table)
