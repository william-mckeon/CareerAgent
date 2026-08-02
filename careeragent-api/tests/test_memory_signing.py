# ============================================================================
# careeragent-api - Memory HMAC signing contract tests
# Maintainer: William McKeon
# ============================================================================
#
# PURPOSE:
#   Pin the EMITTER side of the api -> memory HMAC canonical-string contract so
#   it can never silently drift from careeragent-memory's verifier
#   (careeragent-memory/src/security.py).
#
#   The fixed inputs and pinned literals below are IDENTICAL to
#   careeragent-memory's own test suite. If both test files agree on the pinned
#   literals, the two sides are provably aligned: the api emitter produces
#   exactly the canonical string the memory verifier reconstructs. If anyone
#   edits the canonical-string layout, the json.dumps options, the None-field
#   sentinel, or the timestamp format on either side, one of these literal
#   assertions fails loudly instead of producing a runtime 401 ("Invalid HMAC
#   signature").
#
#   The module-level helpers are pure, and _signed_body is pure config (no
#   I/O happens until start()), so every test runs WITHOUT network.
# ============================================================================

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from client.memory import (
    MemoryClient,
    _canonical_payload_json,
    _canonical_string,
    _canonical_timestamp,
    _fmt_envelope_field,
    _payload_hash,
    _sign,
)

# ----------------------------------------------------------------------------
# FIXED INPUTS — must stay identical to careeragent-memory's test suite.
# ----------------------------------------------------------------------------
SECRET = "memory-test-hmac-secret"
REQUEST_ID = "11111111-1111-1111-1111-111111111111"
TS_ISO = "2026-06-14T01:19:45.003236+00:00"
SOURCE_SERVICE = "careeragent-api"
SESSION_ID = "sess-abc"

INGEST_PAYLOAD = {"role": "user", "content": "hello world"}
RETRIEVE_PAYLOAD = {"query": "what did we discuss", "top_k": 5}

# ----------------------------------------------------------------------------
# PINNED LITERALS — the cross-repo contract, frozen.
# ----------------------------------------------------------------------------
PINNED_INGEST_PAYLOAD_JSON = '{"content":"hello world","role":"user"}'
PINNED_RETRIEVE_PAYLOAD_JSON = '{"query":"what did we discuss","top_k":5}'

PINNED_INGEST_CANONICAL_STRING = (
    "11111111-1111-1111-1111-111111111111"
    "|2026-06-14T01:19:45.003236+00:00"
    "|ingest"
    "|careeragent-api"
    "|sess-abc"
    "|03f4c19b586148b9b2f0b6870ef6e97296b6013a416daa720630accf502651d3"
)

PINNED_INGEST_SIGNATURE = (
    "ccea6b606162938797bc0dfc92311bcb66343892e31ebd33b749d763a475c397"
)
PINNED_RETRIEVE_SIGNATURE = (
    "7b706b096810b6a4dfc7b94283f7afa1f3b1d5e7623b4deb420bd10f10657b39"
)


# ============================================================================
# CANONICAL PAYLOAD JSON
# ============================================================================

def test_ingest_canonical_payload_json_matches_pinned_literal():
    """The ingest payload JSON is frozen byte-for-byte (sorted, compact)."""
    assert _canonical_payload_json(INGEST_PAYLOAD) == PINNED_INGEST_PAYLOAD_JSON


def test_retrieve_canonical_payload_json_matches_pinned_literal():
    """The retrieve payload JSON is frozen byte-for-byte (sorted, compact)."""
    assert _canonical_payload_json(RETRIEVE_PAYLOAD) == PINNED_RETRIEVE_PAYLOAD_JSON


def test_canonical_payload_json_is_sorted_and_compact():
    """Key order is deterministic and there is no insignificant whitespace."""
    out = _canonical_payload_json({"role": "user", "content": "hello world"})
    # sort_keys=True => "content" precedes "role" regardless of insert order.
    assert out == PINNED_INGEST_PAYLOAD_JSON
    assert ", " not in out
    assert ": " not in out


# ============================================================================
# PAYLOAD HASH
# ============================================================================

