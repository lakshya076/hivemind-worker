# Changes

## Security hardening, round 1 (branch `worker-security`)

Fixes the two critical issues from the security review:

- **Critical #1**: the daemon accepted every request when no fleet key was set, while listening on all interfaces.
- **Critical #2**: any web page could drive the daemon through a browser (CORS `*`, no DNS-rebinding protection).

None of these changes alter how the master talks to the worker on any channel: localhost, LAN/Wi-Fi, Tailscale IP, and Tailscale MagicDNS all keep working.

### Authentication is mandatory (A1)
- `hivemind-worker start` with no key configured now **generates** a key (`hive-key-<40 hex>`, the same format as `hive keygen`), saves only the key to the config file, and prints setup instructions.
  - On a terminal the key is printed once.
  - When output is redirected (systemd, NSSM, `Start-Process`) the key is **not** written to the log. Instead it says to run `hivemind-worker key show`.
- `create_app()` raises if there is no key, so the server can't be built unauthenticated by mistake.
- `verify_auth` fails closed (503) if the key disappears at runtime, instead of allowing the request.
- New commands:
  - `hivemind-worker key set` (hidden prompt, keeps the key out of shell history and process lists)
  - `hivemind-worker key show`
  - `hivemind-worker key rotate`

### Keyless mode is loopback-only (A3)
- New flag `start --insecure-no-auth` for local testing. It forces the bind address to `127.0.0.1`, and the network guard additionally rejects every non-loopback client.

### Source-network allowlist (B1)
- New config field `allowed_networks`. The default covers loopback, RFC 1918 LAN, IPv4/IPv6 link-local, IPv6 ULA, and Tailscale (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`).
- Requests from any other address (i.e. the public internet) get **403 before authentication**.
- Handles IPv4-mapped IPv6 and IPv6 zone ids. Invalid CIDRs fail config validation.
- The bind address is unchanged (`0.0.0.0`), so the worker survives DHCP changes and Tailscale starting late.

### No CORS; browser requests refused (C1, C3)
- Removed `CORSMiddleware` (`allow_origins=["*"]` with credentials). No browser client exists.
- Any request with an `Origin` header, or a `Sec-Fetch-Site` other than `none`, gets **403**. `httpx` (the master) and `curl` never send these headers. Typing the URL into an address bar (`Sec-Fetch-Site: none`) still works.

### Host header / DNS-rebinding protection (C4)
- IP literals in `Host` are always accepted. That's how clusters address workers, and DNS rebinding always arrives with a domain name, never an IP.
- Names are accepted only if they are:
  - `localhost`;
  - this machine's hostname, FQDN, or `<hostname>.local`;
  - its **Tailscale MagicDNS** name (full or short), looked up lazily via `tailscale status --json` and refreshed at most once a minute, so it's picked up if Tailscale starts after the worker;
  - or an entry in the new `allowed_hosts` config field.
- Anything else gets **400**.

### Config loading fails closed
- If `worker.json` exists but cannot be parsed or validated, the daemon now **refuses to start**. It previously fell back silently to a keyless default.
- The config file is written owner-only (`0600`) on Linux/macOS, including when it already existed with looser permissions.
- New global option `--config <path>` / env `HIVEMIND_WORKER_CONFIG`, so services don't depend on which home folder they resolve.
- `HIVEMIND_FLEET_KEY` now fills in the key when the config file exists but has no key.

### Bug fix found while testing
- On Windows, when output was redirected to a file (the documented `Start-Process -RedirectStandardOutput` and NSSM `AppStdout` setups), the 🤖 emoji in the startup banner crashed the daemon with `UnicodeEncodeError` before it started listening. Redirected stdout/stderr are now reconfigured to UTF-8.
- `start --port/--host` defaults no longer collide with the saved config (old logic: `if port != 7422 or not config.port`).

### Documentation
- Quickstart uses `key set`; documents defaults, key commands, `--insecure-no-auth`, config fields, and connecting by name.
- **NSSM instructions fixed**. The previous command installed the service as LocalSystem, which didn't find the user's config, so the worker started keyless with SYSTEM privileges. The service now runs as the user (`ObjectName`) with `--config`.
- Fixed the systemd `ExecStart` path typo.

### Compatibility
- Existing `worker.json` files load unchanged; the new fields get defaults.
- Workers that previously ran keyless now generate a key on next start. The master must be given that key (or the worker given the master's key via `key set`).
- A worker reached through a DNS name other than its hostname or MagicDNS name needs that name in `allowed_hosts`.
- A worker reached from a public IP (e.g. a cloud VM addressed by its public address) needs that client range in `allowed_networks`. Better: reach it over Tailscale.

### Testing
- Local test suite (kept out of git via `.gitignore`): 83 passed, 1 skipped (POSIX permission bits, skipped on Windows). It covers:
  - ASGI-level request tests with spoofed client IPs and headers;
  - a real uvicorn server on a socket, driven with httpx;
  - the real `hivemind-worker` executable run as a background process with redirected output and probed over loopback, the LAN IP, the Tailscale IP, and the MagicDNS name;
  - CLI flows;
  - config edge cases.
- Mutation-checked: re-introducing each original bug (fail-open auth, no browser check, any Host accepted, any source IP accepted, corrupt config ignored) makes the suite fail.
