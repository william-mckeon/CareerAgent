# ============================================================================
# tests/test_sse_decoder.py
# ----------------------------------------------------------------------------
# Pure-function tests for src/frontend/sse_decoder.py.
#
# These exercise the riskiest part of the frontend — the SSE decoder's
# edge-case routing — with NO Streamlit, NO network, NO HTTP transport.
# Every test feeds plain strings (the same shape requests.iter_lines()
# yields) and asserts on the structured SSEEvent output.
#
# Coverage:
#   - reasoning-token chunk  -> KIND_REASONING
#   - content-token chunk    -> KIND_CONTENT
#   - terminal stop chunk    -> KIND_FINISH (finish_reason="stop")
#   - [DONE] sentinel        -> KIND_DONE
#   - [ERROR ...] sentinel   -> KIND_ERROR (+ error_status parse)
#   - malformed JSON         -> None (skipped, no raise)
#   - non-string content     -> None (skipped, no raise)
#   - role-only first chunk  -> None (harmless skip)
#   - REGRESSION: co-located content token + finish_reason="length"
#       through decode_sse_stream -> BOTH KIND_CONTENT and KIND_FINISH.
#   - small end-to-end stream through decode_sse_stream.
# ============================================================================

import json

from frontend.sse_decoder import (
    KIND_ARTIFACT,
    KIND_CONTENT,
    KIND_DONE,
    KIND_ERROR,
    KIND_FINISH,
    KIND_PLAN_UPDATE,
    KIND_REASONING,
    KIND_STEP,
    KIND_SUSPEND,
    KIND_TOOL_RESULT,
    KIND_TOOL_START,
    SSEEvent,
    decode_sse_stream,
    parse_chunk,
    parse_error_sentinel,
)


# ----------------------------------------------------------------------------
# Helpers: build SSE "data: {...}" lines the way the upstream emits them.
# ----------------------------------------------------------------------------

def _data_line(payload: dict) -> str:
    """Wrap a chunk dict as a real SSE data line."""
    return "data: " + json.dumps(payload)


def _chunk(delta: dict, finish_reason=None) -> str:
    """Build an OpenAI-style ChatCompletion streaming chunk SSE line."""
    return _data_line(
        {"choices": [{"delta": delta, "finish_reason": finish_reason}]}
    )


# ----------------------------------------------------------------------------
# parse_chunk: token routing
# ----------------------------------------------------------------------------

def test_reasoning_token_chunk_routes_to_reasoning():
    event = parse_chunk(_chunk({"reasoning": "thinking..."}))
    assert event is not None
    assert event.kind == KIND_REASONING
    assert event.text == "thinking..."
    # No finish_reason co-located, so it stays empty.
    assert event.finish_reason == ""


def test_content_token_chunk_routes_to_content():
    event = parse_chunk(_chunk({"content": "Hello"}))
    assert event is not None
    assert event.kind == KIND_CONTENT
    assert event.text == "Hello"
    assert event.finish_reason == ""


def test_terminal_stop_chunk_routes_to_finish():
    # Empty delta + finish_reason="stop" is the canonical terminal chunk.
    event = parse_chunk(_chunk({}, finish_reason="stop"))
    assert event is not None
    assert event.kind == KIND_FINISH
    assert event.finish_reason == "stop"
    assert event.text == ""


# ----------------------------------------------------------------------------
# parse_chunk: sentinels
# ----------------------------------------------------------------------------

def test_done_sentinel():
    event = parse_chunk("data: [DONE]")
    assert event is not None
    assert event.kind == KIND_DONE


def test_error_sentinel_with_status():
    event = parse_chunk("data: [ERROR upstream_status=503]")
    assert event is not None
    assert event.kind == KIND_ERROR
    assert event.error_status == 503
    # Fallback is the length-capped, whitespace-normalised payload.
    assert "ERROR" in event.error


def test_error_sentinel_without_status():
    event = parse_chunk("data: [ERROR something went very wrong]")
    assert event is not None
    assert event.kind == KIND_ERROR
    assert event.error_status is None
    assert event.error  # non-empty fallback string


def test_parse_error_sentinel_caps_length():
    # The raw payload must never be forwarded verbatim at unbounded length.
    long_payload = "[ERROR " + ("x" * 500) + "]"
    event = parse_error_sentinel(long_payload)
    assert event.kind == KIND_ERROR
    assert len(event.error) <= 120
    assert event.error_status is None


# ----------------------------------------------------------------------------
# parse_chunk: malformed / unexpected input is skipped, never raises
# ----------------------------------------------------------------------------

def test_malformed_json_returns_none():
    # Truncated/garbage JSON must be skipped silently, not raise.
    assert parse_chunk("data: {not valid json") is None


def test_non_string_content_token_is_skipped():
    # A numeric delta.content would crash `answer_text += event.text` downstream;
    # the decoder must guard the type and skip with None.
    event = parse_chunk(_chunk({"content": 42}))
    assert event is None


