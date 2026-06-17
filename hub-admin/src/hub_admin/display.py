"""Rich table formatters — display layer, never imported by resources."""

from rich.console import Console
from rich.table import Table

from hub_admin.models import (
    Asset,
    EntityLink,
    ExportedIntegration,
    Integration,
    Manifest,
    ProfileApplyReport,
    ProfileSummary,
)

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


def export_summary(exported: list[ExportedIntegration], output_path: str) -> None:
    table = Table(title=f"Exported integrations -> {output_path}")
    table.add_column("#", style="dim", width=4)
    table.add_column("Integration", style="cyan")
    table.add_column("Section", style="green")
    table.add_column("Devices", justify="right")
    table.add_column("Integration ID", style="dim")
    for i, e in enumerate(exported, 1):
        table.add_row(
            str(i),
            e.name,
            e.section or "[dim](no devices)[/dim]",
            str(e.device_count),
            e.id,
        )
    console.print(table)


def profile_summary(summary: ProfileSummary, title: str | None = None) -> None:
    table = Table(title=title or f"Profile — source: {summary.source_context}")
    table.add_column("#", style="dim", width=4)
    table.add_column("Integration", style="cyan")
    table.add_column("Manifest", style="green")
    table.add_column("Config", justify="center")
    table.add_column("Devices", justify="right")
    for i, ig in enumerate(summary.integrations, 1):
        table.add_row(
            str(i),
            ig.name,
            ig.manifest_name or (ig.manifest_id or "[red](unknown)[/red]"),
            "[green]yes[/green]" if ig.has_config else "[dim]-[/dim]",
            str(ig.device_count),
        )
    console.print(table)
    if summary.site_config_keys:
        console.print(
            f"[dim]Site config:[/dim] {', '.join(summary.site_config_keys)}"
        )
    else:
        console.print("[dim]Site config: (none)[/dim]")


def profile_apply_report(report: ProfileApplyReport) -> None:
    table = Table(title=f"Deployed profile -> {report.target_context}")
    table.add_column("#", style="dim", width=4)
    table.add_column("Integration", style="cyan")
    table.add_column("Manifest", style="green")
    table.add_column("Devices", justify="right")
    table.add_column("Result")
    for i, a in enumerate(report.applied, 1):
        if a.error:
            result = f"[red]{a.error}[/red]"
        else:
            result = "[green]created[/green]"
            if a.matched_by_name:
                result += " [dim](manifest by name)[/dim]"
        manifest = a.manifest_name or a.manifest_id or "[red](unknown)[/red]"
        table.add_row(str(i), a.name, manifest, str(len(a.devices)), result)
    console.print(table)

    for a in report.applied:
        for dev_name, dropped in a.dropped.items():
            console.print(
                f"  [yellow]Dropped {len(dropped)} field(s) not in destination "
                f"schema for {dev_name}:[/yellow] {', '.join(dropped)}"
            )

    if report.site_config_applied:
        console.print(
            "[green]Site config applied.[/green] "
            "[bold]Restart hub-server to take effect "
            "(hub-admin server restart).[/bold]"
        )


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
