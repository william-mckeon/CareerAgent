"""
src/client/api_client.py

Outbound client to careeragent-api.

sessions relays each /chat turn to careeragent-api unchanged and streams the
OpenAI-shaped SSE back to the caller byte-for-byte. The assistant-turn capture
(for persistence) happens in the endpoint, which reads the relayed bytes — this
client stays a transparent pipe.

Outbound auth is CAREERAGENT_API_KEY (careeragent-api's inbound key) — a
separate boundary from this service's own SESSIONS_API_KEY.
"""
import os
from typing import AsyncGenerator, List, Optional, Tuple

import httpx

API_URL = os.environ.get("CAREERAGENT_API_URL", "http://careeragent-api:8001").rstrip("/")
API_KEY = os.environ.get("CAREERAGENT_API_KEY", "")


class ApiClient:
    def __init__(self, url: Optional[str] = None, key: Optional[str] = None):
        self._url = (url or API_URL).rstrip("/")
        self._key = key if key is not None else API_KEY

    async def stream_chat(
        self, messages: List[dict], reasoning_effort: Optional[str],
        approval: Optional[dict] = None, conversation_id: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> AsyncGenerator[bytes, None]:
        """Forward to careeragent-api /chat and yield the raw SSE bytes as they arrive.

        Read timeout is unbounded (read=None) on purpose: an upstream
        high-effort generation or a cold provider can take minutes, exactly as
        careeragent-api itself does on the infra boundary. A non-200 from api
        surfaces as an in-stream [ERROR]/[DONE] (the 200 stream contract).
        """
        payload: dict = {"messages": messages}
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
        if approval is not None:
            payload["approval"] = approval    # P4: {call_id, granted} — executed on resume
        if conversation_id is not None:
            # P4.5: the api needs this to drain mid-run steering back from sessions.
            payload["conversation_id"] = conversation_id
        if mode is not None:
            payload["mode"] = mode                # P7 #20 plan-vs-act
        headers = {"Content-Type": "application/json", "X-API-Key": self._key}
        timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)

        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                async with client.stream(
                    "POST", f"{self._url}/chat", json=payload, headers=headers
                ) as resp:
                    if resp.status_code != 200:
                        await resp.aread()
                        yield f"data: [ERROR] upstream api returned {resp.status_code}\n\n".encode()
                        yield b"data: [DONE]\n\n"
                        return
                    async for chunk in resp.aiter_raw():
                        if chunk:
                            yield chunk
        except httpx.HTTPError:
            yield b"data: [ERROR] careeragent-api is not reachable\n\n"
            yield b"data: [DONE]\n\n"

    async def get_artifact(
        self, application_id: str, artifact_id: Optional[str] = None
    ) -> Tuple[int, Optional[bytes], str, str]:
        """Fetch a rendered résumé artifact's RAW bytes from careeragent-api's download
        proxy (P7 #16) and return (status, content|None, content_type, content_disposition).
        A separate byte-hop from /chat — the file never rides the SSE relay. Never raises."""
        params = {"artifact_id": artifact_id} if artifact_id else None
        url = f"{self._url}/applications/{application_id}/artifact"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(url, params=params, headers={"X-API-Key": self._key})
        except httpx.HTTPError:
            return 0, None, "", ""
        if r.status_code != 200:
            return r.status_code, None, "", ""
        return (
            200,
            r.content,
            r.headers.get("content-type", "application/octet-stream"),
            r.headers.get("content-disposition", ""),
        )

    async def health(self) -> bool:
        """True if careeragent-api answers /health (it requires the key)."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self._url}/health", headers={"X-API-Key": self._key})
                return r.status_code < 500
        except Exception:
            return False
