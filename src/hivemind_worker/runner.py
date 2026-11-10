import datetime
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import stat
import subprocess
import threading
from pathlib import Path
from typing import Dict, List, Optional

import psutil

from hivemind_worker.agent import build_agent_command
from hivemind_worker.sysinfo import resolve_binary
from hivemind_worker.models import (
    DispatchRequest,
    StatusResponse,
    validate_branch_name,
    validate_repo_url,
    validate_run_id,
)

logger = logging.getLogger("hivemind_worker")


def _is_link_or_junction(path: Path) -> bool:
    """Return True if path is a symbolic link or Windows NTFS directory junction."""
    try:
        if path.is_symlink():
            return True
        is_junc = getattr(path, "is_junction", None)
        if callable(is_junc) and is_junc():
            return True
    except Exception:
        return True
    return False


def _safe_tail(
    file_path: Path,
    max_lines: int = 30,
    max_bytes: int = 65536,
    *,
    root_dir: Optional[Path] = None,
) -> str:
    """Read the last N lines of a log file without loading the entire file into memory.
    Refuses symlinks, NTFS junctions, hardlinks, and paths escaping root_dir."""
    try:
        if _is_link_or_junction(file_path) or _is_link_or_junction(file_path.parent):
            return ""
        if not file_path.exists() or not file_path.is_file():
            return ""
        if root_dir is not None and not file_path.resolve().is_relative_to(root_dir.resolve()):
            return ""
        st = file_path.stat()
        if st.st_nlink > 1:
            return ""
        with open(file_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes), os.SEEK_SET)
            data = f.read()
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        return "\n".join(lines[-max_lines:]).strip()
    except Exception:
        return ""


BENCH_OUTPUT_LIMIT = 64 * 1024

# Logs GET /logs/{run_id}?file=<name> may serve, all inside the run's .hive/ folder.
LOG_FILES = {"agent": "agent.log", "test": "test.log", "bench": "bench.log", "wrapper": "wrapper.log"}


def _read_bench_output(bench_log_path: Path, *, root_dir: Optional[Path] = None):
    """Parse benchmark output as JSON, else keep it as raw text.

    Output over BENCH_OUTPUT_LIMIT is never read whole: it would be held in memory,
    sent to the master and saved in both run files. Only its tail is kept.
    Refuses symlinks, NTFS junctions, hardlinks, and paths escaping root_dir.
    """
    try:
        if (
            _is_link_or_junction(bench_log_path)
            or _is_link_or_junction(bench_log_path.parent)
            or not bench_log_path.exists()
            or not bench_log_path.is_file()
        ):
            return {"raw_output": ""}
        if root_dir is not None and not bench_log_path.resolve().is_relative_to(root_dir.resolve()):
            return {"raw_output": ""}
        st = bench_log_path.stat()
        if st.st_nlink > 1:
            return {"raw_output": ""}
        size = st.st_size
        if size > BENCH_OUTPUT_LIMIT:
            tail = _safe_tail(bench_log_path, max_lines=20, max_bytes=4096, root_dir=root_dir)
            return {"raw_output": f"[benchmark output truncated: {size} bytes] {tail}"}
        raw = bench_log_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {"raw_output": ""}
    try:
        return json.loads(raw)
    except Exception:
        return {"raw_output": raw.strip()}


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-") or "strategy"


def _build_stripped_env(workspace_dir: Path, *, include_ai_keys: bool = True) -> Dict[str, str]:
    """
    Construct a secure, stripped environment dictionary for worker subprocesses.
    AI provider API keys are included only when ``include_ai_keys=True`` (for the
    AI coding agent) and omitted for git, test, and benchmark subprocesses.
    """
    env: Dict[str, str] = {}

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

    # 2. AI Provider API Keys (only for agent subprocesses, never for test/bench/git)
    if include_ai_keys:
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
        # Check current os.environ first
        for key in ai_keys:
            if key in os.environ:
                env[key] = os.environ[key]

        # Also load from ~/.env if present and not already set
        env_file = Path.home() / ".env"
        if env_file.exists():
            try:
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("export "):
                        line = line[7:].strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("\"'")
                        if k in ai_keys and k not in env:
                            env[k] = v
            except Exception:
                pass

    # 3. Default Git Author / Committer config + non-interactive git
    env["GIT_AUTHOR_NAME"] = os.environ.get("GIT_AUTHOR_NAME", "HiveMind Worker")
    env["GIT_AUTHOR_EMAIL"] = os.environ.get("GIT_AUTHOR_EMAIL", "worker@hivemind.local")
    env["GIT_COMMITTER_NAME"] = os.environ.get("GIT_COMMITTER_NAME", "HiveMind Worker")
    env["GIT_COMMITTER_EMAIL"] = os.environ.get("GIT_COMMITTER_EMAIL", "worker@hivemind.local")
    env["GIT_TERMINAL_PROMPT"] = "0"

    return env


def _kill_proc_tree(proc: subprocess.Popen) -> None:
    """Terminate and kill a subprocess and all of its descendants."""
    try:
        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
        targets = [*children, parent]
        for p in targets:
            try:
                p.terminate()
            except Exception:
                pass
        _, alive = psutil.wait_procs(targets, timeout=2.0)
        for p in alive:
            try:
                p.kill()
            except Exception:
                pass
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _resolve_agent_binary(binary_name: str, env: Dict[str, str]) -> Optional[str]:
    """Locate the agent executable (same lookup as /health's engine detection)."""
    return resolve_binary(binary_name, env.get("PATH"))


