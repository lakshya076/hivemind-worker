import json
import os
import sys
import time
from pathlib import Path

import pytest

from hivemind_worker import runner as runner_mod
from hivemind_worker.agent import AgentCommand
from hivemind_worker.models import DispatchRequest, StatusResponse
from hivemind_worker.runner import (
    TaskRunner,
    _build_stripped_env,
    _safe_tail,
    _sanitize_git_dir,
    _write_git_exclude,
)

TERMINAL = {"succeeded", "failed", "no_changes", "cancelled"}

FAKE_AGENT = """
import os, pathlib, sys, time
mode = sys.argv[1]
if mode == "edit":
    pathlib.Path("feature.txt").write_text("done\\n")
elif mode == "env":
    pathlib.Path("agent_env.txt").write_text(os.environ.get("ANTHROPIC_API_KEY", "<missing>"))
elif mode == "hook":
    # A prompt-injected agent tries to plant a git hook and an fsmonitor command.
    pathlib.Path(".git/hooks/pre-commit").write_text("#!/bin/sh\\necho pwned > pwned.txt\\n")
    with open(".git/config", "a") as f:
        f.write("[core]\\n\\tfsmonitor = echo pwned > pwned.txt\\n")
    pathlib.Path("feature.txt").write_text("done\\n")
elif mode == "sleep":
    time.sleep(60)
elif mode == "fail":
    sys.exit(3)
"""

CHECK_ENV = """
import os, sys
sys.exit(5 if os.environ.get("ANTHROPIC_API_KEY") else 0)
"""


# ---------------------------------------------------------------- helpers


def test_safe_tail_returns_last_lines(tmp_path):
    log = tmp_path / "agent.log"
    log.write_text("\n".join(f"line {i}" for i in range(100)), encoding="utf-8")
    assert _safe_tail(log, max_lines=3) == "line 97\nline 98\nline 99"
    assert _safe_tail(tmp_path / "missing.log") == ""


def test_safe_tail_refuses_hardlinks(tmp_path):
    secret = tmp_path / "secret.json"
    secret.write_text("fleet_key", encoding="utf-8")
    link = tmp_path / "test.log"
    os.link(secret, link)
    assert _safe_tail(link) == ""


def test_safe_tail_refuses_symlinks(tmp_path):
    secret = tmp_path / "secret.json"
    secret.write_text("fleet_key", encoding="utf-8")
    link = tmp_path / "test.log"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlinks need extra privileges on this machine")
    assert _safe_tail(link) == ""


def test_safe_tail_refuses_paths_outside_root(tmp_path):
    outside = tmp_path / "outside.log"
    outside.write_text("secret", encoding="utf-8")
    root = tmp_path / "run"
    root.mkdir()
    assert _safe_tail(outside, root_dir=root) == ""


