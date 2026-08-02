#!/usr/bin/env python3
# ============================================================================
# careeragent-review - inbound auth (X-API-Key: REVIEW_API_KEY)
# ============================================================================

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException

API_KEY = os.environ.get("REVIEW_API_KEY", "")


async def verify_api_key(x_api_key: str = Header(None, alias="X-API-Key")) -> str:
    """Constant-time X-API-Key check. The only inbound client is careeragent-api."""
    if not API_KEY:
        raise HTTPException(status_code=503, detail="REVIEW_API_KEY is not configured")
    if not x_api_key or not hmac.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="invalid or missing API key")
    return x_api_key
