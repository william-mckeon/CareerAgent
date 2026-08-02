# ============================================================================
# careeragent-api - HMAC signing contract tests
# Maintainer: William McKeon
# ============================================================================
#
# PURPOSE:
#   Pin the EMITTER side of the cross-repo HMAC canonical-string contract so
#   it can never silently drift from careeragent-logger's verifier
#   (careeragent-logger/src/security.py).
#
#   The fixed inputs below are IDENTICAL to the logger's own test suite. If the
#   two test files agree on the pinned literals, the two sides are provably
#   aligned: the emitter produces exactly the canonical string the verifier
#   reconstructs. If anyone edits the canonical-string layout, the json.dumps
#   options, the None-field sentinel, or the timestamp format on either side,
#   one of these literal assertions fails loudly instead of producing a runtime
#   401 ("Invalid HMAC signature").
#
#   These helpers are pure / module-level, so every test runs WITHOUT network.
# ============================================================================

import hashlib
from datetime import datetime, timezone

from client.logger import (
    _canonical_payload_json,
    _canonical_string,
    _canonical_timestamp,
    _fmt_envelope_field,
    _payload_hash,
    _sign,
)

# ----------------------------------------------------------------------------
# FIXED INPUTS — must stay identical to careeragent-logger's test suite.
# ----------------------------------------------------------------------------
REQUEST_ID = "11111111-1111-1111-1111-111111111111"
CLIENT_TIMESTAMP = "2026-06-14T01:19:45.003236+00:00"
EVENT_TYPE = "ops_event"
SOURCE_SERVICE = "careeragent-api"
SESSION_ID = "sess-abc"
USER_ID = "user-xyz"
PAYLOAD = {"action": "test", "outcome": "ok"}

SECRET = "test-hmac-secret"

# ----------------------------------------------------------------------------
# PINNED LITERALS — the cross-repo contract, frozen.
# (a) canonical_payload_json output for PAYLOAD
# (b) the full 7-field canonical string
# ----------------------------------------------------------------------------
PINNED_CANONICAL_PAYLOAD_JSON = '{"action":"test","outcome":"ok"}'

PINNED_PAYLOAD_HASH = (
    "c813d39ea439fd8205574b6d5779dd4ab00b85cf617bb381f2e8cd463cf54959"
)

PINNED_CANONICAL_STRING = (
    "11111111-1111-1111-1111-111111111111"
    "|2026-06-14T01:19:45.003236+00:00"
    "|ops_event"
    "|careeragent-api"
    "|sess-abc"
    "|user-xyz"
    "|c813d39ea439fd8205574b6d5779dd4ab00b85cf617bb381f2e8cd463cf54959"
)


def _build_canonical():
    return _canonical_string(
        request_id=REQUEST_ID,
        client_timestamp_iso=CLIENT_TIMESTAMP,
        event_type=EVENT_TYPE,
        source_service=SOURCE_SERVICE,
        session_id=SESSION_ID,
        user_id=USER_ID,
        payload=PAYLOAD,
    )


def _build_signature(secret=SECRET, **overrides):
    kwargs = dict(
        secret=secret,
        request_id=REQUEST_ID,
        client_timestamp_iso=CLIENT_TIMESTAMP,
        event_type=EVENT_TYPE,
        source_service=SOURCE_SERVICE,
        session_id=SESSION_ID,
        user_id=USER_ID,
        payload=PAYLOAD,
    )
    kwargs.update(overrides)
    return _sign(**kwargs)


# ============================================================================
# CANONICAL PAYLOAD JSON
# ============================================================================

def test_canonical_payload_json_matches_pinned_literal():
    """The canonical JSON serialisation is frozen to a byte-for-byte literal."""
    assert _canonical_payload_json(PAYLOAD) == PINNED_CANONICAL_PAYLOAD_JSON


def test_canonical_payload_json_is_sorted_and_compact():
    """Key order is deterministic and there is no insignificant whitespace."""
    out = _canonical_payload_json({"outcome": "ok", "action": "test"})
    # sort_keys=True => "action" precedes "outcome" regardless of insert order.
    assert out == PINNED_CANONICAL_PAYLOAD_JSON
    assert ", " not in out
    assert ": " not in out


# ============================================================================
# PAYLOAD HASH
# ============================================================================

def test_payload_hash_is_sha256_of_canonical_json():
    expected = hashlib.sha256(
        PINNED_CANONICAL_PAYLOAD_JSON.encode("utf-8")
    ).hexdigest()
    assert _payload_hash(PAYLOAD) == expected
    assert _payload_hash(PAYLOAD) == PINNED_PAYLOAD_HASH


# ============================================================================
# CANONICAL STRING — the 7-field contract
# ============================================================================

