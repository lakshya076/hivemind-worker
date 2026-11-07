import ipaddress

import pytest

from hivemind_worker.config import DEFAULT_ALLOWED_NETWORKS
from hivemind_worker.security import (
    HostAllowlist,
    client_ip_allowed,
    is_browser_request,
    parse_ip,
    parse_networks,
    split_host_header,
)

DEFAULT_NETS = parse_networks(DEFAULT_ALLOWED_NETWORKS)


def test_parse_ip_unwraps_mapped_ipv6_and_zone_ids():
    assert parse_ip("::ffff:192.168.1.5") == ipaddress.ip_address("192.168.1.5")
    assert parse_ip("fe80::1%eth0") == ipaddress.ip_address("fe80::1")
    assert parse_ip("testclient") is None
    assert parse_ip(None) is None


@pytest.mark.parametrize(
    "client",
    ["127.0.0.1", "::1", "10.1.2.3", "172.20.0.5", "192.168.1.20", "100.101.58.76",
     "fd7a:115c:a1e0::1", "::ffff:192.168.1.5", "fe80::1%eth0"],
)
def test_private_and_tailscale_clients_allowed(client):
    assert client_ip_allowed(client, DEFAULT_NETS)


@pytest.mark.parametrize("client", ["8.8.8.8", "203.0.113.9", "2001:db8::1", "::ffff:8.8.8.8", None, "", "testclient"])
def test_public_or_unparseable_clients_blocked(client):
    assert not client_ip_allowed(client, DEFAULT_NETS)


def test_custom_tailscale_only_allowlist():
    nets = parse_networks(["100.64.0.0/10"])
    assert client_ip_allowed("100.101.58.76", nets)
    assert not client_ip_allowed("192.168.1.20", nets)


@pytest.mark.parametrize(
    "headers, browser",
    [
        ({}, False),
        ({"sec-fetch-site": "none"}, False),
        ({"origin": "https://evil.example"}, True),
        ({"origin": "null"}, True),
        ({"sec-fetch-site": "cross-site"}, True),
        ({"sec-fetch-site": "same-origin"}, True),
    ],
)
def test_browser_detection(headers, browser):
    assert is_browser_request(headers) is browser


@pytest.mark.parametrize(
    "header, host",
    [("10.0.0.2:7422", "10.0.0.2"), ("[::1]:7422", "::1"), ("[::1]", "::1"), ("ari", "ari"), ("::1", "::1")],
)
def test_split_host_header(header, host):
    assert split_host_header(header) == host


def no_tailscale():
    return set()


def test_host_allowlist_accepts_ips_localhost_and_extra_names():
    allow = HostAllowlist(["Worker.Example.Lan."], tailscale_lookup=no_tailscale)
    for header in ("10.0.0.2:7422", "[fd7a:115c:a1e0::1]:7422", "localhost:7422", "worker.example.lan"):
        assert allow.is_allowed(header), header


@pytest.mark.parametrize("header", ["evil.example", "evil.example:7422", "", None])
def test_host_allowlist_rejects_rebinding_names(header):
    assert not HostAllowlist(tailscale_lookup=no_tailscale).is_allowed(header)


def test_tailscale_names_are_looked_up_lazily_and_rate_limited():
    calls = []

    def lookup():
        calls.append(1)
        return {"ari.tail1234.ts.net", "ari"}

    allow = HostAllowlist(tailscale_lookup=lookup, refresh_interval=3600)
    assert calls == []  # nothing looked up at startup
    assert allow.is_allowed("ari.tail1234.ts.net:7422")
    assert not allow.is_allowed("unknown.example")
    assert not allow.is_allowed("unknown2.example")
    assert len(calls) == 1
