#!/usr/bin/env python3
# ============================================================================
# careeragent-ats - FastAPI app (deterministic keyword-coverage scorer, port 8010)
# ============================================================================
#
# A pure, stateless scoring box. NO model, NO database, NO network egress. Given
# a resume's text and a job description, it reports how many of the JD's
# important keywords the resume covers. It holds NONE of the user's data.
#
# One job:
#   POST /ats-score — {resume_text, job_description} -> {score, coverage, matched, missing}
#
# The scoring logic lives in src/ats.py (pure, unit-testable functions). This
# module is just the HTTP surface + inbound auth + input validation.
#
# Endpoints (X-API-Key: ATS_API_KEY, except /health):
#   POST /ats-score -> AtsResponse | 400 (empty job_description)
#   GET  /health    -> {status, service}
# ============================================================================

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Security
from starlette.concurrency import run_in_threadpool

# load_dotenv BEFORE importing security — security reads ATS_API_KEY, and a
# non-Docker (.env-only) run needs os.environ populated first. (security also
# re-reads at call time as a belt-and-suspenders.)
load_dotenv()

from ats import score_resume  # noqa: E402
from schemas import AtsRequest, AtsResponse  # noqa: E402
from security import verify_api_key  # noqa: E402

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("careeragent-ats")


class Config:
    ATS_API_KEY = os.environ.get("ATS_API_KEY", "").strip()


config = Config()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 60)
    logger.info("careeragent-ats starting — the deterministic keyword-coverage scorer")
    logger.info("no model, no database, no network egress — pure text analysis")
    if not config.ATS_API_KEY:
        logger.warning("ATS_API_KEY is not set — POST /ats-score will 503.")
    logger.info("careeragent-ats ready.")
    logger.info("=" * 60)
    yield


app = FastAPI(title="careeragent-ats", version="1.0.0", lifespan=lifespan)


@app.post("/ats-score", response_model=AtsResponse)
async def ats_score(body: AtsRequest, api_key: str = Security(verify_api_key)):
    # An empty/whitespace JD has nothing to score against — reject it rather than
    # returning a misleading 0/0 "perfect-or-zero" result.
    if not body.job_description or not body.job_description.strip():
        raise HTTPException(
            status_code=400, detail="job_description is required to score against."
        )
    # An empty resume_text is allowed (score 0, everything missing).
    # score_resume is synchronous + CPU-bound — run it OFF the event loop so a
    # large score can't stall /health or other requests on this single worker.
    result = await run_in_threadpool(score_resume, body.resume_text, body.job_description)
    return AtsResponse(
        score=result.score,
        coverage=result.coverage,
        matched=result.matched,
        missing=result.missing,
    )


@app.get("/health")
async def health():
    return {"status": "ok", "service": "careeragent-ats"}
