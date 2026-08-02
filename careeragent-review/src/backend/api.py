#!/usr/bin/env python3
# ============================================================================
# careeragent-review - FastAPI app (the repo-review harness, port 8007)
# ============================================================================
#
# One real endpoint: POST /review-batch — review a set of the user's GitHub
# repos (explicit list, or enumerated via the GitHub MCP), fan out one bounded
# subagent per repo (parallel), and upsert each structured result into
# careeragent-dossier's projects library. Idempotent per repo+commit.
#
# Outbound boundaries (all over the shared careeragent-network, secrets in .env):
#   - careeragent-infra  /complete   (the model gateway for each subagent)
#   - careeragent-github-mcp  :8082/mcp  (read-only repo access, PAT-less)
#   - careeragent-dossier  /projects (the write target)
#
# Endpoints (X-API-Key: REVIEW_API_KEY, except /health):
#   POST /review-batch   -> ReviewBatchResponse
#   GET  /health         -> {status, infra, github_mcp, dossier}
# ============================================================================

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Security

from client.dossier import DossierClient
from client.infra import InfraClient
from client.mcp_client import MCPClient
from harness.orchestrator import Orchestrator
from schemas import ReviewBatchResponse, ReviewRequest
from security import verify_api_key

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("careeragent-review")


class Config:
    REVIEW_API_KEY = os.environ.get("REVIEW_API_KEY", "").strip()

    INFRA_URL = os.environ.get("INFRA_URL", "").strip().rstrip("/")
    INFRA_API_KEY = os.environ.get("INFRA_API_KEY", "").strip()

    GITHUB_MCP_URL = os.environ.get("GITHUB_MCP_URL", "").strip()

    DOSSIER_URL = os.environ.get("DOSSIER_URL", "").strip().rstrip("/")
    DOSSIER_API_KEY = os.environ.get("DOSSIER_API_KEY", "").strip()

    # Harness knobs (env-tunable).
    MAX_REPOS = int(os.environ.get("MAX_REPOS", "12"))
    REVIEW_CONCURRENCY = int(os.environ.get("REVIEW_CONCURRENCY", "4"))
    PER_REPO_MAX_STEPS = int(os.environ.get("PER_REPO_MAX_STEPS", "12"))
    REVIEW_MODEL = os.environ.get("REVIEW_MODEL", "base").strip()      # infra route
    REVIEW_EFFORT = os.environ.get("REVIEW_EFFORT", "low").strip()


config = Config()

infra_client: Optional[InfraClient] = None
mcp_client: Optional[MCPClient] = None
dossier_client: Optional[DossierClient] = None
orchestrator: Optional[Orchestrator] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global infra_client, mcp_client, dossier_client, orchestrator
    logger.info("=" * 60)
    logger.info("careeragent-review starting — the repo-review harness")

    # infra (model gateway) — required
    infra_client = InfraClient(url=config.INFRA_URL, api_key=config.INFRA_API_KEY)
    await infra_client.start()

    # dossier (write target) — required
    dossier_client = DossierClient(url=config.DOSSIER_URL, api_key=config.DOSSIER_API_KEY)
    await dossier_client.start()

    # GitHub MCP (read source) — fail-soft: a GitHub outage shouldn't crash boot;
    # /review-batch will just return per-repo errors until it recovers.
    mcp_client = MCPClient(url=config.GITHUB_MCP_URL, token=None, server_name="github", read_only=True)
    try:
        await mcp_client.start()
        logger.info("GitHub MCP connected (tools=%d)", len(mcp_client.schemas()))
    except Exception as err:
        logger.warning("GitHub MCP unavailable (%s: %s) — reviews will error until it recovers",
                       type(err).__name__, err)

    orchestrator = Orchestrator(
        infra=infra_client, mcp=mcp_client, dossier=dossier_client,
        max_repos=config.MAX_REPOS, concurrency=config.REVIEW_CONCURRENCY,
        per_repo_max_steps=config.PER_REPO_MAX_STEPS,
        review_model=config.REVIEW_MODEL, review_effort=config.REVIEW_EFFORT,
    )
    logger.info(
        "Harness ready (max_repos=%d, concurrency=%d, per_repo_max_steps=%d, model=%s, effort=%s)",
        config.MAX_REPOS, config.REVIEW_CONCURRENCY, config.PER_REPO_MAX_STEPS,
        config.REVIEW_MODEL, config.REVIEW_EFFORT,
    )
    logger.info("careeragent-review ready.")
    logger.info("=" * 60)
    yield

    for c in (mcp_client, dossier_client, infra_client):
        if c is not None:
            try:
                await c.stop()
            except Exception as err:
                logger.warning("stop error: %s: %s", type(err).__name__, err)


app = FastAPI(title="careeragent-review", version="1.0.0", lifespan=lifespan)


@app.post("/review-batch", response_model=ReviewBatchResponse)
async def review_batch(body: ReviewRequest, api_key: str = Security(verify_api_key)):
    return await orchestrator.review_batch(body)


@app.get("/health")
async def health():
    infra_ok = await infra_client.healthy() if infra_client else False
    dossier_ok = await dossier_client.healthy() if dossier_client else False
    mcp_ok = bool(mcp_client and mcp_client.started)
    return {
        "status": "ok" if (infra_ok and mcp_ok and dossier_ok) else "degraded",
        "infra": "ok" if infra_ok else "unreachable",
        "github_mcp": "ok" if mcp_ok else "unreachable",
        "dossier": "ok" if dossier_ok else "unreachable",
    }
