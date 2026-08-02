#!/usr/bin/env python3
# ============================================================================
# careeragent-jobs - Review client (outbound to careeragent-review)
# ============================================================================
#
# Backs the `review_repos` job kind. Modeled on careeragent-api's ReviewClient:
# owns its own httpx.AsyncClient with X-API-Key pre-attached, never raises on
# HTTP status (returns (status, body) so the handler formats it). A repo-review
# fan-out can take MINUTES — exactly why it runs off the request path as a job —
# so the read timeout is generous (900s).
# ============================================================================

from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

import httpx

logger = logging.getLogger("careeragent-jobs")

Result = Tuple[int, Any]


class ReviewClient:
    """Outbound HTTP client for careeragent-review. Config-only construction;
    start()/stop() managed by the FastAPI lifespan. Never raises on status."""

    def __init__(self, url: str, api_key: str, read_timeout: float = 900.0) -> None:
        if not url:
            raise ValueError("ReviewClient.url is required")
        if not api_key:
            raise ValueError("ReviewClient.api_key is required")
        self.url = url.rstrip("/")
        self._api_key = api_key
        self._read_timeout = read_timeout
        self._http: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        if self._http is not None:
            return
        self._http = httpx.AsyncClient(
            base_url=self.url,
            timeout=httpx.Timeout(connect=10.0, read=self._read_timeout, write=10.0, pool=5.0),
            headers={"X-API-Key": self._api_key},
        )
        logger.info("ReviewClient started (url=%s, read_timeout=%ss)", self.url, self._read_timeout)

    async def stop(self) -> None:
        if self._http is None:
            return
        try:
            await self._http.aclose()
        except Exception as err:
            logger.warning("Error closing ReviewClient: %s: %s", type(err).__name__, err)
        finally:
            self._http = None

    async def review_batch(
        self,
        repos: Optional[List[str]] = None,
        limit: Optional[int] = None,
        focus: Optional[str] = None,
        force: bool = False,
    ) -> Result:
        """POST /review-batch. Returns (status, body). Never raises; a transport
        error surfaces as (0, {"error": ...})."""
        if self._http is None:
            return 0, {"error": "review client not started"}
        body: dict = {"force": bool(force)}
        if repos:
            body["repos"] = repos
        if limit is not None:
            body["limit"] = limit
        if focus:
            body["focus"] = focus
        try:
            resp = await self._http.post("/review-batch", json=body)
        except httpx.HTTPError as err:
            return 0, {"error": f"{type(err).__name__}: {err}"}
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        return resp.status_code, data
