"""Inbound authentication for careeragent-memory.

TWO independent checks gate the api -> memory boundary, mirroring the
compartmentalized model careeragent-logger uses. Both must pass.

1. X-API-Key transport authentication (`MEMORY_API_KEY`)
   - Gates who may talk to careeragent-memory at all. Cheap header check at the
     door, constant-time compared.

2. HMAC-SHA256 payload integrity + replay protection (`MEMORY_HMAC_SECRET`)
   - The caller signs an envelope (request_id, client_timestamp, operation,
     source_service, session_id, payload_hash) with a SECOND secret. memory
     re-derives the canonical string from the parsed request and verifies the
     signature constant-time, then checks the timestamp against a freshness
     window to bound replay.

Why HMAC here now (this reverses an earlier "no HMAC" decision): the two secrets
are independent, so a leaked `MEMORY_API_KEY` alone can no longer forge or tamper
ingest/retrieve requests — an attacker would also need `MEMORY_HMAC_SECRET`. It
also makes the api -> memory boundary use the SAME signed-boundary discipline as
the api -> logger boundary, so the whole system has one integrity model rather
than two. The canonical string below MUST match
careeragent-api/src/client/memory.py byte-for-byte, exactly as the logger boundary
requires.

Compromise containment: a memory compromise exposes MEMORY_API_KEY,
MEMORY_HMAC_SECRET (this boundary) and INFRA_API_KEY (the outbound /embed
boundary). It cannot reach the frontend boundary, the logger, or the compute
provider directly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from fastapi import Header, HTTPException, status

logger = logging.getLogger("careeragent_memory.security")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Reject requests whose client_timestamp is outside this skew window vs server
# time. 5 minutes by default — tight enough to bound replay surface, loose
# enough to tolerate normal clock skew between careeragent-api and careeragent-memory.
REPLAY_WINDOW_SECONDS: int = int(
    os.environ.get("MEMORY_REPLAY_WINDOW_SECONDS", "300")
)


# --------------------------------------------------------------------------- #
# Transport auth (X-API-Key)
# --------------------------------------------------------------------------- #

class APIKeyValidator:
    """Validates the inbound X-API-Key header against the configured secret.

    The comparison is constant-time (`hmac.compare_digest`) so a timing side
    channel cannot be used to recover the key byte by byte.
    """

    def __init__(self, expected_key: str) -> None:
        if not expected_key:
            # Fail loudly at construction rather than silently accepting every caller.
            raise RuntimeError("MEMORY_API_KEY is required but was empty.")
        self._expected_key = expected_key

    def verify(self, provided_key: str | None) -> None:
        if provided_key is None or not hmac.compare_digest(provided_key, self._expected_key):
            logger.warning("Rejected request: missing or invalid X-API-Key")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key",
            )


# Populated once at application startup (see backend/api.py lifespan).
_validator: APIKeyValidator | None = None
_hmac_secret: str = ""


def configure(expected_key: str) -> None:
    """Install the process-wide transport-key validator. Called from lifespan."""
    global _validator
    _validator = APIKeyValidator(expected_key)


def configure_hmac(secret: str) -> None:
    """Install the process-wide HMAC secret. Called from lifespan.

    Fails loudly if empty — memory refuses to serve without the integrity
    secret, the same posture as the transport key.
    """
    global _hmac_secret
    if not secret:
        raise RuntimeError("MEMORY_HMAC_SECRET is required but was empty.")
    _hmac_secret = secret


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """FastAPI dependency: raises 401 unless a valid X-API-Key is present."""
    if _validator is None:  # pragma: no cover - misconfiguration guard
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Auth not configured",
        )
    _validator.verify(x_api_key)


# --------------------------------------------------------------------------- #
# HMAC signing primitives
#
# These MUST match careeragent-api/src/client/memory.py byte-for-byte so that a
# signature computed by the emitter verifies here. Mirrors the proven shape of
# careeragent-logger/src/security.py.
# --------------------------------------------------------------------------- #

def canonical_payload_json(payload: Dict[str, Any]) -> str:
    """Canonical JSON for the operation payload: keys sorted, no whitespace,
    ASCII-safe. Identical to the logger's contract so both repos serialise the
    same bytes before hashing."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def canonical_timestamp(dt: datetime) -> str:
    """Render a datetime as the canonical client_timestamp string: UTC,
    microsecond precision, '+00:00' offset (e.g. '2026-06-14T18:24:01.342000+00:00').

    We re-canonicalise the parsed timestamp before verifying rather than trusting
    `datetime.fromisoformat(s).isoformat() == s` to hold for every emitter.

    MUST match careeragent-api/src/client/memory.py byte-for-byte.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _fmt_envelope_field(value: Optional[Any]) -> str:
    """Render an optional signed envelope field. None -> empty string. MUST match
    the emitter's _fmt_envelope_field byte-for-byte."""
    return "" if value is None else str(value)


