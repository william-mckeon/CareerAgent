#!/usr/bin/env python3
# ============================================================================
# careeragent-ats - inbound auth (X-API-Key: ATS_API_KEY)
# ============================================================================

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException


def _expected_key() -> str:
    """Read the key at CALL time, not import time. The API module calls
    load_dotenv() AFTER importing this module, so a value that lives only in
    .env (a non-Docker dev run) isn't in os.environ yet when this module is
    imported — reading it per request picks it up. `.strip()` matches the
    api's Config normalization so a trailing newline in .env can't cause a
    silent 401."""
    return os.environ.get("ATS_API_KEY", "").strip()


async def verify_api_key(x_api_key: str = Header(None, alias="X-API-Key")) -> str:
    """Constant-time X-API-Key check. The only inbound client is careeragent-api."""
    expected = _expected_key()
    if not expected:
        raise HTTPException(status_code=503, detail="ATS_API_KEY is not configured")
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="invalid or missing API key")
    return x_api_key
