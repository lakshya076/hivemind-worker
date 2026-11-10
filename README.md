# HiveMind Worker Daemon (`hivemind-worker`)

A lightweight, secure, cross-platform HTTP execution daemon for [**HiveMind**](https://github.com/lakshya076/HiveMind) distributed AI coding fleet.

Runs on Linux, macOS, and Windows. Eliminates raw SSH dependency and provides isolated agent workspace execution.

---

## 🚀 Quickstart

### 1. Installation

`hivemind-worker` is not published to PyPI. Install it from a clone of this repository:

```bash
git clone https://github.com/lakshya076/hivemind-worker.git
cd hivemind-worker
uv tool install -e .    # or, inside a virtualenv: pip install -e .
```

The editable install picks up code changes on the next `hivemind-worker start`. Re-run `uv tool install -e . --force` only after dependencies in `pyproject.toml` change.

### 2. Run Worker Daemon

```bash
# Store the fleet key from the master's `hive keygen` (prompted, hidden input)
hivemind-worker key set

# Start the daemon
hivemind-worker start
```

If no key is configured, `start` generates one, saves it to the worker config, and tells you how to install it on the master. A fleet key is **always** required; the daemon never runs unauthenticated on the network.

By default:
- Listens on `0.0.0.0:7422` (reachable via Tailscale `100.x.x.x`, MagicDNS, or the local network)
- Accepts connections only from private networks: loopback, LAN (`10/8`, `172.16/12`, `192.168/16`, IPv6 ULA/link-local) and Tailscale (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`). Public internet addresses get `403`.
- Workspace root defaults to `~/hive-workspace` (or `C:\Users\<user>\hive-workspace` on Windows)
- Authenticates all requests with `Authorization: Bearer <fleet-key>`

### Key management

| Command | Purpose |
|---|---|
| `hivemind-worker key set` | Store a fleet key (hidden prompt; keeps it out of shell history and process lists) |
| `hivemind-worker key show` | Print the configured key |
| `hivemind-worker key rotate` | Replace the key with a new random one |

### Local testing without a key

```bash
hivemind-worker start --insecure-no-auth
```
Binds to `127.0.0.1` and serves loopback clients only, whatever `--host` says.

### Configuration file

Settings live in `~/.hivemind/worker.json` (owner-only permissions on Linux/macOS). Point at another file with `--config <path>` or `HIVEMIND_WORKER_CONFIG`. If the file exists but cannot be read, the daemon refuses to start rather than falling back to defaults.

| Field | Default | Meaning |
|---|---|---|
| `fleet_key` | generated on first start | Shared key the master must present |
| `host` / `port` | `0.0.0.0` / `7422` | Bind address |
| `allowed_networks` | private + Tailscale ranges | Source CIDRs allowed to connect. E.g. `["100.64.0.0/10"]` for a Tailscale-only worker |
| `allowed_hosts` | `[]` | Extra DNS names the master may use to reach this worker (see below) |
| `max_concurrent_runs` | `2` | Agent tasks allowed to execute at the same time; extra dispatches wait for a slot |
| `max_retained_workspaces` | `20` | Completed run working trees kept on disk (`result.json` and `.hive/` logs are always kept) |
| `agent_timeout` | `1800` | Seconds an agent session may run before its process tree is killed |
| `test_timeout` | `300` | Seconds each test or benchmark command may run before it is killed |
| `git_timeout` | `300` | Seconds each git clone, fetch or push may run before it is killed |

---

## 📡 API Endpoints

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `GET` | `/` | none | Identifies the daemon (`name`, `version`). `hive discover` uses it to find workers without sending the key |
| `GET` | `/health` | key | Hardware and tool capability probe (<0.1s response) |
| `POST` | `/dispatch` | key | Dispatches an AI agent run in an isolated workspace |
| `GET` | `/status/{run_id}` | key | Polls live status, git branch, test results, and log tail |
| `GET` | `/logs/{run_id}?file=agent\|test\|bench\|wrapper&max_bytes=N` | key | The end of one of the run's logs: `{run_id, file, size, truncated, content}`. `max_bytes` defaults to 1 MiB, at most 5 MiB. 404 if the run or that log doesn't exist |
| `DELETE` | `/cancel/{run_id}` | key | Aborts a queued or running task and terminates its process tree |
| `GET` | `/runs` | key | Lists all historical and active runs |

`/health` lists optional features in `features` (currently `base_branch` and `logs`). Unknown dispatch fields are ignored, so an older worker given a `base_branch` would quietly start from the default branch; the master checks this list first and refuses instead.

`/dispatch` rejects invalid payloads with HTTP 422 before anything touches disk: a `run_id` that is not 1–128 characters of `[A-Za-z0-9._-]` (or contains `..`), a `repo_url` or `branch` that could be read as a git option or remote helper, a `branch` outside `exp/` (the worker force-pushes it, so it must never be `main` or another real branch), an `agent_engine` other than `claude-code`, `claude`, `agy` or `aider`, a model name with characters outside `[A-Za-z0-9._:/@+-]` or not starting with a letter or digit, and oversized fields (prompt over 200,000 characters, `repo_url` over 2,048, test or bench command over 4,096).

---

## ⚙️ How a Run Works

1. **Queue.** The run starts as `starting` and waits for one of `max_concurrent_runs` slots. A queued run can be cancelled before it starts.
2. **Workspace.** The run gets `<workspace_root>/<run_id>/`. Every run starts from a fresh clone: anything already in that directory (for example a `.git` left by an earlier run with the same ID) is deleted first. Each `repo_url` is cached as a bare mirror in `<workspace_root>/.mirrors/`, so later clones are local and fast; `origin` still points at `repo_url`. If refreshing the mirror fails, the worker clones straight from `repo_url`, so it never works on a stale copy. If that fails too, the run fails with git's error before the agent starts. Clones, fetches and the final push are killed after `git_timeout` seconds, and cancelling a run also stops a clone or push in progress.
3. **Branch.** The worker checks out the branch the master sends (`exp/<run_id>/<worker>-<strategy>`). It starts from `base_branch` if the master sends one (any valid branch on origin; the run fails if it doesn't exist, never falling back). Otherwise it starts from the remote default branch (`origin/HEAD`, then `origin/main`, then `origin/master`). `/status` reports the base actually used in `base_branch`. If the master sends no branch, the worker uses `exp/<first 12 characters of run_id>-worker-<strategy>`. Worker and agent files (`.hive/`, `.agy/`, `.aider*`, `result.json`, `__pycache__/`) are listed in `.git/info/exclude`, so the repository's `.gitignore` is never changed.
4. **Agent.** `.hive/prompt.txt` gets a fixed header naming the strategy, then `Task:` and the master's prompt. The header means the file never starts with the task text, which matters because aider runs a message starting with `/` or `!` as a command, and Claude Code treats a leading `/` as a slash command.
   - Aider reads it with `--message-file`. It runs with `--yes-always`, which accepts every routine prompt. So the worker also turns off the prompts it must never accept: self-upgrade via pip (`--no-check-update`), fetching URLs found in the prompt (`--no-detect-urls`), editing `.gitignore` (`--no-gitignore`), and the analytics opt-in (`--no-analytics`).
   - Claude Code reads it from stdin and runs with `--permission-mode acceptEdits`, so it can edit files but not run shell commands.
   - `agy` takes the prompt as an argument, so it must be a native executable. The worker refuses to pass a prompt to a `.cmd`/`.bat` shim.
   - On Windows, `.cmd` shims (such as npm's `claude.cmd`) are started through `cmd.exe` with the whole command quoted, so install paths with spaces (`C:\Users\John Smith\...`) work.
   - `/health` and the runner find agent CLIs the same way: `PATH` first, then the usual install folders (`~/.local/bin`, `~/.cargo/bin`, npm's global folder on Windows). The worker never reports an engine it can't start, even when a service runs with a minimal `PATH`.
   - No agent is ever started through a shell. The agent is killed with its whole process tree after `agent_timeout` seconds.
5. **Commit.** As soon as the agent exits, its changes are committed as `hive: <strategy> (run <run_id>)`.
6. **Tests and benchmark.** `test_command` and `bench_command` run in the workspace and are killed after `test_timeout` seconds. Benchmark output is parsed as JSON; output that is not JSON is kept as `{"raw_output": ...}`. Output over 64 KiB is not read whole: only its last lines are kept, marked `[benchmark output truncated: <size> bytes]`.
7. **Push.** Exactly the agent's commit is force-pushed to the branch. Files the tests or benchmark create (`.coverage`, build output) and anything they commit are never pushed.
8. **Result.** The final status is written to `<run_id>/result.json`, readable only by the worker's user on Linux/macOS. Logs stay in `<run_id>/.hive/` (`wrapper.log`, `agent.log`, `test.log`, `bench.log`).

| Status | Meaning |
|---|---|
| `starting` | Queued or preparing the workspace |
| `running` | Agent, tests or benchmark in progress |
| `succeeded` | Agent exited 0, tests passed (or none given), and the branch was pushed (`pushed: true`, `commit` set) |
| `no_changes` | Agent and tests succeeded but no files changed; nothing was pushed |
| `failed` | Agent exited non-zero, tests failed, a timeout hit, `git push` failed, or the worker restarted mid-run. `error` says which |
| `cancelled` | Cancelled through `DELETE /cancel/{run_id}`; tests, commit and push are skipped |

After each run, working trees beyond the newest `max_retained_workspaces` completed runs are deleted. `result.json` and `.hive/` logs are kept, so `/runs` and `/status` keep the history. Only the newest `max(200, 10 × max_retained_workspaces)` finished runs stay in memory; older ones drop out of `/runs` and `/status` until the next restart reloads them from disk.

---

## 🛡️ Security Features

**Network and authentication**
- **Mandatory Fleet API Key Authentication**: Rejects unauthenticated requests with HTTP 401. The daemon refuses to run without a key (except `--insecure-no-auth`, which is loopback-only). If the key disappears at runtime, requests get 503 instead of being let through.
- **Private-Network Allowlist**: Requests from addresses outside `allowed_networks` are rejected with 403 before authentication, so a worker accidentally exposed to the internet (cloud VM, port forward) is not reachable.
- **Browser Request Blocking**: No CORS is served, and any request carrying `Origin` or a cross-site `Sec-Fetch-Site` header is rejected with 403. Web pages cannot drive the worker.
- **DNS-Rebinding Protection**: The `Host` header must be an IP address, `localhost`, this machine's hostname, its Tailscale MagicDNS name, or an entry in `allowed_hosts`; anything else gets 400.
- **Fail-closed config**: If `worker.json` exists but cannot be read or validated, the daemon refuses to start instead of falling back to a keyless default.

**Runs**
- **Workspace confinement**: Every run lives in `<workspace_root>/<run_id>/`; the `run_id` is validated so it cannot escape. The worker never follows symlinks, hardlinks or NTFS junctions when reading logs and benchmark output, writing logs, `prompt.txt` and `result.json`, or deleting old workspaces, so a run cannot make it read, overwrite or delete files outside the workspace.
- **Git injection protection**: `repo_url` values that start with `-`, use remote helpers (`ext::`, `fd::`), are UNC network shares (`\\server\share`, `//server/share`), or contain whitespace are rejected. Git commands pass `--` before URLs and branch names.
- **Agent cannot hijack git**: After the agent exits, the worker deletes anything in `.git/hooks/` and restores `.git/config` from a copy taken before the agent ran. Every git command (clone, checkout, commit, push) runs with an empty hooks directory, `core.fsmonitor=false` and `protocol.ext.allow=never`, and push also uses `--no-verify`. If the agent removes `.git` or replaces it with a link, the run fails instead of committing.
- **Stripped environment**: Subprocesses get only `PATH`, OS essentials, `HOME` and a git identity, with no other variables (no `SSH_AUTH_SOCK`, cloud credentials, and so on). AI provider keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `GROQ_API_KEY`, `DEEPSEEK_API_KEY`, `MISTRAL_API_KEY`) are read from the environment or `~/.env` and put in the environment of the agent only, not of tests, benchmarks or git. This keeps them out of environment dumps and test logs. **It is not isolation**; see the trust model below.
- **Exp branches only**: The worker only pushes branches under `exp/`, so a bad dispatch can never force-push `main`.

### ⚠️ Trust model: tests run agent-written code as the worker's user

`test_command` and `bench_command` run the repository's code *after the agent has edited it*. Anything that influences the agent can therefore run code on the worker: a prompt, or text in the repository the agent reads (prompt injection). That code runs as the worker's OS user, with that user's full file access. It can read:

* the git push key (`~/.ssh/...`), and with it push to every repository that key can reach;
* the fleet key in `~/.hivemind/worker.json`. It is shared by the whole fleet, so it lets anyone drive **every** worker;
* AI keys in `~/.env`, and anything else in that user's home.

The stripped environment does not change this, because the code can simply read the files. Until container sandboxing lands (roadmap Phase 4), limit what a compromised run can reach:

1. **Run the worker as a dedicated OS user** (e.g. `hivemind`), never your personal account. Give it its own home, its own `~/.env` with only the AI key it needs, and no personal files.
2. **Give that user a deploy key** for only the repositories the fleet works on (GitHub: *Settings → Deploy keys → Allow write access*), not a key on your personal account.
3. **Only dispatch repositories and prompts you trust**, and remember that the agent reads the whole repository.

```bash
# Debian/Ubuntu example
sudo useradd --create-home --shell /bin/bash hivemind
sudo -iu hivemind ssh-keygen -t ed25519 -N "" -f ~hivemind/.ssh/id_ed25519   # add the .pub as a deploy key
# install hivemind-worker and the agent CLIs as `hivemind`, then use User=hivemind in the systemd unit
```

Passing the key with `hivemind-worker start --key <KEY>` still works but is deprecated and prints a warning, because the key stays visible in the process list while the worker runs. Use `hivemind-worker key set`.

---

## 🔄 Running as a Background Service

### Linux (systemd service)

1. Save configuration once on the worker (as the same user the service will run as):
   ```bash
   hivemind-worker key set
   ```

2. Create `/etc/systemd/system/hivemind-worker.service`:
   ```ini
   [Unit]
   Description=HiveMind Worker Daemon
   After=network.target tailscaled.service

   [Service]
   Type=simple
   User=<username>
   Environment="PATH=/home/<username>/.local/bin:/usr/local/bin:/usr/bin:/bin"
   ExecStart=/home/<username>/.local/bin/hivemind-worker start
   Restart=always
   RestartSec=5

   [Install]
   WantedBy=multi-user.target
   ```
   *(Update `User` and binary path to match your environment).*

3. Enable and start:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now hivemind-worker
   ```

4. Check logs:
   ```bash
   journalctl -u hivemind-worker -f
   ```

---

### Windows

#### Method 1: Run in Background for Current Session (No Auto-Startup)

If you only want to run the worker in the background during your current session (stops when closed or rebooted):

* **Option A: Hidden Background Process (PowerShell)**
  ```powershell
  Start-Process -WindowStyle Hidden -FilePath "hivemind-worker" -ArgumentList "start" -RedirectStandardOutput "$env:USERPROFILE\.hivemind\worker.log" -RedirectStandardError "$env:USERPROFILE\.hivemind\worker.log"
  ```
  *To stop the background worker:*
  ```powershell
  Stop-Process -Name "hivemind-worker" -Force
  ```

* **Option B: PowerShell Background Job**
  ```powershell
  Start-Job -Name "HiveMindWorker" -ScriptBlock { hivemind-worker start }
  ```
  *To view status, logs, or stop:*
  ```powershell
  Get-Job -Name "HiveMindWorker"
  Receive-Job -Name "HiveMindWorker" -Keep   # View logs
  Stop-Job -Name "HiveMindWorker"           # Stop job
  ```

---

#### Method 2: Auto-Start on System Startup / Logon

##### Option A: Windows Task Scheduler (Native)
Run in PowerShell as Administrator:
```powershell
# Save settings first
hivemind-worker key set

# Register auto-start task on logon
$Action = New-ScheduledTaskAction -Execute "hivemind-worker.exe" -Argument "start"
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "HiveMindWorker" -Action $Action -Trigger $Trigger -Settings $Settings -Description "HiveMind Worker AI Fleet Daemon"
```

##### Option B: Windows Service via NSSM (Runs even when logged out)

> [!WARNING]
> NSSM services run as **LocalSystem** unless you set `ObjectName`. LocalSystem has a different home folder, so it would not find your worker config, API keys (`~/.env`) or git credentials, and any job would run with full SYSTEM privileges. Always run the service as your own (or a dedicated) user and pass `--config` explicitly.

```powershell
hivemind-worker key set   # run as the account the service will use

$WorkerPath = (Get-Command hivemind-worker).Source
$Config = "$env:USERPROFILE\.hivemind\worker.json"
nssm install HiveMindWorker $WorkerPath "--config `"$Config`" start"
nssm set HiveMindWorker ObjectName ".\$env:USERNAME" "<your-windows-password>"
nssm set HiveMindWorker AppStdout "$env:USERPROFILE\.hivemind\worker-service.log"
nssm set HiveMindWorker AppStderr "$env:USERPROFILE\.hivemind\worker-service.log"
nssm start HiveMindWorker
```
*(For a Microsoft account, use `MicrosoftAccount\you@example.com` as the user name.)*

---

### Reaching the worker by name

The master normally connects by IP (LAN or Tailscale `100.x`), which always works. Connecting by name also works for `localhost`, the machine's hostname (and `<hostname>.local`), and its Tailscale MagicDNS name (picked up automatically, even if Tailscale starts after the worker). For any other DNS name, add it to `allowed_hosts` in the worker config.

---

## 🧪 Development & Tests

Tests live in `tests/` and use pytest. They run offline: the HTTP API is driven through FastAPI's `TestClient`, and full runs use a temporary local git origin with a stand-in Python script as the agent, so no AI CLI or API key is needed. Your real `~/.hivemind` and `~/.env` are never read.

```bash
uv run pytest            # creates .venv with the dev dependency group on first run
uv run pytest -k runner  # run a subset
```

GitHub Actions (`.github/workflows/tests.yml`) runs the same suite on Ubuntu and Windows with Python 3.10 and 3.13, for every pull request and every push to `main`.