def compute_signature(
    request_id: str,
    client_timestamp: str,
    operation: str,
    source_service: str,
    session_id: Optional[str],
    payload: Dict[str, Any],
) -> str:
    """Compute the HMAC-SHA256 signature for a memory request.

    Canonical string (six pipe-separated fields):
        {request_id}|{client_timestamp}|{operation}
          |{source_service}|{session_id}|{sha256(payload_canonical)}

    operation is "ingest" or "retrieve". The attribution fields
    (source_service, session_id) are signed so they cannot be rewritten in
    transit. None renders as the empty string. The payload is the
    operation-specific subset (ingest: {role, content}; retrieve: {query}
    plus {top_k} when present).

    Returns a 64-character lowercase hex digest.
    """
    payload_canonical = canonical_payload_json(payload)
    payload_hash = hashlib.sha256(payload_canonical.encode("utf-8")).hexdigest()

    canonical_string = "|".join(
        (
            request_id,
            client_timestamp,
            operation,
            _fmt_envelope_field(source_service),
            _fmt_envelope_field(session_id),
            payload_hash,
        )
    )

    return hmac.new(
        _hmac_secret.encode("utf-8"),
        canonical_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_replay_window(client_timestamp: datetime) -> Tuple[bool, str]:
    """Reject requests whose client_timestamp falls outside the replay window.

    Bounds replay of a captured signed request and catches grossly skewed
    caller clocks. abs() so both past and future skew are rejected.
    """
    if client_timestamp.tzinfo is None:
        client_timestamp = client_timestamp.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    skew_seconds = abs((now - client_timestamp).total_seconds())

    if skew_seconds > REPLAY_WINDOW_SECONDS:
        return False, (
            f"client_timestamp outside replay window "
            f"(skew {skew_seconds:.0f}s > {REPLAY_WINDOW_SECONDS}s)"
        )
    return True, ""


def verify_request(
    operation: str,
    request_id: str,
    client_timestamp: datetime,
    source_service: str,
    session_id: Optional[str],
    payload: Dict[str, Any],
    signature: str,
) -> Tuple[bool, str]:
    """Run the full integrity check on an inbound memory request.

    Order matters: cheap replay-window check first, then the HMAC. Returns
    (True, "") on success, or (False, reason) on the first failing check.
    """
    if not _hmac_secret:
        logger.error("HMAC verification failed: MEMORY_HMAC_SECRET is not configured")
        return False, "Server configuration error: HMAC secret not set"

    ok, error = verify_replay_window(client_timestamp)
    if not ok:
        return False, error

    # Re-canonicalise the parsed timestamp to the exact form the emitter signed.
    iso_timestamp = canonical_timestamp(client_timestamp)
    expected = compute_signature(
        request_id=request_id,
        client_timestamp=iso_timestamp,
        operation=operation,
        source_service=source_service,
        session_id=session_id,
        payload=payload,
    )

    if not hmac.compare_digest(signature.lower(), expected.lower()):
        return False, "Invalid HMAC signature"

    return True, ""


def enforce(
    operation: str,
    request_id: str,
    client_timestamp: datetime,
    source_service: str,
    session_id: Optional[str],
    payload: Dict[str, Any],
    signature: str,
) -> None:
    """Verify a request and raise HTTP 401 on failure.

    The specific reason (replay skew, bad signature) is logged server-side
    only; the client gets a generic detail so verification internals are not
    leaked to a potential attacker.
    """
    ok, error = verify_request(
        operation=operation,
        request_id=request_id,
        client_timestamp=client_timestamp,
        source_service=source_service,
        session_id=session_id,
        payload=payload,
        signature=signature,
    )
    if not ok:
        logger.warning(
            "Rejected %s (request_id=%s, session=%s): %s",
            operation, request_id, session_id, error,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing signature",
        )
