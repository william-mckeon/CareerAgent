#!/usr/bin/env python3
# ============================================================================
# careeragent-code - FastAPI app (the read-only code workspace, port 8012)
# ============================================================================
#
# Gives the coach a REAL local checkout of the user's repos so a deep code review
# works off actual files instead of the github-MCP's 6 KB-capped API straw (see
# careeragent-api/specs/0016-deep-code-review.md). Clone-on-demand into a cache
# volume; read-only grep/file/tree/list over the result. Holds the read-only
# GitHub PAT so careeragent-api stays credential-less. NEVER executes cloned code.
#
# Endpoints (X-API-Key: CODE_API_KEY, except /health):
#   POST /sync   {repo}                  → clone/refresh; {repo,head_sha,files,bytes,cached}
#   POST /grep   {repo,pattern,glob?}    → ripgrep the repo
#   GET  /file   ?repo=&path=            → one file's text (size-capped, traversal-safe)
#   GET  /tree   ?repo=                  → the file tree
#   GET  /list                           → cached repos
#   POST /refresh {limit?}               → nightly warm: discover + clone/pull owner repos (Slice E)
#   GET  /health                         → {status, service}
# ============================================================================

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, Security
from fastapi.responses import JSONResponse

load_dotenv()

from safety import CodeProblem  # noqa: E402
from schemas import (  # noqa: E402
    FileResponse, GrepRequest, GrepResponse, RefreshRequest, RefreshResponse,
    RepoInfo, SyncRequest, SyncResponse, TreeResponse,
)
from security import verify_api_key  # noqa: E402
from workspace import Workspace  # noqa: E402

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("careeragent-code")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


class Config:
    CODE_API_KEY = os.environ.get("CODE_API_KEY", "").strip()
    GITHUB_PAT = os.environ.get("GITHUB_PAT", "").strip()  # read-only; stays in this box
    CACHE_DIR = os.environ.get("CODE_CACHE_DIR", "/cache").strip()
    PORT = os.environ.get("CODE_PORT", "8012")
    GIT_TIMEOUT = float(_int_env("CODE_GIT_TIMEOUT", 120))
    RG_TIMEOUT = float(_int_env("CODE_RG_TIMEOUT", 20))
    MAX_FILE_BYTES = _int_env("CODE_MAX_FILE_BYTES", 400_000)
    MAX_TREE_ENTRIES = _int_env("CODE_MAX_TREE_ENTRIES", 4000)
    MAX_GREP_MATCHES = _int_env("CODE_MAX_GREP_MATCHES", 200)
    MAX_CACHE_BYTES = _int_env("CODE_MAX_CACHE_BYTES", 2_000_000_000)
    MAX_REPO_BYTES = _int_env("CODE_MAX_REPO_BYTES", 500_000_000)
    # Slice E — nightly warm bounds. REFRESH_BUDGET_BYTES 0 → default to 85% of the
    # cache cap in Workspace (so a warm never evicts what it just warmed).
    REFRESH_MAX_REPOS = _int_env("CODE_REFRESH_MAX_REPOS", 20)
    REFRESH_BUDGET_BYTES = _int_env("CODE_REFRESH_BUDGET_BYTES", 0)


config = Config()
workspace: Optional[Workspace] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global workspace
    workspace = Workspace(
        config.CACHE_DIR, config.GITHUB_PAT or None,
        git_timeout=config.GIT_TIMEOUT, rg_timeout=config.RG_TIMEOUT,
        max_file_bytes=config.MAX_FILE_BYTES, max_tree_entries=config.MAX_TREE_ENTRIES,
        max_grep_matches=config.MAX_GREP_MATCHES, max_cache_bytes=config.MAX_CACHE_BYTES,
        max_repo_bytes=config.MAX_REPO_BYTES,
        max_refresh_repos=config.REFRESH_MAX_REPOS,
        refresh_budget_bytes=config.REFRESH_BUDGET_BYTES or None,
    )
    logger.info("=" * 60)
    logger.info("careeragent-code — the read-only code workspace")
    logger.info("cache dir      : %s", config.CACHE_DIR)
    logger.info("GitHub PAT     : %s", "set (read-only)" if config.GITHUB_PAT else "NOT set (public repos only)")
    logger.info("caps           : file=%d tree=%d grep=%d cache=%d",
                config.MAX_FILE_BYTES, config.MAX_TREE_ENTRIES,
                config.MAX_GREP_MATCHES, config.MAX_CACHE_BYTES)
    logger.info("refresh bounds : max_repos=%d budget_bytes=%s",
                config.REFRESH_MAX_REPOS,
                config.REFRESH_BUDGET_BYTES or "cache cap − per-repo cap")
    if not config.CODE_API_KEY:
        logger.warning("CODE_API_KEY is not set — the endpoints will 503.")
    logger.info("careeragent-code ready on :%s", config.PORT)
    logger.info("=" * 60)
    yield


app = FastAPI(title="careeragent-code", version="1.0.0", lifespan=lifespan)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    logger.error("unhandled error on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


def _ws() -> Workspace:
    if workspace is None:
        raise HTTPException(status_code=503, detail="workspace not available")
    return workspace


@app.post("/sync", response_model=SyncResponse)
async def sync(body: SyncRequest, api_key: str = Security(verify_api_key)):
    try:
        return SyncResponse(**_ws().sync(body.repo))
    except CodeProblem as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@app.post("/grep", response_model=GrepResponse)
async def grep(body: GrepRequest, api_key: str = Security(verify_api_key)):
    try:
        return GrepResponse(**_ws().grep(body.repo, body.pattern, body.glob))
    except CodeProblem as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@app.get("/file", response_model=FileResponse)
async def file(repo: str = Query(...), path: str = Query(...),
               api_key: str = Security(verify_api_key)):
    try:
        return FileResponse(**_ws().read_file(repo, path))
    except CodeProblem as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@app.get("/tree", response_model=TreeResponse)
async def tree(repo: str = Query(...), api_key: str = Security(verify_api_key)):
    try:
        return TreeResponse(**_ws().tree(repo))
    except CodeProblem as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@app.get("/list", response_model=List[RepoInfo])
async def list_repos(api_key: str = Security(verify_api_key)):
    return [RepoInfo(**r) for r in _ws().list_repos()]


@app.post("/refresh", response_model=RefreshResponse)
async def refresh(body: RefreshRequest, api_key: str = Security(verify_api_key)):
    """Nightly warm (Slice E): discover the user's owner repos and clone/pull each,
    bounded + fail-soft. Separate from the on-demand /sync path, which is untouched.
    A discovery failure (no token / GitHub unreachable) → 4xx/5xx the caller retries;
    a single bad repo is counted in `errors` and the sweep continues."""
    try:
        return RefreshResponse(**_ws().refresh(body.limit))
    except CodeProblem as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "careeragent-code"}
