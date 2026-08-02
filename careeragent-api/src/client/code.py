#!/usr/bin/env python3
# ============================================================================
# careeragent-api - Code-workspace client (outbound to careeragent-code)
# ============================================================================
#
# Backs the agent's deep-code-review tools (sync_repo / code_search / read_code /
# list_repo_tree). Modeled on FetchClient/JobsClient: owns its own httpx client
# with X-API-Key pre-attached, never raises on status (returns (status, body) so
# dispatch formats it). careeragent-code holds the GitHub PAT; this relay carries
# NONE — the coach stays credential-less.
#
# /sync can CLONE a repo (a network op that takes seconds), so its read timeout is
# generous; the read tools are fast.
# ============================================================================

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

import httpx

logger = logging.getLogger("careeragent-api")

Result = Tuple[int, Any]


class CodeClient:
    """Outbound HTTP client for careeragent-code. Config-only construction;
    start()/stop() managed by the FastAPI lifespan. Never raises on status."""

    def __init__(self, url: str, api_key: str, read_timeout: float = 150.0) -> None:
        if not url:
            raise ValueError("CodeClient.url is required")
        if not api_key:
            raise ValueError("CodeClient.api_key is required")
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
        logger.info("CodeClient started (url=%s)", self.url)

    async def stop(self) -> None:
        if self._http is None:
            return
        try:
            await self._http.aclose()
        except Exception as err:
            logger.warning("Error closing CodeClient: %s: %s", type(err).__name__, err)
        finally:
            self._http = None

    async def _req(self, method: str, path: str, *, json: Any = None,
                   params: Any = None) -> Result:
        if self._http is None:
            return 0, {"detail": "code client not started"}
        try:
            resp = await self._http.request(method, path, json=json, params=params)
        except httpx.HTTPError as err:
            return 0, {"detail": f"{type(err).__name__}: {err}"}
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        return resp.status_code, data

    async def sync(self, repo: str) -> Result:
        """POST /sync — clone/refresh a repo into the workspace cache."""
        return await self._req("POST", "/sync", json={"repo": repo})

    async def grep(self, repo: str, pattern: str, glob: Optional[str] = None) -> Result:
        """POST /grep — ripgrep a synced repo."""
        body: dict = {"repo": repo, "pattern": pattern}
        if glob:
            body["glob"] = glob
        return await self._req("POST", "/grep", json=body)

    async def file(self, repo: str, path: str) -> Result:
        """GET /file — one file's text (bounded, traversal-safe)."""
        return await self._req("GET", "/file", params={"repo": repo, "path": path})

    async def tree(self, repo: str) -> Result:
        """GET /tree — the repo's file tree."""
        return await self._req("GET", "/tree", params={"repo": repo})
