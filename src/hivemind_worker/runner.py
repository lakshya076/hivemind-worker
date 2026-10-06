import datetime
import json
import logging
import os
import platform
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil

from hivemind_worker.agent import build_agent_args
from hivemind_worker.models import DispatchRequest, StatusResponse

logger = logging.getLogger("hivemind_worker")


def _safe_tail(file_path: Path, max_lines: int = 30) -> str:
    """Read the last N lines of a log file safely."""
    if not file_path.exists():
        return ""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            return "".join(lines[-max_lines:]).strip()
    except Exception:
        return ""


def _build_stripped_env(workspace_dir: Path) -> Dict[str, str]:
    """
    Construct a secure, stripped environment dictionary for the agent subprocess.
    Includes only essential execution paths and AI provider API keys.
    """
    env: Dict[str, str] = {}
    is_windows = platform.system().lower() == "windows"

    # 1. Essential System Variables
    pass_keys = [
        "PATH",
        "SYSTEMROOT",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "PATHEXT",
        "WINDIR",
        "APPDATA",
        "LOCALAPPDATA",
        "USERPROFILE",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "HOME",
        "LANG",
        "LC_ALL",
        "TERM",
    ]
    for key in pass_keys:
        if key in os.environ:
            env[key] = os.environ[key]

    # 2. AI Provider API Keys
    ai_keys = [
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "GROQ_API_KEY",
        "DEEPSEEK_API_KEY",
        "MISTRAL_API_KEY",
    ]
    for key in ai_keys:
        if key in os.environ:
            env[key] = os.environ[key]

    # 3. Default Git Author / Committer config
    env["GIT_AUTHOR_NAME"] = os.environ.get("GIT_AUTHOR_NAME", "HiveMind Worker")
    env["GIT_AUTHOR_EMAIL"] = os.environ.get("GIT_AUTHOR_EMAIL", "worker@hivemind.local")
    env["GIT_COMMITTER_NAME"] = os.environ.get("GIT_COMMITTER_NAME", "HiveMind Worker")
    env["GIT_COMMITTER_EMAIL"] = os.environ.get("GIT_COMMITTER_EMAIL", "worker@hivemind.local")

    return env


