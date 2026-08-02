"""
tests/test_security.py

The inbound X-API-Key gate. No network — verify_api_key is a pure coroutine over
the configured DOSSIER_API_KEY (set in conftest before import).
"""
import pytest
from fastapi import HTTPException

from security import verify_api_key


async def test_valid_key_passes(valid_api_key):
    assert await verify_api_key(valid_api_key) == valid_api_key


async def test_missing_key_rejected():
    with pytest.raises(HTTPException) as exc:
        await verify_api_key(None)
    assert exc.value.status_code == 401


async def test_wrong_key_rejected():
    with pytest.raises(HTTPException) as exc:
        await verify_api_key("not-the-key")
    assert exc.value.status_code == 401
