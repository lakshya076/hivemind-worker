# Changes: `hivemind-worker` (branch `worker-security`)

Every change on this branch, one point each. Master-side changes are in the HiveMind repo's `CHANGES.md` (branch `security`).

---

## Security round 1: critical issues

**Fixes:**
- Critical #1: the daemon accepted every request when no fleet key was set, while listening on all interfaces.
- Critical #2: any web page could drive the daemon through a browser (CORS `*`, no DNS-rebinding protection).

### Authentication is mandatory (A1)
1. `hivemind-worker start` generates a fleet key when none is configured. Format: `hive-key-<40 hex>`, the same as the master's `hive keygen`. (`cli.py`, `config.py: generate_fleet_key`)
2. Only the generated key is written to the config file. One-off `--port/--host/--workspace` overrides are not saved without `--save`. (`cli.py: _persist_key`)
3. On a terminal the generated key is printed once, with instructions for the master.
4. When output is redirected (systemd, NSSM, `Start-Process`), the key is **not** written to the log. The log instead says to run `hivemind-worker key show`.
5. `create_app()` raises `ValueError` if there is no key, so the server can't be built unauthenticated by mistake. (`server.py`)
6. `verify_auth` fails closed (HTTP 503) if the key disappears at runtime. It used to return early and allow the request. (`server.py`)
7. New command `hivemind-worker key set`: stores a key via a hidden prompt (or `--key`), keeping it out of shell history and process lists.
8. New command `hivemind-worker key show`: prints the configured key, exits 1 if none.
9. New command `hivemind-worker key rotate`: replaces the key with a new random one.
10. `key set` / `key rotate` change only `fleet_key` and keep every other saved setting.

### Keyless mode is loopback-only (A3)
11. New flag `start --insecure-no-auth` for local testing.
12. In that mode the bind address is forced to `127.0.0.1`, whatever `--host` says.
13. In that mode the network guard also rejects every non-loopback client (`LOOPBACK_NETWORKS`), as a second safeguard.