class TaskRunner:
    """Manages active runs and background execution threads."""

    def __init__(self, workspace_root: Path):
        self.workspace_root = workspace_root
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self._runs: Dict[str, StatusResponse] = {}
        self._active_procs: Dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self._load_persisted_runs()

    def _load_persisted_runs(self):
        """Scan workspace root for previously saved run results."""
        if not self.workspace_root.exists():
            return
        for run_dir in self.workspace_root.iterdir():
            if run_dir.is_dir():
                result_file = run_dir / "result.json"
                if result_file.exists():
                    try:
                        with open(result_file, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        st = StatusResponse.model_validate(data)
                        self._runs[st.run_id] = st
                    except Exception:
                        pass

    def get_run_status(self, run_id: str) -> Optional[StatusResponse]:
        with self._lock:
            st = self._runs.get(run_id)
            if not st:
                return None
            # Update live log tail if running
            run_dir = self.workspace_root / run_id
            agent_log = run_dir / "agent.log"
            wrapper_log = run_dir / "wrapper.log"
            if st.status in ("starting", "running"):
                st.log_tail = _safe_tail(agent_log) or _safe_tail(wrapper_log)
            return st

    def list_runs(self) -> List[StatusResponse]:
        with self._lock:
            return list(self._runs.values())

    def get_active_run_ids(self) -> List[str]:
        with self._lock:
            return [
                rid for rid, r in self._runs.items()
                if r.status in ("starting", "running")
            ]

    def cancel_run(self, run_id: str) -> bool:
        """Cancel a running task and terminate its process tree."""
        with self._lock:
            proc = self._active_procs.get(run_id)
            if not proc:
                return False

            try:
                parent = psutil.Process(proc.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.terminate()
                    except Exception:
                        pass
                parent.terminate()
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

            if run_id in self._runs:
                self._runs[run_id].status = "cancelled"
                self._runs[run_id].error = "Task was cancelled by master."
                self._runs[run_id].finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
                self._persist_result(run_id)
            return True

    def dispatch(self, req: DispatchRequest) -> StatusResponse:
        """Initialize and start a dispatch run in a background thread."""
        with self._lock:
            if req.run_id in self._runs and self._runs[req.run_id].status in ("starting", "running"):
                return self._runs[req.run_id]

            status = StatusResponse(
                run_id=req.run_id,
                status="starting",
                branch=req.branch,
                agent_engine=req.agent_engine,
                strategy=req.strategy,
                started_at=datetime.datetime.now(datetime.timezone.utc).isoformat()
            )
            self._runs[req.run_id] = status

        thread = threading.Thread(
            target=self._run_task_worker,
            args=(req,),
            daemon=True
        )
        thread.start()
        return status

    def _persist_result(self, run_id: str):
        st = self._runs.get(run_id)
        if not st:
            return
        run_dir = self.workspace_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        res_file = run_dir / "result.json"
        try:
            with open(res_file, "w", encoding="utf-8") as f:
                f.write(st.model_dump_json(indent=2))
        except Exception:
            pass

    def _run_task_worker(self, req: DispatchRequest):
        """Main execution flow for a dispatched task."""
        run_dir = Path(req.workspace_dir) if req.workspace_dir else (self.workspace_root / req.run_id)
        run_dir.mkdir(parents=True, exist_ok=True)

        wrapper_log_path = run_dir / "wrapper.log"
        agent_log_path = run_dir / "agent.log"
        test_log_path = run_dir / "test.log"
        prompt_file = run_dir / "prompt.txt"

        def log_wrapper(msg: str):
            logger.info(f"[{req.run_id}] {msg}")
            with open(wrapper_log_path, "a", encoding="utf-8") as f:
                ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"[{ts}] {msg}\n")

        log_wrapper(f"Starting task {req.run_id} (Engine: {req.agent_engine}, Strategy: {req.strategy})")

        # 1. Format Prompt
        full_prompt = (
            f"{req.prompt.rstrip()}\n\n"
            f"You are pursuing strategy {req.strategy_index} of {req.strategy_count}: {req.strategy}. "
            "Take a distinct approach.\n"
            "Change code only. Do not commit, push, or change git remotes.\n"
        )
        with open(prompt_file, "w", encoding="utf-8") as f:
            f.write(full_prompt)

        branch = req.branch or f"exp/worker-{req.strategy}"
        env = _build_stripped_env(run_dir)

        try:
            # 2. Git Setup / Clone
            git_dir = run_dir / ".git"
            if not git_dir.exists():
                log_wrapper(f"Cloning repo {req.repo_url} into {run_dir}")
                clone_res = subprocess.run(
                    ["git", "clone", req.repo_url, "."],
                    cwd=run_dir,
                    capture_output=True,
                    text=True,
                    env=env
                )
                if clone_res.returncode != 0:
                    raise RuntimeError(f"Git clone failed: {clone_res.stderr or clone_res.stdout}")
            else:
                log_wrapper("Workspace already cloned. Fetching latest origin...")
                subprocess.run(["git", "fetch", "origin"], cwd=run_dir, capture_output=True, env=env)

            # 3. Checkout target branch
            log_wrapper(f"Checking out branch {branch}...")
            # Detect default branch name (main vs master)
            branch_check = subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", "refs/remotes/origin/main"],
                cwd=run_dir,
                env=env
            )
            base_ref = "origin/main" if branch_check.returncode == 0 else "origin/master"

            checkout_res = subprocess.run(
                ["git", "checkout", "-B", branch, base_ref],
                cwd=run_dir,
                capture_output=True,
                text=True,
                env=env
            )
            if checkout_res.returncode != 0:
                # Fallback to local checkout if remote ref not found
                subprocess.run(["git", "checkout", "-B", branch], cwd=run_dir, capture_output=True, env=env)

            # 4. Append standard ignores
            gitignore_path = run_dir / ".gitignore"
            ignores = ["\n# HiveMind artifacts\n", ".agy/\n", "__pycache__/\n", "*.pyc\n", "*.pyo\n", ".hive/\n"]
            try:
                existing_gi = gitignore_path.read_text(encoding="utf-8") if gitignore_path.exists() else ""
                with open(gitignore_path, "a", encoding="utf-8") as f:
                    for ig in ignores:
                        if ig.strip() and ig.strip() not in existing_gi:
                            f.write(ig)
            except Exception:
                pass

            # 5. Build Agent Command
            agent_args = build_agent_args(req.agent_engine, full_prompt, model=req.model)
            
            # Resolve binary path cross-platform (e.g. C:\...\claude.cmd or /usr/local/bin/claude)
            binary_name = agent_args[0]
            resolved_bin = shutil.which(binary_name)
            use_shell = False
            if resolved_bin:
                agent_args[0] = resolved_bin
                if platform.system().lower() == "windows" and resolved_bin.lower().endswith((".cmd", ".bat")):
                    use_shell = True
            elif platform.system().lower() == "windows":
                use_shell = True

            log_wrapper(f"Executing agent command: {' '.join(agent_args)}")

            with self._lock:
                self._runs[req.run_id].status = "running"
                self._runs[req.run_id].branch = branch

            # 6. Execute Agent Process
            with open(agent_log_path, "w", encoding="utf-8") as agent_out:
                proc = subprocess.Popen(
                    agent_args,
                    cwd=run_dir,
                    env=env,
                    shell=use_shell,
                    stdout=agent_out,
                    stderr=subprocess.STDOUT
                )
                with self._lock:
                    self._active_procs[req.run_id] = proc

                # Wait for agent completion (30m max timeout)
                agent_exit = proc.wait(timeout=1800)

            with self._lock:
                self._active_procs.pop(req.run_id, None)
                self._runs[req.run_id].agent_exit_code = agent_exit

            log_wrapper(f"Agent finished with exit code {agent_exit}")

            # 7. Run Test Command if specified
            test_exit = None
            if req.test_command:
                log_wrapper(f"Running test command: {req.test_command}")
                with open(test_log_path, "w", encoding="utf-8") as test_out:
                    t_proc = subprocess.run(
                        req.test_command,
                        cwd=run_dir,
                        shell=True,
                        env=env,
                        stdout=test_out,
                        stderr=subprocess.STDOUT,
                        timeout=300
                    )
                    test_exit = t_proc.returncode
                log_wrapper(f"Test command completed with exit code {test_exit}")
                with self._lock:
                    self._runs[req.run_id].test_exit_code = test_exit

            # 8. Run Benchmark Command if specified
            bench_exit = None
            bench_data = None
            if req.bench_command:
                log_wrapper(f"Running bench command: {req.bench_command}")
                b_proc = subprocess.run(
                    req.bench_command,
                    cwd=run_dir,
                    shell=True,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=300
                )
                bench_exit = b_proc.returncode
                try:
                    bench_data = json.loads(b_proc.stdout)
                except Exception:
                    bench_data = {"raw_output": b_proc.stdout}
                with self._lock:
                    self._runs[req.run_id].bench_exit_code = bench_exit
                    self._runs[req.run_id].benchmark_data = bench_data

            # 9. Git Commit & Push
            status_res = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=run_dir,
                capture_output=True,
                text=True,
                env=env
            )
            has_changes = bool(status_res.stdout.strip())

            if has_changes:
                log_wrapper("Modifications detected. Committing changes...")
                subprocess.run(["git", "add", "-A"], cwd=run_dir, env=env)
                subprocess.run(
                    ["git", "commit", "-m", f"hive: {req.strategy} (run {req.run_id})"],
                    cwd=run_dir,
                    env=env
                )
                log_wrapper(f"Pushing branch {branch} to origin...")
                push_res = subprocess.run(
                    ["git", "push", "-u", "origin", branch, "--force"],
                    cwd=run_dir,
                    capture_output=True,
                    text=True,
                    env=env
                )
                if push_res.returncode != 0:
                    log_wrapper(f"Warning: Git push failed: {push_res.stderr or push_res.stdout}")
            else:
                log_wrapper("No file modifications generated by agent.")

            # 10. Determine Final Status
            # Succeeds if agent ran and tests passed (if tests were provided)
            is_success = True
            if agent_exit != 0:
                is_success = False
            if test_exit is not None and test_exit != 0:
                is_success = False

            with self._lock:
                self._runs[req.run_id].status = "succeeded" if is_success else "failed"
                self._runs[req.run_id].log_tail = _safe_tail(agent_log_path) or _safe_tail(wrapper_log_path)
                self._runs[req.run_id].finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
                if not is_success and test_exit is not None and test_exit != 0:
                    self._runs[req.run_id].error = f"Test suite failed with exit code {test_exit}"
                elif not is_success and agent_exit != 0:
                    self._runs[req.run_id].error = f"Agent process exited with code {agent_exit}"

            log_wrapper(f"Run {req.run_id} marked as {self._runs[req.run_id].status}")

        except Exception as exc:
            log_wrapper(f"Fatal task error: {str(exc)}")
            with self._lock:
                self._runs[req.run_id].status = "failed"
                self._runs[req.run_id].error = str(exc)
                self._runs[req.run_id].log_tail = _safe_tail(agent_log_path) or _safe_tail(wrapper_log_path)
                self._runs[req.run_id].finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        finally:
            self._persist_result(req.run_id)
