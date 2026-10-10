"""Network-level request guards for the worker daemon.

Three independent checks run before authentication on every request:

* Source network (B1): the client IP must fall inside ``allowed_networks``
  (private LAN, Tailscale and loopback by default), so a worker accidentally
  exposed to the internet rejects public traffic outright.
* Browser requests (C3): the master (httpx) and curl never send ``Origin`` or
  cross-site ``Sec-Fetch-Site`` headers, so any request that has them comes
  from a web page and is refused. This blocks cross-site attacks driven
  through a browser on the worker's network.
* Host header (C4): DNS rebinding always arrives with the attacker's domain
  in ``Host``. IP literals are always accepted (that is how clusters address
  workers), and names must be this machine's own names, its Tailscale
  MagicDNS name, or a configured ``allowed_hosts`` entry.
"""

import ipaddress
import json
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
from typing import Iterable, List, Optional, Set

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def parse_networks(cidrs: Iterable[str]) -> List[IPNetwork]:
    return [ipaddress.ip_network(cidr, strict=False) for cidr in cidrs]


def parse_ip(value: Optional[str]) -> Optional[IPAddress]:
    """Parse a client address, unwrapping IPv4-mapped IPv6 and zone ids."""
    if not value:
        return None
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def client_ip_allowed(client_host: Optional[str], networks: List[IPNetwork]) -> bool:
    ip = parse_ip(client_host)
    if ip is None:
        return False
    return any(ip.version == net.version and ip in net for net in networks)


def is_browser_request(headers) -> bool:
    """True for requests issued by a web page (fetch/XHR/form/img from a site).

    A user typing the worker URL into the address bar sends
    ``Sec-Fetch-Site: none`` and no ``Origin`` on GET, so that still works.
    """
    if headers.get("origin") is not None:
        return True
    fetch_site = headers.get("sec-fetch-site")
    return fetch_site is not None and fetch_site.lower() != "none"


def split_host_header(host_header: str) -> str:
    """Return the hostname part of a Host header (no port, no IPv6 brackets)."""
    value = host_header.strip()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end != -1 else value
    if value.count(":") == 1:
        return value.rsplit(":", 1)[0]
    return value


def _normalize_name(name: str) -> str:
    return name.strip().rstrip(".").lower()


def _local_machine_names() -> Set[str]:
    names = {"localhost"}
    for raw in (socket.gethostname(), platform.node(), socket.getfqdn()):
        if raw:
            name = _normalize_name(raw)
            short = name.split(".", 1)[0]
            names.update({name, short, f"{short}.local"})
    return {n for n in names if n}


def _find_tailscale_binary() -> Optional[str]:
    binary = shutil.which("tailscale")
    if binary:
        return binary
    candidates = [
        r"C:\Program Files\Tailscale\tailscale.exe",
        r"C:\Program Files (x86)\Tailscale\tailscale.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Tailscale\tailscale.exe"),
        "/usr/bin/tailscale",
        "/usr/local/bin/tailscale",
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    ]
    return next((path for path in candidates if os.path.isfile(path)), None)


def tailscale_self_names() -> Set[str]:
    """This node's MagicDNS names (full and short), or empty if unavailable."""
    binary = _find_tailscale_binary()
    if not binary:
        return set()
    try:
        res = subprocess.run(
            [binary, "status", "--json", "--self=true", "--peers=false"],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
        if res.returncode != 0:
            return set()
        dns_name = _normalize_name(json.loads(res.stdout).get("Self", {}).get("DNSName") or "")
    except Exception:
        return set()
    if not dns_name:
        return set()
    return {dns_name, dns_name.split(".", 1)[0]}


class HostAllowlist:
    """Decides whether a Host header is a legitimate name for this worker.

    Tailscale names are looked up lazily and re-checked at most every
    ``refresh_interval`` seconds, so a worker that starts before tailscaled
    (or is renamed) picks up its MagicDNS name without a restart.
    """

    def __init__(
        self,
        extra_hosts: Iterable[str] = (),
        tailscale_lookup=tailscale_self_names,
        refresh_interval: float = 60.0,
    ):
        self._static = _local_machine_names() | {_normalize_name(h) for h in extra_hosts if h.strip()}
        self._tailscale_lookup = tailscale_lookup
        self._refresh_interval = refresh_interval
        self._tailscale_names: Set[str] = set()
        self._last_refresh: Optional[float] = None
        self._lock = threading.Lock()

    def _known(self, name: str) -> bool:
        return name in self._static or name in self._tailscale_names

    def is_allowed(self, host_header: Optional[str]) -> bool:
        if not host_header:
            return False
        hostname = split_host_header(host_header)
        if parse_ip(hostname) is not None:
            return True
        name = _normalize_name(hostname)
        if not name:
            return False
        if self._known(name):
            return True
        with self._lock:
            now = time.monotonic()
            if self._last_refresh is None or now - self._last_refresh >= self._refresh_interval:
                self._last_refresh = now
                self._tailscale_names = set(self._tailscale_lookup())
        return self._known(name)