### Source-network allowlist (B1)
14. New config field `allowed_networks` (list of CIDRs). (`config.py`)
15. The default allows loopback, RFC 1918 (`10/8`, `172.16/12`, `192.168/16`), link-local (`169.254/16`, `fe80::/10`), IPv6 ULA (`fc00::/7`), and Tailscale (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`).
16. Requests from any other address get **HTTP 403 before authentication**. (`security.py: client_ip_allowed`, `server.py: network_guard_middleware`)
17. IPv4-mapped IPv6 addresses (`::ffff:a.b.c.d`) and IPv6 zone ids (`fe80::1%eth0`) are handled correctly.
18. An invalid CIDR in `allowed_networks` fails config validation.
19. The bind address stays `0.0.0.0`, so DHCP changes or Tailscale starting late don't stop the worker.

### Browser requests refused (C1, C3)
20. Removed `CORSMiddleware`, which allowed `allow_origins=["*"]` with credentials, methods and headers. (`server.py`)
21. Requests with an `Origin` header get **HTTP 403**. (`security.py: is_browser_request`)
22. Requests with `Sec-Fetch-Site` other than `none` get **HTTP 403**.
23. Typing the worker URL into an address bar (`Sec-Fetch-Site: none`, no `Origin`) still works.
24. The master (`httpx`) and `curl` send neither header, so they are unaffected.

### Host header / DNS-rebinding protection (C4)
25. IP literals in `Host` (IPv4, or IPv6 in brackets, with or without a port) are always accepted. (`security.py: HostAllowlist`)
26. Accepted names: `localhost`, the machine's hostname, its FQDN, its short name, and `<hostname>.local`.
27. The machine's Tailscale MagicDNS name (full and short) is also accepted, read via `tailscale status --json --peers=false`.
28. The MagicDNS lookup is lazy and refreshed at most once every 60 s, so a worker that starts before Tailscale picks the name up later without a restart.
29. The lookup runs in a threadpool so a slow `tailscale` call can't block the event loop.
30. New config field `allowed_hosts` for any extra DNS names.
31. Any other `Host` gets **HTTP 400** "Unrecognised Host header".

### Config loading
32. If `worker.json` exists but can't be parsed or validated, `WorkerConfig.load()` raises `ConfigError` and the CLI exits 1. It used to fall back silently to a keyless default. (`config.py`)
33. The config file is written owner-only (`0600`) on Linux/macOS, including when it already existed with looser permissions. (`config.py: save`)
34. New global option `--config <path>`, also settable through the env var `HIVEMIND_WORKER_CONFIG`. (`cli.py`, `config.py`)
35. `HIVEMIND_FLEET_KEY` now fills in the key when the config file exists but has no key.
36. `start --port/--host` default to "not given" rather than `7422`/`0.0.0.0`, fixing the old `if port != 7422 or not config.port` override logic.
37. `hivemind-worker info` shows the allowed networks, and shows "generated on first start" when no key is set.

### Bug fix found by the new tests
38. On Windows, redirected output (the documented `Start-Process -RedirectStandardOutput` and NSSM `AppStdout` setups) crashed the daemon with `UnicodeEncodeError` on the 🤖 banner emoji before it started listening. Redirected stdout/stderr are now reconfigured to UTF-8. (`cli.py: main`)

### Documentation (`README.md`)
39. The Quickstart uses `hivemind-worker key set` instead of `start --key <KEY>`.
40. New sections: defaults, key management commands, `--insecure-no-auth`, config file fields, and connecting by name.
41. The Security Features list describes mandatory auth, the network allowlist, browser blocking and DNS-rebinding protection.
42. **NSSM instructions fixed.** They installed the service as LocalSystem, which didn't find the user's config, so it ran keyless with SYSTEM privileges. The service now uses `ObjectName` (run as the user) and `--config`, with a warning box explaining why.
43. The systemd and Task Scheduler steps use `key set`.
44. Fixed the systemd `ExecStart` path typo (`/home/<username/...`).

### Repository
45. `.gitignore`: added `.venv/` and `tests/` (tests are kept local).

### Compatibility notes
46. Existing `worker.json` files load unchanged; new fields get defaults.
47. Workers that ran keyless now generate a key on next start. The master needs that key, or the worker can be given the master's key via `key set`.
48. Reaching a worker through a DNS name other than its hostname or MagicDNS name requires adding that name to `allowed_hosts`.
49. Reaching a worker from a public IP requires adding that range to `allowed_networks`. Better: connect over Tailscale.

### Testing (local, not committed)
50. 83 passed, 1 skipped (POSIX permission bits, skipped on Windows).
51. ASGI-level tests send real HTTP requests with spoofed client IPs and headers.
52. A real uvicorn server on a socket is driven with `httpx`, the master's client library.
53. The real `hivemind-worker` executable is run as a background process with redirected output and probed over loopback, LAN IP, Tailscale IP and MagicDNS name.
54. Mutation-checked: re-introducing each original bug (fail-open auth, keyless `verify_auth`, no browser check, any Host, any source IP, corrupt config ignored) makes the suite fail.

---

## Round 3: high-impact accuracy and performance upgrades

### Prompt delivery and agent execution (A1, A2, A3, security #7)
55. Replaced `build_agent_args` with `build_agent_command` returning an `AgentCommand` dataclass (`agent.py`).
56. Aider receives the prompt via `--message-file <prompt_file>` instead of `--message <prompt>` on the command line, preserving multiline prompts and avoiding command-line length limits.
57. Claude Code receives the prompt over `stdin` (`stdin_file`) instead of `-p <prompt>` on the command line, so multiline prompts and special characters arrive intact on Windows `.cmd` installs.
58. Claude Code is launched with `--permission-mode acceptEdits` so headless runs can edit files without allowing arbitrary shell commands.
59. `hive dispatch --model` is now passed through to Claude Code (`--model <model>`).
60. New `validate_model()` validates model names against a strict allowlist regex (`^[A-Za-z0-9._:/@+\-]{1,200}$`) so a model string can never inject flags or shell metacharacters.
61. The runner never launches agents with `shell=True` (`runner.py`).
62. On Windows, `.cmd` / `.bat` shims are invoked via `[COMSPEC, "/d", "/s", "/c", resolved_bin, ...]` with `shell=False` when the prompt is in a file/stdin, and refused if `prompt_in_args` is set (`agy`).

### Accurate run status and git hygiene (A4, A5, A6, A7, A8, A9, A14)
63. `StatusResponse` (`models.py`) adds `"no_changes"` to `status`, plus `pushed: Optional[bool]` and `commit: Optional[str]`.
64. When an agent finishes with exit code 0 and passing tests but makes no file changes, the run is marked `"no_changes"` with `pushed = False` instead of `"succeeded"`.
65. When `git push` fails, the run is marked `"failed"` with `pushed = False` and the git error message instead of logging a warning and reporting `"succeeded"`.
66. When `git push` succeeds, `pushed = True` and `commit` stores the pushed commit SHA.
67. `cancel_run()` records the run ID in `_cancelled`, terminates the entire agent/test process tree via `_kill_proc_tree()`, and stops the background worker thread before tests, commit, or push can run or overwrite `"cancelled"`.
68. When an agent or test command exceeds its timeout, `_kill_proc_tree()` terminates the entire subprocess tree so timed-out agents do not keep running in the background.
69. Worker ignore rules (`.hive/`, `.agy/`, `result.json`, `__pycache__/`, `*.pyc`, `*.pyo`) are written to `.git/info/exclude` instead of `.gitignore`, so experiment branches never dirty the repository's `.gitignore`.
70. `_detect_base_ref()` checks `origin/HEAD` first, then `origin/main`, `origin/master`, and other remote branches, and resets/cleans reused workspaces before checkout.
71. On startup, `_load_persisted_runs()` marks any leftover `"starting"` or `"running"` runs from before a worker restart as `"failed"`.
72. When `test_command` fails, `log_tail` reads from `test.log` so test failure output is visible in `hive status`.

### Performance (P2, P5)
73. `TaskRunner._ensure_mirror()` caches a local bare git repository per `repo_url` under `<workspace_root>/.mirrors/` and clones new run workspaces from the local cache before pointing `origin` at `repo_url`.
74. `_safe_tail()` seeks to the end of the log file (last 64 KB) instead of reading the entire file into memory on every status poll.

### Testing (local, not committed)
75. Added 11 tests in `tests/test_runner_accuracy_perf.py` covering multiline prompt delivery with special characters over `.cmd` shims, `--permission-mode acceptEdits`, `--model` forwarding and validation, `"no_changes"` status, failed `git push` handling, process-tree termination on cancel and timeout, `.git/info/exclude` preservation of `.gitignore`, test failure log surfacing, stale run recovery on restart, cached git mirrors, and `_safe_tail`.
76. Total worker suite: 92 passed, 3 skipped.

---

## Round 4: remaining high-severity security fixes

### S1: Path traversal via `run_id` and `workspace_dir` confinement
77. New `validate_run_id()` (`models.py`) enforces `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` and forbids `..`, rejecting traversal strings, slashes, drive letters, and leading dashes.
78. `DispatchRequest` validates `run_id` on ingress so `POST /dispatch` rejects traversal IDs with HTTP 422 before reaching the runner.
79. New `TaskRunner._run_dir_for()` (`runner.py`) resolves `<workspace_root>/<run_id>` and verifies `target.parent == workspace_root` and `target.is_relative_to(workspace_root)`.
80. Client-supplied `workspace_dir` in `DispatchRequest` is ignored by the worker (`_run_task_worker`, `get_run_status`, `_persist_result` all use `_run_dir_for`), so a caller cannot direct the worker to unlink or overwrite files outside `workspace_root`.
81. Added 14 tests for S1 in `tests/test_runner_accuracy_perf.py`.

### S2: Git option and remote-helper injection via `repo_url` and `branch`
82. New `validate_repo_url()` (`models.py`) rejects `repo_url` values starting with `-` (e.g. `--upload-pack=...`), using git remote helper protocols (`ext::`, `fd::`), or containing whitespace/control characters.
83. New `validate_branch_name()` (`models.py`) enforces `^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$` and rejects leading dashes, `..`, `//`, `@{`, and `.lock` suffixes.
84. `DispatchRequest` validates `repo_url` and `branch` on ingress (HTTP 422 on invalid values), and `TaskRunner` re-validates both before executing git commands.
85. `git clone`, `git remote set-url`, and `git push` invocations in `runner.py` pass `--` before positional URL/branch arguments.
86. Added 16 tests for S2 in `tests/test_runner_accuracy_perf.py` (total worker suite: 122 passed, 3 skipped).

---

## Round 5: medium-severity accuracy and performance improvements

### P4: Bounded job concurrency queue
87. `WorkerConfig` (`config.py`) adds `max_concurrent_runs` (default `2`, bounded `1..64`) and `create_app()` (`server.py`) passes it into `TaskRunner`.
88. `TaskRunner` (`runner.py`) enforces `max_concurrent_runs` via a `threading.Semaphore` so excess dispatched runs wait in `"starting"` instead of spawning unbounded concurrent git clones, agent processes, and test suites.
89. Queued runs waiting for an execution slot poll `_is_cancelled()` every 50 ms so `DELETE /cancel/<run_id>` immediately cancels queued tasks before they ever start.
90. Added test `test_p4_max_concurrent_runs_queues_excess_jobs_and_allows_cancelling_queued_job` in `tests/test_runner_accuracy_perf.py`.

### P3: Automatic worker workspace retention and git checkout pruning
91. `WorkerConfig` (`config.py`) adds `max_retained_workspaces` (default `20`, `>= 0`) and `create_app()` (`server.py`) passes it into `TaskRunner`.
92. New `TaskRunner.prune_old_workspaces()` (`runner.py`) sorts completed runs (`succeeded`, `failed`, `no_changes`, `cancelled`) newest-first and deletes the checked-out repository files and `.git` directory from runs older than the newest `max_retained_workspaces` while preserving `result.json` and `.hive/` logs so `/runs` and `/status/<run_id>` retain full history.
93. `_remove_entry_readonly_safe()` clears read-only attributes before unlinking so Windows `.git/objects/pack/*` files are removed cleanly, and `_run_task_worker()` triggers `prune_old_workspaces()` automatically on task completion.
94. Added test `test_p3_prune_old_workspaces_removes_git_checkouts_but_preserves_results_and_logs` in `tests/test_runner_accuracy_perf.py` (total worker suite: 124 passed, 3 skipped).

---

## Round 6: deep security audit fixes (High severity)

### V1: Post-agent git hook / `.git/config` neutralization and UNC share rejection
95. New `_sanitize_git_dir()` (`runner.py`) clears `.git/hooks/` and restores `.git/config` from a pre-agent snapshot immediately after the agent exits and before post-agent git commands run, preventing a prompt-injected agent (under `--permission-mode acceptEdits`) from executing arbitrary commands via `pre-commit`/`pre-push` hooks or `core.fsmonitor`/`core.hooksPath`/`core.sshCommand`.
96. Post-agent `git status`, `git add`, `git commit`, and `git push` invocations pass `-c core.hooksPath=<empty_hooks_dir>`, `-c core.fsmonitor=false`, and `--no-verify`.
97. `validate_repo_url()` (`models.py`) rejects Windows/POSIX UNC network share paths (`\\server\share`, `//server/share`, `file:////...`) to prevent SMB NTLM hash coercion.
98. Added 5 tests for V1 in `tests/test_runner_accuracy_perf.py`.

### V2: Isolate AI provider API keys from `test_command`, `bench_command`, and `git`
99. `_build_stripped_env()` (`runner.py`) adds an `include_ai_keys` parameter (`True` by default for backward compatibility).
100. `TaskRunner._run_task_worker()` passes AI provider API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `GROQ_API_KEY`, `DEEPSEEK_API_KEY`, `MISTRAL_API_KEY`) only to the AI agent subprocess and omits them from `git`, `test_command`, and `bench_command` subprocesses so agent-modified tests or benchmarks cannot exfiltrate API keys into logs or commits.
101. Added `test_v2_ai_api_keys_are_passed_to_agent_but_never_leaked_to_test_or_bench_commands` in `tests/test_runner_accuracy_perf.py`.

