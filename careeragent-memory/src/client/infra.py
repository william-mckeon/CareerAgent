"""InfraClient — the outbound boundary to careeragent-infra's /embed route.

This is the only thing careeragent-memory calls upstream. It mirrors the client
pattern in careeragent-api/src/client/infra.py: a single httpx.AsyncClient
constructed once at start() with X-API-Key pre-attached, so every call carries
the credential automatically.

careeragent-infra's /embed is a serverless, scale-to-zero provider route, so the
read timeout is generous enough to absorb a cold start but still bounded
(MEMORY_EMBED_TIMEOUT) — when it is exceeded, callers fail open rather than hang.
"""

from __future__ import annotations

import logging
from typing import List, Sequence, Tuple

import httpx

logger = logging.getLogger("careeragent_memory.infra")


class InfraEmbedError(Exception):
    """Raised when /embed cannot produce vectors (unreachable, timeout, non-200,
    or 'not configured'). Retrieval treats this as a fail-open signal; ingest
    surfaces it as a 503 so the caller knows the write did not land."""


class InfraClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        connect_timeout: float = 5.0,
        read_timeout: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=connect_timeout,
            pool=connect_timeout,
        )
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"X-API-Key": self._api_key},
            timeout=self._timeout,
        )
        logger.info("InfraClient started (base_url=%s)", self._base_url)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("InfraClient closed")

    async def embed(self, inputs: str | Sequence[str]) -> List[List[float]]:
        """Embed one string or a batch of strings.

        A list is sent in a single /embed call (one provider round-trip) — the
        batch form careeragent-infra documents. Returns one vector per input, in
        input order.
        """
        if self._client is None:  # pragma: no cover - lifecycle guard
            raise InfraEmbedError("InfraClient used before start()")

        payload = {"input": inputs}
        try:
            resp = await self._client.post("/embed", json=payload)
        except httpx.TimeoutException as exc:
            raise InfraEmbedError(f"/embed timed out: {type(exc).__name__}") from exc
        except httpx.HTTPError as exc:
            raise InfraEmbedError(f"/embed transport error: {type(exc).__name__}") from exc

        if resp.status_code != 200:
            # 503 means EMBEDDING_MODEL_URL is unset or the embedder host is down.
            raise InfraEmbedError(f"/embed returned HTTP {resp.status_code}")

        try:
            body = resp.json()
            data = body["data"]
            ordered = sorted(data, key=lambda d: d.get("index", 0))
            vectors = [item["embedding"] for item in ordered]
        except (KeyError, TypeError, ValueError) as exc:
            raise InfraEmbedError(f"/embed returned an unparseable body: {exc}") from exc

        if not vectors:
            raise InfraEmbedError("/embed returned no vectors")

        # Guard against a mis-aligned provider response. The count must match the
        # number of inputs, and for a batch every item must carry a distinct
        # `index` — otherwise the sort above collapses everything to 0 and we
        # could silently pair the wrong vector with a turn.
        expected = 1 if isinstance(inputs, str) else len(inputs)
        if len(vectors) != expected:
            raise InfraEmbedError(
                f"/embed returned {len(vectors)} vectors for {expected} input(s)"
            )
        if expected > 1:
            indices = [d.get("index") for d in data]
            if any(i is None for i in indices) or len(set(indices)) != expected:
                raise InfraEmbedError("/embed response indices missing or not distinct")
        return vectors

    async def embed_one(self, text: str) -> List[float]:
        """Convenience wrapper: embed a single string, return its vector."""
        return (await self.embed(text))[0]

    async def embed_health(self) -> Tuple[str, str]:
        """Best-effort probe of the embedding route for /health reporting.

        Returns (status, url). Never raises — any failure maps to 'unreachable'.
        careeragent-infra's /health reports the embedding endpoint independently;
        we surface that field if present.
        """
        url = f"{self._base_url}/health"
        if self._client is None:
            return ("unreachable", url)
        try:
            resp = await self._client.get("/health")
            if resp.status_code != 200:
                return ("unreachable", url)
            embed_state = resp.json().get("embedding", "unknown")
            return (str(embed_state), url)
        except (httpx.HTTPError, ValueError):
            return ("unreachable", url)
