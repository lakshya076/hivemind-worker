import sys
from pathlib import Path
from typing import Optional

import click
import uvicorn
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from hivemind_worker import __version__
from hivemind_worker.config import (
    CONFIG_PATH_ENV,
    ConfigError,
    WorkerConfig,
    generate_fleet_key,
    get_default_worker_config_path,
)
from hivemind_worker.server import create_app
from hivemind_worker.sysinfo import get_capabilities_tags, get_system_health

console = Console()

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _config_path(ctx: click.Context) -> Path:
    return ctx.obj["config_path"]


def _load_or_exit(config_path: Path) -> WorkerConfig:
    try:
        return WorkerConfig.load(config_path)
    except ConfigError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(1)


def _persist_key(config_path: Path, key: str) -> None:
    """Store only the fleet key, leaving every other saved setting untouched."""
    persisted = _load_or_exit(config_path)
    persisted.fleet_key = key
    persisted.save(config_path)


def _print_new_key(key: str, config_path: Path, reason: str) -> None:
    if sys.stdout.isatty():
        key_line = f"[bold white]Key:[/bold white] [bold cyan]{key}[/bold cyan]"
    else:
        # Running as a service: keep the key out of log files.
        key_line = "[bold white]Key:[/bold white] run [bold white]hivemind-worker key show[/bold white] to display it"
    console.print(Panel.fit(
        f"[bold green]{reason}[/bold green]\n\n"
        f"{key_line}\n"
        f"[dim]Saved to:[/dim] {config_path}\n\n"
        "[bold yellow]Use it on the master (first worker in a new fleet):[/bold yellow]\n"
        "  Save it to [bold white]~/.hivemind/fleet.key[/bold white] on the master, or set HIVEMIND_FLEET_KEY.\n\n"
        "[bold yellow]Already have a fleet key from `hive keygen`?[/bold yellow]\n"
        "  Run [bold white]hivemind-worker key set[/bold white] and paste it instead.",
        title="HiveMind Fleet Key",
        border_style="green",
    ))


@click.group()
@click.version_option(package_name="hivemind-worker", prog_name="hivemind-worker", version=__version__)
@click.option(
    "--config",
    "config_path",
    envvar=CONFIG_PATH_ENV,
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=f"Worker config file (default: ~/.hivemind/worker.json, or ${CONFIG_PATH_ENV}).",
)
@click.pass_context
def main(ctx: click.Context, config_path: Optional[Path]):
    """HiveMind Worker Daemon CLI."""
    # When output is redirected to a file (services, Start-Process), Windows
    # defaults to cp1252 and the banner's emoji would crash startup.
    for stream in (sys.stdout, sys.stderr):
        if not stream.isatty() and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path.expanduser() if config_path else get_default_worker_config_path()


@main.command(name="start")
@click.option("--key", "-k", default=None, help="Fleet API key for authentication (or set HIVEMIND_FLEET_KEY).")
@click.option("--port", "-p", default=None, type=int, help="Port to listen on (default: 7422).")
@click.option("--host", "-h", default=None, help="Host address to bind (default: 0.0.0.0).")
@click.option("--workspace", "-w", default=None, help="Root folder for task workspaces (default: ~/hive-workspace).")
@click.option("--save", is_flag=True, default=False, help="Save these options as default in the worker config file.")
@click.option(
    "--insecure-no-auth",
    is_flag=True,
    default=False,
    help="Run without a fleet key for local testing. Forces binding to 127.0.0.1 and serves loopback clients only.",
)
@click.pass_context
def start_cmd(
    ctx: click.Context,
    key: Optional[str],
    port: Optional[int],
    host: Optional[str],
    workspace: Optional[str],
    save: bool,
    insecure_no_auth: bool,
):
    """Start the HiveMind worker daemon HTTP server."""
    config_path = _config_path(ctx)
    config = _load_or_exit(config_path)

    # Apply overrides
    if key:
        config.fleet_key = key
    if port is not None:
        config.port = port
    if host:
        config.host = host
    if workspace:
        config.workspace_root = str(Path(workspace).expanduser().resolve())

    if save:
        saved_path = config.save(config_path)
        console.print(f"[dim]Saved worker configuration to {saved_path}[/dim]")

    if insecure_no_auth:
        if config.host not in LOOPBACK_HOSTS:
            console.print(
                f"[yellow]--insecure-no-auth: binding to 127.0.0.1 instead of {config.host} "
                "(unauthenticated mode is loopback-only).[/yellow]"
            )
            config.host = "127.0.0.1"
    elif not config.fleet_key:
        config.fleet_key = generate_fleet_key()
        _persist_key(config_path, config.fleet_key)
        _print_new_key(config.fleet_key, config_path, "No fleet key was configured, so one was generated.")

    health = get_system_health()
    engines_str = ", ".join(health.engines) if health.engines else "[yellow]none detected[/yellow]"
    if insecure_no_auth:
        key_status = "[bold red]DISABLED (loopback-only test mode)[/bold red]"
    else:
        key_status = "[bold green]required[/bold green]"

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

    app = create_app(config, insecure_no_auth=insecure_no_auth)
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_level="info",
    )


@main.group(name="key")
def key_group():
    """Show, set, or rotate this worker's fleet key."""


@key_group.command(name="show")
@click.pass_context
def key_show_cmd(ctx: click.Context):
    """Print the configured fleet key."""
    config_path = _config_path(ctx)
    config = _load_or_exit(config_path)
    if not config.fleet_key:
        console.print("[yellow]No fleet key configured.[/yellow] Run [bold white]hivemind-worker key set[/bold white] "
                      "or start the worker once to generate one.")
        sys.exit(1)
    click.echo(config.fleet_key)


@key_group.command(name="set")
@click.option("--key", "key", prompt="Fleet key", hide_input=True, help="Fleet key (prompted for if omitted).")
@click.pass_context
def key_set_cmd(ctx: click.Context, key: str):
    """Store a fleet key (e.g. one created with `hive keygen` on the master)."""
    key = key.strip()
    if not key:
        console.print("[bold red]Error:[/bold red] Fleet key cannot be empty.")
        sys.exit(1)
    config_path = _config_path(ctx)
    _persist_key(config_path, key)
    console.print(f"[bold green]Fleet key saved to {config_path}.[/bold green] Restart the worker to apply it.")


@key_group.command(name="rotate")
@click.pass_context
def key_rotate_cmd(ctx: click.Context):
    """Generate a new fleet key, replacing the current one."""
    config_path = _config_path(ctx)
    new_key = generate_fleet_key()
    _persist_key(config_path, new_key)
    _print_new_key(new_key, config_path, "Generated a new fleet key. Restart the worker and update the master.")


@main.command(name="info")
@click.pass_context
def info_cmd(ctx: click.Context):
    """Display system capabilities and detected AI tooling."""
    health = get_system_health()
    tags = get_capabilities_tags()
    config = _load_or_exit(_config_path(ctx))

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
    table.add_row("Fleet Key Status", "Set" if config.fleet_key else "[yellow]Not set (generated on first start)[/yellow]")
    table.add_row("Allowed Networks", ", ".join(config.allowed_networks))

    console.print()
    console.print(table)
    console.print()


if __name__ == "__main__":
    main()
