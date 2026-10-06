from typing import Any, List, Optional
from pydantic import BaseModel, Field


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
    workspace_dir: Optional[str] = Field(default=None, description="Optional workspace directory override")


class DispatchResponse(BaseModel):
    """Response returned upon accepting a dispatch job."""
    run_id: str
    status: str
    message: str


class StatusResponse(BaseModel):
    """Live status report for a specific run."""
    run_id: str
    status: str = Field(..., description="starting | running | succeeded | failed | cancelled | unknown")
    branch: Optional[str] = None
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
