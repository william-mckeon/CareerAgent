#!/usr/bin/env python3
# ============================================================================
# careeragent-code - inbound auth (X-API-Key: CODE_API_KEY)
# ============================================================================
# The only inbound client is careeragent-api. Constant-time key check, read at
# call time (the api module load_dotenv()s after importing this) — mirrors the
# sibling services.
# ============================================================================

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException


def _expected_key() -> str:
    return os.environ.get("CODE_API_KEY", "").strip()


async def verify_api_key(x_api_key: str = Header(None, alias="X-API-Key")) -> str:
    expected = _expected_key()
    if not expected:
        raise HTTPException(status_code=503, detail="CODE_API_KEY is not configured")
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="invalid or missing API key")
    return x_api_key
