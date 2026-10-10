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