def test_payload_hash_is_sha256_of_canonical_json():
    expected = hashlib.sha256(
        PINNED_INGEST_PAYLOAD_JSON.encode("utf-8")
    ).hexdigest()
    assert _payload_hash(INGEST_PAYLOAD) == expected


# ============================================================================
# CANONICAL STRING — the 6-field contract
# ============================================================================

def test_ingest_canonical_string_matches_pinned_literal():
    """Full ingest canonical string is frozen byte-for-byte vs memory."""
    cs = _canonical_string(
        REQUEST_ID, TS_ISO, "ingest", SOURCE_SERVICE, SESSION_ID, INGEST_PAYLOAD
    )
    assert cs == PINNED_INGEST_CANONICAL_STRING


def test_canonical_string_has_exactly_six_pipe_fields():
    cs = _canonical_string(
        REQUEST_ID, TS_ISO, "ingest", SOURCE_SERVICE, SESSION_ID, INGEST_PAYLOAD
    )
    fields = cs.split("|")
    assert len(fields) == 6
    assert fields[0] == REQUEST_ID
    assert fields[1] == TS_ISO
    assert fields[2] == "ingest"
    assert fields[3] == SOURCE_SERVICE
    assert fields[4] == SESSION_ID
    assert fields[5] == _payload_hash(INGEST_PAYLOAD)


def test_fmt_envelope_field_none_renders_empty_string():
    assert _fmt_envelope_field(None) == ""
    assert _fmt_envelope_field("x") == "x"


def test_canonical_string_none_session_renders_empty():
    """None session_id serialises as an empty field, not "None"."""
    cs = _canonical_string(
        REQUEST_ID, TS_ISO, "ingest", SOURCE_SERVICE, None, INGEST_PAYLOAD
    )
    fields = cs.split("|")
    assert len(fields) == 6
    assert fields[4] == ""
    assert "None" not in fields


# ============================================================================
# SIGNATURE — pinned golden literals (the cross-repo contract)
# ============================================================================

def test_ingest_signature_matches_pinned_golden():
    sig = _sign(SECRET, REQUEST_ID, TS_ISO, "ingest", SOURCE_SERVICE, SESSION_ID, INGEST_PAYLOAD)
    assert sig == PINNED_INGEST_SIGNATURE


def test_retrieve_signature_matches_pinned_golden():
    sig = _sign(SECRET, REQUEST_ID, TS_ISO, "retrieve", SOURCE_SERVICE, SESSION_ID, RETRIEVE_PAYLOAD)
    assert sig == PINNED_RETRIEVE_SIGNATURE


def test_signature_is_64_hex_lowercase():
    sig = _sign(SECRET, REQUEST_ID, TS_ISO, "ingest", SOURCE_SERVICE, SESSION_ID, INGEST_PAYLOAD)
    assert len(sig) == 64
    assert sig == sig.lower()
    assert all(c in "0123456789abcdef" for c in sig)


def test_signature_is_deterministic():
    a = _sign(SECRET, REQUEST_ID, TS_ISO, "ingest", SOURCE_SERVICE, SESSION_ID, INGEST_PAYLOAD)
    b = _sign(SECRET, REQUEST_ID, TS_ISO, "ingest", SOURCE_SERVICE, SESSION_ID, INGEST_PAYLOAD)
    assert a == b


def test_signature_changes_when_secret_changes():
    base = _sign(SECRET, REQUEST_ID, TS_ISO, "ingest", SOURCE_SERVICE, SESSION_ID, INGEST_PAYLOAD)
    other = _sign(SECRET + "-x", REQUEST_ID, TS_ISO, "ingest", SOURCE_SERVICE, SESSION_ID, INGEST_PAYLOAD)
    assert base != other


# ============================================================================
# CANONICAL TIMESTAMP
# ============================================================================

def test_canonical_timestamp_uses_plus_offset_not_zulu():
    dt = datetime(2026, 6, 14, 1, 19, 45, 3236, tzinfo=timezone.utc)
    out = _canonical_timestamp(dt)
    assert out.endswith("+00:00")
    assert not out.endswith("Z")
    assert "Z" not in out


