# HiveMind Worker Daemon (`hivemind-worker`)

A lightweight, secure, cross-platform HTTP execution daemon for [**HiveMind**](https://github.com/lakshya076/HiveMind) distributed AI coding fleet.

Runs on Linux, macOS, and Windows. Eliminates raw SSH dependency and provides isolated agent workspace execution.

---

## 🚀 Quickstart

### 1. Installation

```bash
# Install via pip or uv
pip install hivemind-worker
```

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

---

## 📡 API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Hardware and tool capability probe (<0.1s response) |
| `POST` | `/dispatch` | Dispatches an AI agent run in an isolated workspace |
| `GET` | `/status/{run_id}` | Polls live status, git branch, test results, and log tail |
| `DELETE` | `/cancel/{run_id}` | Aborts a running task and terminates the agent process tree |
| `GET` | `/runs` | Lists all historical and active runs |

---

## 🛡️ Security Features

- **Mandatory Fleet API Key Authentication**: Rejects unauthenticated requests with HTTP 401. The daemon refuses to run without a key (except `--insecure-no-auth`, which is loopback-only).
- **Private-Network Allowlist**: Requests from addresses outside `allowed_networks` are rejected with 403 before authentication, so a worker accidentally exposed to the internet (cloud VM, port forward) is not reachable.
- **Browser Request Blocking**: No CORS is served, and any request carrying `Origin` or a cross-site `Sec-Fetch-Site` header is rejected with 403. Web pages cannot drive the worker.
- **DNS-Rebinding Protection**: The `Host` header must be an IP address, `localhost`, this machine's hostname, its Tailscale MagicDNS name, or an entry in `allowed_hosts`; anything else gets 400.
- **Workspace Isolation**: Git operations and file edits are strictly scoped to the assigned task directory.
- **Stripped Subprocess Environment**: Subprocesses receive only essential execution variables (`PATH`, git user config, target API keys) — no host SSH keys or personal credentials.

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


