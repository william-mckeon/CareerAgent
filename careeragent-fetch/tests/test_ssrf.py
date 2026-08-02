"""
tests/test_ssrf.py — the SSRF validator. Hermetic: getaddrinfo is monkeypatched,
so NO DNS and NO network egress ever happen (validate_url raises before httpx is
ever used).
"""
import socket

import pytest

import ssrf


def _patch_resolve(monkeypatch, ip):
    """Make every getaddrinfo return exactly one record pointing at `ip`."""
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port or 0))]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake_getaddrinfo)


# --- literal IPs are validated directly (no DNS) -------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.5/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",   # cloud metadata
        "http://100.64.0.1/",                           # RFC 6598 CGNAT / shared space
        "http://100.127.255.255/",                      # CGNAT upper boundary
        "http://0.0.0.0/",
        "http://[::1]/",                                # IPv6 loopback
        "http://[fd00:ec2::254]/",                      # IPv6 metadata (ULA)
        "http://[fe80::1]/",                            # IPv6 link-local
    ],
)
def test_blocks_literal_private_and_metadata_ips(url):
    with pytest.raises(ssrf.SSRFBlocked):
        ssrf.validate_url(url)


def test_blocks_ipv4_mapped_loopback():
    # ::ffff:127.0.0.1 must be unwrapped and blocked as loopback.
    with pytest.raises(ssrf.SSRFBlocked):
        ssrf.validate_url("http://[::ffff:127.0.0.1]/")


# --- hostnames are validated on their RESOLVED addresses -----------------------
def test_blocks_localhost_hostname(monkeypatch):
    _patch_resolve(monkeypatch, "127.0.0.1")
    with pytest.raises(ssrf.SSRFBlocked):
        ssrf.validate_url("http://localhost/")


def test_blocks_hostname_resolving_to_private(monkeypatch):
    _patch_resolve(monkeypatch, "10.0.0.5")
    with pytest.raises(ssrf.SSRFBlocked):
        ssrf.validate_url("http://internal.evil.example.com/")


def test_blocks_hostname_resolving_to_metadata(monkeypatch):
    _patch_resolve(monkeypatch, "169.254.169.254")
    with pytest.raises(ssrf.SSRFBlocked):
        ssrf.validate_url("http://rebind.evil.example.com/")


def test_blocks_hostname_resolving_to_cgnat(monkeypatch):
    # A public hostname (or a 302) that lands on RFC 6598 shared space — live
    # internal space on AWS EKS/Fargate — must be refused like any private range.
    _patch_resolve(monkeypatch, "100.64.0.5")
    with pytest.raises(ssrf.SSRFBlocked):
        ssrf.validate_url("http://rebind.evil.example.com/")


def test_allows_public_ip(monkeypatch):
    # A hostname resolving to a public IP passes — with the network mocked, so
    # there is no real egress.
    _patch_resolve(monkeypatch, "93.184.216.34")   # example.com's public IP
    ssrf.validate_url("https://example.com/jobs/123")   # must not raise


# --- scheme allowlist ----------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://ftp.example.com/x",
        "gopher://example.com/",
        "data:text/plain;base64,aGk=",
        "ws://example.com/",
    ],
)
def test_rejects_non_http_schemes(url):
    with pytest.raises(ssrf.InvalidURL):
        ssrf.validate_url(url)


def test_rejects_missing_host():
    with pytest.raises(ssrf.InvalidURL):
        ssrf.validate_url("http:///nohost")


# --- fetch_url validates BEFORE it connects (zero egress on a blocked URL) ------
async def test_fetch_url_blocks_before_any_connection(monkeypatch):
    _patch_resolve(monkeypatch, "127.0.0.1")
    with pytest.raises(ssrf.SSRFBlocked):
        await ssrf.fetch_url(
            "http://localhost/", max_bytes=1000, timeout=1,
            max_redirects=2, max_text_chars=1000,
        )
