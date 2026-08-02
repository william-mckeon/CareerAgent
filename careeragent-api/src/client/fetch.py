#!/usr/bin/env python3
# ============================================================================
# careeragent-api - Fetch client (outbound to careeragent-fetch)
# ============================================================================
#
# Backs the agent's `fetch_url` tool. Modeled on ReviewClient/DossierClient: owns
# its own httpx.AsyncClient with X-API-Key pre-attached, never raises on HTTP
# status (returns (status, body) so dispatch formats it).
#
# Unlike ReviewClient (a repo-review fan-out can take MINUTES), a URL fetch is a
# single bounded GET behind careeragent-fetch's OWN timeout + size cap — so the
# api-side read timeout here is SHORT. careeragent-fetch is the box that isolates
# the SSRF/egress blast radius; this client just relays the coach's URL to it and
# hands back the cleaned text. The coach never fetches anything itself.
# ============================================================================

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

import httpx

logger = logging.getLogger("careeragent-api")

Result = Tuple[int, Any]


class FetchClient:
    """Outbound HTTP client for careeragent-fetch. Config-only construction;
    start()/stop() managed by the FastAPI lifespan. Never raises on status.

    The read timeout is intentionally short: careeragent-fetch enforces its own
    connect/read timeout and byte cap on the actual egress, so a slow or huge
    target fails INSIDE careeragent-fetch and comes back as a clean 4xx/5xx well
    before this client's own read window. A generous window here would just let a
    misbehaving fetch service hang the coach's step."""

    def __init__(self, url: str, api_key: str, read_timeout: float = 20.0) -> None:
        if not url:
            raise ValueError("FetchClient.url is required")
        if not api_key:
            raise ValueError("FetchClient.api_key is required")
        self.url = url.rstrip("/")
        self._api_key = api_key
        self._read_timeout = read_timeout
        self._http: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        if self._http is not None:
            return
        self._http = httpx.AsyncClient(
            base_url=self.url,
            timeout=httpx.Timeout(connect=5.0, read=self._read_timeout, write=10.0, pool=5.0),
            headers={"X-API-Key": self._api_key},
        )
        logger.info("FetchClient started (url=%s, read_timeout=%ss)", self.url, self._read_timeout)

    async def stop(self) -> None:
        if self._http is None:
            return
        try:
            await self._http.aclose()
        except Exception as err:
            logger.warning("Error closing FetchClient: %s: %s", type(err).__name__, err)
        finally:
            self._http = None

    async def fetch(self, url: str) -> Result:
        """POST /fetch — hand a user-supplied URL to careeragent-fetch and get back
        `{text, truncated, final_url, title}` (200) or a `{detail}` error (4xx/5xx).
        Returns (status, body). Never raises."""
        if self._http is None:
            return 0, {"detail": "fetch client not started"}
        if not isinstance(url, str) or not url.strip():
            return 400, {"detail": "a non-empty 'url' is required."}
        try:
            resp = await self._http.post("/fetch", json={"url": url.strip()})
        except httpx.HTTPError as err:
            return 0, {"detail": f"{type(err).__name__}: {err}"}
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        return resp.status_code, data

    async def search(self, query: str, max_results: Optional[int] = None) -> Result:
        """POST /search — run a web search and get back `{query, provider, results:
        [{title,url,snippet,score}], answer}` (200) or a `{detail}` error. The
        search API key lives in careeragent-fetch; this relay carries none. Returns
        (status, body). Never raises."""
        if self._http is None:
            return 0, {"detail": "search client not started"}
        if not isinstance(query, str) or not query.strip():
            return 400, {"detail": "a non-empty 'query' is required."}
        payload: dict = {"query": query.strip()}
        if isinstance(max_results, int):
            payload["max_results"] = max_results
        try:
            resp = await self._http.post("/search", json=payload)
        except httpx.HTTPError as err:
            return 0, {"detail": f"{type(err).__name__}: {err}"}
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        return resp.status_code, data