def _agent_launch_args(resolved_bin: str, args: List[str], env: Dict[str, str]):
    """Command to start an agent without a shell.

    Windows can't execute .cmd/.bat shims (npm's claude.cmd) directly, so they go
    through cmd.exe. With `cmd /s /c`, cmd strips the first and last quote of the
    command line, so the whole command must be wrapped in one extra pair of quotes;
    passing it as a list let subprocess's quoting be stripped and broke any shim
    path containing a space (C:\\Users\\John Smith\\...). Callers must never put
    the prompt in ``args`` for a shim.
    """
    is_windows = platform.system().lower() == "windows"
    if is_windows and resolved_bin.lower().endswith((".cmd", ".bat")):
        comspec = env.get("COMSPEC") or os.environ.get("COMSPEC", "cmd.exe")
        inner = subprocess.list2cmdline([resolved_bin, *args])
        return f'{subprocess.list2cmdline([comspec])} /d /s /c "{inner}"'
    return [resolved_bin, *args]


def _write_git_exclude(run_dir: Path) -> None:
    """Write worker ignore patterns to .git/info/exclude so .gitignore is never dirtied."""
    git_dir = run_dir / ".git"
    if _is_link_or_junction(git_dir) or not git_dir.is_dir():
        return
    exclude_path = git_dir / "info" / "exclude"
    patterns = [".hive/", ".agy/", ".aider*", "result.json", "__pycache__/", "*.pyc", "*.pyo"]
    try:
        if _is_link_or_junction(exclude_path.parent):
            TaskRunner._remove_entry_readonly_safe(exclude_path.parent)
        if _is_link_or_junction(exclude_path):
            TaskRunner._remove_entry_readonly_safe(exclude_path)
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        existing_lines = {line.strip() for line in existing.splitlines()}
        missing = [p for p in patterns if p not in existing_lines]
        if missing:
            prefix = "" if (not existing or existing.endswith("\n")) else "\n"
            with open(exclude_path, "a", encoding="utf-8") as f:
                f.write(prefix + "# HiveMind worker artifacts\n" + "\n".join(missing) + "\n")
    except Exception:
        pass


def _sanitize_git_dir(run_dir: Path, config_snapshot: Optional[bytes] = None) -> None:
    """Neutralize .git/hooks and restore .git/config so an agent with file-edit access
    cannot execute shell commands via pre-commit/pre-push hooks or core.fsmonitor/sshCommand."""
    git_dir = run_dir / ".git"
    if _is_link_or_junction(git_dir):
        TaskRunner._remove_entry_readonly_safe(git_dir)
        if config_snapshot is not None:
            raise RuntimeError("Repository .git directory was replaced with a symlink or junction by the agent.")
        return
    if not git_dir.exists() or not git_dir.is_dir():
        if config_snapshot is not None:
            raise RuntimeError("Repository .git directory was removed or corrupted by the agent.")
        return

    if config_snapshot is not None:
        cfg_file = git_dir / "config"
        try:
            if _is_link_or_junction(cfg_file):
                TaskRunner._remove_entry_readonly_safe(cfg_file)
            cfg_file.write_bytes(config_snapshot)
        except Exception as exc:
            raise RuntimeError(f"Failed to restore .git/config snapshot: {exc}") from exc

    hooks_dir = git_dir / "hooks"
    try:
        if _is_link_or_junction(hooks_dir) or (hooks_dir.exists() and not hooks_dir.is_dir()):
            TaskRunner._remove_entry_readonly_safe(hooks_dir)
        if hooks_dir.exists():
            for entry in list(hooks_dir.iterdir()):
                TaskRunner._remove_entry_readonly_safe(entry)
        else:
            hooks_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _detect_base_ref(run_dir: Path, env: Dict[str, str]) -> Optional[str]:
    """Find the remote default branch reference (e.g. origin/main, origin/master, or origin/HEAD)."""
    sym = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
        cwd=run_dir,
        capture_output=True,
        text=True,
        env=env,
    )
    if sym.returncode == 0 and sym.stdout.strip():
        ref = sym.stdout.strip()
        if ref.startswith("refs/remotes/"):
            return ref[len("refs/remotes/"):]
        return ref

    for candidate in ("origin/main", "origin/master"):
        chk = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/remotes/{candidate}"],
            cwd=run_dir,
            capture_output=True,
            env=env,
        )
        if chk.returncode == 0:
            return candidate

    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin"],
        cwd=run_dir,
        capture_output=True,
        text=True,
        env=env,
    )
    if refs.returncode == 0:
        for line in refs.stdout.splitlines():
            name = line.strip()
            if name and name != "origin/HEAD" and not name.startswith("origin/exp/"):
                return name
    return None


def _git_head(run_dir: Path, env: Dict[str, str]) -> Optional[str]:
    res = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=run_dir,
        capture_output=True,
        text=True,
        env=env,
    )
    if res.returncode == 0:
        return res.stdout.strip() or None
    return None