### V3: Symlink, hardlink, and Windows NTFS junction confinement
102. New `_is_link_or_junction()` (`runner.py`) detects both symbolic links and Windows NTFS directory junctions (`Path.is_junction()`).
103. `_safe_tail()` (`runner.py`) refuses symbolic links, NTFS junctions, hardlinked files (`st_nlink > 1`), and paths resolving outside `root_dir`, preventing a malicious test or agent from linking `.hive/test.log` or `.hive/agent.log` to host secrets such as `~/.hivemind/worker.json` or `~/.env`.
104. `TaskRunner._remove_entry_readonly_safe()` unlinks symbolic links and removes NTFS directory junctions (`os.rmdir`) without calling `shutil.rmtree()`, preventing recursive deletion of junction targets outside `workspace_root`.
105. `TaskRunner._prepare_git_workspace()` and `_run_managed_proc()` remove pre-existing links/junctions before reading `result.json` or opening log files for writing.
106. Added 2 tests for V3 in `tests/test_runner_accuracy_perf.py`.

---

## Round 7: deep security audit fixes (Medium severity)

### V5: Managed `bench_command` execution with cancellation and process-tree timeout cleanup
107. `TaskRunner._run_task_worker()` (`runner.py`) executes `bench_command` through `_run_managed_proc()` (logging output to `.hive/bench.log`) instead of raw `subprocess.run()`, so `DELETE /cancel/<run_id>` can terminate an active benchmark and a timed-out benchmark kills its entire child process tree via `_kill_proc_tree()`.
108. Benchmark timeouts mark the run `"failed"` with a clear timeout message and surface the tail of `.hive/bench.log` in `log_tail`.
109. Added `test_v5_bench_command_timeout_kills_process_tree_and_marks_failed` in `tests/test_runner_accuracy_perf.py`.

### V8: Bounded payload fields in `DispatchRequest`
110. `DispatchRequest` (`models.py`) enforces length and numeric bounds on `repo_url` (`<= 2048`), `prompt` (`1..200000`), `strategy` (`1..256`), `strategy_index` (`1..1000`), `strategy_count` (`1..1000`), `agent_engine` (`1..64`), `model` (`<= 200`), `test_command` (`<= 4096`), `bench_command` (`<= 4096`), and `workspace_dir` (`<= 1024`), rejecting oversized payloads with HTTP 422 before writing to disk.
111. Added `test_v8_dispatch_request_enforces_payload_field_bounds` in `tests/test_runner_accuracy_perf.py` (total worker suite: 135 passed, 2 skipped).
