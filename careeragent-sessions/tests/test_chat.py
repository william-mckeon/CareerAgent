"""
tests/test_chat.py — the SSE capture scanner (_scan_sse).

This is the riskiest pure logic: pulling the assistant's answer + the [DONE] /
[ERROR] sentinels off the relayed stream so the transcript can be persisted
without changing what the caller receives.
"""
import json

from backend.api import _extract_suspend, _scan_sse


def _event(delta: dict) -> str:
    chunk = {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
    return f"data: {json.dumps(chunk)}"


def _careeragent(**ca) -> str:
    return f"data: {json.dumps({'careeragent': ca})}"


class TestScanSse:
    def test_extracts_content(self):
        content, done, error = _scan_sse(_event({"content": "Hello"}))
        assert content == "Hello"
        assert not done and not error

    def test_ignores_reasoning_only(self):
        content, done, error = _scan_sse(_event({"reasoning": "thinking..."}))
        assert content == ""
        assert not done and not error

    def test_ignores_typed_progress_frames(self):
        # P7 #19: a typed careeragent frame must NOT be captured as transcript
        # content (it has no delta.content) nor mistaken for a suspend.
        for frame in (
            _careeragent(event="plan_update", plan=[{"content": "a", "status": "pending"}]),
            _careeragent(event="tool_start", name="search_projects", args="{}"),
            _careeragent(event="tool_result", name="search_projects", ok=True),
            _careeragent(event="step", text="converging"),
        ):
            content, done, error = _scan_sse(frame)
            assert content == "" and not done and not error
            assert _extract_suspend(frame) is None      # never a paused run

    def test_detects_done(self):
        _, done, error = _scan_sse("data: [DONE]")
        assert done and not error

    def test_finish_reason_marks_done(self):
        # careeragent-api ends at the finish_reason chunk, not [DONE].
        import json
        event = "data: " + json.dumps(
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        )
        _, done, error = _scan_sse(event)
        assert done and not error

    def test_null_finish_reason_is_not_done(self):
        content, done, _ = _scan_sse(_event({"content": "hi"}))
        assert content == "hi" and not done

    def test_detects_error(self):
        _, done, error = _scan_sse("data: [ERROR] upstream blew up")
        assert error

    def test_ignores_keepalive_and_garbage(self):
        content, done, error = _scan_sse(": keep-alive\ndata: not-json")
        assert content == "" and not done and not error

    def test_multiline_event_with_role_then_content(self):
        block = _event({"role": "assistant"}) + "\n" + _event({"content": "Hi"})
        content, _, _ = _scan_sse(block)
        assert content == "Hi"


# Review Finding 3: every in-band [ERROR ...] shape the api emits must be detected,
# not just the closing-bracket "[ERROR]" — else an errored agent turn is
# mis-persisted as a clean, complete assistant message.
class TestScanSseErrorShapes:
    def test_bare_error_bracket(self):
        _, _, error = _scan_sse("data: [ERROR]")
        assert error

    def test_error_with_type(self):
        _, _, error = _scan_sse("data: [ERROR RuntimeError]")
        assert error

    def test_error_with_upstream_status(self):
        _, _, error = _scan_sse("data: [ERROR upstream=503]")
        assert error
