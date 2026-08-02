"""Tests for careeragent-memory's inbound HMAC verification (src/security.py).

These pin GOLDEN signature/canonicalisation literals that are shared with the
careeragent-api emitter's test suite, proving the two sides serialise and sign the
same bytes (byte-for-byte agreement across the api -> memory boundary).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import security


# --------------------------------------------------------------------------- #
# Fixed inputs (shared with the emitter test suite)
# --------------------------------------------------------------------------- #

SECRET = "memory-test-hmac-secret"
REQUEST_ID = "11111111-1111-1111-1111-111111111111"
CLIENT_TS_STR = "2026-06-14T01:19:45.003236+00:00"
CLIENT_TS_DT = datetime(2026, 6, 14, 1, 19, 45, 3236, tzinfo=timezone.utc)
SOURCE_SERVICE = "careeragent-api"
SESSION_ID = "sess-abc"

INGEST_PAYLOAD = {"role": "user", "content": "hello world"}
RETRIEVE_PAYLOAD = {"query": "what did we discuss", "top_k": 5}

# Golden literals — assert exact equality.
GOLDEN_INGEST_JSON = '{"content":"hello world","role":"user"}'
GOLDEN_RETRIEVE_JSON = '{"query":"what did we discuss","top_k":5}'
GOLDEN_INGEST_SIG = "ccea6b606162938797bc0dfc92311bcb66343892e31ebd33b749d763a475c397"
GOLDEN_RETRIEVE_SIG = "7b706b096810b6a4dfc7b94283f7afa1f3b1d5e7623b4deb420bd10f10657b39"


@pytest.fixture(autouse=True)
def _configure_hmac():
    """Install the fixed HMAC secret for every test in this module."""
    security.configure_hmac(SECRET)
    yield


# --------------------------------------------------------------------------- #
# canonical_payload_json — golden bytes
# --------------------------------------------------------------------------- #

def test_canonical_payload_json_ingest_golden():
    assert security.canonical_payload_json(INGEST_PAYLOAD) == GOLDEN_INGEST_JSON


def test_canonical_payload_json_retrieve_golden():
    assert security.canonical_payload_json(RETRIEVE_PAYLOAD) == GOLDEN_RETRIEVE_JSON


# --------------------------------------------------------------------------- #
# compute_signature — golden signatures (byte-for-byte agreement w/ emitter)
# --------------------------------------------------------------------------- #

def test_compute_signature_ingest_golden():
    sig = security.compute_signature(
        REQUEST_ID, CLIENT_TS_STR, "ingest", SOURCE_SERVICE, SESSION_ID, INGEST_PAYLOAD
    )
    assert sig == GOLDEN_INGEST_SIG


def test_compute_signature_retrieve_golden():
    sig = security.compute_signature(
        REQUEST_ID, CLIENT_TS_STR, "retrieve", SOURCE_SERVICE, SESSION_ID, RETRIEVE_PAYLOAD
    )
    assert sig == GOLDEN_RETRIEVE_SIG


# --------------------------------------------------------------------------- #
# canonical_timestamp
# --------------------------------------------------------------------------- #

def test_canonical_timestamp_aware_utc():
    assert security.canonical_timestamp(CLIENT_TS_DT) == CLIENT_TS_STR


def test_canonical_timestamp_naive_treated_as_utc():
    naive = datetime(2026, 6, 14, 1, 19, 45, 3236)
    assert security.canonical_timestamp(naive) == CLIENT_TS_STR


def test_canonical_timestamp_non_utc_offset_normalizes():
    # +02:00 means 01:19:45 UTC is 03:19:45 local; canonicalising back to UTC
    # must yield the same +00:00 string as the aware-UTC datetime.
    other_tz = timezone(timedelta(hours=2))
    local = datetime(2026, 6, 14, 3, 19, 45, 3236, tzinfo=other_tz)
    out = security.canonical_timestamp(local)
    assert out == CLIENT_TS_STR
    assert out.endswith("+00:00")


# --------------------------------------------------------------------------- #
# verify_request — happy path (fresh timestamp)
# --------------------------------------------------------------------------- #

def _sign_fresh(operation, payload, now):
    iso = security.canonical_timestamp(now)
    return security.compute_signature(
        REQUEST_ID, iso, operation, SOURCE_SERVICE, SESSION_ID, payload
    )


def test_verify_request_fresh_ingest_ok():
    now = datetime.now(timezone.utc)
    sig = _sign_fresh("ingest", INGEST_PAYLOAD, now)
    ok, err = security.verify_request(
        "ingest", REQUEST_ID, now, SOURCE_SERVICE, SESSION_ID, INGEST_PAYLOAD, sig
    )
    assert (ok, err) == (True, "")


def test_verify_request_fresh_retrieve_ok():
    now = datetime.now(timezone.utc)
    sig = _sign_fresh("retrieve", RETRIEVE_PAYLOAD, now)
    ok, err = security.verify_request(
        "retrieve", REQUEST_ID, now, SOURCE_SERVICE, SESSION_ID, RETRIEVE_PAYLOAD, sig
    )
    assert (ok, err) == (True, "")


# --------------------------------------------------------------------------- #
# verify_request — tamper detection (signature mismatch)
# --------------------------------------------------------------------------- #

def test_verify_request_tampered_content_fails():
    now = datetime.now(timezone.utc)
    sig = _sign_fresh("ingest", INGEST_PAYLOAD, now)
    tampered = {"role": "user", "content": "goodbye world"}
    ok, err = security.verify_request(
        "ingest", REQUEST_ID, now, SOURCE_SERVICE, SESSION_ID, tampered, sig
    )
    assert ok is False
    assert err == "Invalid HMAC signature"


def test_verify_request_tampered_operation_fails():
    now = datetime.now(timezone.utc)
    sig = _sign_fresh("ingest", INGEST_PAYLOAD, now)
    ok, err = security.verify_request(
        "retrieve", REQUEST_ID, now, SOURCE_SERVICE, SESSION_ID, INGEST_PAYLOAD, sig
    )
    assert ok is False
    assert err == "Invalid HMAC signature"


def test_verify_request_tampered_session_id_fails():
    now = datetime.now(timezone.utc)
    sig = _sign_fresh("ingest", INGEST_PAYLOAD, now)
    ok, err = security.verify_request(
        "ingest", REQUEST_ID, now, SOURCE_SERVICE, "sess-OTHER", INGEST_PAYLOAD, sig
    )
    assert ok is False
    assert err == "Invalid HMAC signature"


def test_verify_request_tampered_source_service_fails():
    now = datetime.now(timezone.utc)
    sig = _sign_fresh("ingest", INGEST_PAYLOAD, now)
    ok, err = security.verify_request(
        "ingest", REQUEST_ID, now, "evil-service", SESSION_ID, INGEST_PAYLOAD, sig
    )
    assert ok is False
    assert err == "Invalid HMAC signature"


# --------------------------------------------------------------------------- #
# verify_replay_window
# --------------------------------------------------------------------------- #

def test_verify_replay_window_accepts_now():
    ok, err = security.verify_replay_window(datetime.now(timezone.utc))
    assert (ok, err) == (True, "")


def test_verify_replay_window_rejects_past():
    stale = datetime.now(timezone.utc) - timedelta(seconds=10000)
    ok, err = security.verify_replay_window(stale)
    assert ok is False
    assert "replay window" in err


def test_verify_replay_window_rejects_future():
    future = datetime.now(timezone.utc) + timedelta(seconds=10000)
    ok, err = security.verify_replay_window(future)
    assert ok is False
    assert "replay window" in err


def test_verify_request_stale_but_correctly_signed_fails_on_window():
    stale = datetime.now(timezone.utc) - timedelta(seconds=10000)
    sig = _sign_fresh("ingest", INGEST_PAYLOAD, stale)  # correctly signed for that ts
    ok, err = security.verify_request(
        "ingest", REQUEST_ID, stale, SOURCE_SERVICE, SESSION_ID, INGEST_PAYLOAD, sig
    )
    assert ok is False
    assert "replay window" in err  # fails on the cheap replay check, before HMAC


# --------------------------------------------------------------------------- #
# enforce
# --------------------------------------------------------------------------- #

def test_enforce_raises_401_on_bad_signature():
    now = datetime.now(timezone.utc)
    with pytest.raises(HTTPException) as exc_info:
        security.enforce(
            "ingest", REQUEST_ID, now, SOURCE_SERVICE, SESSION_ID, INGEST_PAYLOAD,
            "deadbeef" * 8,  # 64 hex chars, wrong
        )
    assert exc_info.value.status_code == 401


def test_enforce_returns_none_on_valid_fresh_request():
    now = datetime.now(timezone.utc)
    sig = _sign_fresh("ingest", INGEST_PAYLOAD, now)
    result = security.enforce(
        "ingest", REQUEST_ID, now, SOURCE_SERVICE, SESSION_ID, INGEST_PAYLOAD, sig
    )
    assert result is None
