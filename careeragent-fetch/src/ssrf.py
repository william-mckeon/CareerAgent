#!/usr/bin/env python3
# ============================================================================
# careeragent-fetch - SSRF-safe URL fetch + HTML→text extraction
# ============================================================================
#
# This module is the whole point of careeragent-fetch: it is the ONLY place in
# CareerAgent that opens a socket to a user-controlled URL. Everything here is
# written defensively.
#
# The control list (validated BEFORE any connection, and re-validated on EVERY
# redirect hop):
#   - scheme allowlist: http / https only (no file:, ftp:, gopher:, data:, ...)
#   - resolve the host to ALL A/AAAA records (socket.getaddrinfo) and reject if
#     ANY resolved IP is loopback / private / link-local / ULA / multicast /
#     reserved / unspecified / a cloud metadata endpoint.
#   - literal-IP hosts are validated directly (no DNS).
#   - redirects are NOT auto-followed; we follow manually, capped, revalidating.
#   - the response body is streamed with a hard byte cap (413 before HTML→text).
#   - the content-type is gated to text/html, application/xhtml+xml, text/plain.
#
# RESIDUAL RISK — DNS rebinding: we resolve+validate, then let httpx connect by
# hostname (which re-resolves). A DNS answer that flips between the validate and
# the connect could point the actual socket at a blocked IP. We shrink — but do
# NOT fully close — this window by (a) capping redirects and revalidating every
# hop, and (b) keeping timeouts short. Fully closing it would require pinning the
# validated IP into the socket while preserving TLS SNI/cert verification against
# the hostname; that is deliberately out of scope for P5 and documented in the
# spec. For a first-fetch box behind api-only auth, this is an accepted residual.
# ============================================================================

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin, urlsplit

import httpx

# --- tunables (defaults; the API layer passes env-configured values in) --------
DEFAULT_MAX_FETCH_BYTES = 2_000_000
DEFAULT_FETCH_TIMEOUT = 8.0
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_MAX_TEXT_CHARS = 100_000

ALLOWED_CONTENT_TYPES = {"text/html", "application/xhtml+xml", "text/plain"}
USER_AGENT = "careeragent-fetch/1.0 (+https://careeragent.local)"

# Cloud metadata endpoints. These are already link-local (v4) / ULA (v6) and so
# are caught by the generic checks below, but the spec asks us to assert them
# explicitly — a metadata leak is the single worst SSRF outcome here.
_METADATA_IPS = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("fd00:ec2::254"),
}
_IPV6_ULA = ipaddress.ip_network("fc00::/7")
# RFC 6598 shared / Carrier-Grade-NAT space. Python's ipaddress does NOT classify
# this as private/reserved, so it slips past every is_* predicate below — yet in
# AWS EKS custom-CNI / Fargate / VPC-internal deployments it is LIVE internal
# space. Block it explicitly (an is_global positive-gate would also work, but an
# explicit range keeps the refusal reason legible).
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


# --- exceptions: each carries the HTTP status the API layer should return ------
class FetchProblem(Exception):
    """Base for everything /fetch can go wrong with. status_code drives the HTTP."""
    status_code = 502

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class InvalidURL(FetchProblem):
    status_code = 400          # unparseable / bad scheme / no host / unresolvable


class SSRFBlocked(FetchProblem):
    status_code = 400          # resolves to a disallowed address


class UpstreamError(FetchProblem):
    status_code = 502          # timeout / transport error / bad upstream status


class ResponseTooLarge(FetchProblem):
    status_code = 413          # Content-Length or streamed body over the cap


class UnsupportedContentType(FetchProblem):
    status_code = 415          # not text/html, xhtml, or text/plain


