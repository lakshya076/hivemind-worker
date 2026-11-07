import os
import platform
import shutil
from pathlib import Path
from typing import List, Optional
import psutil

from hivemind_worker import __version__
from hivemind_worker.models import HealthResponse

# Advertised in /health so masters can refuse to use a feature on a worker that would
# silently ignore it (unknown dispatch fields are dropped, so an old worker given a
# base_branch would quietly start from the default branch).
WORKER_FEATURES = ["base_branch", "logs"]


def resolve_binary(name: str, search_path: Optional[str] = None) -> Optional[str]:
    """Locate an executable on PATH or in standard user install locations.

    The single lookup used both by /health (engine detection) and by the runner
    (launching agents), so a worker never reports an engine it can't start or
    the reverse. Services often run with a minimal PATH, hence the fallbacks.
    """
    resolved = shutil.which(name, path=search_path) if search_path else None
    resolved = resolved or shutil.which(name)
    if resolved:
        return resolved

    home = Path.home()
    candidates = [
        home / ".local" / "bin" / name,
        home / ".cargo" / "bin" / name,
        Path("/usr/local/bin") / name,
        Path("/usr/bin") / name,
    ]
    if platform.system().lower() == "windows":
        candidates += [
            home / ".local" / "bin" / f"{name}.exe",
            home / ".local" / "bin" / f"{name}.cmd",
            home / "AppData" / "Roaming" / "npm" / f"{name}.cmd",
            home / "AppData" / "Local" / "agy" / "bin" / f"{name}.exe",
            home / "AppData" / "Local" / "Programs" / name / f"{name}.exe",
        ]
    return next((str(c) for c in candidates if c.is_file()), None)


def is_tool_installed(name: str) -> bool:
    return resolve_binary(name) is not None


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
        active_runs=active_runs,
        features=list(WORKER_FEATURES),
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
