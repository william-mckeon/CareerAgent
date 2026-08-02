"""
tests/test_api.py — inbound auth (verify_api_key) + the HTTP contract of
POST /ats-score. Hermetic: the key is set via the environment (verify_api_key
reads it at call time), no server, no network.
"""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import backend.api as api
import security


# --- inbound auth (mirrors the sibling services) -------------------------------
# verify_api_key reads ATS_API_KEY from the environment at call time (so a
# .env-only run still authenticates), hence the tests set the env var.
async def test_rejects_missing_key(monkeypatch):
    monkeypatch.setenv("ATS_API_KEY", "secret")
    with pytest.raises(HTTPException) as e:
        await security.verify_api_key(None)
    assert e.value.status_code == 401


async def test_rejects_wrong_key(monkeypatch):
    monkeypatch.setenv("ATS_API_KEY", "secret")
    with pytest.raises(HTTPException) as e:
        await security.verify_api_key("nope")
    assert e.value.status_code == 401


async def test_accepts_matching_key(monkeypatch):
    monkeypatch.setenv("ATS_API_KEY", "secret")
    assert await security.verify_api_key("secret") == "secret"


async def test_accepts_key_with_surrounding_whitespace(monkeypatch):
    # A trailing newline in .env must not cause a silent 401 (Config.strip parity).
    monkeypatch.setenv("ATS_API_KEY", "secret\n")
    assert await security.verify_api_key("secret") == "secret"


async def test_503_when_unconfigured(monkeypatch):
    monkeypatch.delenv("ATS_API_KEY", raising=False)
    with pytest.raises(HTTPException) as e:
        await security.verify_api_key("anything")
    assert e.value.status_code == 503


# --- the HTTP contract of POST /ats-score --------------------------------------
def test_empty_job_description_returns_400(monkeypatch):
    monkeypatch.setenv("ATS_API_KEY", "k")
    with TestClient(api.app) as client:
        resp = client.post(
            "/ats-score",
            headers={"X-API-Key": "k"},
            json={"resume_text": "Python engineer", "job_description": "   "},
        )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "job_description is required to score against."


def test_missing_key_on_endpoint_401(monkeypatch):
    monkeypatch.setenv("ATS_API_KEY", "k")
    with TestClient(api.app) as client:
        resp = client.post(
            "/ats-score",
            json={"resume_text": "x", "job_description": "Python required"},
        )
    assert resp.status_code == 401


def test_ats_score_success_shape(monkeypatch):
    monkeypatch.setenv("ATS_API_KEY", "k")
    with TestClient(api.app) as client:
        resp = client.post(
            "/ats-score",
            headers={"X-API-Key": "k"},
            json={
                "resume_text": "Python developer skilled in Django and Docker.",
                "job_description": (
                    "Seeking a Python developer with Django, Docker, and "
                    "Kubernetes experience."
                ),
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    # exact response contract the careeragent-api client is written to.
    assert set(body.keys()) == {"score", "coverage", "matched", "missing"}
    assert isinstance(body["score"], int) and 0 <= body["score"] <= 100
    assert isinstance(body["coverage"], str) and "/" in body["coverage"]
    assert isinstance(body["matched"], list)
    assert isinstance(body["missing"], list)
    assert "python" in body["matched"]
    assert "kubernetes" in body["missing"]
    # coverage string agrees with the lists
    assert body["coverage"] == f"{len(body['matched'])}/{len(body['matched']) + len(body['missing'])}"


def test_empty_resume_scores_zero_over_http(monkeypatch):
    monkeypatch.setenv("ATS_API_KEY", "k")
    with TestClient(api.app) as client:
        resp = client.post(
            "/ats-score",
            headers={"X-API-Key": "k"},
            json={"resume_text": "", "job_description": "Python, Docker, AWS."},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["score"] == 0
    assert body["matched"] == []


def test_oversize_resume_is_rejected_422(monkeypatch):
    # A multi-MB body would burn CPU past the api's read timeout — the schema cap
    # rejects it cleanly (422) instead of scoring it.
    monkeypatch.setenv("ATS_API_KEY", "k")
    with TestClient(api.app) as client:
        resp = client.post(
            "/ats-score",
            headers={"X-API-Key": "k"},
            json={"resume_text": "x" * 200_001, "job_description": "Python required"},
        )
    assert resp.status_code == 422


def test_health_is_unauthenticated():
    with TestClient(api.app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "careeragent-ats"}