def test_non_string_reasoning_token_is_skipped():
    event = parse_chunk(_chunk({"reasoning": ["a", "list"]}))
    assert event is None


def test_role_only_first_chunk_is_skipped():
    # The very first chunk of many streams is role-only with empty content.
    event = parse_chunk(_chunk({"role": "assistant", "content": ""}))
    assert event is None


def test_blank_line_and_comment_skipped():
    assert parse_chunk("") is None
    assert parse_chunk(":") is None  # SSE keep-alive comment
    assert parse_chunk(": heartbeat") is None


def test_non_data_line_skipped():
    assert parse_chunk("event: ping") is None


def test_data_prefix_without_space_accepted():
    # SSE spec permits "data:{...}" with no trailing space.
    event = parse_chunk("data:" + json.dumps(
        {"choices": [{"delta": {"content": "x"}, "finish_reason": None}]}
    ))
    assert event is not None
    assert event.kind == KIND_CONTENT
    assert event.text == "x"


def test_chunk_with_no_choices_skipped():
    assert parse_chunk('data: {"choices": []}') is None


# ----------------------------------------------------------------------------
# REGRESSION: co-located content token + finish_reason in a SINGLE chunk.
# ----------------------------------------------------------------------------
# Just-fixed bug: when the upstream put a final content token AND
# finish_reason="length" in the SAME chunk, the early-return at the content
# branch dropped the finish_reason — a truncation was silently lost. The fix
# carries finish_reason on the token event and decode_sse_stream emits a
# synthetic KIND_FINISH after it. A consumer that only inspects KIND_FINISH
# must still observe the truncation.

def test_regression_colocated_content_and_finish_length():
    line = _chunk({"content": "truncated tail"}, finish_reason="length")

    events = list(decode_sse_stream([line]))

    # Exactly two events: the content token, then a synthetic finish.
    kinds = [e.kind for e in events]
    assert kinds == [KIND_CONTENT, KIND_FINISH], kinds

    content_event, finish_event = events
    assert content_event.text == "truncated tail"
    # The content event carries the co-located finish_reason...
    assert content_event.finish_reason == "length"
    # ...and a dedicated KIND_FINISH event surfaces it for finish-only consumers.
    assert finish_event.finish_reason == "length"


def test_regression_colocated_reasoning_and_finish():
    # Same co-location logic must hold for a reasoning token too.
    line = _chunk({"reasoning": "almost done"}, finish_reason="length")
    events = list(decode_sse_stream([line]))
    kinds = [e.kind for e in events]
    assert kinds == [KIND_REASONING, KIND_FINISH], kinds
    assert events[-1].finish_reason == "length"


# ----------------------------------------------------------------------------
# End-to-end: a small realistic stream through decode_sse_stream.
# ----------------------------------------------------------------------------

def test_small_end_to_end_stream():
    lines = [
        "",  # blank separator
        _chunk({"role": "assistant", "content": ""}),  # role-only -> skipped
        _chunk({"reasoning": "Let me "}),
        _chunk({"reasoning": "think. "}),
        _chunk({"content": "Hello"}),
        _chunk({"content": ", "}),
        _chunk({"content": "world"}),
        ": keep-alive",  # comment -> skipped
        _chunk({}, finish_reason="stop"),  # terminal stop
        "data: [DONE]",
    ]

    events = list(decode_sse_stream(lines))
    kinds = [e.kind for e in events]

    assert kinds == [
        KIND_REASONING,
        KIND_REASONING,
        KIND_CONTENT,
        KIND_CONTENT,
        KIND_CONTENT,
        KIND_FINISH,
        KIND_DONE,
    ], kinds

    reasoning_text = "".join(e.text for e in events if e.kind == KIND_REASONING)
    content_text = "".join(e.text for e in events if e.kind == KIND_CONTENT)
    assert reasoning_text == "Let me think. "
    assert content_text == "Hello, world"

    finish_events = [e for e in events if e.kind == KIND_FINISH]
    assert len(finish_events) == 1
    assert finish_events[0].finish_reason == "stop"


def test_done_sentinel_stops_iteration_early():
    # Anything after [DONE] must not be yielded.
    lines = [
        _chunk({"content": "hi"}),
        "data: [DONE]",
        _chunk({"content": "should not appear"}),
    ]
    events = list(decode_sse_stream(lines))
    kinds = [e.kind for e in events]
    assert kinds == [KIND_CONTENT, KIND_DONE]


def test_none_lines_tolerated():
    # requests.iter_lines() can yield None on some terminations.
    lines = [None, _chunk({"content": "ok"}), None, "data: [DONE]"]
    events = list(decode_sse_stream(lines))
    assert [e.kind for e in events] == [KIND_CONTENT, KIND_DONE]


