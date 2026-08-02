#!/usr/bin/env python3
# ============================================================================
# careeragent-api - ATS client (outbound to careeragent-ats)
# ============================================================================
#
# Backs the agent's `ats_score` READ tool. Modeled on FetchClient/ReviewClient:
# owns its own httpx.AsyncClient with X-API-Key pre-attached, never raises on
# HTTP status (returns (status, body) so tools.dispatch formats it).
#
# careeragent-ats is a PURE, DETERMINISTIC scorer — no model, no database, no
# network egress — so a scoring call is a single fast round-trip and the read
# timeout here is short. Unlike careeragent-fetch, there is no untrusted-egress
# blast radius to isolate: careeragent-ats only compares two blobs of the user's
# OWN text (their saved résumé vs. the JD the api hands it) and returns keyword
# coverage. The api resolves both blobs from dossier — the coach never pastes
# résumé/JD text through this tool (ADR-002: the score is grounded in what's
# actually stored, never in model-invented text).
# ============================================================================

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

import httpx

logger = logging.getLogger("careeragent-api")

Result = Tuple[int, Any]


class AtsClient:
    """Outbound HTTP client for careeragent-ats. Config-only construction;
    start()/stop() managed by the FastAPI lifespan. Never raises on status.

    The read timeout is short: the scorer is deterministic and CPU-bound with a
    bounded keyword set, so a healthy call returns in well under a second. A
    generous window would only let a wedged scorer hang the coach's step."""

    def __init__(self, url: str, api_key: str, read_timeout: float = 15.0) -> None:
        if not url:
            raise ValueError("AtsClient.url is required")
        if not api_key:
            raise ValueError("AtsClient.api_key is required")
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
        logger.info("AtsClient started (url=%s, read_timeout=%ss)", self.url, self._read_timeout)

    async def stop(self) -> None:
        if self._http is None:
            return
        try:
            await self._http.aclose()
        except Exception as err:
            logger.warning("Error closing AtsClient: %s: %s", type(err).__name__, err)
        finally:
            self._http = None

    async def score(self, resume_text: str, job_description: str) -> Result:
        """POST /ats-score — hand careeragent-ats the user's saved résumé + the JD and
        get back `{score, coverage, matched, missing}` (200) or a `{detail}` error
        (400 on an empty JD, 4xx/5xx otherwise). Returns (status, body). Never raises."""
        if self._http is None:
            return 0, {"detail": "ats client not started"}
        try:
            resp = await self._http.post(
                "/ats-score",
                json={"resume_text": resume_text or "", "job_description": job_description or ""},
            )
        except httpx.HTTPError as err:
            return 0, {"detail": f"{type(err).__name__}: {err}"}
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        return resp.status_code, data
