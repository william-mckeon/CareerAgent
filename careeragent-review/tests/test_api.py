"""
tests/test_api.py — inbound auth (verify_api_key). Hermetic: patches the module
key, no server, no network.
"""
import pytest
from fastapi import HTTPException

import security


async def test_rejects_missing_key(monkeypatch):
    monkeypatch.setattr(security, "API_KEY", "secret")
    with pytest.raises(HTTPException) as e:
        await security.verify_api_key(None)
    assert e.value.status_code == 401


async def test_rejects_wrong_key(monkeypatch):
    monkeypatch.setattr(security, "API_KEY", "secret")
    with pytest.raises(HTTPException) as e:
        await security.verify_api_key("nope")
    assert e.value.status_code == 401


async def test_accepts_matching_key(monkeypatch):
    monkeypatch.setattr(security, "API_KEY", "secret")
    assert await security.verify_api_key("secret") == "secret"


async def test_503_when_unconfigured(monkeypatch):
    monkeypatch.setattr(security, "API_KEY", "")
    with pytest.raises(HTTPException) as e:
        await security.verify_api_key("anything")
    assert e.value.status_code == 503
