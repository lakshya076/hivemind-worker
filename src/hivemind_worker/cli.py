import os
import sys
from pathlib import Path
from typing import Optional

import click
import uvicorn
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from hivemind_worker import __version__
from hivemind_worker.config import WorkerConfig, get_default_worker_config_path
from hivemind_worker.server import create_app
from hivemind_worker.sysinfo import get_capabilities_tags, get_system_health

console = Console()


@click.group()
@click.version_option(package_name="hivemind-worker", prog_name="hivemind-worker", version=__version__)
def main():
    """HiveMind Worker Daemon CLI."""
    pass


@main.command(name="start")
@click.option("--key", "-k", default=None, help="Fleet API key for authentication (or set HIVEMIND_FLEET_KEY).")
@click.option("--port", "-p", default=7422, type=int, help="Port to listen on (default: 7422).")
@click.option("--host", "-h", default="0.0.0.0", help="Host address to bind (default: 0.0.0.0).")
@click.option("--workspace", "-w", default=None, help="Root folder for task workspaces (default: ~/hive-workspace).")
@click.option("--save", is_flag=True, default=False, help="Save these options as default in ~/.hivemind/worker.json.")
def start_cmd(
    key: Optional[str],
    port: int,
    host: str,
    workspace: Optional[str],
    save: bool,
):
    """Start the HiveMind worker daemon HTTP server."""
    config = WorkerConfig.load()

    # Apply overrides
    if key:
        config.fleet_key = key
    if port != 7422 or not config.port:
        config.port = port
    if host != "0.0.0.0" or not config.host:
        config.host = host
    if workspace:
        config.workspace_root = str(Path(workspace).expanduser().resolve())

    if save:
        saved_path = config.save()
        console.print(f"[dim]Saved worker configuration to {saved_path}[/dim]")

    health = get_system_health()
    engines_str = ", ".join(health.engines) if health.engines else "[yellow]none detected[/yellow]"
    key_status = "[bold green]configured[/bold green]" if config.fleet_key else "[bold red]open (no key set)[/bold red]"

    console.print(Panel.fit(
        f"[bold cyan]HiveMind Worker Daemon v{__version__}[/bold cyan]\n\n"
        f"[bold white]Host:[/bold white] [green]{config.host}:{config.port}[/green]\n"
        f"[bold white]Fleet Auth:[/bold white] {key_status}\n"
        f"[bold white]Workspace Root:[/bold white] [cyan]{config.workspace_root}[/cyan]\n"
        f"[bold white]AI Engines:[/bold white] [magenta]{engines_str}[/magenta]\n"
        f"[bold white]Hardware:[/bold white] [dim]{health.os.capitalize()} · {health.cpu_cores} cores · {health.ram_gb} GB RAM[/dim]",
        title="🤖 Worker Node Online",
        border_style="cyan",
    ))

    if not config.fleet_key:
        console.print(
            "[yellow]Warning: No fleet API key configured. Requests will not require authentication.[/yellow]\n"
            "[dim]To set a key, run: [bold white]hivemind-worker start --key <fleet-key>[/bold white][/dim]\n"
        )

    app = create_app(config)
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_level="info",
    )


@main.command(name="info")
def info_cmd():
    """Display system capabilities and detected AI tooling."""
    health = get_system_health()
    tags = get_capabilities_tags()
    config = WorkerConfig.load()

    table = Table(title=f"Worker Node Capabilities ({health.hostname})", border_style="cyan")
    table.add_column("Property", style="bold white")
    table.add_column("Value", style="green")

    table.add_row("HiveMind Worker Version", health.version)
    table.add_row("Operating System", f"{health.os} ({health.arch})")
    table.add_row("CPU Cores", str(health.cpu_cores))
    table.add_row("RAM Total", f"{health.ram_gb} GB")
    table.add_row("Git Installed", "Yes" if health.git_ready else "[red]No[/red]")
    table.add_row("Detected AI Engines", ", ".join(health.engines) if health.engines else "[yellow]None[/yellow]")
    table.add_row("Capabilities Tags", ", ".join(tags))
    table.add_row("Default Workspace Root", config.workspace_root)
    table.add_row("Configured Port", str(config.port))
    table.add_row("Fleet Key Status", "Set" if config.fleet_key else "[yellow]Not set[/yellow]")

    console.print()
    console.print(table)
    console.print()


if __name__ == "__main__":
    main()
