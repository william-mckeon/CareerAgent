"""tests/test_security.py — inbound X-API-Key auth (pure, no I/O)."""
import pytest
from fastapi import HTTPException

from security import verify_api_key  # noqa: E402

from .conftest import TEST_API_KEY


class TestVerifyApiKey:
    @pytest.mark.asyncio
    async def test_correct_key_passes(self):
        assert await verify_api_key(key=TEST_API_KEY) == TEST_API_KEY

    @pytest.mark.asyncio
    async def test_wrong_key_raises_401(self):
        with pytest.raises(HTTPException) as exc:
            await verify_api_key(key="nope")
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_empty_key_raises_401(self):
        with pytest.raises(HTTPException) as exc:
            await verify_api_key(key="")
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_none_key_raises_401(self):
        with pytest.raises(HTTPException) as exc:
            await verify_api_key(key=None)
        assert exc.value.status_code == 401
