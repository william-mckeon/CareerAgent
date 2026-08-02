#!/usr/bin/env python3
# ============================================================================
# careeragent-jobs - Dossier client (outbound to careeragent-dossier)
# ============================================================================
#
# Backs the scheduled reminder job kinds (`follow_up_scan`, `resume_freshness`).
# Modeled on this service's ReviewClient/SessionsClient: owns its own
# httpx.AsyncClient with X-API-Key pre-attached, never raises on HTTP status
# (returns (status, body) so the handler formats it). Read-only — it only GETs
# the application tracker; it never mutates the dossier.
# ============================================================================

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

import httpx

logger = logging.getLogger("careeragent-jobs")

Result = Tuple[int, Any]


class DossierClient:
    """Outbound HTTP client for careeragent-dossier's read-only tracker search.
    Config-only construction; start()/stop() managed by the FastAPI lifespan.
    Never raises on status."""

    def __init__(self, url: str, api_key: str, timeout: float = 30.0) -> None:
        if not url:
            raise ValueError("DossierClient.url is required")
        if not api_key:
            raise ValueError("DossierClient.api_key is required")
        self.url = url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._http: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        if self._http is not None:
            return
        self._http = httpx.AsyncClient(
            base_url=self.url,
            timeout=httpx.Timeout(connect=5.0, read=self._timeout, write=5.0, pool=5.0),
            headers={"X-API-Key": self._api_key},
        )
        logger.info("DossierClient started (url=%s)", self.url)

    async def stop(self) -> None:
        if self._http is None:
            return
        try:
            await self._http.aclose()
        except Exception as err:
            logger.warning("Error closing DossierClient: %s: %s", type(err).__name__, err)
        finally:
            self._http = None

    async def search_applications(
        self,
        status: Optional[str] = None,
        stale: Optional[bool] = None,
        follow_up_due: Optional[bool] = None,
        limit: int = 200,
    ) -> Result:
        """GET /applications with the reminder filters. Returns (status, body)
        where body is a JSON list of application rows. Never raises; a transport
        error surfaces as (0, {"error": ...})."""
        if self._http is None:
            return 0, {"error": "dossier client not started"}
        params: dict = {"limit": limit}
        if status:
            params["status"] = status
        if stale is not None:
            params["stale"] = str(bool(stale)).lower()
        if follow_up_due is not None:
            params["follow_up_due"] = str(bool(follow_up_due)).lower()
        try:
            resp = await self._http.get("/applications", params=params)
        except httpx.HTTPError as err:
            return 0, {"error": f"{type(err).__name__}: {err}"}
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        return resp.status_code, data
