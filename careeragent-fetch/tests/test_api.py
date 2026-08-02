"""
tests/test_api.py — inbound auth (verify_api_key) + the API-layer upload size cap.
Hermetic: sets the key via the environment (verify_api_key reads it at call time),
no server, no network egress.
"""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import backend.api as api
import security


# --- inbound auth (mirrors the sibling services) -------------------------------
# verify_api_key reads FETCH_API_KEY from the environment at call time (so a
# .env-only run still authenticates — see the load_dotenv ordering fix), hence the
# tests set the env var rather than patching a module attribute.
async def test_rejects_missing_key(monkeypatch):
    monkeypatch.setenv("FETCH_API_KEY", "secret")
    with pytest.raises(HTTPException) as e:
        await security.verify_api_key(None)
    assert e.value.status_code == 401


async def test_rejects_wrong_key(monkeypatch):
    monkeypatch.setenv("FETCH_API_KEY", "secret")
    with pytest.raises(HTTPException) as e:
        await security.verify_api_key("nope")
    assert e.value.status_code == 401


async def test_accepts_matching_key(monkeypatch):
    monkeypatch.setenv("FETCH_API_KEY", "secret")
    assert await security.verify_api_key("secret") == "secret"


async def test_accepts_key_with_surrounding_whitespace(monkeypatch):
    # A trailing newline in .env must not cause a silent 401 (Config.strip parity).
    monkeypatch.setenv("FETCH_API_KEY", "secret\n")
    assert await security.verify_api_key("secret") == "secret"


async def test_503_when_unconfigured(monkeypatch):
    monkeypatch.delenv("FETCH_API_KEY", raising=False)
    with pytest.raises(HTTPException) as e:
        await security.verify_api_key("anything")
    assert e.value.status_code == 503


# --- the upload size cap is enforced on ACTUAL bytes read (413) -----------------
def test_extract_rejects_oversized_upload_413(monkeypatch):
    monkeypatch.setenv("FETCH_API_KEY", "k")
    monkeypatch.setattr(api.config, "MAX_UPLOAD_BYTES", 100)
    with TestClient(api.app) as client:
        big = b"%PDF-1.4" + b"0" * 500
        resp = client.post(
            "/extract",
            headers={"X-API-Key": "k"},
            files={"file": ("resume.pdf", big, "application/pdf")},
        )
    assert resp.status_code == 413


def test_health_is_unauthenticated():
    with TestClient(api.app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "careeragent-fetch"}


# --- POST /search (P7 web search) ----------------------------------------------
def test_search_requires_auth(monkeypatch):
    monkeypatch.setenv("FETCH_API_KEY", "k")
    with TestClient(api.app) as client:
        resp = client.post("/search", json={"query": "pm jobs"})   # no X-API-Key
    assert resp.status_code in (401, 403)


def test_search_503_when_provider_key_unset(monkeypatch):
    # No TAVILY_API_KEY -> the provider refuses before any network egress (503).
    monkeypatch.setenv("FETCH_API_KEY", "k")
    monkeypatch.setattr(api.config, "SEARCH_API_KEY", "")
    with TestClient(api.app) as client:
        resp = client.post("/search", headers={"X-API-Key": "k"}, json={"query": "pm jobs"})
    assert resp.status_code == 503


def test_search_400_on_empty_query(monkeypatch):
    monkeypatch.setenv("FETCH_API_KEY", "k")
    monkeypatch.setattr(api.config, "SEARCH_API_KEY", "key")
    with TestClient(api.app) as client:
        resp = client.post("/search", headers={"X-API-Key": "k"}, json={"query": "   "})
    assert resp.status_code == 400


def test_search_happy_path_shapes_response(monkeypatch):
    from search import SearchOutcome, SearchHit
    monkeypatch.setenv("FETCH_API_KEY", "k")
    monkeypatch.setattr(api.config, "SEARCH_API_KEY", "key")

    async def fake_run_search(query, *, provider, api_key, max_results, timeout):
        return SearchOutcome(
            results=[SearchHit("Acme PM", "https://acme.com/1", "we want PMs", 0.9)],
            answer="one role", provider="tavily")

    monkeypatch.setattr(api, "run_search", fake_run_search)
    with TestClient(api.app) as client:
        resp = client.post("/search", headers={"X-API-Key": "k"}, json={"query": "pm jobs"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "tavily"
    assert body["query"] == "pm jobs"
    assert body["results"][0]["url"] == "https://acme.com/1"
    assert body["answer"] == "one role"