@dataclass
class FetchResult:
    text: str
    truncated: bool
    final_url: str
    title: Optional[str]


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def _reason_blocked(ip: ipaddress._BaseAddress) -> Optional[str]:
    """Return a human reason if this IP must be refused, else None.

    Order matters only for the message; every branch is a hard block. IPv4-mapped
    IPv6 (::ffff:127.0.0.1) is unwrapped first so a mapped private/loopback can't
    slip through the v6 predicates.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip in _METADATA_IPS:
        return "cloud metadata endpoint"
    if ip.is_unspecified:
        return "unspecified address"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.is_private:
        return "private"
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return "shared address space (CGNAT)"
    if ip.is_multicast:
        return "multicast"
    if ip.is_reserved:
        return "reserved"
    if isinstance(ip, ipaddress.IPv6Address) and ip in _IPV6_ULA:
        return "IPv6 unique-local"
    return None


def validate_url(url: str) -> None:
    """Raise InvalidURL / SSRFBlocked if `url` must not be fetched; else return.

    Called before the first connection AND before every redirect hop. Validates
    the RESOLVED IP(s), not merely the hostname string — a hostname that resolves
    to any blocked address is refused.
    """
    try:
        parsed = urlsplit(url)
    except Exception as err:  # pragma: no cover - urlsplit is very forgiving
        raise InvalidURL(f"could not parse URL: {err}")

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise InvalidURL(
            f"unsupported URL scheme '{parsed.scheme}' (only http/https allowed)"
        )

    host = parsed.hostname
    if not host:
        raise InvalidURL("URL has no host")

    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise InvalidURL("URL has an invalid port")

    # Literal IP host → validate it directly, no DNS.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        reason = _reason_blocked(literal)
        if reason:
            raise SSRFBlocked(f"blocked address {host} ({reason})")
        return

    # Hostname → resolve to ALL records and validate EVERY one (defeats a record
    # set that mixes a public and a private address).
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as err:
        raise InvalidURL(f"could not resolve host '{host}': {err}")
    if not infos:
        raise InvalidURL(f"host '{host}' resolved to no addresses")

    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            raise SSRFBlocked(f"host '{host}' resolved to an unparseable address")
        reason = _reason_blocked(ip)
        if reason:
            raise SSRFBlocked(
                f"host '{host}' resolves to blocked address {ip_str} ({reason})"
            )


# ---------------------------------------------------------------------------
# HTML → text
# ---------------------------------------------------------------------------
def _html_to_text(html: str, base_url: str) -> tuple[str, Optional[str]]:
    """trafilatura for clean main content, BeautifulSoup for the title + fallback."""
    text: Optional[str] = None
    title: Optional[str] = None

    try:
        import trafilatura

        text = trafilatura.extract(
            html, url=base_url, include_comments=False, include_tables=True,
            favor_recall=True,
        ) or None
    except Exception:
        text = None

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        if soup.title and soup.title.string:
            stripped = soup.title.string.strip()
            title = stripped or None
        if text is None:
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
    except Exception:
        if text is None:
            text = ""

    return (text or ""), title


# ---------------------------------------------------------------------------
# the safe fetch
# ---------------------------------------------------------------------------
async def fetch_url(
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_FETCH_BYTES,
    timeout: float = DEFAULT_FETCH_TIMEOUT,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
) -> FetchResult:
    """Fetch `url` safely and return clean text.

    Redirects are followed manually (follow_redirects=False) so the SSRF check
    runs on each hop's target. The body is streamed and aborted the moment it
    exceeds max_bytes, so the size cap is enforced during the read — never merely
    trusting Content-Length.
    """
    current = url
    redirects = 0
    timeout_cfg = httpx.Timeout(timeout, connect=timeout)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
    }

    buf = bytearray()
    ctype = ""
    encoding = "utf-8"
    final_url = url

    async with httpx.AsyncClient(
        follow_redirects=False, timeout=timeout_cfg, headers=headers
    ) as client:
        while True:
            validate_url(current)  # BEFORE every hop, including redirects
            try:
                async with client.stream("GET", current) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise UpstreamError("redirect with no Location header")
                        redirects += 1
                        if redirects > max_redirects:
                            raise UpstreamError(
                                f"exceeded max redirects ({max_redirects})"
                            )
                        current = urljoin(current, location)
                        continue

                    if resp.status_code >= 400:
                        raise UpstreamError(f"upstream returned HTTP {resp.status_code}")

                    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                    if ctype not in ALLOWED_CONTENT_TYPES:
                        raise UnsupportedContentType(
                            f"unsupported content-type '{ctype or 'unknown'}' "
                            "(only html / xhtml / plain text)"
                        )

                    # Reject an honest oversized Content-Length up front...
                    clen = resp.headers.get("content-length")
                    if clen and clen.isdigit() and int(clen) > max_bytes:
                        raise ResponseTooLarge(
                            f"response too large (Content-Length {clen} > {max_bytes})"
                        )

                    # ...but never trust it: enforce the cap while streaming.
                    async for chunk in resp.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > max_bytes:
                            raise ResponseTooLarge(
                                f"response exceeded {max_bytes} bytes"
                            )

                    final_url = str(resp.url)
                    encoding = resp.encoding or "utf-8"
                    break
            except FetchProblem:
                raise
            except httpx.TimeoutException as err:
                raise UpstreamError(f"timed out fetching URL: {err}")
            except httpx.HTTPError as err:
                raise UpstreamError(f"could not fetch URL: {type(err).__name__}: {err}")

    try:
        raw = bytes(buf).decode(encoding, errors="replace")
    except (LookupError, TypeError):
        raw = bytes(buf).decode("utf-8", errors="replace")

    if ctype == "text/plain":
        text, title = raw, None
    else:
        text, title = _html_to_text(raw, final_url)

    truncated = False
    if len(text) > max_text_chars:
        text = text[:max_text_chars]
        truncated = True

    return FetchResult(text=text, truncated=truncated, final_url=final_url, title=title)
