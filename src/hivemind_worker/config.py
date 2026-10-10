import ipaddress
import json
import os
import secrets
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field, field_validator

# Source networks allowed to reach the daemon by default: loopback, private LAN
# ranges (RFC 1918 / IPv6 ULA / link-local) and Tailscale (CGNAT + Tailscale ULA).
# Anything else (i.e. the public internet) is rejected before authentication.
DEFAULT_ALLOWED_NETWORKS = [
    "127.0.0.0/8",
    "::1/128",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "fe80::/10",
    "fc00::/7",
    "100.64.0.0/10",
    "fd7a:115c:a1e0::/48",
]

LOOPBACK_NETWORKS = ["127.0.0.0/8", "::1/128"]

CONFIG_PATH_ENV = "HIVEMIND_WORKER_CONFIG"


class ConfigError(Exception):
    """Raised when the worker configuration exists but cannot be used."""


def get_worker_config_dir() -> Path:
    """Returns ~/.hivemind config directory on host."""
    path = Path.home() / ".hivemind"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_default_worker_config_path() -> Path:
    """Returns the worker config path ($HIVEMIND_WORKER_CONFIG or ~/.hivemind/worker.json)."""
    override = os.environ.get(CONFIG_PATH_ENV)
    if override:
        return Path(override).expanduser()
    return get_worker_config_dir() / "worker.json"


def get_default_workspace_root() -> Path:
    """Returns default workspace root (~/hive-workspace)."""
    path = Path.home() / "hive-workspace"
    path.mkdir(parents=True, exist_ok=True)
    return path


def generate_fleet_key() -> str:
    """Generate a fleet key in the same format as the master's `hive keygen`."""
    return f"hive-key-{secrets.token_hex(20)}"


class WorkerConfig(BaseModel):
    """Configuration for the local HiveMind worker daemon."""
    fleet_key: Optional[str] = Field(default=None, description="Shared fleet authorization key")
    host: str = Field(default="0.0.0.0", description="Bind host address")
    port: int = Field(default=7422, description="Daemon HTTP port")
    workspace_root: str = Field(
        default_factory=lambda: str(get_default_workspace_root()),
        description="Root folder for isolated job workspaces"
    )
    allowed_networks: List[str] = Field(
        default_factory=lambda: list(DEFAULT_ALLOWED_NETWORKS),
        description="Source networks (CIDR) allowed to connect; others are rejected with 403",
    )
    allowed_hosts: List[str] = Field(
        default_factory=list,
        description="Extra DNS names clients may use to reach this worker (Host header allowlist)",
    )

    @field_validator("allowed_networks")
    @classmethod
    def _validate_networks(cls, value: List[str]) -> List[str]:
        for cidr in value:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError as exc:
                raise ValueError(f"Invalid network in allowed_networks: {cidr!r} ({exc})") from exc
        return value

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "WorkerConfig":
        config_path = path or get_default_worker_config_path()
        if config_path.exists():
            # Fail closed: a config that exists but cannot be read must never
            # silently fall back to an unauthenticated default.
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                config = cls.model_validate(data)
            except Exception as exc:
                raise ConfigError(
                    f"Could not read worker config '{config_path}': {exc}. "
                    "Fix the file or delete it to regenerate."
                ) from exc
            if not config.fleet_key and os.environ.get("HIVEMIND_FLEET_KEY"):
                config.fleet_key = os.environ["HIVEMIND_FLEET_KEY"]
            return config

        # Fallback to environment variables
        env_key = os.environ.get("HIVEMIND_FLEET_KEY")
        env_port = os.environ.get("HIVEMIND_WORKER_PORT")
        port = int(env_port) if env_port and env_port.isdigit() else 7422
        return cls(fleet_key=env_key, port=port)

    def save(self, path: Optional[Path] = None) -> Path:
        config_path = path or get_default_worker_config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        # The file holds the fleet key: create it owner-only (0600) on POSIX.
        # On Windows the mode is ignored; files under the user profile already
        # inherit an ACL restricted to that user, SYSTEM and Administrators.
        fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(self.model_dump_json(indent=2))
        try:
            os.chmod(config_path, 0o600)
        except OSError:
            pass
        return config_path
