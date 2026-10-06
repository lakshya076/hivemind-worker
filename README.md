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
# Start the daemon with API key
hivemind-worker start --key <YOUR_API_KEY> --port 7422
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
