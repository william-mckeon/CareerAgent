#!/usr/bin/env python3
# ============================================================================
# careeragent-fetch - web search (provider-pluggable)
# ============================================================================
#
# The coach's fetch_url can READ a URL it already has; web search lets it FIND
# one — a company's careers page, a posting by title, salary data, background on
# an employer. This module is the egress for that, living in careeragent-fetch
# so the search API key stays here (careeragent-api stays credential-less) and
# outbound HTTP stays in the one box.
#
# SSRF note (important): unlike /fetch — which hands a MODEL-CHOSEN url to the
# SSRF guard (src/ssrf.py) so it can't hit internal/metadata endpoints — search
# calls a FIXED, hard-coded provider host (api.tavily.com) with the query as a
# body param. There is no user-controlled destination, so the private-IP SSRF
# block deliberately does NOT apply here. (A future self-hosted SearXNG would
# sit on a private IP the guard would wrongly block — another reason /search is
# its own path.) Adding a provider = adding one hard-coded host here.
#
# Fail-clean: every error raises SearchProblem(status_code, detail) so the API
# maps it 1:1 onto an HTTP status the coach can react to — never an exception
# that escapes.
# ============================================================================

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx

logger = logging.getLogger("careeragent-fetch")

# The ONLY hosts this box will talk to for search — hard-coded, never model-input.
TAVILY_ENDPOINT = "https://api.tavily.com/search"

_MAX_QUERY_CHARS = 400
_DEFAULT_MAX_RESULTS = 5
_MIN_RESULTS, _MAX_RESULTS = 1, 10


class SearchProblem(Exception):
    """A typed search failure whose ``status_code`` maps onto the HTTP response.
    503 = not configured; 400 = bad query; 502 = provider error/unreachable."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str
    score: float = 0.0


@dataclass
class SearchOutcome:
    results: List[SearchHit] = field(default_factory=list)
    answer: Optional[str] = None
    provider: str = ""


def _clamp_results(n: Any) -> int:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_RESULTS
    return max(_MIN_RESULTS, min(_MAX_RESULTS, n))


# --------------------------------------------------------------------- Tavily
def parse_tavily(body: Any) -> SearchOutcome:
    """Pure, network-free parse of a Tavily /search JSON body → normalized outcome.
    Tolerant of missing/typed-wrong fields (drops a result with no url)."""
    results: List[SearchHit] = []
    if isinstance(body, dict):
        rows = body.get("results")
        for r in (rows if isinstance(rows, list) else []):  # a non-list 'results' -> no crash
            if not isinstance(r, dict):
                continue
            url = str(r.get("url") or "").strip()
            if not url:
                continue
            raw_score = r.get("score")
            try:
                score = float(raw_score) if isinstance(raw_score, (int, float)) else 0.0
            except (ValueError, OverflowError):  # a pathological huge int score
                score = 0.0
            results.append(SearchHit(
                title=str(r.get("title") or "").strip(),
                url=url,
                snippet=str(r.get("content") or "").strip(),
                score=score,
            ))
    answer = body.get("answer") if isinstance(body, dict) else None
    return SearchOutcome(results=results,
                         answer=(str(answer).strip() or None) if answer else None,
                         provider="tavily")


async def _tavily_search(query: str, max_results: int, api_key: str,
                         timeout: float, include_answer: bool) -> SearchOutcome:
    if not api_key:
        raise SearchProblem(503, "web search is not configured (no TAVILY_API_KEY).")
    payload = {
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
        "include_answer": include_answer,
    }
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=timeout, write=5.0, pool=5.0)
        ) as client:
            resp = await client.post(
                TAVILY_ENDPOINT, json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError as err:
        raise SearchProblem(502, f"search provider unreachable: {type(err).__name__}")
    if resp.status_code in (401, 403):
        raise SearchProblem(502, "search provider rejected the API key.")
    if resp.status_code == 429:
        raise SearchProblem(502, "search provider rate limit reached — try again shortly.")
    if resp.status_code != 200:
        raise SearchProblem(502, f"search provider error (status {resp.status_code}).")
    try:
        body = resp.json()
    except Exception:
        raise SearchProblem(502, "search provider returned an unparseable response.")
    try:
        return parse_tavily(body)
    except Exception:  # belt-and-suspenders: any unexpected shape → clean 502, never a 500
        raise SearchProblem(502, "search provider returned an unexpected response shape.")


# name -> provider coroutine. Add a provider = add one hard-coded host + parser here.
_PROVIDERS: Dict[str, Callable[..., Awaitable[SearchOutcome]]] = {
    "tavily": _tavily_search,
}


async def run_search(
    query: str,
    *,
    provider: str,
    api_key: str,
    max_results: Any = _DEFAULT_MAX_RESULTS,
    timeout: float = 12.0,
    include_answer: bool = True,
) -> SearchOutcome:
    """Run one web search via the configured provider. Raises SearchProblem on any
    failure (never returns a partial/ambiguous result)."""
    q = (query or "").strip()
    if not q:
        raise SearchProblem(400, "a non-empty 'query' is required.")
    if len(q) > _MAX_QUERY_CHARS:
        q = q[:_MAX_QUERY_CHARS]
    fn = _PROVIDERS.get((provider or "tavily").strip().lower())
    if fn is None:
        raise SearchProblem(503, f"unknown search provider '{provider}'.")
    return await fn(q, _clamp_results(max_results), api_key, timeout, include_answer)
