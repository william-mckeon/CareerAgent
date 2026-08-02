"""
src/security.py

Inbound authentication for careeragent-jobs.

Every endpoint except GET /health requires a valid X-API-Key header, validated
against JOBS_API_KEY (the careeragent-api <-> jobs boundary secret). The
comparison is constant-time so a timing side-channel cannot recover the key.

The key is read at CALL time (not import time) so a rotated JOBS_API_KEY takes
effect on the next request without a restart, and so tests can set it before the
first call. This is the SAME pattern every CareerAgent service uses; it is
isolated here so it can later move from a single shared key to a per-caller
lookup without touching the endpoint logic.
"""
import os
import secrets

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(key: str = Security(_api_key_header)) -> str:
    """FastAPI dependency: 401 if the X-API-Key header is missing or wrong."""
    expected = os.environ.get("JOBS_API_KEY", "")
    if not key or not secrets.compare_digest(key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return key
