#!/usr/bin/env python3
# ============================================================================
# careeragent-fetch - FastAPI app (the egress + extraction box, port 8008)
# ============================================================================
#
# The system's FIRST server-side fetch of a user-controlled URL and its FIRST
# handler of untrusted uploaded files. A small, stateless box that isolates that
# blast radius away from the coach. It holds NONE of the user's data.
#
# Two jobs:
#   POST /fetch    — fetch a job-posting URL (SSRF-guarded) and return clean text.
#   POST /extract  — extract text from an uploaded PDF/DOCX resume.
#
# The SSRF control list lives in src/ssrf.py; the file-safety controls in
# src/extract.py. Both raise typed problems whose .status_code maps 1:1 onto the
# HTTP status in the contract (see specs/0001-fetch.md).
#
# Endpoints (X-API-Key: FETCH_API_KEY, except /health):
#   POST /fetch    -> FetchResponse    | 400 / 502 / 413 / 415
#   POST /extract  -> ExtractResponse  | 415 / 413 / 422 / 400
#   GET  /health   -> {status, service}
# ============================================================================

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Security, UploadFile

# load_dotenv BEFORE importing security — security reads FETCH_API_KEY, and a
# non-Docker (.env-only) run needs os.environ populated first. (security also
# re-reads at call time as a belt-and-suspenders.)
load_dotenv()

from extract import ExtractProblem, FileTooLarge  # noqa: E402
from runner import extract_isolated  # noqa: E402
from schemas import (  # noqa: E402
    ExtractResponse, FetchRequest, FetchResponse,
    SearchRequest, SearchResponse, SearchResult,
)
from search import SearchProblem, run_search  # noqa: E402
from security import verify_api_key  # noqa: E402
from ssrf import FetchProblem, fetch_url  # noqa: E402

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("careeragent-fetch")

_UPLOAD_CHUNK = 64 * 1024


class Config:
    FETCH_API_KEY = os.environ.get("FETCH_API_KEY", "").strip()

    # Fetch-side limits.
    MAX_FETCH_BYTES = int(os.environ.get("MAX_FETCH_BYTES", "2000000"))
    FETCH_TIMEOUT = float(os.environ.get("FETCH_TIMEOUT", "8"))
    MAX_REDIRECTS = int(os.environ.get("MAX_REDIRECTS", "5"))

    # Extract-side limits.
    MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", "10000000"))
    MAX_PDF_PAGES = int(os.environ.get("MAX_PDF_PAGES", "30"))
    # Isolation bounds for the untrusted parse (runner.py): wall-clock timeout and
    # the child's address-space ceiling. A decompression-bomb PDF hits one of these
    # in the isolated process instead of taking the API worker down.
    EXTRACT_TIMEOUT = float(os.environ.get("EXTRACT_TIMEOUT", "20"))
    MAX_EXTRACT_MEM_BYTES = int(os.environ.get("MAX_EXTRACT_MEM_BYTES", "1073741824"))

    # Shared text cap (both endpoints).
    MAX_TEXT_CHARS = int(os.environ.get("MAX_TEXT_CHARS", "100000"))

    # Web-search (POST /search). The provider API key lives ONLY here so
    # careeragent-api stays credential-less. TAVILY_API_KEY is the default name;
    # SEARCH_API_KEY is accepted as a provider-neutral fallback.
    SEARCH_PROVIDER = os.environ.get("SEARCH_PROVIDER", "tavily").strip().lower()
    SEARCH_API_KEY = (os.environ.get("TAVILY_API_KEY")
                      or os.environ.get("SEARCH_API_KEY") or "").strip()
    SEARCH_TIMEOUT = float(os.environ.get("SEARCH_TIMEOUT", "12"))
    SEARCH_MAX_RESULTS = int(os.environ.get("SEARCH_MAX_RESULTS", "5"))


