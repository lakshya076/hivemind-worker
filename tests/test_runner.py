import json
import os
import sys
import time
from pathlib import Path

import pytest

from hivemind_worker import runner as runner_mod
from hivemind_worker.agent import AgentCommand
from hivemind_worker.models import DispatchRequest, StatusResponse
import subprocess

from hivemind_worker.runner import (
    TaskRunner,
    _agent_launch_args,
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


def add_branch(tmp_path, origin, git, name, marker):
    """Push a branch to origin whose tree has an extra marker file."""
    work = tmp_path / f"branch-{name.replace('/', '-')}"
    git(tmp_path, "clone", str(origin), str(work))
    git(work, "checkout", "-b", name)
    (work / marker).write_text("from base\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-m", f"base {name}")
    git(work, "push", "origin", name)


def test_run_starts_from_requested_base_branch(tmp_path, origin_repo, fake_agent, git):
    add_branch(tmp_path, origin_repo, git, "feature/login", "BASE_MARKER")
    runner = TaskRunner(tmp_path / "ws")
    req = dispatch(runner, origin_repo, base_branch="feature/login")

    st = wait_for(runner, "r1")

    assert st.status == "succeeded", st.error
    assert st.base_branch == "feature/login"
    files = git(origin_repo, "ls-tree", "-r", "--name-only", req.branch).splitlines()
    assert "BASE_MARKER" in files and "feature.txt" in files
    # The pushed branch sits on top of the base, not on main.
    assert git(origin_repo, "merge-base", "--is-ancestor", "feature/login", req.branch) == ""


def test_default_base_is_reported(tmp_path, origin_repo, fake_agent):
    runner = TaskRunner(tmp_path / "ws")
    dispatch(runner, origin_repo)
    assert wait_for(runner, "r1").base_branch == "main"


def test_missing_base_branch_fails_without_falling_back(tmp_path, origin_repo, fake_agent, git):
    runner = TaskRunner(tmp_path / "ws")
    req = dispatch(runner, origin_repo, base_branch="does-not-exist")

    st = wait_for(runner, "r1")

    assert st.status == "failed"
    assert "Base branch 'does-not-exist' not found on origin" in st.error
    assert st.agent_exit_code is None  # the agent never ran on the default branch instead
    assert git(origin_repo, "branch", "--list", req.branch) == ""


def test_read_log_returns_the_end_of_large_logs(tmp_path):
    runner = TaskRunner(tmp_path / "ws")
    runner._runs["r1"] = StatusResponse(run_id="r1", status="succeeded")
    hive = tmp_path / "ws" / "r1" / ".hive"
    hive.mkdir(parents=True)
    (hive / "agent.log").write_bytes("".join(f"line {i}\n" for i in range(1000)).encode())

    whole = runner.read_log("r1", "agent", 1 << 20)
    tail = runner.read_log("r1", "agent", 20)

    assert whole["truncated"] is False and whole["content"].startswith("line 0\n")
    assert tail["truncated"] is True and tail["size"] == whole["size"]
    assert tail["content"].endswith("line 999\n") and len(tail["content"]) == 20


def test_read_log_refuses_unknown_runs_missing_logs_and_links(tmp_path):
    runner = TaskRunner(tmp_path / "ws")
    runner._runs["r1"] = StatusResponse(run_id="r1", status="running")
    hive = tmp_path / "ws" / "r1" / ".hive"
    hive.mkdir(parents=True)
    secret = tmp_path / "worker.json"
    secret.write_text('{"fleet_key": "secret"}', encoding="utf-8")
    os.link(secret, hive / "test.log")  # planted hardlink to a secret

    with pytest.raises(KeyError):
        runner.read_log("unknown", "agent", 100)
    with pytest.raises(ValueError):
        runner.read_log("../escape", "agent", 100)
    with pytest.raises(FileNotFoundError):
        runner.read_log("r1", "agent", 100)  # not written yet
    with pytest.raises(FileNotFoundError):
        runner.read_log("r1", "test", 100)  # hardlink refused
    with pytest.raises(FileNotFoundError):
        runner.read_log("r1", "../../worker", 100)  # only the four known logs


def test_failed_mirror_refresh_is_never_used(tmp_path, origin_repo):
    runner = TaskRunner(tmp_path / "ws")
    env = _build_stripped_env(tmp_path, include_ai_keys=False)
    assert runner._ensure_mirror(str(origin_repo), env) is not None  # first call clones the mirror

    origin_repo.rename(tmp_path / "origin-gone.git")  # origin unreachable from now on

    # A stale mirror must not be handed out: the agent would silently work on an old base.
    assert runner._ensure_mirror(str(origin_repo), env) is None


def test_run_fails_clearly_when_origin_is_unreachable(tmp_path, origin_repo, fake_agent, git):
    runner = TaskRunner(tmp_path / "ws")
    dispatch(runner, origin_repo, run_id="first")
    assert wait_for(runner, "first").status == "succeeded"
    origin_repo.rename(tmp_path / "origin-gone.git")

    dispatch(runner, origin_repo, run_id="second")
    st = wait_for(runner, "second")

    assert st.status == "failed"
    assert "Git clone failed" in st.error
    assert st.agent_exit_code is None  # the agent never ran on a stale checkout


def test_native_agents_launch_without_a_shell():
    assert _agent_launch_args("/usr/bin/aider", ["--yes-always"], {}) == ["/usr/bin/aider", "--yes-always"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows .cmd shims")
@pytest.mark.parametrize("folder", ["plain", "with space", "John Smith (work)"])
def test_windows_cmd_shim_runs_from_any_path(tmp_path, folder):
    shim_dir = tmp_path / folder
    shim_dir.mkdir()
    shim = shim_dir / "claude.cmd"
    shim.write_text("@echo off\r\necho SHIM-RAN %*\r\n", encoding="utf-8")

    args = _agent_launch_args(str(shim), ["-p", "--permission-mode", "acceptEdits", "--model", "sonnet"], dict(os.environ))
    res = subprocess.run(args, capture_output=True, text=True)

    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "SHIM-RAN -p --permission-mode acceptEdits --model sonnet"


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


def test_read_bench_output_refuses_hardlinks_and_outside_root(tmp_path):
    from hivemind_worker.runner import _read_bench_output

    secret = tmp_path / "worker.json"
    secret.write_text('{"fleet_key": "secret"}', encoding="utf-8")
    run_dir = tmp_path / "run"
    hive = run_dir / ".hive"
    hive.mkdir(parents=True)
    bench_log = hive / "bench.log"
    os.link(secret, bench_log)

    assert _read_bench_output(bench_log, root_dir=run_dir) == {"raw_output": ""}
    assert _read_bench_output(secret, root_dir=run_dir) == {"raw_output": ""}


def test_sanitize_git_dir_rejects_missing_or_linked_git_dir_after_agent(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with pytest.raises(RuntimeError, match="removed or corrupted"):
        _sanitize_git_dir(run_dir, b"[core]\n\tbare = false\n")


def test_git_cmd_timeout_terminates_process(tmp_path):
    runner = TaskRunner(tmp_path / "ws", git_timeout=0.5)
    with pytest.raises(RuntimeError, match="Git operation timed out"):
        runner._run_git_cmd(
            "r-timeout",
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            env=dict(os.environ),
        )
    assert "r-timeout" not in runner._active_procs


def test_redispatched_run_clears_stale_hive_logs(tmp_path, origin_repo, fake_agent):
    runner = TaskRunner(tmp_path / "ws")
    dispatch(runner, origin_repo, run_id="r-reuse", test_command=f'"{sys.executable}" -c "print(\'old-test-log\')"')
    st1 = wait_for(runner, "r-reuse")
    assert st1.status == "succeeded"
    assert runner.read_log("r-reuse", "test", 1024)["content"].strip() == "old-test-log"

    # Re-dispatch same run_id without a test_command: old test.log must not survive
    dispatch(runner, origin_repo, run_id="r-reuse", test_command=None)
    st2 = wait_for(runner, "r-reuse")
    assert st2.status == "succeeded"
    with pytest.raises(FileNotFoundError):
        runner.read_log("r-reuse", "test", 1024)


def test_redispatched_run_sanitizes_git_hooks_before_checkout(tmp_path, origin_repo, fake_agent):
    runner = TaskRunner(tmp_path / "ws")
    dispatch(runner, origin_repo, run_id="r-hook-reuse")
    assert wait_for(runner, "r-hook-reuse").status == "succeeded"

    # Simulate an interrupted run that left a post-checkout hook and fsmonitor in the reused workspace
    run_dir = tmp_path / "ws" / "r-hook-reuse"
    pwned = run_dir / "checkout_pwned.txt"
    hook = run_dir / ".git" / "hooks" / "post-checkout"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f"#!/bin/sh\necho pwned > {pwned}\n", encoding="utf-8")
    with open(run_dir / ".git" / "config", "a", encoding="utf-8") as f:
        f.write(f"[core]\n\tfsmonitor = echo pwned > {pwned}\n")

    dispatch(runner, origin_repo, run_id="r-hook-reuse")
    assert wait_for(runner, "r-hook-reuse").status == "succeeded"
    assert not pwned.exists()
    assert not hook.exists()


def test_get_run_status_returns_isolated_snapshot_when_running(tmp_path):
    runner = TaskRunner(tmp_path / "ws")
    run_dir = tmp_path / "ws" / "r-snap"
    (run_dir / ".hive").mkdir(parents=True)
    (run_dir / ".hive" / "agent.log").write_text("live output\n", encoding="utf-8")
    runner._runs["r-snap"] = StatusResponse(run_id="r-snap", status="running")

    st = runner.get_run_status("r-snap")
    assert st is not None and st.log_tail == "live output"
    assert runner._runs["r-snap"].log_tail is None


def test_persist_result_and_load_refuse_hardlinks_to_external_files(tmp_path):
    ws = tmp_path / "ws"
    runner = TaskRunner(ws)
    secret = tmp_path / "worker.json"
    secret.write_text('{"fleet_key": "do-not-overwrite"}', encoding="utf-8")

    run_dir = ws / "r-link"
    run_dir.mkdir(parents=True)
    os.link(secret, run_dir / "result.json")

    runner._runs["r-link"] = StatusResponse(run_id="r-link", status="succeeded")
    runner._persist_result("r-link")

    # External file must not be overwritten through the planted hardlink
    assert secret.read_text(encoding="utf-8") == '{"fleet_key": "do-not-overwrite"}'
    assert json.loads((run_dir / "result.json").read_text(encoding="utf-8"))["status"] == "succeeded"

    # And _load_persisted_runs skips hardlinked result.json files
    run_dir2 = ws / "r-link-load"
    run_dir2.mkdir(parents=True)
    fake_result = tmp_path / "fake_result.json"
    fake_result.write_text(StatusResponse(run_id="r-link-load", status="running").model_dump_json(), encoding="utf-8")
    os.link(fake_result, run_dir2 / "result.json")
    runner2 = TaskRunner(ws)
    assert runner2.get_run_status("r-link-load") is None