def test_stripped_env_drops_secrets_and_scopes_ai_keys(home, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    (home / ".env").write_text('export OPENROUTER_API_KEY="sk-or"\nAWS_X=1\n', encoding="utf-8")

    agent_env = _build_stripped_env(home, include_ai_keys=True)
    tool_env = _build_stripped_env(home, include_ai_keys=False)

    for env in (agent_env, tool_env):
        assert "AWS_SECRET_ACCESS_KEY" not in env and "SSH_AUTH_SOCK" not in env and "AWS_X" not in env
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert "PATH" in env
    assert agent_env["ANTHROPIC_API_KEY"] == "sk-ant"
    assert agent_env["OPENROUTER_API_KEY"] == "sk-or"
    assert "ANTHROPIC_API_KEY" not in tool_env and "OPENROUTER_API_KEY" not in tool_env


def test_git_exclude_is_used_instead_of_gitignore(tmp_path):
    (tmp_path / ".git" / "info").mkdir(parents=True)
    _write_git_exclude(tmp_path)
    _write_git_exclude(tmp_path)  # idempotent
    exclude = (tmp_path / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert exclude.count(".hive/") == 1
    assert ".aider*" in exclude  # aider's chat history files must never be committed
    assert not (tmp_path / ".gitignore").exists()


def test_sanitize_git_dir_removes_hooks_and_restores_config(tmp_path):
    hooks = tmp_path / ".git" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "pre-push").write_text("evil", encoding="utf-8")
    config = tmp_path / ".git" / "config"
    config.write_bytes(b"[core]\n\tfsmonitor = evil\n")
    _sanitize_git_dir(tmp_path, b"[core]\n\tbare = false\n")
    assert list(hooks.iterdir()) == []
    assert config.read_bytes() == b"[core]\n\tbare = false\n"


# ---------------------------------------------------------------- TaskRunner state


@pytest.mark.parametrize("run_id", ["../escape", "a/b", ".."])
def test_run_dir_is_confined_to_workspace_root(tmp_path, run_id):
    with pytest.raises(ValueError):
        TaskRunner(tmp_path / "ws")._run_dir_for(run_id)


def test_restart_marks_interrupted_runs_failed(tmp_path):
    ws = tmp_path / "ws"
    for run_id, status in (("r-running", "running"), ("r-done", "succeeded")):
        (ws / run_id).mkdir(parents=True)
        (ws / run_id / "result.json").write_text(
            StatusResponse(run_id=run_id, status=status).model_dump_json(), encoding="utf-8"
        )

    runner = TaskRunner(ws)

    interrupted = runner.get_run_status("r-running")
    assert interrupted.status == "failed" and "restarted" in interrupted.error
    assert json.loads((ws / "r-running" / "result.json").read_text())["status"] == "failed"
    assert runner.get_run_status("r-done").status == "succeeded"


def test_prune_keeps_results_and_logs(tmp_path):
    ws = tmp_path / "ws"
    runner = TaskRunner(ws, max_retained_workspaces=1)
    for i in range(3):
        run_id = f"r{i}"
        run_dir = ws / run_id
        (run_dir / ".hive").mkdir(parents=True)
        (run_dir / ".hive" / "agent.log").write_text("log", encoding="utf-8")
        (run_dir / "src.py").write_text("code", encoding="utf-8")
        runner._runs[run_id] = StatusResponse(
            run_id=run_id, status="succeeded", finished_at=f"2026-11-07T00:00:0{i}"
        )
        runner._persist_result(run_id)

    assert runner.prune_old_workspaces() == 2

    assert (ws / "r2" / "src.py").exists()  # newest kept
    for old in ("r0", "r1"):
        assert not (ws / old / "src.py").exists()
        assert (ws / old / "result.json").exists()
        assert (ws / old / ".hive" / "agent.log").exists()


def test_cancel_unknown_or_finished_run_returns_false(tmp_path):
    runner = TaskRunner(tmp_path / "ws")
    runner._runs["done"] = StatusResponse(run_id="done", status="succeeded")
    assert runner.cancel_run("done") is False
    assert runner.cancel_run("missing") is False
    assert runner.cancel_run("../x") is False


# ---------------------------------------------------------------- full runs against a real git origin


@pytest.fixture
def fake_agent(tmp_path, monkeypatch):
    """Replace the agent CLI with a Python script; ``mode`` picks its behaviour."""
    script = tmp_path / "fake_agent.py"
    script.write_text(FAKE_AGENT, encoding="utf-8")
    state = {"mode": "edit"}

    def build(engine, prompt_file, prompt_text, model=None):
        return AgentCommand(args=[sys.executable, str(script), state["mode"]])

    monkeypatch.setattr(runner_mod, "build_agent_command", build)
    return state


def wait_for(runner: TaskRunner, run_id: str, statuses=TERMINAL, timeout: float = 60) -> StatusResponse:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = runner.get_run_status(run_id)
        if st and st.status in statuses:
            return st
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} did not reach {statuses}: {runner.get_run_status(run_id)}")


def dispatch(runner: TaskRunner, origin: Path, run_id: str = "r1", **fields) -> DispatchRequest:
    values = {
        "run_id": run_id,
        "repo_url": str(origin),
        "prompt": "do the thing",
        "strategy": "simd",
        "branch": f"exp/{run_id}/ari-simd",
        "agent_engine": "claude-code",
    }
    values.update(fields)
    req = DispatchRequest(**values)
    runner.dispatch(req)
    return req


def test_successful_run_pushes_branch_to_origin(tmp_path, origin_repo, fake_agent, git):
    runner = TaskRunner(tmp_path / "ws")
    req = dispatch(runner, origin_repo)

    st = wait_for(runner, "r1")

    assert st.status == "succeeded", st.error
    assert st.pushed is True
    assert st.branch == req.branch
    assert st.commit == git(origin_repo, "rev-parse", f"refs/heads/{req.branch}")
    assert git(origin_repo, "show", f"{req.branch}:feature.txt") == "done"
    # Worker artifacts are never committed.
    files = git(origin_repo, "ls-tree", "-r", "--name-only", req.branch).splitlines()
    assert ".hive/prompt.txt" not in files and "result.json" not in files
    saved = json.loads((tmp_path / "ws" / "r1" / "result.json").read_text())
    assert saved["status"] == "succeeded"


def test_agent_without_changes_reports_no_changes(tmp_path, origin_repo, fake_agent, git):
    fake_agent["mode"] = "noop"
    runner = TaskRunner(tmp_path / "ws")
    req = dispatch(runner, origin_repo)

    st = wait_for(runner, "r1")

    assert st.status == "no_changes" and st.pushed is False
    assert git(origin_repo, "branch", "--list", req.branch) == ""


def test_agent_failure_reports_exit_code(tmp_path, origin_repo, fake_agent):
    fake_agent["mode"] = "fail"
    runner = TaskRunner(tmp_path / "ws")
    dispatch(runner, origin_repo)

    st = wait_for(runner, "r1")

    assert st.status == "failed" and st.agent_exit_code == 3


