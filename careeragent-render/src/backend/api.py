#!/usr/bin/env python3
# ============================================================================
# careeragent-render - FastAPI app (the résumé document renderer, port 8009)
# ============================================================================
#
# A pure, stateless rendering box. NO model, NO database, NO network egress.
# Given a résumé's markdown text and a target format, it returns the rendered
# document bytes (base64-encoded). It holds NONE of the user's data — the caller
# (careeragent-api) persists the bytes (e.g. via careeragent-dossier).
#
# One job:
#   POST /render — {resume, format, title?} -> {content_b64, format, bytes, filename}
#
# The rendering logic lives in src/render.py (pure, unit-testable functions).
# This module is just the HTTP surface + inbound auth + input validation.
#
# Endpoints (X-API-Key: RENDER_API_KEY, except /health):
#   POST /render -> RenderResponse | 400 (empty resume / bad format) | 413 (oversize)
#   GET  /health -> {status, service}
# ============================================================================

from __future__ import annotations

import asyncio
import base64
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Security

# load_dotenv BEFORE importing security — security reads RENDER_API_KEY, and a
# non-Docker (.env-only) run needs os.environ populated first. (security also
# re-reads at call time as a belt-and-suspenders.)
load_dotenv()

from render import SUPPORTED_FORMATS, render  # noqa: E402
from schemas import RenderRequest, RenderResponse  # noqa: E402
from security import verify_api_key  # noqa: E402

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("careeragent-render")


class Config:
    RENDER_API_KEY = os.environ.get("RENDER_API_KEY", "").strip()

    # Input size cap: the max résumé markdown accepted, measured on the ACTUAL
    # UTF-8 byte length (never a client-declared length). A giant blob is
    # rejected with 413 BEFORE any parsing/layout, so it can't blow up memory.
    # NOTE: this bounds MEMORY, not CPU — reportlab layout is ~O(n^2) PER BLOCK,
    # so render.py ALSO caps per-block size (_MAX_BLOCK_CHARS). RENDER_TIMEOUT is
    # a defense-in-depth backstop on wall-clock (below).
    MAX_RESUME_BYTES = int(os.environ.get("MAX_RESUME_BYTES", "200000"))  # ~200 KB

    # Hard wall-clock deadline for a single render, as a backstop against any
    # unforeseen pathological layout. Kept BELOW careeragent-api's 30s read timeout
    # so the caller gets a clean 503 instead of a client-side ReadTimeout.
    RENDER_TIMEOUT_SECONDS = float(os.environ.get("RENDER_TIMEOUT_SECONDS", "25"))


config = Config()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 60)
    logger.info("careeragent-render starting — the résumé document renderer")
    logger.info("no model, no database, no network egress — markdown in, bytes out")
    logger.info("input cap: max_resume_bytes=%d", config.MAX_RESUME_BYTES)
    if not config.RENDER_API_KEY:
        logger.warning("RENDER_API_KEY is not set — POST /render will 503.")
    logger.info("careeragent-render ready.")
    logger.info("=" * 60)
    yield


app = FastAPI(title="careeragent-render", version="1.0.0", lifespan=lifespan)


@app.post("/render", response_model=RenderResponse)
async def render_endpoint(body: RenderRequest, api_key: str = Security(verify_api_key)):
    # 1) An empty/whitespace résumé has nothing to render.
    if not body.resume or not body.resume.strip():
        raise HTTPException(status_code=400, detail="resume text is required to render.")

    # 2) Only pdf/docx are supported (case-insensitive).
    fmt = (body.format or "").strip().lower()
    if fmt not in SUPPORTED_FORMATS:
        raise HTTPException(status_code=400, detail="format must be 'pdf' or 'docx'.")

    # 3) Enforce the input size cap on the ACTUAL bytes — reject a giant blob
    #    before spending any memory/CPU on layout.
    if len(body.resume.encode("utf-8")) > config.MAX_RESUME_BYTES:
        raise HTTPException(status_code=413, detail="resume too large to render.")

    # Render OFF the event loop (asyncio.to_thread): reportlab/python-docx are
    # synchronous CPU work, so keep them from blocking /health + other requests.
    # A hard wall-clock deadline backstops any unforeseen slow layout (render.py's
    # per-block cap is the primary bound) so a single request can't hang the caller.
    try:
        data = await asyncio.wait_for(
            asyncio.to_thread(render, body.resume, fmt, body.title),
            timeout=config.RENDER_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=503,
            detail="rendering timed out — the résumé is too large or complex to render.",
        )
    except ValueError as exc:
        # render() re-validates defensively; map any bad-input ValueError to 400.
        raise HTTPException(status_code=400, detail=str(exc))

    return RenderResponse(
        content_b64=base64.b64encode(data).decode("ascii"),
        format=fmt,
        bytes=len(data),
        filename=_suggest_filename(fmt, body.title),
    )


def _suggest_filename(fmt: str, title):
    # Kept in sync with render._filename; re-derived here so the response's
    # filename is correct even though render() returns only bytes.
    from render import _filename
    return _filename(fmt, title)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "careeragent-render"}
