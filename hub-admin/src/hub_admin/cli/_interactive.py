"""Interactive setup wizard — migrated from hub_manage_integrations.py.

Preserves the original input()-driven flow while using the new
resource and display layers.
"""

import json

from rich.console import Console

from hub_admin import display
from hub_admin.client import connect
from hub_admin.config import load_config
from hub_admin.resources.devices import DeviceResource
from hub_admin.resources.integrations import IntegrationResource
from hub_admin.resources.links import LinkResource
from hub_admin.resources.manifests import ManifestResource
from hub_admin.resources.server import restart_hub_server

console = Console()


def _confirm(prompt: str) -> bool:
    return input(prompt).strip().lower() in ("y", "yes")


def _prompt_from_schema(schema: dict | None, label: str = "config") -> dict | None:
    """Walk a JSON-schema-like Struct and prompt the user for each field."""
    if not schema:
        return None

    console.print(f"\n  {label} schema: {json.dumps(schema, indent=2)}")
    props = schema.get("properties", {})
    required = schema.get("required", [])

    if not props:
        raw = input(f"  Enter {label} as JSON (or Enter to skip): ").strip()
        return json.loads(raw) if raw else None

    config: dict = {}
    for key, spec in props.items():
        prop_type = spec.get("type", "string")
        is_required = key in required
        suffix = " (required)" if is_required else " (optional, Enter to skip)"
        default = spec.get("default")
        default_hint = f" [default: {default}]" if default is not None else ""

        val = input(f"    {key} ({prop_type}){default_hint}{suffix}: ").strip()
        if not val:
            if default is not None:
                config[key] = default
            elif is_required:
                console.print(f"    [red]ERROR: {key} is required.[/red]")
                return None
            continue

        if prop_type == "integer":
            config[key] = int(val)
        elif prop_type == "number":
            config[key] = float(val)
        elif prop_type == "boolean":
            config[key] = val.lower() in ("true", "1", "yes")
        else:
            config[key] = val

    return config if config else None


# ── Device addition ──────────────────────────────────────────────────────


def _prompt_add_devices(
    dev_res: DeviceResource,
    integration_id: str,
    manifest_name: str,
    dev_schema: dict | None,
    device_config_path: str | None,
):
    if device_config_path and _confirm("\n  Add devices from device_config.json? (y/n): "):
        section = dev_res.find_matching_section(device_config_path, manifest_name)
        if section:
            console.print(f"  Auto-matched config section: '{section}'")
        else:
            sections = dev_res.list_config_sections(device_config_path)
            console.print(
                f"  No auto-match for manifest '{manifest_name}'.\n"
                f"  Available sections: "
                f"{', '.join(f'{i+1}) {s}' for i, s in enumerate(sections))}"
            )
            choice = input("  Pick section # (or Enter to skip): ").strip()
            if not choice:
                return
            idx = int(choice) - 1
            if idx < 0 or idx >= len(sections):
                console.print("  Invalid selection.")
                return
            section = sections[idx]

        allowed = dev_res.allowed_keys_from_schema(dev_schema)

        def _on_drop(dev_name: str, dropped: list[str]):
            console.print(
                f"    [yellow]Dropped {len(dropped)} field(s) not in "
                f"'{manifest_name}' schema for {dev_name}:[/yellow] "
                f"{', '.join(dropped)}"
            )

        results = dev_res.add_from_config(
            integration_id, section, device_config_path,
            allowed_keys=allowed, on_drop=_on_drop,
        )
        for name, device_id in results:
            console.print(f"    Device created: {name} -> {device_id}")

    elif _confirm("  Add devices manually? (y/n): "):
        while True:
            name = input("  Device name: ").strip()
            if not name:
                console.print("  Skipped (empty name).")
                break

            setup_info = _prompt_from_schema(dev_schema, label="device setup_info")
            if setup_info is None:
                raw = input("  Enter setup_info as JSON (or Enter for empty): ").strip()
                setup_info = json.loads(raw) if raw else {}

            device_id = dev_res.create(integration_id, name, setup_info)
            console.print(f"    Device created: {name} -> {device_id}")

            if not _confirm("\n  Add another device? (y/n): "):
                break


# ── Entity linking ───────────────────────────────────────────────────────