def test_ai_keys_reach_agent_but_not_test_command(tmp_path, origin_repo, fake_agent, git, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    fake_agent["mode"] = "env"
    check = tmp_path / "check_env.py"
    check.write_text(CHECK_ENV, encoding="utf-8")
    runner = TaskRunner(tmp_path / "ws")
    req = dispatch(runner, origin_repo, test_command=f'"{sys.executable}" "{check}"')

    st = wait_for(runner, "r1")

    assert st.test_exit_code == 0, "test command could see ANTHROPIC_API_KEY"
    assert st.status == "succeeded", st.error
    assert git(origin_repo, "show", f"{req.branch}:agent_env.txt") == "sk-ant-secret"


def test_failing_tests_mark_run_failed(tmp_path, origin_repo, fake_agent):
    runner = TaskRunner(tmp_path / "ws")
    dispatch(runner, origin_repo, test_command=f'"{sys.executable}" -c "import sys; sys.exit(4)"')

    st = wait_for(runner, "r1")

    assert st.status == "failed" and st.test_exit_code == 4
    assert "Test suite failed" in st.error


def test_benchmark_json_is_captured(tmp_path, origin_repo, fake_agent):
    bench = tmp_path / "bench.py"
    bench.write_text('import json; print(json.dumps({"ops_per_sec": 1234}))', encoding="utf-8")
    runner = TaskRunner(tmp_path / "ws")
    dispatch(runner, origin_repo, bench_command=f'"{sys.executable}" "{bench}"')

    st = wait_for(runner, "r1")

    assert st.bench_exit_code == 0
    assert st.benchmark_data == {"ops_per_sec": 1234}


@pytest.mark.parametrize("task", ["!echo pwned > pwned.txt", "/run echo pwned", "/init"])
def test_prompt_never_starts_with_the_task_text(tmp_path, origin_repo, fake_agent, task):
    # aider runs a message starting with "/" or "!" as a command; Claude Code reads a
    # leading "/" as a slash command. The task must sit behind a fixed header.
    fake_agent["mode"] = "noop"
    runner = TaskRunner(tmp_path / "ws")
    dispatch(runner, origin_repo, prompt=task)
    wait_for(runner, "r1")

    prompt = (tmp_path / "ws" / "r1" / ".hive" / "prompt.txt").read_text(encoding="utf-8")
    assert prompt[0] not in "/!"
    assert prompt.startswith("You are pursuing strategy 1 of 1: simd.")
    assert f"Task:\n{task}\n" in prompt


def test_huge_benchmark_output_is_not_read_whole(tmp_path, origin_repo, fake_agent):
    bench = tmp_path / "noisy_bench.py"
    bench.write_text('import sys\nsys.stdout.write("x" * 300_000)\nprint()\nprint("last line")\n', encoding="utf-8")
    runner = TaskRunner(tmp_path / "ws")
    dispatch(runner, origin_repo, bench_command=f'"{sys.executable}" "{bench}"')

    st = wait_for(runner, "r1")

    raw = st.benchmark_data["raw_output"]
    assert raw.startswith("[benchmark output truncated:")
    assert raw.endswith("last line")
    assert len(raw) < 5000
    assert len((tmp_path / "ws" / "r1" / "result.json").read_bytes()) < 20_000


def test_agent_planted_git_hooks_never_run(tmp_path, origin_repo, fake_agent, git):
    fake_agent["mode"] = "hook"
    runner = TaskRunner(tmp_path / "ws")
    req = dispatch(runner, origin_repo)

    st = wait_for(runner, "r1")

    assert st.status == "succeeded", st.error
    assert not (tmp_path / "ws" / "r1" / "pwned.txt").exists()
    files = git(origin_repo, "ls-tree", "-r", "--name-only", req.branch).splitlines()
    assert "pwned.txt" not in files


def test_agent_timeout_kills_agent_and_fails_run(tmp_path, origin_repo, fake_agent):
    fake_agent["mode"] = "sleep"
    runner = TaskRunner(tmp_path / "ws", agent_timeout=1)
    dispatch(runner, origin_repo)

    st = wait_for(runner, "r1", timeout=30)

    assert st.status == "failed" and "timed out after 1s" in st.error
    assert runner.get_active_run_ids() == []


def test_cancel_kills_running_agent(tmp_path, origin_repo, fake_agent, git):
    fake_agent["mode"] = "sleep"
    runner = TaskRunner(tmp_path / "ws")
    req = dispatch(runner, origin_repo)
    wait_for(runner, "r1", statuses={"running"})
    deadline = time.monotonic() + 30
    while "r1" not in runner._active_procs and time.monotonic() < deadline:
        time.sleep(0.05)
    proc = runner._active_procs["r1"]

    assert runner.cancel_run("r1") is True

    assert runner.get_run_status("r1").status == "cancelled"
    proc.wait(timeout=10)
    assert git(origin_repo, "branch", "--list", req.branch) == ""


def test_concurrency_limit_queues_and_queued_run_can_be_cancelled(tmp_path, origin_repo, fake_agent):
    fake_agent["mode"] = "sleep"
    runner = TaskRunner(tmp_path / "ws", max_concurrent_runs=1)
    dispatch(runner, origin_repo, run_id="first")
    wait_for(runner, "first", statuses={"running"})
    dispatch(runner, origin_repo, run_id="second")
    time.sleep(0.5)

    assert runner.get_run_status("second").status == "starting"
    assert runner.cancel_run("second") is True
    assert runner.cancel_run("first") is True
    assert runner.get_run_status("second").status == "cancelled"