class TaskRunner:
    """Manages active runs and background execution threads."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        agent_timeout: float = 1800.0,
        test_timeout: float = 300.0,
        git_timeout: float = 300.0,
        max_concurrent_runs: int = 2,
        max_retained_workspaces: int = 20,
    ):
        self.workspace_root = workspace_root
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.mirrors_root = self.workspace_root / ".mirrors"
        self.empty_hooks_dir = self.workspace_root / ".empty-hooks"
        self.empty_hooks_dir.mkdir(parents=True, exist_ok=True)
        self.agent_timeout = agent_timeout
        self.test_timeout = test_timeout
        self.git_timeout = git_timeout
        self.max_concurrent_runs = max(1, int(max_concurrent_runs))
        self.max_retained_workspaces = max(0, int(max_retained_workspaces))
        self._run_slots = threading.Semaphore(self.max_concurrent_runs)
        self._runs: Dict[str, StatusResponse] = {}
        self._active_procs: Dict[str, subprocess.Popen] = {}
        self._active_threads: Dict[str, threading.Thread] = {}
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()
        self._mirror_locks_guard = threading.Lock()
        self._mirror_locks: Dict[str, threading.Lock] = {}
        self._load_persisted_runs()

    def _run_dir_for(self, run_id: str) -> Path:
        """Return the confined workspace directory for run_id inside self.workspace_root."""
        clean_id = validate_run_id(run_id)
        root = self.workspace_root.expanduser().resolve()
        target = (root / clean_id).resolve()
        if target.parent != root or not target.is_relative_to(root):
            raise ValueError(f"Refusing run_id {run_id!r} outside workspace_root")
        return target

    def _get_mirror_lock(self, key: str) -> threading.Lock:
        with self._mirror_locks_guard:
            lock = self._mirror_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._mirror_locks[key] = lock
            return lock

    def _load_persisted_runs(self):
        """Scan workspace root for previously saved run results."""
        if not self.workspace_root.exists() or _is_link_or_junction(self.workspace_root):
            return
        for run_dir in self.workspace_root.iterdir():
            if _is_link_or_junction(run_dir) or not run_dir.is_dir() or run_dir.name.startswith("."):
                continue
            result_file = run_dir / "result.json"
            if _is_link_or_junction(result_file) or not result_file.is_file():
                continue
            try:
                if result_file.stat().st_nlink > 1:
                    continue
                with open(result_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                st = StatusResponse.model_validate(data)
                if st.status in ("starting", "running"):
                    st.status = "failed"
                    st.error = st.error or "Worker restarted while task was in progress."
                    st.finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    self._runs[st.run_id] = st
                    self._persist_result(st.run_id)
                else:
                    self._runs[st.run_id] = st
            except Exception:
                pass

    def get_run_status(self, run_id: str) -> Optional[StatusResponse]:
        try:
            run_dir = self._run_dir_for(run_id)
        except ValueError:
            return None
        with self._lock:
            st = self._runs.get(run_id)
            if not st:
                return None
            if st.status not in ("starting", "running"):
                return st
            snapshot = st.model_copy()

        agent_log = run_dir / ".hive" / "agent.log" if (run_dir / ".hive" / "agent.log").exists() else (run_dir / "agent.log")
        wrapper_log = run_dir / ".hive" / "wrapper.log" if (run_dir / ".hive" / "wrapper.log").exists() else (run_dir / "wrapper.log")
        snapshot.log_tail = _safe_tail(agent_log, root_dir=run_dir) or _safe_tail(wrapper_log, root_dir=run_dir)
        return snapshot

    def read_log(self, run_id: str, name: str, max_bytes: int) -> Dict[str, object]:
        """Return the last ``max_bytes`` of one of a run's logs (.hive/<name>.log).

        Raises KeyError for an unknown run and FileNotFoundError when that log does not
        exist (yet). Like _safe_tail, it never follows a symlink, junction or hardlink an
        agent or test may have planted to read files outside the run.
        """
        if name not in LOG_FILES:
            raise FileNotFoundError(name)
        run_dir = self._run_dir_for(run_id)
        with self._lock:
            if run_id not in self._runs:
                raise KeyError(run_id)
        path = run_dir / ".hive" / LOG_FILES[name]
        if _is_link_or_junction(path.parent) or _is_link_or_junction(path) or not path.is_file():
            raise FileNotFoundError(name)
        st = path.stat()
        if st.st_nlink > 1 or not path.resolve().is_relative_to(run_dir.resolve()):
            raise FileNotFoundError(name)
        with open(path, "rb") as f:
            start = max(0, st.st_size - max_bytes)
            f.seek(start)
            data = f.read(max_bytes)
        return {
            "run_id": run_id,
            "file": name,
            "size": st.st_size,
            "truncated": start > 0,
            "content": data.decode("utf-8", errors="replace"),
        }

    def list_runs(self) -> List[StatusResponse]:
        with self._lock:
            return list(self._runs.values())

    def get_active_run_ids(self) -> List[str]:
        with self._lock:
            return [
                rid for rid, r in self._runs.items()
                if r.status in ("starting", "running")
            ]

    def _is_cancelled(self, run_id: str) -> bool:
        with self._lock:
            st = self._runs.get(run_id)
            return run_id in self._cancelled or (st is not None and st.status == "cancelled")

    def cancel_run(self, run_id: str) -> bool:
        """Cancel a running task, stop its runner thread, and terminate its process tree."""
        try:
            validate_run_id(run_id)
        except ValueError:
            return False
        with self._lock:
            st = self._runs.get(run_id)
            proc = self._active_procs.pop(run_id, None)
            is_active = proc is not None or (st is not None and st.status in ("starting", "running"))
            if not is_active:
                return False

            self._cancelled.add(run_id)
            if st is not None:
                st.status = "cancelled"
                st.error = "Task was cancelled by master."
                st.finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
                self._persist_result(run_id)

        if proc is not None:
            _kill_proc_tree(proc)
        return True

    def dispatch(self, req: DispatchRequest) -> StatusResponse:
        """Initialize and start a dispatch run in a background thread."""
        self._run_dir_for(req.run_id)
        validate_repo_url(req.repo_url)
        validate_branch_name(req.branch)
        prev_thread: Optional[threading.Thread] = None
        with self._lock:
            if req.run_id in self._runs and self._runs[req.run_id].status in ("starting", "running"):
                return self._runs[req.run_id]
            prev_thread = self._active_threads.get(req.run_id)

        if prev_thread is not None and prev_thread.is_alive():
            prev_thread.join(timeout=5.0)

        with self._lock:
            if req.run_id in self._runs and self._runs[req.run_id].status in ("starting", "running"):
                return self._runs[req.run_id]

            self._cancelled.discard(req.run_id)
            status = StatusResponse(
                run_id=req.run_id,
                status="starting",
                branch=req.branch,
                base_branch=req.base_branch,
                agent_engine=req.agent_engine,
                strategy=req.strategy,
                started_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            )
            self._runs[req.run_id] = status
            self._persist_result(req.run_id)

            thread = threading.Thread(
                target=self._run_task_worker,
                args=(req,),
                daemon=True,
            )
            self._active_threads[req.run_id] = thread
        thread.start()
        return status

    def _persist_result(self, run_id: str):
        st = self._runs.get(run_id)
        if not st:
            return
        try:
            run_dir = self._run_dir_for(run_id)
        except ValueError:
            return
        if _is_link_or_junction(run_dir):
            return
        run_dir.mkdir(parents=True, exist_ok=True)
        res_file = run_dir / "result.json"
        try:
            if _is_link_or_junction(res_file) or (res_file.exists() and res_file.stat().st_nlink > 1):
                self._remove_entry_readonly_safe(res_file)
            fd = os.open(res_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(st.model_dump_json(indent=2))
        except Exception:
            pass

    @staticmethod
    def _remove_entry_readonly_safe(target: Path) -> None:
        """Delete a file or directory tree, clearing read-only attributes (e.g. .git/objects/pack) if needed.
        Never traverses symbolic links or Windows NTFS directory junctions."""
        def _onerror(func, path, _exc_info):
            try:
                os.chmod(path, stat.S_IWRITE)
                func(path)
            except Exception:
                pass

        if _is_link_or_junction(target):
            try:
                target.unlink()
            except OSError:
                try:
                    os.rmdir(target)
                except Exception:
                    pass
            return

        if target.is_dir():
            shutil.rmtree(target, onerror=_onerror)
        else:
            try:
                target.unlink()
            except PermissionError:
                try:
                    os.chmod(target, stat.S_IWRITE)
                    target.unlink()
                except Exception:
                    pass
            except FileNotFoundError:
                pass

    def prune_old_workspaces(self, keep: Optional[int] = None) -> int:
        """Delete git working trees of older completed runs while preserving result.json and .hive/ logs."""
        keep_count = self.max_retained_workspaces if keep is None else max(0, int(keep))
        terminal_statuses = ("succeeded", "failed", "no_changes", "cancelled")
        with self._lock:
            completed = [
                r for r in self._runs.values()
                if r.status in terminal_statuses
            ]
        completed.sort(
            key=lambda r: (r.finished_at or r.started_at or "", r.run_id),
            reverse=True,
        )
        to_prune = completed[keep_count:]
        pruned_runs = 0
        preserved_names = {"result.json", ".hive"}

        for run in to_prune:
            try:
                run_dir = self._run_dir_for(run.run_id)
            except ValueError:
                continue
            if not run_dir.exists() or not run_dir.is_dir():
                continue
            removed_any = False
            try:
                for entry in list(run_dir.iterdir()):
                    if entry.name in preserved_names:
                        continue
                    self._remove_entry_readonly_safe(entry)
                    removed_any = True
            except Exception:
                pass
            if removed_any:
                pruned_runs += 1

        max_in_memory = max(200, keep_count * 10)
        if len(completed) > max_in_memory:
            with self._lock:
                for stale_run in completed[max_in_memory:]:
                    if stale_run.status in terminal_statuses:
                        self._runs.pop(stale_run.run_id, None)
        return pruned_runs

    def _run_git_cmd(
        self,
        run_id: str,
        args: List[str],
        *,
        cwd: Path,
        env: Dict[str, str],
        timeout: Optional[float] = None,
    ) -> subprocess.CompletedProcess:
        """Run a git subprocess with a bounded timeout and cancellation tracking."""
        effective_timeout = self.git_timeout if timeout is None else timeout
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        with self._lock:
            if run_id in self._cancelled:
                _kill_proc_tree(proc)
                return subprocess.CompletedProcess(args, -1, "", "Task was cancelled by master.")
            self._active_procs[run_id] = proc
        try:
            stdout, stderr = proc.communicate(timeout=effective_timeout)
            return subprocess.CompletedProcess(args, proc.returncode, stdout or "", stderr or "")
        except subprocess.TimeoutExpired as exc:
            with self._lock:
                self._active_procs.pop(run_id, None)
            _kill_proc_tree(proc)
            try:
                proc.communicate(timeout=2.0)
            except Exception:
                pass
            raise RuntimeError(f"Git operation timed out after {int(effective_timeout)}s") from exc
        finally:
            with self._lock:
                if self._active_procs.get(run_id) is proc:
                    self._active_procs.pop(run_id, None)

    def _safe_git_prefix(self) -> List[str]:
        """Git invocation prefix that disables hooks, fsmonitor, and ext:: remote helpers."""
        return [
            "git",
            "-c", f"core.hooksPath={self.empty_hooks_dir}",
            "-c", "core.fsmonitor=false",
            "-c", "protocol.ext.allow=never",
        ]

    def _ensure_mirror(self, repo_url: str, env: Dict[str, str]) -> Optional[Path]:
        """Create or update a local bare mirror for repo_url so clones stay local and fast."""
        repo_url = validate_repo_url(repo_url)
        digest = hashlib.sha256(repo_url.encode("utf-8")).hexdigest()[:32]
        mirror_dir = self.mirrors_root / f"{digest}.git"
        lock = self._get_mirror_lock(digest)
        safe_git = self._safe_git_prefix()

        with lock:
            try:
                if _is_link_or_junction(self.mirrors_root):
                    self._remove_entry_readonly_safe(self.mirrors_root)
                self.mirrors_root.mkdir(parents=True, exist_ok=True)
                if _is_link_or_junction(mirror_dir):
                    self._remove_entry_readonly_safe(mirror_dir)
                if not (mirror_dir / "HEAD").exists():
                    if mirror_dir.exists():
                        shutil.rmtree(mirror_dir, ignore_errors=True)
                    res = subprocess.run(
                        [*safe_git, "clone", "--bare", "--", repo_url, str(mirror_dir)],
                        capture_output=True,
                        text=True,
                        env=env,
                        timeout=self.git_timeout,
                    )
                    if res.returncode != 0:
                        shutil.rmtree(mirror_dir, ignore_errors=True)
                        return None
                else:
                    res = subprocess.run(
                        [*safe_git, "--git-dir", str(mirror_dir), "fetch", "--prune", "origin", "+refs/heads/*:refs/heads/*"],
                        capture_output=True,
                        text=True,
                        env=env,
                        timeout=self.git_timeout,
                    )
                    if res.returncode != 0:
                        # Never hand out a stale mirror: the agent would silently work on an
                        # old base. The caller clones straight from repo_url instead, which
                        # is either fresh or fails with git's real error.
                        logger.warning(f"Mirror refresh failed for {repo_url}; cloning directly: {res.stderr.strip()[:200]}")
                        return None
                return mirror_dir
            except Exception:
                return None

    def _prepare_git_workspace(
        self,
        req: DispatchRequest,
        run_dir: Path,
        branch: str,
        env: Dict[str, str],
        log_wrapper,
    ) -> Optional[str]:
        """Clone a fresh repository workspace using the local bare mirror cache and checkout the target branch."""
        repo_url = validate_repo_url(req.repo_url)
        branch = validate_branch_name(branch) or f"exp/{req.run_id[:12]}-worker"
        safe_git = self._safe_git_prefix()
        mirror_dir = self._ensure_mirror(repo_url, env)
        if self._is_cancelled(req.run_id):
            return None

        # Clear any pre-existing workspace files (including any poisoned .git or stale .hive
        # from an interrupted or previous run with the same run_id) before cloning fresh.
        existing_items = list(run_dir.iterdir())
        for item in existing_items:
            self._remove_entry_readonly_safe(item)

        try:
            if mirror_dir is not None:
                clone_res = self._run_git_cmd(
                    req.run_id,
                    [*safe_git, "clone", "--", str(mirror_dir), "."],
                    cwd=run_dir,
                    env=env,
                )
                if clone_res.returncode == 0:
                    subprocess.run(
                        [*safe_git, "remote", "set-url", "origin", "--", repo_url],
                        cwd=run_dir,
                        capture_output=True,
                        text=True,
                        env=env,
                    )
            else:
                clone_res = self._run_git_cmd(
                    req.run_id,
                    [*safe_git, "clone", "--", repo_url, "."],
                    cwd=run_dir,
                    env=env,
                )
            if self._is_cancelled(req.run_id):
                return None
            if clone_res.returncode != 0:
                raise RuntimeError(f"Git clone failed: {(clone_res.stderr or clone_res.stdout).strip()}")
        finally:
            # Rewrite result.json from memory (mode 0600); the clone needed an empty directory.
            self._persist_result(req.run_id)

        _write_git_exclude(run_dir)

        log_wrapper(f"Checking out branch {branch}...")
        if req.base_branch:
            base_ref = f"origin/{req.base_branch}"
            exists = subprocess.run(
                [*safe_git, "show-ref", "--verify", "--quiet", f"refs/remotes/{base_ref}"],
                cwd=run_dir,
                capture_output=True,
                env=env,
            )
            if exists.returncode != 0:
                # Never fall back to the default branch: the master asked for this base.
                raise RuntimeError(f"Base branch '{req.base_branch}' not found on origin")
        else:
            base_ref = _detect_base_ref(run_dir, env)
        with self._lock:
            # Report the base actually used, so the master can verify it.
            self._runs[req.run_id].base_branch = base_ref[len("origin/"):] if base_ref else None
        log_wrapper(f"Starting from {base_ref or 'an empty repository'}")
        if base_ref:
            checkout_res = subprocess.run(
                [*safe_git, "checkout", "-B", branch, base_ref],
                cwd=run_dir,
                capture_output=True,
                text=True,
                env=env,
            )
            if checkout_res.returncode != 0:
                raise RuntimeError(
                    f"Failed to checkout {branch} from {base_ref}: {(checkout_res.stderr or checkout_res.stdout).strip()}"
                )
            subprocess.run([*safe_git, "reset", "--hard", base_ref], cwd=run_dir, capture_output=True, env=env)
            subprocess.run(
                [*safe_git, "clean", "-fd", "-e", ".hive", "-e", "result.json"],
                cwd=run_dir,
                capture_output=True,
                env=env,
            )
        else:
            subprocess.run([*safe_git, "checkout", "-B", branch], cwd=run_dir, capture_output=True, env=env)

        _sanitize_git_dir(run_dir)
        return _git_head(run_dir, env)

    def _run_managed_proc(
        self,
        run_id: str,
        args,
        *,
        cwd: Path,
        env: Dict[str, str],
        shell: bool,
        stdin_file: Optional[Path],
        stdout_path: Path,
        timeout: float,
    ) -> int:
        """Spawn a subprocess tracked in _active_procs with timeout and cancellation support."""
        stdin_fh = open(stdin_file, "rb") if stdin_file is not None else None
        try:
            if _is_link_or_junction(stdout_path.parent):
                self._remove_entry_readonly_safe(stdout_path.parent)
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            if _is_link_or_junction(stdout_path) or stdout_path.exists():
                self._remove_entry_readonly_safe(stdout_path)
            if not stdout_path.parent.resolve().is_relative_to(cwd.resolve()):
                raise RuntimeError(f"Refusing to write log outside workspace: {stdout_path}")
            with open(stdout_path, "w", encoding="utf-8") as out_fh:
                proc = subprocess.Popen(
                    args,
                    cwd=cwd,
                    env=env,
                    shell=shell,
                    stdin=stdin_fh if stdin_fh is not None else subprocess.DEVNULL,
                    stdout=out_fh,
                    stderr=subprocess.STDOUT,
                )
                with self._lock:
                    if run_id in self._cancelled:
                        _kill_proc_tree(proc)
                        return -1
                    self._active_procs[run_id] = proc

                try:
                    return proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    with self._lock:
                        self._active_procs.pop(run_id, None)
                    _kill_proc_tree(proc)
                    raise
                finally:
                    with self._lock:
                        self._active_procs.pop(run_id, None)
        finally:
            if stdin_fh is not None:
                try:
                    stdin_fh.close()
                except Exception:
                    pass

    def _run_task_worker(self, req: DispatchRequest):
        """Main execution flow for a dispatched task."""
        try:
            run_dir = self._run_dir_for(req.run_id)
            run_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.error(f"[{req.run_id}] Failed to create workspace dir: {exc}")
            with self._lock:
                if req.run_id not in self._cancelled:
                    self._runs[req.run_id].status = "failed"
                    self._runs[req.run_id].error = f"Workspace creation error: {exc}"
                    self._runs[req.run_id].finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self._persist_result(req.run_id)
            return

        env = _build_stripped_env(run_dir, include_ai_keys=False)
        agent_env = _build_stripped_env(run_dir, include_ai_keys=True)
        branch = req.branch or f"exp/{req.run_id[:12]}-worker-{_slug(req.strategy)}"

        hive_meta_dir = run_dir / ".hive"
        wrapper_log_path = hive_meta_dir / "wrapper.log"
        agent_log_path = hive_meta_dir / "agent.log"
        test_log_path = hive_meta_dir / "test.log"
        bench_log_path = hive_meta_dir / "bench.log"
        prompt_file = hive_meta_dir / "prompt.txt"

        def log_wrapper(msg: str):
            logger.info(f"[{req.run_id}] {msg}")
            try:
                if _is_link_or_junction(hive_meta_dir):
                    self._remove_entry_readonly_safe(hive_meta_dir)
                hive_meta_dir.mkdir(parents=True, exist_ok=True)
                if _is_link_or_junction(wrapper_log_path) or (wrapper_log_path.exists() and wrapper_log_path.stat().st_nlink > 1):
                    self._remove_entry_readonly_safe(wrapper_log_path)
                if not hive_meta_dir.resolve().is_relative_to(run_dir.resolve()):
                    return
                with open(wrapper_log_path, "a", encoding="utf-8") as f:
                    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    f.write(f"[{ts}] {msg}\n")
            except Exception:
                pass

        slot_acquired = False
        try:
            while not slot_acquired:
                if self._is_cancelled(req.run_id):
                    return
                slot_acquired = self._run_slots.acquire(timeout=0.05)

            if self._is_cancelled(req.run_id):
                return

            # 1. Git Setup / Clone (before creating .hive so git clone sees an empty dir)
            base_head = self._prepare_git_workspace(req, run_dir, branch, env, log_wrapper)
            if self._is_cancelled(req.run_id):
                return
            git_config_path = run_dir / ".git" / "config"
            git_config_snapshot = git_config_path.read_bytes() if git_config_path.exists() else None

            # 2. Setup run directory metadata files
            if _is_link_or_junction(hive_meta_dir):
                self._remove_entry_readonly_safe(hive_meta_dir)
            hive_meta_dir.mkdir(parents=True, exist_ok=True)
            log_wrapper(f"Starting task {req.run_id} (Engine: {req.agent_engine}, Strategy: {req.strategy})")

            # 3. Format Prompt. It must never start with the task text: aider runs a
            # message starting with "/" or "!" as a command (e.g. "!curl ... | sh"),
            # and Claude Code treats a leading "/" as a slash command.
            full_prompt = (
                f"You are pursuing strategy {req.strategy_index} of {req.strategy_count}: {req.strategy}. "
                "Take a distinct approach.\n"
                "Change code only. Do not commit, push, or change git remotes.\n\n"
                f"Task:\n{req.prompt.rstrip()}\n"
            )
            if _is_link_or_junction(prompt_file) or (prompt_file.exists() and prompt_file.stat().st_nlink > 1):
                self._remove_entry_readonly_safe(prompt_file)
            prompt_file.write_text(full_prompt, encoding="utf-8")

            # 4. Build Agent Command (never uses shell=True)
            cmd = build_agent_command(req.agent_engine, prompt_file, full_prompt, model=req.model)
            binary_name = cmd.args[0]
            resolved_bin = _resolve_agent_binary(binary_name, env)
            if not resolved_bin:
                raise RuntimeError(f"Agent executable '{binary_name}' not found on PATH.")

            is_windows = platform.system().lower() == "windows"
            is_batch_shim = is_windows and resolved_bin.lower().endswith((".cmd", ".bat"))
            if is_batch_shim:
                if cmd.prompt_in_args:
                    raise RuntimeError(
                        f"Refusing to pass prompt on command line to batch shim '{resolved_bin}'; "
                        f"install a native '{binary_name}' executable."
                    )
            exec_args = _agent_launch_args(resolved_bin, cmd.args[1:], env)

            log_wrapper(f"Executing agent command: {resolved_bin} (engine={req.agent_engine})")

            with self._lock:
                if req.run_id in self._cancelled:
                    return
                self._runs[req.run_id].status = "running"
                self._runs[req.run_id].branch = branch
                self._persist_result(req.run_id)

            # 5. Execute Agent Process
            try:
                agent_exit = self._run_managed_proc(
                    req.run_id,
                    exec_args,
                    cwd=run_dir,
                    env=agent_env,
                    shell=False,
                    stdin_file=cmd.stdin_file,
                    stdout_path=agent_log_path,
                    timeout=self.agent_timeout,
                )
            except subprocess.TimeoutExpired:
                try:
                    _sanitize_git_dir(run_dir, git_config_snapshot)
                except Exception:
                    pass
                log_wrapper(f"Agent timed out after {int(self.agent_timeout)}s and was terminated.")
                with self._lock:
                    if req.run_id not in self._cancelled:
                        self._runs[req.run_id].status = "failed"
                        self._runs[req.run_id].error = f"Agent timed out after {int(self.agent_timeout)}s"
                        self._runs[req.run_id].log_tail = _safe_tail(agent_log_path, root_dir=run_dir) or _safe_tail(wrapper_log_path, root_dir=run_dir)
                        self._runs[req.run_id].finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
                return

            if self._is_cancelled(req.run_id):
                try:
                    _sanitize_git_dir(run_dir, git_config_snapshot)
                except Exception:
                    pass
                log_wrapper("Run was cancelled; skipping tests, commit, and push.")
                return

            with self._lock:
                self._runs[req.run_id].agent_exit_code = agent_exit
            log_wrapper(f"Agent finished with exit code {agent_exit}")

            # Neutralize any git hooks or .git/config modifications written by the agent
            _sanitize_git_dir(run_dir, git_config_snapshot)

            # 6. Commit the agent's changes before tests and benchmark run, so files they
            # create (.coverage, build output) are never committed, and an agent that
            # changed nothing skips them.
            _write_git_exclude(run_dir)
            safe_git = self._safe_git_prefix()
            status_res = subprocess.run(
                [*safe_git, "status", "--porcelain"],
                cwd=run_dir,
                capture_output=True,
                text=True,
                env=env,
            )
            if status_res.stdout.strip():
                log_wrapper("Modifications detected. Committing changes...")
                subprocess.run([*safe_git, "add", "-A"], cwd=run_dir, capture_output=True, env=env)
                subprocess.run(
                    [*safe_git, "commit", "--no-verify", "-m", f"hive: {req.strategy} (run {req.run_id})"],
                    cwd=run_dir,
                    capture_output=True,
                    text=True,
                    env=env,
                )
            agent_head = _git_head(run_dir, env)
            has_changes = bool(agent_head and agent_head != base_head)
            if not has_changes:
                log_wrapper("No file modifications generated by agent; skipping tests and benchmark.")

            # 7. Run Test Command if specified
            test_exit = None
            if req.test_command and has_changes:
                if self._is_cancelled(req.run_id):
                    return
                log_wrapper(f"Running test command: {req.test_command}")
                try:
                    test_exit = self._run_managed_proc(
                        req.run_id,
                        req.test_command,
                        cwd=run_dir,
                        env=env,
                        shell=True,
                        stdin_file=None,
                        stdout_path=test_log_path,
                        timeout=self.test_timeout,
                    )
                except subprocess.TimeoutExpired:
                    log_wrapper(f"Test command timed out after {int(self.test_timeout)}s")
                    with self._lock:
                        if req.run_id not in self._cancelled:
                            self._runs[req.run_id].status = "failed"
                            self._runs[req.run_id].error = f"Test command timed out after {int(self.test_timeout)}s"
                            self._runs[req.run_id].log_tail = _safe_tail(test_log_path, root_dir=run_dir) or _safe_tail(agent_log_path, root_dir=run_dir)
                            self._runs[req.run_id].finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    return

                if self._is_cancelled(req.run_id):
                    return
                log_wrapper(f"Test command completed with exit code {test_exit}")
                with self._lock:
                    self._runs[req.run_id].test_exit_code = test_exit

            # 8. Run Benchmark Command if specified
            bench_exit = None
            bench_data = None
            if req.bench_command and has_changes:
                if self._is_cancelled(req.run_id):
                    return
                log_wrapper(f"Running bench command: {req.bench_command}")
                try:
                    bench_exit = self._run_managed_proc(
                        req.run_id,
                        req.bench_command,
                        cwd=run_dir,
                        env=env,
                        shell=True,
                        stdin_file=None,
                        stdout_path=bench_log_path,
                        timeout=self.test_timeout,
                    )
                except subprocess.TimeoutExpired:
                    log_wrapper(f"Benchmark command timed out after {int(self.test_timeout)}s")
                    with self._lock:
                        if req.run_id not in self._cancelled:
                            self._runs[req.run_id].status = "failed"
                            self._runs[req.run_id].error = f"Benchmark command timed out after {int(self.test_timeout)}s"
                            self._runs[req.run_id].log_tail = _safe_tail(bench_log_path, root_dir=run_dir) or _safe_tail(agent_log_path, root_dir=run_dir)
                            self._runs[req.run_id].finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    return

                if self._is_cancelled(req.run_id):
                    return
                bench_data = _read_bench_output(bench_log_path, root_dir=run_dir)
                with self._lock:
                    self._runs[req.run_id].bench_exit_code = bench_exit
                    self._runs[req.run_id].benchmark_data = bench_data

            if self._is_cancelled(req.run_id):
                return

            # 9. Push. Tests and benchmark ran agent-written code: restore .git again and push
            # exactly the agent's commit, never anything they committed or left behind.
            _sanitize_git_dir(run_dir, git_config_snapshot)

            pushed = False
            commit_sha: Optional[str] = None
            push_error: Optional[str] = None

            if self._is_cancelled(req.run_id):
                return

            if has_changes:
                log_wrapper(f"Pushing branch {branch} to origin...")
                push_res = self._run_git_cmd(
                    req.run_id,
                    [*safe_git, "push", "--no-verify", "--force", "origin", "--", f"{agent_head}:refs/heads/{branch}"],
                    cwd=run_dir,
                    env=env,
                )
                if self._is_cancelled(req.run_id):
                    return
                if push_res.returncode == 0:
                    pushed = True
                    commit_sha = agent_head
                else:
                    push_error = (push_res.stderr or push_res.stdout).strip() or f"exit {push_res.returncode}"
                    log_wrapper(f"Error: Git push failed: {push_error}")

            # 10. Determine Final Status
            if test_exit is not None and test_exit != 0:
                tail = _safe_tail(test_log_path, root_dir=run_dir) or _safe_tail(agent_log_path, root_dir=run_dir) or _safe_tail(wrapper_log_path, root_dir=run_dir)
            else:
                tail = _safe_tail(agent_log_path, root_dir=run_dir) or _safe_tail(wrapper_log_path, root_dir=run_dir)

            if agent_exit != 0:
                final_status = "failed"
                final_error: Optional[str] = f"Agent process exited with code {agent_exit}"
            elif test_exit is not None and test_exit != 0:
                final_status = "failed"
                final_error = f"Test suite failed with exit code {test_exit}"
            elif not has_changes:
                final_status = "no_changes"
                final_error = "Agent changed no files; tests and benchmark were not run."
            elif not pushed:
                final_status = "failed"
                final_error = f"Git push failed: {push_error}"
            else:
                final_status = "succeeded"
                final_error = None

            with self._lock:
                if req.run_id in self._cancelled:
                    return
                st = self._runs[req.run_id]
                st.status = final_status
                st.pushed = pushed
                st.commit = commit_sha
                st.error = final_error
                st.log_tail = tail
                st.finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

            log_wrapper(f"Run {req.run_id} marked as {final_status}")

        except Exception as exc:
            log_wrapper(f"Fatal task error: {str(exc)}")
            with self._lock:
                if req.run_id not in self._cancelled:
                    st = self._runs[req.run_id]
                    st.status = "failed"
                    st.error = str(exc)
                    st.log_tail = _safe_tail(agent_log_path, root_dir=run_dir) or _safe_tail(wrapper_log_path, root_dir=run_dir)
                    st.finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        finally:
            with self._lock:
                if self._active_threads.get(req.run_id) is threading.current_thread():
                    self._active_threads.pop(req.run_id, None)
            if slot_acquired:
                self._run_slots.release()
            self._persist_result(req.run_id)
            try:
                self.prune_old_workspaces()
            except Exception:
                pass