def test_canonical_timestamp_includes_six_digit_microseconds():
    dt = datetime(2026, 6, 14, 1, 19, 45, 3236, tzinfo=timezone.utc)
    out = _canonical_timestamp(dt)
    frac = out.split("+")[0].split(".")[1]
    assert len(frac) == 6
    assert out == "2026-06-14T01:19:45.003236+00:00"


def test_canonical_timestamp_naive_assumed_utc():
    naive = datetime(2026, 6, 14, 1, 19, 45, 3236)
    out = _canonical_timestamp(naive)
    assert out == "2026-06-14T01:19:45.003236+00:00"


def test_canonical_timestamp_normalises_non_utc_to_utc():
    eastern = timezone(timedelta(hours=-4))
    dt = datetime(2026, 6, 13, 21, 19, 45, 3236, tzinfo=eastern)
    out = _canonical_timestamp(dt)
    assert out == "2026-06-14T01:19:45.003236+00:00"


# ============================================================================
# MEMORYCLIENT CONSTRUCTOR VALIDATION
# ============================================================================

def test_constructor_raises_on_empty_hmac_secret():
    with pytest.raises(ValueError):
        MemoryClient(url="http://memory:8004", api_key="k", hmac_secret="")


def test_constructor_raises_on_empty_url():
    with pytest.raises(ValueError):
        MemoryClient(url="", api_key="k", hmac_secret=SECRET)


def test_constructor_raises_on_empty_api_key():
    with pytest.raises(ValueError):
        MemoryClient(url="http://memory:8004", api_key="", hmac_secret=SECRET)


# ============================================================================
# _signed_body — wire body shape + round-trip self-consistency
# ============================================================================

def _make_client():
    # source_service default is "careeragent-api"; pure config, no I/O.
    return MemoryClient(
        url="http://memory:8004",
        api_key="k",
        hmac_secret=SECRET,
    )


def test_signed_body_ingest_has_expected_keys():
    client = _make_client()
    body = client._signed_body("ingest", "sess-abc", {"role": "user", "content": "hi"})
    for key in (
        "request_id",
        "client_timestamp",
        "source_service",
        "hmac_signature",
        "session_id",
        "role",
        "content",
    ):
        assert key in body, f"missing key {key!r} in signed body"
    assert body["source_service"] == "careeragent-api"
    assert body["session_id"] == "sess-abc"
    assert body["role"] == "user"
    assert body["content"] == "hi"


def test_signed_body_ingest_signature_round_trips():
    """The hmac_signature in the body recomputes from the fields pulled back
    out of that same body — proving self-consistency of the emitter."""
    client = _make_client()
    payload = {"role": "user", "content": "hi"}
    body = client._signed_body("ingest", "sess-abc", payload)

    recomputed = _sign(
        SECRET,
        body["request_id"],
        body["client_timestamp"],
        "ingest",
        body["source_service"],
        body["session_id"],
        {"role": body["role"], "content": body["content"]},
    )
    assert recomputed == body["hmac_signature"]


def test_signed_body_retrieve_includes_top_k_when_present():
    client = _make_client()
    payload = {"query": "what did we discuss", "top_k": 5}
    body = client._signed_body("retrieve", "sess-abc", payload)

    assert body["query"] == "what did we discuss"
    assert body["top_k"] == 5

    recomputed = _sign(
        SECRET,
        body["request_id"],
        body["client_timestamp"],
        "retrieve",
        body["source_service"],
        body["session_id"],
        {"query": body["query"], "top_k": body["top_k"]},
    )
    assert recomputed == body["hmac_signature"]


def test_signed_body_retrieve_omits_top_k_when_absent():
    client = _make_client()
    payload = {"query": "what did we discuss"}
    body = client._signed_body("retrieve", "sess-abc", payload)

    assert "top_k" not in body
    assert body["query"] == "what did we discuss"

    recomputed = _sign(
        SECRET,
        body["request_id"],
        body["client_timestamp"],
        "retrieve",
        body["source_service"],
        body["session_id"],
        {"query": body["query"]},
    )
    assert recomputed == body["hmac_signature"]