def _prompt_entity_links(link_res: LinkResource) -> bool:
    """Returns True if any new links were configured."""
    if not _confirm("\n  Set up entity links? (y/n): "):
        return False

    assets = link_res.list_assets()
    if not assets:
        console.print("  No assets found on this hub.\n")
        return False

    display.assets_table(assets)
    links = link_res.list_links(assets)
    display.links_table(links)

    any_added = False
    while True:
        choice = input("  Source asset # (or Enter to finish): ").strip()
        if not choice:
            break
        if not choice.isdigit():
            console.print("  Invalid selection.\n")
            continue
        src_idx = int(choice) - 1
        if src_idx < 0 or src_idx >= len(assets):
            console.print("  Invalid selection.\n")
            continue
        source = assets[src_idx]

        targets = [a for a in assets if a.id != source.id]
        console.print(f"\n  Targets for {source.name}:")
        console.print(f"  {'#':<4} {'Name':<25} {'Type':<15} {'Asset ID'}")
        console.print("  " + "-" * 70)
        for i, t in enumerate(targets, 1):
            console.print(f"  {i:<4} {t.name:<25} {t.asset_type:<15} {t.id}")
        print()

        tgt_choice = input("  Target asset # (or Enter to cancel): ").strip()
        if not tgt_choice or not tgt_choice.isdigit():
            continue
        tgt_idx = int(tgt_choice) - 1
        if tgt_idx < 0 or tgt_idx >= len(targets):
            console.print("  Invalid selection.\n")
            continue
        target = targets[tgt_idx]

        try:
            added = link_res.add_available_link(source.id, target.id)
            if added:
                any_added = True
                console.print(
                    f"  [green]Configured:[/green] {source.name} -> {target.name}"
                )
            else:
                console.print(
                    f"  [yellow]Already configured:[/yellow] "
                    f"{source.name} -> {target.name}"
                )
        except Exception as e:
            console.print(f"  [red]ERROR updating site config: {e}[/red]")

        if not _confirm("\n  Add another link? (y/n): "):
            break

    if any_added:
        console.print(
            "  [bold]NOTE:[/bold] Entity link changes require a hub server "
            "restart to take effect."
        )
    print()
    return any_added


# ── Main loop ────────────────────────────────────────────────────────────


def run_interactive(context: str, device_config_path: str | None = None):
    cfg = load_config(context=context)

    console.print(f"Starting port-forward to {cfg.context}...")

    with connect(cfg) as conn:
        console.print(f"Port-forward ready. Connecting via localhost:{cfg.port}...\n")

        manifest_res = ManifestResource(conn.channel)
        int_res = IntegrationResource(conn.channel)
        dev_res = DeviceResource(conn.channel)
        link_res = LinkResource(conn.channel)

        manifests = manifest_res.list()
        links_configured = False
        if not device_config_path:
            device_config_path = (
                cfg.device_config_paths[0] if cfg.device_config_paths else None
            )

        while True:
            # Phase 1: check existing integrations
            integrations = int_res.list()
            if integrations:
                display.integrations_table(integrations)
                choice = input(
                    "Select integration # to add devices "
                    "(or 'n' for new integration, 'q' to quit): "
                ).strip()

                if choice.lower() == "q":
                    break

                if choice.lower() != "n":
                    if choice.isdigit():
                        idx = int(choice) - 1
                        if idx < 0 or idx >= len(integrations):
                            console.print("Invalid selection.\n")
                            continue
                        selected = integrations[idx]
                    else:
                        selected = next(
                            (ig for ig in integrations if ig.id == choice),
                            None,
                        )
                        if selected is None:
                            console.print("Integration ID not found.\n")
                            continue

                    manifest_name = manifest_res.resolve_name(
                        selected.manifest_id, manifests
                    )
                    console.print(
                        f"\n-> Integration: {selected.name} ({selected.id})"
                    )
                    _prompt_add_devices(
                        dev_res,
                        selected.id,
                        manifest_name,
                        selected.device_setup_info_schema,
                        device_config_path,
                    )
                    if _prompt_entity_links(link_res):
                        links_configured = True
                    print()
                    continue

            # Phase 2: create a new integration from a manifest
            if not manifests:
                console.print("No manifests found on this hub.")
                break

            display.manifests_table(manifests)
            choice = input(
                "Select manifest # to add integration "
                "(or 'n' to skip, 'q' to quit): "
            ).strip()

            if choice.lower() == "q":
                break

            if choice.lower() == "n":
                if _prompt_entity_links(link_res):
                    links_configured = True
                continue

            if choice.isdigit():
                idx = int(choice) - 1
                if idx < 0 or idx >= len(manifests):
                    console.print("Invalid selection.\n")
                    continue
                selected_m = manifests[idx]
            else:
                selected_m = next(
                    (m for m in manifests if m.manifest_id == choice),
                    None,
                )
                if selected_m is None:
                    console.print("Manifest ID not found.\n")
                    continue

            console.print(
                f"\n-> Manifest: {selected_m.name} v{selected_m.version} "
                f"({selected_m.manifest_id})"
            )

            int_schema, dev_schema = manifest_res.get_schemas(
                selected_m.manifest_id
            )
            integration_config = _prompt_from_schema(
                int_schema, label="integration config"
            )
            integration_id = int_res.create(
                selected_m.manifest_id, integration_config
            )
            console.print(f"\n  Integration created: {integration_id}")

            _prompt_add_devices(
                dev_res,
                integration_id,
                selected_m.name,
                dev_schema,
                device_config_path,
            )
            if _prompt_entity_links(link_res):
                links_configured = True
            print()

        # Offer to restart hub-server if links were configured
        if links_configured:
            console.print(
                "\n[bold]Entity links were configured during this session.[/bold]"
            )
            if _confirm("Restart hub-server pod to apply changes? (y/n): "):
                console.print("Restarting hub-server...")
                ok, msg = restart_hub_server(conn.context, conn.namespace)
                style = "green" if ok else "red"
                console.print(f"[{style}]{msg}[/{style}]")

    console.print("Done.")