def test_canonical_string_matches_pinned_literal():
    """Full canonical string is frozen byte-for-byte against the logger."""
    assert _build_canonical() == PINNED_CANONICAL_STRING


def test_canonical_string_has_exactly_seven_pipe_fields():
    fields = _build_canonical().split("|")
    assert len(fields) == 7


def test_canonical_string_field_order_and_payload_hash():
    """Fields appear in the documented order and the 7th is the payload hash."""
    fields = _build_canonical().split("|")
    assert fields[0] == REQUEST_ID
    assert fields[1] == CLIENT_TIMESTAMP
    assert fields[2] == EVENT_TYPE
    assert fields[3] == SOURCE_SERVICE
    assert fields[4] == SESSION_ID
    assert fields[5] == USER_ID
    assert fields[6] == _payload_hash(PAYLOAD)
    # And the 7th field is exactly sha256(canonical_payload_json(payload)).
    assert fields[6] == hashlib.sha256(
        _canonical_payload_json(PAYLOAD).encode("utf-8")
    ).hexdigest()


# ============================================================================
# NONE ATTRIBUTION FIELDS -> ""
# ============================================================================

def test_fmt_envelope_field_none_renders_empty_string():
    assert _fmt_envelope_field(None) == ""
    assert _fmt_envelope_field("x") == "x"


def test_canonical_string_none_session_and_user_render_empty():
    """None session_id/user_id serialise as empty fields, not "None"."""
    cs = _canonical_string(
        request_id=REQUEST_ID,
        client_timestamp_iso=CLIENT_TIMESTAMP,
        event_type=EVENT_TYPE,
        source_service=SOURCE_SERVICE,
        session_id=None,
        user_id=None,
        payload=PAYLOAD,
    )
    fields = cs.split("|")
    assert len(fields) == 7
    assert fields[4] == ""
    assert fields[5] == ""
    # Guard against the classic str(None) == "None" regression.
    assert "None" not in fields


# ============================================================================
# CANONICAL TIMESTAMP
# ============================================================================

def test_canonical_timestamp_uses_plus_offset_not_zulu():
    dt = datetime(2026, 6, 14, 1, 19, 45, 3236, tzinfo=timezone.utc)
    out = _canonical_timestamp(dt)
    assert out.endswith("+00:00")
    assert not out.endswith("Z")
    assert "Z" not in out


def test_canonical_timestamp_includes_microseconds():
    dt = datetime(2026, 6, 14, 1, 19, 45, 3236, tzinfo=timezone.utc)
    out = _canonical_timestamp(dt)
    # timespec="microseconds" => fractional second part has exactly 6 digits.
    frac = out.split("+")[0].split(".")[1]
    assert len(frac) == 6
    assert out == "2026-06-14T01:19:45.003236+00:00"


def test_canonical_timestamp_normalises_to_utc():
    """A non-UTC aware datetime is converted to a +00:00 UTC string."""
    from datetime import timedelta

    eastern = timezone(timedelta(hours=-4))
    dt = datetime(2026, 6, 13, 21, 19, 45, 3236, tzinfo=eastern)
    out = _canonical_timestamp(dt)
    assert out == "2026-06-14T01:19:45.003236+00:00"


def test_canonical_timestamp_naive_assumed_utc():
    naive = datetime(2026, 6, 14, 1, 19, 45, 3236)
    out = _canonical_timestamp(naive)
    assert out == "2026-06-14T01:19:45.003236+00:00"


# ============================================================================
# SIGNATURE DETERMINISM
# ============================================================================

def test_signature_is_deterministic_for_same_inputs_and_secret():
    sig1 = _build_signature()
    sig2 = _build_signature()
    assert sig1 == sig2
    assert len(sig1) == 64
    assert sig1 == sig1.lower()


def test_signature_changes_when_secret_changes():
    assert _build_signature(secret=SECRET) != _build_signature(
        secret=SECRET + "-different"
    )


def test_signature_changes_when_any_field_changes():
    base = _build_signature()
    assert _build_signature(request_id="22222222-2222-2222-2222-222222222222") != base
    assert _build_signature(client_timestamp_iso="2026-06-14T01:19:46.003236+00:00") != base
    assert _build_signature(event_type="conversation_capture") != base
    assert _build_signature(source_service="someone-else") != base
    assert _build_signature(session_id="sess-other") != base
    assert _build_signature(user_id="user-other") != base
    assert _build_signature(payload={"action": "test", "outcome": "no"}) != base


def test_signature_changes_when_none_vs_present_attribution():
    """None vs empty-string-equivalent must still be distinguishable upstream.

    None renders as "" in the canonical string, so a signature over
    session_id=None must differ from one over session_id="sess-abc".
    """
    sig_present = _build_signature(session_id=SESSION_ID)
    sig_none = _build_signature(session_id=None)
    assert sig_present != sig_none
