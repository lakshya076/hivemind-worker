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
# Start the daemon with FLEET key
hivemind-worker start --key <YOUR_FLEET_KEY> --port 7422
```

By default:
- Listens on `0.0.0.0:7422` (accessible via Tailscale mesh IP `100.x.x.x` or local network)
- Workspace root defaults to `~/hive-workspace` (or `C:\Users\<user>\hive-workspace` on Windows)
- Authenticates all requests with `Authorization: Bearer <fleet-key>`

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

- **Fleet API Key Authentication**: Rejects unauthenticated requests with HTTP 401.
- **Workspace Isolation**: Git operations and file edits are strictly scoped to the assigned task directory.
- **Stripped Subprocess Environment**: Subprocesses receive only essential execution variables (`PATH`, git user config, target API keys) — no host SSH keys or personal credentials.

---

## 🔄 Running as a Background Service

### Linux (systemd service)

1. Save configuration once on the worker:
   ```bash
   hivemind-worker start --key <FLEET_KEY> --port 7422 --save
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
   ExecStart=/home/<username/.local/bin/hivemind-worker start
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
hivemind-worker start --key <FLEET_KEY> --port 7422 --save

# Register auto-start task on logon
$Action = New-ScheduledTaskAction -Execute "hivemind-worker.exe" -Argument "start"
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "HiveMindWorker" -Action $Action -Trigger $Trigger -Settings $Settings -Description "HiveMind Worker AI Fleet Daemon"
```

##### Option B: Windows Service via NSSM (Runs even when logged out)
```powershell
$WorkerPath = (Get-Command hivemind-worker).Source
nssm install HiveMindWorker $WorkerPath "start"
nssm set HiveMindWorker AppStdout "$env:USERPROFILE\.hivemind\worker-service.log"
nssm set HiveMindWorker AppStderr "$env:USERPROFILE\.hivemind\worker-service.log"
nssm start HiveMindWorker
```


