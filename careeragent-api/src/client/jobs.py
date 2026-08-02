#!/usr/bin/env python3
# ============================================================================
# careeragent-api - Jobs client (outbound to careeragent-jobs)
# ============================================================================
#
# Backs the agent's `spawn_job` control tool (P7 #18). Modeled on AtsClient/
# RenderClient: owns its own httpx.AsyncClient with X-API-Key pre-attached, never
# raises on HTTP status (returns (status, body) so the loop formats it).
#
# The api only ENQUEUES a job here and returns immediately — the actual slow work
# (a repo review, etc.) runs on the careeragent-jobs WORKER, which injects the
# result back into the conversation when done ("do not poll"). So the read timeout
# is short: enqueue is a single fast INSERT round-trip, never the job's runtime.
# ============================================================================

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

import httpx

logger = logging.getLogger("careeragent-api")

Result = Tuple[int, Any]


class JobsClient:
    """Outbound HTTP client for careeragent-jobs. Config-only construction;
    start()/stop() managed by the FastAPI lifespan. Never raises on status."""

    def __init__(self, url: str, api_key: str, read_timeout: float = 20.0) -> None:
        if not url:
            raise ValueError("JobsClient.url is required")
        if not api_key:
            raise ValueError("JobsClient.api_key is required")
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
        logger.info("JobsClient started (url=%s)", self.url)

    async def stop(self) -> None:
        if self._http is None:
            return
        try:
            await self._http.aclose()
        except Exception as err:
            logger.warning("Error closing JobsClient: %s: %s", type(err).__name__, err)
        finally:
            self._http = None

    async def enqueue(self, kind: str, spec: Optional[dict], conversation_id: Optional[str]) -> Result:
        """POST /jobs — enqueue a background job and get back `{id, status}` (201) or a
        `{detail}` error (400 unknown kind, 4xx/5xx). The worker runs it later and
        injects the result into `conversation_id`. Returns (status, body). Never raises."""
        if self._http is None:
            return 0, {"detail": "jobs client not started"}
        payload = {"kind": kind, "spec": spec or {}}
        if conversation_id:
            payload["conversation_id"] = conversation_id
        try:
            resp = await self._http.post("/jobs", json=payload)
        except httpx.HTTPError as err:
            return 0, {"detail": f"{type(err).__name__}: {err}"}
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        return resp.status_code, data
