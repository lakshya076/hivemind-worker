import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import List, Tuple
import psutil

from hivemind_worker import __version__
from hivemind_worker.models import HealthResponse


def is_tool_installed(name: str) -> bool:
    """Check if an executable is found on system PATH or standard user bin locations."""
    if shutil.which(name) is not None:
        return True
    
    # Check common fallback locations
    home = Path.home()
    fallbacks = [
        home / ".local" / "bin" / name,
        home / ".cargo" / "bin" / name,
        Path("/usr/local/bin") / name,
        Path("/usr/bin") / name,
        home / "AppData" / "Local" / "Programs" / name,
    ]
    if platform.system().lower() == "windows":
        fallbacks.extend([
            home / ".local" / "bin" / f"{name}.exe",
            home / ".local" / "bin" / f"{name}.cmd",
            home / "AppData" / "Local" / "agy" / "bin" / f"{name}.exe",
        ])
    return any(p.exists() for p in fallbacks)


def detect_installed_engines() -> List[str]:
    """Detect available AI coding agent CLIs on the system."""
    engines = []
    if is_tool_installed("claude"):
        engines.append("claude-code")
    if is_tool_installed("agy"):
        engines.append("agy")
    if is_tool_installed("aider"):
        engines.append("aider")
    return engines


def get_system_health(active_runs: List[str] = None) -> HealthResponse:
    """Collect real-time system metrics, hardware specs, and agent readiness."""
    active_runs = active_runs or []
    os_name = platform.system().lower()
    arch = platform.machine()
    hostname = platform.node()
    cpu_cores = os.cpu_count() or 1
    
    try:
        ram_gb = round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except Exception:
        ram_gb = 0.0

    engines = detect_installed_engines()
    git_ready = is_tool_installed("git")

    status = "busy" if active_runs else "ready"

    return HealthResponse(
        hostname=hostname,
        os=os_name,
        arch=arch,
        cpu_cores=cpu_cores,
        ram_gb=ram_gb,
        engines=engines,
        git_ready=git_ready,
        version=__version__,
        status=status,
        active_runs=active_runs
    )


def get_capabilities_tags() -> List[str]:
    """Return capability tags for HiveMind cluster discovery."""
    tags = []
    os_name = platform.system().lower()
    tags.append(f"os-{os_name}")
    tags.append(f"arch-{platform.machine().lower()}")

    cores = os.cpu_count() or 1
    if cores >= 16:
        tags.append("high-cpu")
    else:
        tags.append("standard-cpu")

    for eng in detect_installed_engines():
        tags.append(f"engine:{eng}")

    if is_tool_installed("docker"):
        tags.append("docker")

    return tags
