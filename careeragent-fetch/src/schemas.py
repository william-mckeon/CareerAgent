#!/usr/bin/env python3
# ============================================================================
# careeragent-fetch - request/response schemas
# ============================================================================

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


class FetchRequest(BaseModel):
    """Body for POST /fetch — a single user-controlled URL to fetch.

    The URL is validated for SSRF *before* any socket is opened (see src/ssrf.py);
    this model only carries the string across the wire.
    """
    url: str


class FetchResponse(BaseModel):
    """POST /fetch success (200) — clean text + provenance.

    - text: extracted main-content text (HTML→text) or the raw body (text/plain),
      capped at MAX_TEXT_CHARS.
    - truncated: true if the text was cut at the char cap.
    - final_url: the URL actually fetched, after following (revalidated) redirects.
    - title: the page <title>, or null when none was found.
    """
    text: str
    truncated: bool
    final_url: str
    title: Optional[str] = None


class ExtractResponse(BaseModel):
    """POST /extract success (200) — text pulled from an uploaded resume.

    - text: extracted text, capped at MAX_TEXT_CHARS.
    - truncated: true if the text was cut at the char cap.
    - format: the sniffed document kind — "pdf" or "docx" (by magic bytes, not name).
    - chars: len(text) after any cap (a convenience for the caller).
    """
    text: str
    truncated: bool
    format: str
    chars: int


class SearchRequest(BaseModel):
    """Body for POST /search — a web-search query (the coach's own words).

    Unlike /fetch there is no user-controlled URL: the destination is a fixed,
    hard-coded provider host, so this never touches the SSRF guard.
    """
    query: str
    max_results: Optional[int] = None


class SearchResult(BaseModel):
    """One surfaced result — a LEAD, not a fetched page."""
    title: str
    url: str
    snippet: str
    score: float = 0.0


class SearchResponse(BaseModel):
    """POST /search success (200) — surfaced results + an optional provider answer.

    - query:   the query that was run.
    - provider: which backend answered (e.g. "tavily").
    - results: the surfaced {title, url, snippet, score} leads.
    - answer:  the provider's short synthesized answer, if any (unverified).
    """
    query: str
    provider: str
    results: List[SearchResult]
    answer: Optional[str] = None
