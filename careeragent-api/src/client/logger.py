#!/usr/bin/env python3
# ============================================================================
# careeragent-api - Logger Client
# Maintainer: William McKeon
# Fire-and-forget event emission to careeragent-logger
# ============================================================================
#
# ROLE:
#   This file is the outbound HTTP client careeragent-api uses to emit events
#   to careeragent-logger (port 8003). It owns:
#     1. The HMAC-SHA256 canonical-string construction that signs every
#        event payload. The contract must match
#        careeragent-logger/src/security.py byte-for-byte — any drift causes
#        401 responses with confusing "Invalid HMAC signature" messages.
#     2. The in-memory asyncio.Queue that buffers events.
#     3. The background asyncio task that drains the queue and POSTs to
#        careeragent-logger.
#     4. The drop-oldest overflow policy when careeragent-logger is down or
#        slow and the queue fills.
#     5. The two public emit methods (emit_ops_event,
#        emit_conversation_capture) that backend/api.py calls.
#
# DESIGN: FIRE-AND-FORGET
#   The single most important property of this client is that emit calls
#   MUST NOT block the /chat request path. careeragent-api's responsibility is
#   to serve /chat; careeragent-logger's availability is non-essential to that
#   responsibility. So:
#     - emit methods are synchronous (they don't await anything).
#     - emit methods do not perform I/O. They construct the event, compute
#       the signature, and enqueue. Total work is microseconds.
#     - All HTTP I/O happens in a separate background task draining the
#       queue.
#     - If careeragent-logger is unreachable, the drain task's POST fails, the
#       event is logged-and-dropped, and the loop continues. The /chat
#       request handler never knows.
#     - If the queue fills (sustained careeragent-logger outage), the oldest
#       pending events are evicted to keep memory bounded. Newer events are
#       preferred over older ones on the assumption that fresher data is
#       more useful when the storm passes.
#
# INTEGRATION CONTRACT (must match careeragent-logger DATASHEET §2.1):
#
#   Every event is a JSON body POSTed to {LOGGER_URL}/events with:
#     Headers:
#       Content-Type: application/json
#       X-API-Key:    <LOGGER_API_KEY>
#     Body envelope (common to all event types):
#       event_type:       "ops_event" | "conversation_capture" | "audit_event"
#       request_id:       UUID (correlation ID, provided by caller)
#       source_service:   "careeragent-api" (this client's identifier)
#       client_timestamp: ISO-8601 UTC string, generated at emit time
#       session_id:       optional, string ≤64 or null
#       user_id:          optional, UUID or null
#       hmac_signature:   64-char lowercase hex, see below
#       payload:          object, per-event-type shape
#
#   The hmac_signature is HMAC-SHA256 over the canonical string:
#       {request_id}|{client_timestamp}|{event_type}
#         |{source_service}|{session_id}|{user_id}|{payload_hash}
#
#   The attribution fields (source_service, session_id, user_id) are signed
#   so they cannot be rewritten in transit; None session_id/user_id render as
#   the empty string. client_timestamp is the canonical UTC form produced by
#   _canonical_timestamp() (fixed microsecond precision, '+00:00' offset),
#   signed and sent identically so the receiver can re-canonicalise and match.
#
#   where payload_hash is hex(SHA256(canonical_payload_json)), and
#   canonical_payload_json is:
#
#       json.dumps(payload,
#                  sort_keys=True,
#                  separators=(",", ":"),
#                  default=str,
#                  ensure_ascii=False)
#
#   The four json.dumps options matter byte-for-byte:
#     - sort_keys=True       deterministic key ordering
#     - separators=(",", ":")  no whitespace between elements or k:v
#     - default=str          convert datetime/UUID to str()
#     - ensure_ascii=False   preserve unicode chars (UTF-8 bytes signed)
#
#   The HMAC key is LOGGER_HMAC_SECRET.encode("utf-8"). The signature is the
#   .hexdigest() result, lowercase, 64 chars.
#
#   careeragent-logger receives the JSON body, extracts the payload dict,
#   re-runs the same json.dumps + SHA-256 + canonical-string + HMAC
#   construction, and compares against hmac_signature. Mismatch → 401.
#
#   In addition, careeragent-logger enforces a replay-window check:
#   client_timestamp must be within LOGGER_REPLAY_WINDOW_SECONDS (default
#   300s) of careeragent-logger's server time. Outside the window → 401.
#
# WHEN TO EMIT (per careeragent-logger DATASHEET §6.1):
#
#   Standard careeragent-api ops_event actions:
#     "request_received"   — after auth passes, before any other work
#     "upstream_call"      — before opening the streaming POST to
#                            careeragent-infra
#     "upstream_error"     — on any careeragent-infra failure (network, 5xx,
#                            timeout)
#     "stream_complete"    — after the SSE stream closes cleanly
#
#   conversation_capture is emitted after stream_complete, with:
#     input_text       — the last user message (raw)
#     output_text      — the assembled visible answer (delta.content tokens
#                        concatenated; reasoning chain is NOT captured — see
#                        Note below)
#     input_hash       — SHA-256 of input_text (computed here)
#     output_hash      — SHA-256 of output_text (computed here)
#     model_used       — model identifier reported by upstream, if known
#     reasoning_effort — "low"|"medium"|"high" if the frontend specified one
#     latency_ms       — end-to-end /chat duration
#     input_tokens     — reported by upstream, if available
#     output_tokens    — reported by upstream, if available
#
# NOTE ON REASONING-CHAIN CAPTURE (current decision, revisitable):
#   output_text contains ONLY the visible answer (delta.content tokens), not
#   the reasoning chain (delta.reasoning tokens). Rationale:
#     - The reasoning chain is internal to the model and model-specific (the
#       model's reasoning format); future model swaps would change its shape
#       entirely.
#     - Captures record user-facing behavior (the visible reply), which is
#       what the observability and audit use cases care about.
#     - The careeragent-logger DATASHEET §2.1 says "Full model response" for
#       output_text; that phrasing is ambiguous between "everything the
#       model emitted" and "the model's user-visible reply." We're choosing
#       the latter interpretation for now.
#   If the reasoning chain is ever needed, the conversation_capture payload
#   schema can be extended with a new optional field in a coordinated
#   careeragent-logger + careeragent-api release. careeragent-api can begin
#   emitting the new field as soon as careeragent-logger accepts it.
#
# RULES — WHAT THIS FILE MUST NEVER DO:
#   ❌ Block the /chat request path on a logger call. emit methods are sync,
#      do no I/O, and return in microseconds.
#   ❌ Retry failed POSTs. careeragent-logger's design accepts that events can
#      be lost when it's unreachable; retries inside careeragent-api would just
#      pile up memory pressure. The drain loop logs the failure, calls
#      task_done(), and moves on.
#   ❌ Log LOGGER_API_KEY or LOGGER_HMAC_SECRET anywhere. They live in the
#      client instance's private attributes, are used internally for header
#      attachment and signing, and never appear in any log line.
#   ❌ Raise from emit methods. Any exception during event construction
#      (which should be impossible given the inputs are validated) is caught
#      and logged; we never propagate emit failures to the /chat handler.
#   ❌ Probe careeragent-logger for connectivity at startup. start() creates
#      the httpx client and launches the drain task without doing any
#      preflight call. If careeragent-logger is down at startup, the first few
#      drain attempts will fail and log warnings; no crash, no delayed
#      startup.
#   ❌ Drift from careeragent-logger's canonical-string contract. The four
#      json.dumps options above are non-negotiable. If careeragent-logger's
#      contract changes, this file must change in lockstep.
#
# RULES — WHAT THIS FILE MUST ALWAYS DO:
#   ✅ Attach X-API-Key: <LOGGER_API_KEY> to every outbound POST. The
#      httpx.AsyncClient is constructed once with this header so every call
#      inherits it automatically.
#   ✅ Compute hmac_signature using the exact canonical-string protocol
#      documented above. Every emit goes through _sign() before the event is
#      enqueued.
#   ✅ Generate client_timestamp at emit time (not enqueue time, not drain
#      time). The replay window is checked against this value, so it must
#      reflect when the event was constructed, not when it was eventually
#      transmitted.
#   ✅ Compute SHA-256 hashes of input_text and output_text inside this
#      module. The caller passes the texts; this module derives the hashes.
#      Centralizing the hash function ensures consistency.
#   ✅ Bound queue size and use drop-oldest on overflow. Without this, a
#      sustained careeragent-logger outage would grow the queue indefinitely
#      and exhaust memory.
#   ✅ Call task_done() for every event pulled from the queue, regardless of
#      whether the POST succeeded. This allows queue.join() to work during
#      graceful shutdown.
#   ✅ Drain the queue (best-effort, with timeout) on shutdown before closing
#      the httpx client. We don't want to lose pending events to a clean
#      shutdown.
#
# COMPARTMENTALIZED AUTH (per careeragent-api README §"Security Model"):
#   This client uses LOGGER_API_KEY for transport and LOGGER_HMAC_SECRET for
#   payload signing — two independent secrets at the same boundary, matching
#   careeragent-logger's two-layer auth model (DATASHEET §"Security model").
#   Neither value is shared with any other boundary:
#     - CAREERAGENT_API_KEY (inbound, frontend → careeragent-api) is NEVER
#       forwarded.
#     - INFRA_API_KEY (outbound, careeragent-api → careeragent-infra) is
#       unrelated.
#     - LOGGER_API_KEY / LOGGER_HMAC_SECRET live only on the careeragent-api
#       ↔ careeragent-logger boundary and never leak to other code paths.
# ============================================================================

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx


# Module-level logger. Uses the careeragent-api logging hierarchy so the format
# string set up in backend/api.py's setup_logging() applies here
# automatically. WARNINGs and above are visible by default; DEBUG output
# (drain loop lifecycle, per-event success) appears only when
# CAREERAGENT_LOG_LEVEL=DEBUG.
logger = logging.getLogger("careeragent-api.client.logger")


# ============================================================================
# CANONICAL STRING / HMAC SIGNING
# ============================================================================
# These four helpers implement the signing contract documented in the
# header. They are module-level rather than class methods because they are
# pure functions of their inputs (no instance state) and that makes them
# easier to test in isolation against careeragent-logger's known-good fixtures.

def _canonical_payload_json(payload: Dict[str, Any]) -> str:
    """
    Serialize a payload dict to its canonical JSON form.

    The four json.dumps options MUST match careeragent-logger/src/security.py
    byte-for-byte. Any drift causes HMAC verification to fail on the
    receiving side, surfaced as HTTP 401 with "Invalid HMAC signature".

    Returns:
        str: the canonical JSON serialisation of payload.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        ensure_ascii=False,
    )


def _payload_hash(payload: Dict[str, Any]) -> str:
    """
    SHA-256 of the canonical payload JSON, as a 64-char lowercase hex string.
    This is the fourth segment of the canonical string.
    """
    canonical = _canonical_payload_json(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _fmt_envelope_field(value: Optional[str]) -> str:
    """
    Render an optional envelope field for the canonical string.

    None is rendered as the empty string. This sentinel MUST match
    careeragent-logger/src/security.py byte-for-byte so signatures verify.
    """
    return "" if value is None else str(value)


def _canonical_string(
    request_id: str,
    client_timestamp_iso: str,
    event_type: str,
    source_service: str,
    session_id: Optional[str],
    user_id: Optional[str],
    payload: Dict[str, Any],
) -> str:
    """
    Build the canonical string signed by the HMAC:

        {request_id}|{client_timestamp_iso}|{event_type}
        |{source_service}|{session_id}|{user_id}|{payload_hash}

    where payload_hash is SHA-256 of the canonical payload JSON.

    The attribution fields (source_service, session_id, user_id) are part of
    the signature so they cannot be rewritten in transit without breaking
    verification — they are persisted on the row and feed the audit trail.
    None session_id/user_id render as the empty string. This construction
    MUST match careeragent-logger/src/security.py byte-for-byte.
    """
    ph = _payload_hash(payload)
    return "|".join(
        (
            request_id,
            client_timestamp_iso,
            event_type,
            _fmt_envelope_field(source_service),
            _fmt_envelope_field(session_id),
            _fmt_envelope_field(user_id),
            ph,
        )
    )


def _sign(
    secret: str,
    request_id: str,
    client_timestamp_iso: str,
    event_type: str,
    source_service: str,
    session_id: Optional[str],
    user_id: Optional[str],
    payload: Dict[str, Any],
) -> str:
    """
    Compute HMAC-SHA256 over the canonical string, keyed with the UTF-8 bytes
    of `secret`. Returns the 64-char lowercase hex digest.

    Args:
        secret:               LOGGER_HMAC_SECRET value (UTF-8 string).
        request_id:           Correlation UUID for the /chat call.
        client_timestamp_iso: Canonical timestamp of event construction
                              (see _canonical_timestamp).
        event_type:           "ops_event" | "conversation_capture" | "audit_event".
        source_service:       Emitter identifier (signed attribution field).
        session_id:           Caller session correlation, or None (signed).
        user_id:              Caller user id, or None (signed).
        payload:              The per-event-type payload dict.

    Returns:
        str: 64-char lowercase hex HMAC digest.
    """
    canonical = _canonical_string(
        request_id=request_id,
        client_timestamp_iso=client_timestamp_iso,
        event_type=event_type,
        source_service=source_service,
        session_id=session_id,
        user_id=user_id,
        payload=payload,
    )
    return hmac.new(
        secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _sha256_hex(text: str) -> str:
    """
    SHA-256 of the UTF-8 encoded text, as a 64-char lowercase hex string.
    Used to populate input_hash and output_hash on conversation_capture
    events.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_timestamp(dt: datetime) -> str:
    """
    Render a datetime as the canonical client_timestamp string.

    Normalised to UTC with fixed microsecond precision and a '+00:00'
    offset, e.g. '2026-05-14T18:24:01.342000+00:00'. We sign over this exact
    string AND put it on the wire, so the receiver verifies over a value it
    can reproduce by re-canonicalising the parsed timestamp — rather than
    depending on `datetime.fromisoformat(s).isoformat() == s` holding.

    This function MUST match careeragent-logger/src/security.py byte-for-byte.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _utc_now_canonical() -> str:
    """
    Current UTC time as a canonical client_timestamp string.

    Generated at emit time so the replay-window check on the receiving side
    is meaningful (replay-window is measured between client_timestamp and
    server's now-time).
    """
    return _canonical_timestamp(datetime.now(timezone.utc))


# ============================================================================
# LOGGER CLIENT
# ============================================================================

class LoggerClient:
    """
    Fire-and-forget client for careeragent-logger.

    Lifecycle:
      1. Construct with config values (typically from .env via Config).
      2. Call `await client.start()` in the FastAPI lifespan startup block,
         after the upstream careeragent-infra client (InfraClient) is
         initialised.
      3. Call sync `client.emit_ops_event(...)` or
         `client.emit_conversation_capture(...)` from request handlers.
      4. Call `await client.stop()` in the FastAPI lifespan shutdown block.

    Threading / concurrency:
      All operations are within a single asyncio event loop. Multiple
      concurrent /chat handlers may call emit_* simultaneously; the
      underlying asyncio.Queue serialises these operations safely.

    Failure modes:
      - careeragent-logger unreachable → drain attempts fail, events lost,
                                       warnings logged, /chat unaffected.
      - careeragent-logger returns 4xx/5xx → event dropped, warning logged,
                                       no retry.
      - queue full (overflow)       → drop-oldest, warning logged.
      - drain task crashes          → next emit's enqueue still succeeds
                                      until queue fills. Catastrophic failure
                                      is logged inside the drain loop's outer
                                      except handler.
    """

    # ----------------------------------------------------------------------
    # CONSTRUCTION
    # ----------------------------------------------------------------------

    def __init__(
        self,
        url: str,
        api_key: str,
        hmac_secret: str,
        source_service: str = "careeragent-api",
        queue_max_size: int = 1000,
        connect_timeout: float = 5.0,
        read_timeout: float = 10.0,
    ):
        """
        Args:
            url:             Base URL of careeragent-logger, no trailing slash.
                             E.g. http://careeragent-logger:8003.
            api_key:         LOGGER_API_KEY value. Sent as X-API-Key on every
                             outbound POST.
            hmac_secret:     LOGGER_HMAC_SECRET value. Used to sign every
                             event payload before enqueue.
            source_service:  Identifier for the source_service envelope
                             field. Defaults to "careeragent-api".
            queue_max_size:  Maximum pending events before drop-oldest
                             overflow kicks in. Default 1000 — accommodates a
                             sustained outage of several minutes at typical
                             /chat throughput.
            connect_timeout: TCP connect timeout in seconds. Short by design
                             — if careeragent-logger is unreachable, fail fast.
            read_timeout:    HTTP read timeout in seconds. Short because
                             careeragent-logger's /events is a synchronous
                             insert; if it takes >10s, something is wrong
                             upstream.

        Raises:
            ValueError: if any of url, api_key, or hmac_secret is empty.
                        These are required at construction time — starting
                        careeragent-api without them would silently drop every
                        event, which is worse than a hard startup failure.
        """
        if not url:
            raise ValueError("LoggerClient: url is required")
        if not api_key:
            raise ValueError("LoggerClient: api_key is required")
        if not hmac_secret:
            raise ValueError("LoggerClient: hmac_secret is required")

        # Strip trailing slash to give predictable joins on /events
        # regardless of whether the operator's .env value ended with one.
        self._url: str = url.rstrip("/")
        self._api_key: str = api_key
        self._hmac_secret: str = hmac_secret
        self._source_service: str = source_service
        self._queue_max_size: int = queue_max_size
        self._connect_timeout: float = connect_timeout
        self._read_timeout: float = read_timeout

        # Queue is created up front so emit_* can be called before start()
        # without crashing (events queue and get drained when the background
        # task starts). In practice api.py instantiates this client AND calls
        # start() inside the same lifespan startup, so the "emit before
        # start" window is microseconds.
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_max_size)
        self._http_client: Optional[httpx.AsyncClient] = None
        self._drain_task: Optional[asyncio.Task] = None
        self._is_running: bool = False

    # ----------------------------------------------------------------------
    # LIFECYCLE
    # ----------------------------------------------------------------------

    async def start(self) -> None:
        """
        Initialise the httpx client and launch the background drain task.

        Idempotent — calling start() twice is a no-op.

        Does NOT probe careeragent-logger for connectivity. The client starts
        regardless of whether careeragent-logger is reachable; if
        careeragent-logger is down, drain attempts will fail and log warnings
        until it comes back online. This is the fire-and-forget contract —
        the gateway must come up even if the capture layer is temporarily
        unavailable.
        """
        if self._is_running:
            logger.debug("LoggerClient.start() called twice; ignoring")
            return

        # Construct the httpx client with the API key pre-attached. Every
        # outbound POST inherits this header automatically — no code path can
        # forget to attach it.
        self._http_client = httpx.AsyncClient(
            base_url=self._url,
            timeout=httpx.Timeout(
                connect=self._connect_timeout,
                read=self._read_timeout,
                write=5.0,
                pool=2.0,
            ),
            headers={"X-API-Key": self._api_key},
        )

        # Launch the drain loop. asyncio.create_task schedules it on the
        # current event loop; it begins running on the next loop iteration.
        # There's a small window where emit_* can be called before the drain
        # task is scheduled — events queue normally and are drained in order
        # once the task runs.
        self._drain_task = asyncio.create_task(self._drain_loop())
        self._is_running = True

        logger.info(
            f"LoggerClient started "
            f"(url={self._url}, "
            f"source_service={self._source_service}, "
            f"queue_max_size={self._queue_max_size}, "
            f"connect_timeout={self._connect_timeout}s, "
            f"read_timeout={self._read_timeout}s)"
        )

    async def stop(self, drain_timeout: float = 5.0) -> None:
        """
        Stop the drain loop and close the httpx client.

        Idempotent — calling stop() twice is a no-op.

        Sequence:
          1. Mark the client as no longer running so any post-stop emit calls
             log a warning instead of enqueuing into a queue that won't be
             drained.
          2. Wait up to `drain_timeout` seconds for pending events to drain.
             If careeragent-logger is responsive this typically completes in
             well under a second.
          3. Cancel the drain task. Any in-flight POST is interrupted.
          4. Close the httpx client.

        Args:
            drain_timeout: Seconds to wait for the queue to drain before
                           giving up and cancelling. Default 5s.
        """
        if not self._is_running:
            logger.debug("LoggerClient.stop() called before start; ignoring")
            return
        self._is_running = False

        # Best-effort drain of pending events.
        remaining = self._queue.qsize()
        if remaining > 0:
            logger.info(
                f"LoggerClient stopping; "
                f"{remaining} event(s) pending, "
                f"draining (timeout={drain_timeout}s)"
            )
            try:
                await asyncio.wait_for(
                    self._queue.join(),
                    timeout=drain_timeout,
                )
                logger.info("LoggerClient queue drained cleanly")
            except asyncio.TimeoutError:
                still_pending = self._queue.qsize()
                logger.warning(
                    f"LoggerClient drain timeout after {drain_timeout}s; "
                    f"{still_pending} event(s) lost at shutdown"
                )

        # Cancel the drain loop. If it's currently awaiting queue.get() the
        # CancelledError fires immediately; if it's inside a POST, the POST is
        # interrupted and CancelledError propagates.
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
            except Exception as err:
                logger.warning(
                    f"Unexpected error from drain task on shutdown: "
                    f"{type(err).__name__}: {err}"
                )
        self._drain_task = None

        # Close the httpx client. We do this last so that any in-flight POSTs
        # the drain task was running have already been cancelled.
        if self._http_client is not None:
            try:
                await self._http_client.aclose()
            except Exception as err:
                logger.warning(
                    f"Error closing logger httpx client: "
                    f"{type(err).__name__}: {err}"
                )
            self._http_client = None

        logger.info("LoggerClient stopped")

    # ----------------------------------------------------------------------
    # PUBLIC EMIT METHODS
    # ----------------------------------------------------------------------

    def emit_ops_event(
        self,
        action: str,
        outcome: str,
        request_id: str,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Enqueue an ops_event. Synchronous and non-blocking — returns in
        microseconds. Total work: construct the envelope, sign the payload,
        enqueue. No I/O.

        Standard careeragent-api actions (per careeragent-logger DATASHEET §6.1):
            "request_received" — emitted on /chat ingress after auth passes.
            "upstream_call"    — emitted before calling careeragent-infra.
            "upstream_error"   — emitted on any careeragent-infra failure.
            "stream_complete"  — emitted after the SSE stream closes.

        Args:
            action:     Short identifier for the event, ≤128 chars. E.g.
                        "request_received".
            outcome:    Outcome label, ≤32 chars. E.g. "success", "failure",
                        "timeout", "denied". The agent /chat path emits an HONEST
                        per-turn taxonomy on stream_complete (Phase 3 slice 3):
                        "final" (a clean, grounded, verified answer), "max_steps"
                        (budget exhausted -> synthesis), "unverified" (shipped a
                        write-claim the ledger couldn't back), "ungrounded" (shipped a
                        resume with claims the Tier-1 dossier gate couldn't back),
                        "blocked" (the Guardian verifier substantively objected to a
                        resume claim — shipped with the claims flagged), "unverifiable"
                        (the verifier itself couldn't run and failed closed — a spike
                        here means the VERIFIER is broken, kept distinct from "blocked"
                        so a verifier outage stays visible), "paused" (the coach
                        called ask_user / requested a write approval and suspended —
                        it resumes on the user's answer, not an ending), "interrupted"
                        (the user stopped the run mid-flight), "model_error". A punt/uncertain
                        turn is NEVER logged as "success".
            request_id: Correlation UUID for the originating /chat call. Used
                        to join ops_events and conversation_capture rows
                        after the fact.
            session_id: Optional session correlation ID. May be None — the
                        reference stack does not track sessions and emits
                        null.
            user_id:    Optional user UUID. Reserved, nullable; emitted as
                        null in the reference stack.
            details:    Optional free-form context dict. Use sparingly;
                        anything routinely-queried should become a
                        first-class field via a future schema change.
        """
        payload: Dict[str, Any] = {
            "action": action,
            "outcome": outcome,
            "details": details if details is not None else {},
        }
        self._build_sign_and_enqueue(
            event_type="ops_event",
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            payload=payload,
        )

    def emit_conversation_capture(
        self,
        request_id: str,
        input_text: str,
        output_text: str,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        model_used: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        latency_ms: Optional[int] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
    ) -> None:
        """
        Enqueue a conversation_capture. Synchronous and non-blocking.

        Captures the full /chat exchange for observability and audit.
        input_hash and output_hash are computed here from the provided texts;
        the caller does not need to supply them.

        Note on output_text (see header NOTE ON REASONING-CHAIN CAPTURE for
        full rationale):
            output_text contains ONLY the visible answer (concatenated
            delta.content tokens from the SSE chunk stream). The reasoning
            chain (delta.reasoning tokens) is NOT included. This decision is
            revisitable; if the reasoning chain is ever needed, extend the
            payload schema in a coordinated careeragent-logger + careeragent-api
            release.

        Args:
            request_id:       Correlation UUID for the /chat call (must match
                              the request_id used on the accompanying
                              ops_events).
            input_text:       Raw user input. Typically the content of the
                              last user message in the /chat messages array,
                              1..200_000 chars per the careeragent-logger
                              contract.
            output_text:      Assembled visible answer, 0..1_000_000 chars.
            session_id:       Optional session correlation ID.
            user_id:          Optional user UUID.
            model_used:       Model identifier reported by upstream, e.g. the
                              provider's model string.
            reasoning_effort: "low" | "medium" | "high" if the frontend
                              specified one; None if it did not (in which
                              case careeragent-infra applied its server-side
                              default).
            latency_ms:       End-to-end /chat latency in milliseconds.
            input_tokens:     Token count reported by upstream, if available
                              in the final SSE chunk's usage field.
            output_tokens:    Same for output tokens.
        """
        payload: Dict[str, Any] = {
            "input_text": input_text,
            "output_text": output_text,
            "input_hash": _sha256_hex(input_text),
            "output_hash": _sha256_hex(output_text),
            "model_used": model_used,
            "reasoning_effort": reasoning_effort,
            "latency_ms": latency_ms,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        self._build_sign_and_enqueue(
            event_type="conversation_capture",
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            payload=payload,
        )

    # ----------------------------------------------------------------------
    # INTERNAL: EVENT CONSTRUCTION
    # ----------------------------------------------------------------------

    def _build_sign_and_enqueue(
        self,
        event_type: str,
        request_id: str,
        session_id: Optional[str],
        user_id: Optional[str],
        payload: Dict[str, Any],
    ) -> None:
        """
        Construct the full event envelope, compute its HMAC signature, and
        enqueue it for the drain task.

        Generates client_timestamp at this moment (not at drain time)
        because:
          1. The careeragent-logger replay-window check compares
             client_timestamp against careeragent-logger's wall clock. If we
             generated client_timestamp later (in the drain task), an event
             that sat in the queue for several minutes during an
             careeragent-logger outage might fail the replay-window check once
             the outage resolved and we tried to send it.
          2. Generating at construction time makes client_timestamp reflect
             when the /chat actually happened, not when it was eventually
             transmitted. That's more useful for operators debugging latency
             questions.
        """
        # Drop the event unless the client is running. _is_running is the
        # single source of truth: True only between start() and stop(). Gating
        # purely on it closes the post-stop leak window (an event enqueued
        # after stop() has no consumer and would leak in the queue forever);
        # in this service emissions only ever happen on the /chat hot path,
        # which is reachable only after lifespan start() has run.
        if not self._is_running:
            logger.debug(
                f"LoggerClient: dropping {event_type} emitted after stop "
                f"(request_id={request_id})"
            )
            return

        timestamp = _utc_now_canonical()
        signature = _sign(
            secret=self._hmac_secret,
            request_id=request_id,
            client_timestamp_iso=timestamp,
            event_type=event_type,
            source_service=self._source_service,
            session_id=session_id,
            user_id=user_id,
            payload=payload,
        )

        event: Dict[str, Any] = {
            "event_type": event_type,
            "request_id": request_id,
            "source_service": self._source_service,
            "client_timestamp": timestamp,
            "session_id": session_id,
            "user_id": user_id,
            "hmac_signature": signature,
            "payload": payload,
        }

        self._enqueue(event)

    def _enqueue(self, event: Dict[str, Any]) -> None:
        """
        Enqueue an event with drop-oldest overflow handling.

        Single-producer-style use: backend/api.py request handlers all run on
        the same event loop, so concurrent emit calls are interleaved but
        never truly simultaneous. asyncio.Queue is safe under this access
        pattern.

        Overflow policy: if the queue is full, evict the oldest pending event
        and log a warning, then enqueue the new event. The rationale for
        drop-oldest rather than drop-newest is that during a sustained
        outage, fresher data is more operationally useful than stale data
        when the storm passes.
        """
        if self._queue.full():
            try:
                dropped = self._queue.get_nowait()
                # Mark as done so queue.join() accounting stays balanced —
                # we're acknowledging consumption of the event, just without
                # ever sending it.
                self._queue.task_done()
                logger.warning(
                    f"LoggerClient queue full "
                    f"(max={self._queue_max_size}); "
                    f"dropped oldest event "
                    f"(type={dropped.get('event_type')}, "
                    f"request_id={dropped.get('request_id')})"
                )
            except asyncio.QueueEmpty:
                # Another consumer drained between our full() check and our
                # get_nowait(). Highly unlikely in single-loop asyncio but
                # tolerated for robustness.
                pass

        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # Should be unreachable given the drop-oldest above, but log
            # rather than crash if it ever happens.
            logger.warning(
                f"LoggerClient queue full even after drop-oldest; "
                f"dropping new event "
                f"(type={event.get('event_type')}, "
                f"request_id={event.get('request_id')})"
            )

    # ----------------------------------------------------------------------
    # INTERNAL: DRAIN LOOP AND HTTP POST
    # ----------------------------------------------------------------------

    async def _drain_loop(self) -> None:
        """
        Background coroutine: pull events from the queue and POST them.

        Runs until cancelled (during stop()). Each event is processed
        regardless of POST success or failure — failures are logged but do
        not trigger retries. task_done() is always called so queue.join()
        works during graceful shutdown.

        Exception handling philosophy:
          - asyncio.CancelledError: re-raise immediately. We're being shut
            down. The currently-being-processed event is lost.
          - Any other Exception: log a warning, drop the event, continue the
            loop. We never let a single bad event take down the drain task.
        """
        logger.debug("LoggerClient drain loop started")
        try:
            while True:
                # Wait for an event. queue.get() blocks until one is
                # available; during shutdown the asyncio.CancelledError from
                # task.cancel() interrupts this call.
                event = await self._queue.get()

                try:
                    await self._post_event(event)
                except asyncio.CancelledError:
                    # Being cancelled mid-post. Mark this event as done (it's
                    # lost) and let the cancellation propagate.
                    self._queue.task_done()
                    raise
                except Exception as err:
                    logger.warning(
                        f"LoggerClient POST failed "
                        f"({type(err).__name__}: {err}); event lost "
                        f"(type={event.get('event_type')}, "
                        f"request_id={event.get('request_id')})"
                    )
                    self._queue.task_done()
                else:
                    # Success. DEBUG-level only to avoid spamming logs under
                    # normal operation.
                    logger.debug(
                        f"LoggerClient POST success "
                        f"(type={event.get('event_type')}, "
                        f"request_id={event.get('request_id')})"
                    )
                    self._queue.task_done()
        except asyncio.CancelledError:
            logger.debug("LoggerClient drain loop cancelled")
            raise
        except Exception as err:
            # Catastrophic — the drain loop itself crashed. This should be
            # unreachable given the inner handlers above, but we log loudly so
            # the operator knows the capture pipeline has silently stopped
            # working.
            logger.error(
                f"LoggerClient drain loop CRASHED with unexpected "
                f"{type(err).__name__}: {err}. "
                f"Logger emission is now broken until careeragent-api is "
                f"restarted. Please report this as a bug.",
                exc_info=True,
            )
            raise

    async def _post_event(self, event: Dict[str, Any]) -> None:
        """
        POST a single event to careeragent-logger's /events endpoint.

        Does NOT retry. Does NOT read the response body for any reason other
        than logging on unexpected status codes.

        On non-201 status: log a warning with the status code. We deliberately
        do NOT log the response body because it can contain the request_id,
        action, etc. that are already in our log line, and we want to keep
        this concise. Operators who need to debug a specific event should
        query careeragent-logger directly.

        On network error (timeout, connection refused, etc.): the exception
        propagates to the drain loop's except handler.
        """
        if self._http_client is None:
            # Shouldn't be reachable — start() always creates the client
            # before launching the drain task. Defensive check.
            raise RuntimeError(
                "LoggerClient._post_event: http_client is None"
            )

        response = await self._http_client.post("/events", json=event)

        if response.status_code != 201:
            logger.warning(
                f"careeragent-logger returned HTTP {response.status_code} "
                f"for event "
                f"(type={event.get('event_type')}, "
                f"request_id={event.get('request_id')})"
            )


# ============================================================================
# END OF FILE
# ============================================================================