def test_sseevent_is_frozen():
    event = SSEEvent(kind=KIND_CONTENT, text="x")
    try:
        event.text = "mutated"  # type: ignore[misc]
    except Exception as exc:
        assert exc.__class__.__name__ in ("FrozenInstanceError", "AttributeError")
    else:
        raise AssertionError("SSEEvent should be immutable (frozen=True)")


# ----------------------------------------------------------------------------
# parse_chunk: P4 suspend frame (the coach paused to ask the user)
# ----------------------------------------------------------------------------
def _suspend_line(**ca) -> str:
    return _data_line({"careeragent": {"event": "suspend", **ca}})


def test_suspend_frame_routes_to_suspend_with_pending():
    line = _suspend_line(
        pending_call_id="call_x", pending_kind="question",
        payload={"question": "Which role?", "options": ["Staff", "Senior"]})
    event = parse_chunk(line)
    assert event is not None and event.kind == KIND_SUSPEND
    assert event.pending["call_id"] == "call_x"
    assert event.pending["kind"] == "question"
    assert event.pending["question"] == "Which role?"
    assert event.pending["options"] == ["Staff", "Senior"]


def test_suspend_defaults_missing_fields():
    # A minimal suspend (approval, no options) still parses with safe defaults.
    event = parse_chunk(_suspend_line(pending_call_id="c1", pending_kind="approval"))
    assert event.kind == KIND_SUSPEND
    assert event.pending["kind"] == "approval"
    assert event.pending["question"] == "" and event.pending["options"] == []


def test_plan_proposal_suspend_carries_the_plan_payload():
    # P7 #20: a plan_proposal suspend carries {summary, steps} on pending.payload so
    # the UI can render the plan + Approve / Not-now.
    event = parse_chunk(_suspend_line(
        pending_call_id="call_pp", pending_kind="plan_proposal",
        payload={"summary": "Tailor it", "steps": [{"content": "Draft bullets", "status": "pending"}]}))
    assert event.kind == KIND_SUSPEND
    assert event.pending["kind"] == "plan_proposal"
    assert event.pending["payload"]["summary"] == "Tailor it"
    assert event.pending["payload"]["steps"][0]["content"] == "Draft bullets"


def test_suspend_without_call_id_is_not_a_suspend():
    # No pending_call_id -> can't be resumed -> not treated as a suspend (skipped).
    assert parse_chunk(_data_line({"careeragent": {"event": "suspend"}})) is None


def test_careeragent_frame_of_another_event_is_ignored():
    # An UNKNOWN careeragent event (not suspend, not a P7 #19 typed kind) is ignored
    # (forward-compat). "progress" is not one of the typed kinds.
    assert parse_chunk(_data_line({"careeragent": {"event": "progress", "pending_call_id": "x"}})) is None
    assert parse_chunk(_data_line({"careeragent": {"event": "totally_new_kind"}})) is None


# ---- P7 #19 typed structured streaming frames ------------------------------------
def test_typed_plan_update_frame_decodes():
    ev = parse_chunk(_data_line({"careeragent": {
        "event": "plan_update", "plan": [{"content": "Draft resume", "status": "in_progress"}]}}))
    assert ev is not None and ev.kind == KIND_PLAN_UPDATE
    assert ev.typed["plan"][0]["content"] == "Draft resume"
    assert "choices" not in (ev.typed or {})     # never a content channel


def test_typed_tool_frames_decode():
    start = parse_chunk(_data_line({"careeragent": {
        "event": "tool_start", "name": "search_projects", "args": "{}"}}))
    assert start.kind == KIND_TOOL_START and start.typed["name"] == "search_projects"
    result = parse_chunk(_data_line({"careeragent": {
        "event": "tool_result", "name": "search_projects", "ok": True}}))
    assert result.kind == KIND_TOOL_RESULT and result.typed["ok"] is True
    step = parse_chunk(_data_line({"careeragent": {"event": "step", "text": "converging"}}))
    assert step.kind == KIND_STEP


def test_normal_content_chunk_is_unaffected_by_suspend_branch():
    event = parse_chunk(_chunk({"content": "hello"}))
    assert event.kind == KIND_CONTENT and event.text == "hello"


# ---- P7 #16 artifact frame -------------------------------------------------------
def test_artifact_frame_decodes_with_metadata():
    ev = parse_chunk(_data_line({"careeragent": {
        "event": "artifact", "artifact_id": "art-1", "application_id": "app-1",
        "format": "pdf", "filename": "acme-swe.pdf", "bytes": 1234}}))
    assert ev is not None and ev.kind == KIND_ARTIFACT
    assert ev.typed["artifact_id"] == "art-1"
    assert ev.typed["application_id"] == "app-1"
    assert ev.typed["format"] == "pdf" and ev.typed["filename"] == "acme-swe.pdf"
    # metadata only — the bytes NEVER ride the frame
    assert "content_b64" not in ev.typed and "content" not in ev.typed
    assert "choices" not in (ev.typed or {})
