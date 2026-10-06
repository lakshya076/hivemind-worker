import json
import os
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field


def get_worker_config_dir() -> Path:
    """Returns ~/.hivemind config directory on host."""
    path = Path.home() / ".hivemind"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_default_worker_config_path() -> Path:
    """Returns ~/.hivemind/worker.json path."""
    return get_worker_config_dir() / "worker.json"


def get_default_workspace_root() -> Path:
    """Returns default workspace root (~/hive-workspace)."""
    path = Path.home() / "hive-workspace"
    path.mkdir(parents=True, exist_ok=True)
    return path


class WorkerConfig(BaseModel):
    """Configuration for the local HiveMind worker daemon."""
    fleet_key: Optional[str] = Field(default=None, description="Shared fleet authorization key")
    host: str = Field(default="0.0.0.0", description="Bind host address")
    port: int = Field(default=7422, description="Daemon HTTP port")
    workspace_root: str = Field(
        default_factory=lambda: str(get_default_workspace_root()),
        description="Root folder for isolated job workspaces"
    )

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "WorkerConfig":
        config_path = path or get_default_worker_config_path()
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return cls.model_validate(data)
            except Exception:
                pass
        
        # Fallback to environment variables
        env_key = os.environ.get("HIVEMIND_FLEET_KEY")
        env_port = os.environ.get("HIVEMIND_WORKER_PORT")
        port = int(env_port) if env_port and env_port.isdigit() else 7422
        return cls(fleet_key=env_key, port=port)

    def save(self, path: Optional[Path] = None) -> Path:
        config_path = path or get_default_worker_config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(self.model_dump_json(indent=2))
        return config_path
