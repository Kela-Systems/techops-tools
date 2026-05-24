"""Hub Admin CLI — Typer application with subcommands."""

import typer

from hub_admin.cli import devices, integrations, links, manifests, server

app = typer.Typer(
    name="hub-admin",
    help="Production-grade CLI for hub device & integration management.",
    no_args_is_help=True,
)

app.add_typer(manifests.app, name="manifests")
app.add_typer(integrations.app, name="integrations")
app.add_typer(devices.app, name="devices")
app.add_typer(links.app, name="links")
app.add_typer(server.app, name="server")


@app.command()
def interactive(
    context: str = typer.Option("kela-office-01", prompt="kubectl context"),
    device_config: str = typer.Option(
        None, "--device-config", "-d", help="Path to device_config.json"
    ),
):
    """Launch the interactive setup wizard."""
    from hub_admin.cli._interactive import run_interactive

    run_interactive(context, device_config_path=device_config)