config = Config()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 60)
    logger.info("careeragent-fetch starting — the egress + extraction box")
    logger.info(
        "fetch limits : max_bytes=%d timeout=%ss max_redirects=%d",
        config.MAX_FETCH_BYTES, config.FETCH_TIMEOUT, config.MAX_REDIRECTS,
    )
    logger.info(
        "extract limits: max_upload=%d max_pdf_pages=%d max_text_chars=%d",
        config.MAX_UPLOAD_BYTES, config.MAX_PDF_PAGES, config.MAX_TEXT_CHARS,
    )
    if not config.FETCH_API_KEY:
        logger.warning("FETCH_API_KEY is not set — the POST endpoints will 503.")
    logger.info("careeragent-fetch ready.")
    logger.info("=" * 60)
    yield


app = FastAPI(title="careeragent-fetch", version="1.0.0", lifespan=lifespan)


async def _read_capped(upload: UploadFile, max_bytes: int) -> bytes:
    """Read the whole upload into memory, aborting once it exceeds max_bytes.

    Enforces the cap on the ACTUAL bytes read — Content-Length is never trusted.
    """
    buf = bytearray()
    while True:
        chunk = await upload.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise FileTooLarge(f"upload exceeded {max_bytes} bytes")
    return bytes(buf)


@app.post("/fetch", response_model=FetchResponse)
async def fetch(body: FetchRequest, api_key: str = Security(verify_api_key)):
    try:
        result = await fetch_url(
            body.url,
            max_bytes=config.MAX_FETCH_BYTES,
            timeout=config.FETCH_TIMEOUT,
            max_redirects=config.MAX_REDIRECTS,
            max_text_chars=config.MAX_TEXT_CHARS,
        )
    except FetchProblem as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return FetchResponse(
        text=result.text,
        truncated=result.truncated,
        final_url=result.final_url,
        title=result.title,
    )


@app.post("/extract", response_model=ExtractResponse)
async def extract(
    file: UploadFile = File(...), api_key: str = Security(verify_api_key)
):
    try:
        data = await _read_capped(file, config.MAX_UPLOAD_BYTES)
        # Parse in an isolated, memory- and time-bounded process, OFF the event
        # loop (asyncio.to_thread), so a hostile file can neither freeze the worker
        # (blocking /health + concurrent requests) nor OOM-kill it.
        result = await asyncio.to_thread(
            extract_isolated,
            data,
            max_pdf_pages=config.MAX_PDF_PAGES,
            max_text_chars=config.MAX_TEXT_CHARS,
            timeout=config.EXTRACT_TIMEOUT,
            mem_bytes=config.MAX_EXTRACT_MEM_BYTES,
        )
    except ExtractProblem as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return ExtractResponse(
        text=result.text,
        truncated=result.truncated,
        format=result.format,
        chars=result.chars,
    )


@app.post("/search", response_model=SearchResponse)
async def search_endpoint(body: SearchRequest, api_key: str = Security(verify_api_key)):
    """Find pages by query via the configured provider (default Tavily). NO SSRF
    guard here — the destination is a fixed hard-coded provider host, not a
    model-chosen URL; the coach reads a surfaced result by handing its url to the
    SSRF-guarded /fetch. Errors map onto SearchProblem.status_code."""
    try:
        outcome = await run_search(
            body.query,
            provider=config.SEARCH_PROVIDER,
            api_key=config.SEARCH_API_KEY,
            max_results=body.max_results if body.max_results is not None else config.SEARCH_MAX_RESULTS,
            timeout=config.SEARCH_TIMEOUT,
        )
    except SearchProblem as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return SearchResponse(
        query=(body.query or "").strip(),
        provider=outcome.provider,
        results=[SearchResult(title=h.title, url=h.url, snippet=h.snippet, score=h.score)
                 for h in outcome.results],
        answer=outcome.answer,
    )


@app.get("/health")
async def health():
    return {"status": "ok", "service": "careeragent-fetch"}
