"""
tests/test_api.py — inbound auth (verify_api_key) + the HTTP contract of
POST /render. Hermetic: the key is set via the environment (verify_api_key reads
it at call time), no server, no network.
"""
import base64

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import backend.api as api
import security

PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"

RESUME = "# Grace Hopper\n\n## Skills\n- COBOL\n- Compilers\n"


# --- inbound auth (mirrors the sibling services) -------------------------------
# verify_api_key reads RENDER_API_KEY from the environment at call time (so a
# .env-only run still authenticates), hence the tests set the env var.
async def test_rejects_missing_key(monkeypatch):
    monkeypatch.setenv("RENDER_API_KEY", "secret")
    with pytest.raises(HTTPException) as e:
        await security.verify_api_key(None)
    assert e.value.status_code == 401


async def test_rejects_wrong_key(monkeypatch):
    monkeypatch.setenv("RENDER_API_KEY", "secret")
    with pytest.raises(HTTPException) as e:
        await security.verify_api_key("nope")
    assert e.value.status_code == 401


async def test_accepts_matching_key(monkeypatch):
    monkeypatch.setenv("RENDER_API_KEY", "secret")
    assert await security.verify_api_key("secret") == "secret"


async def test_accepts_key_with_surrounding_whitespace(monkeypatch):
    # A trailing newline in .env must not cause a silent 401 (Config.strip parity).
    monkeypatch.setenv("RENDER_API_KEY", "secret\n")
    assert await security.verify_api_key("secret") == "secret"


async def test_503_when_unconfigured(monkeypatch):
    monkeypatch.delenv("RENDER_API_KEY", raising=False)
    with pytest.raises(HTTPException) as e:
        await security.verify_api_key("anything")
    assert e.value.status_code == 503


# --- the HTTP contract of POST /render -----------------------------------------
def test_render_pdf_success_shape(monkeypatch):
    monkeypatch.setenv("RENDER_API_KEY", "k")
    with TestClient(api.app) as client:
        resp = client.post(
            "/render",
            headers={"X-API-Key": "k"},
            json={"resume": RESUME, "format": "pdf", "title": "Grace Hopper"},
        )
    assert resp.status_code == 200
    body = resp.json()
    # exact response contract the careeragent-api client is written to.
    assert set(body.keys()) == {"content_b64", "format", "bytes", "filename"}
    assert body["format"] == "pdf"
    assert isinstance(body["bytes"], int) and body["bytes"] > 0
    assert body["filename"] == "grace-hopper.pdf"
    decoded = base64.b64decode(body["content_b64"])
    # base64 decodes back to exactly `bytes` bytes, and it's a real PDF.
    assert len(decoded) == body["bytes"]
    assert decoded.startswith(PDF_MAGIC)


def test_render_docx_success_shape(monkeypatch):
    monkeypatch.setenv("RENDER_API_KEY", "k")
    with TestClient(api.app) as client:
        resp = client.post(
            "/render",
            headers={"X-API-Key": "k"},
            json={"resume": RESUME, "format": "docx"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["format"] == "docx"
    assert body["filename"] == "resume.docx"
    decoded = base64.b64decode(body["content_b64"])
    assert len(decoded) == body["bytes"]
    assert decoded.startswith(ZIP_MAGIC)  # docx is a zip


def test_empty_resume_returns_400(monkeypatch):
    monkeypatch.setenv("RENDER_API_KEY", "k")
    with TestClient(api.app) as client:
        resp = client.post(
            "/render",
            headers={"X-API-Key": "k"},
            json={"resume": "   ", "format": "pdf"},
        )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "resume text is required to render."


def test_bad_format_returns_400(monkeypatch):
    monkeypatch.setenv("RENDER_API_KEY", "k")
    with TestClient(api.app) as client:
        resp = client.post(
            "/render",
            headers={"X-API-Key": "k"},
            json={"resume": RESUME, "format": "rtf"},
        )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "format must be 'pdf' or 'docx'."


def test_oversize_resume_returns_413(monkeypatch):
    monkeypatch.setenv("RENDER_API_KEY", "k")
    monkeypatch.setattr(api.config, "MAX_RESUME_BYTES", 100)
    with TestClient(api.app) as client:
        resp = client.post(
            "/render",
            headers={"X-API-Key": "k"},
            json={"resume": "# " + ("x" * 500), "format": "pdf"},
        )
    assert resp.status_code == 413
    assert resp.json()["detail"] == "resume too large to render."


def test_metacharacters_render_over_http(monkeypatch):
    # `<`, `>`, `&` in the résumé must not break rendering over the wire.
    monkeypatch.setenv("RENDER_API_KEY", "k")
    with TestClient(api.app) as client:
        resp = client.post(
            "/render",
            headers={"X-API-Key": "k"},
            json={"resume": "# A & B <C>\n\n- built C++ & <legacy> >99% uptime\n",
                  "format": "pdf"},
        )
    assert resp.status_code == 200
    assert base64.b64decode(resp.json()["content_b64"]).startswith(PDF_MAGIC)


def test_missing_key_on_endpoint_401(monkeypatch):
    monkeypatch.setenv("RENDER_API_KEY", "k")
    with TestClient(api.app) as client:
        resp = client.post(
            "/render",
            json={"resume": RESUME, "format": "pdf"},
        )
    assert resp.status_code == 401


def test_health_is_unauthenticated():
    with TestClient(api.app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "careeragent-render"}
