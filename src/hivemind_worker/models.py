import re
from typing import Any, List, Optional
from pydantic import BaseModel, Field, field_validator

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")
_FORBIDDEN_URL_PREFIXES = ("ext::", "fd::")


def validate_run_id(value: str) -> str:
    """Ensure run_id is a single safe directory name with no path traversal."""
    if not isinstance(value, str):
        raise ValueError("run_id must be a string")
    cleaned = value.strip()
    if not cleaned or ".." in cleaned or not _RUN_ID_RE.match(cleaned):
        raise ValueError(
            f"Invalid run_id {value!r}: must be 1-128 characters of [A-Za-z0-9._-] and cannot contain '..'"
        )
    return cleaned


def validate_repo_url(value: str) -> str:
    """Reject git option injection (-o, --upload-pack), remote helper protocols (ext::), and control chars."""
    if not isinstance(value, str):
        raise ValueError("repo_url must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("repo_url cannot be empty")
    if cleaned.startswith("-"):
        raise ValueError(f"Invalid repo_url {value!r}: cannot start with '-'")
    lower = cleaned.lower()
    if lower.startswith(_FORBIDDEN_URL_PREFIXES) or "::" in lower.split("://", 1)[0]:
        raise ValueError(f"Invalid repo_url {value!r}: forbidden git remote helper protocol")
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in cleaned):
        raise ValueError(f"Invalid repo_url {value!r}: whitespace and control characters are not allowed")
    return cleaned


def validate_branch_name(value: Optional[str]) -> Optional[str]:
    """Ensure branch name cannot be interpreted as a git flag or invalid ref."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("branch must be a string")
    cleaned = value.strip()
    if not cleaned:
        return None
    if (
        cleaned.startswith("-")
        or cleaned.endswith(("/", ".lock", "."))
        or ".." in cleaned
        or "//" in cleaned
        or "@{" in cleaned
        or not _BRANCH_RE.match(cleaned)
    ):
        raise ValueError(f"Invalid branch name {value!r}")
    return cleaned


class DispatchRequest(BaseModel):
    """Payload sent by HiveMind master to trigger a task run."""
    run_id: str = Field(..., description="Unique identifier for the task run")
    repo_url: str = Field(..., description="Git remote repository URL")
    prompt: str = Field(..., description="Core task prompt or instruction")
    strategy: str = Field(default="strategy-1", description="Assigned strategy name")
    strategy_index: int = Field(default=1, description="1-based index of current strategy")
    strategy_count: int = Field(default=1, description="Total number of parallel strategies")
    branch: Optional[str] = Field(default=None, description="Target branch to push (e.g. exp/worker-1-strategy)")
    agent_engine: str = Field(default="claude-code", description="AI agent engine (aider, agy, claude-code)")
    model: Optional[str] = Field(default=None, description="Optional model override")
    test_command: Optional[str] = Field(default=None, description="Verification test command (e.g. 'pytest -q')")
    bench_command: Optional[str] = Field(default=None, description="Benchmark command producing JSON metrics")
    workspace_dir: Optional[str] = Field(
        default=None,
        description="Deprecated/ignored client hint; runs are always confined to the worker's workspace_root",
    )

    @field_validator("run_id")
    @classmethod
    def _check_run_id(cls, value: str) -> str:
        return validate_run_id(value)

    @field_validator("repo_url")
    @classmethod
    def _check_repo_url(cls, value: str) -> str:
        return validate_repo_url(value)

    @field_validator("branch")
    @classmethod
    def _check_branch(cls, value: Optional[str]) -> Optional[str]:
        return validate_branch_name(value)


class DispatchResponse(BaseModel):
    """Response returned upon accepting a dispatch job."""
    run_id: str
    status: str
    message: str


class StatusResponse(BaseModel):
    """Live status report for a specific run."""
    run_id: str
    status: str = Field(
        ...,
        description="starting | running | succeeded | failed | no_changes | cancelled | unknown",
    )
    branch: Optional[str] = None
    pushed: Optional[bool] = Field(default=None, description="True once the branch is on origin")
    commit: Optional[str] = Field(default=None, description="Commit SHA pushed to the branch")
    agent_engine: Optional[str] = None
    strategy: Optional[str] = None
    agent_exit_code: Optional[int] = None
    test_exit_code: Optional[int] = None
    bench_exit_code: Optional[int] = None
    benchmark_data: Optional[Any] = None
    error: Optional[str] = None
    log_tail: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class HealthResponse(BaseModel):
    """Hardware and tool capability probe response."""
    hostname: str
    os: str
    arch: str
    cpu_cores: int
    ram_gb: float
    engines: List[str]
    git_ready: bool
    version: str
    status: str = Field(default="ready", description="ready | busy")
    active_runs: List[str] = Field(default_factory=list)


class CancelResponse(BaseModel):
    """Response returned upon cancelling a task run."""
    run_id: str
    cancelled: bool
    message: str
