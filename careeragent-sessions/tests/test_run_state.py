"""
tests/test_run_state.py — the P4 suspend-frame scanner (_extract_suspend).

The riskiest pure logic on the sessions side: recognizing careeragent-api's
namespaced suspend frame in the relayed SSE stream and pulling out the pending
request + snapshot, without mistaking a normal content frame for one. The store
SQL is exercised live against the real DB (scripts/verify), not mocked here.
"""
import json

from backend.api import _extract_suspend, _scan_sse


def _suspend_frame(**ca) -> str:
    return "data: " + json.dumps({"careeragent": {"event": "suspend", **ca}})


class TestExtractSuspend:
    def test_detects_a_suspend_frame_and_extracts_it(self):
        frame = _suspend_frame(
            pending_call_id="call_ask_1", pending_kind="question",
            payload={"question": "Which role?", "options": ["Staff", "Senior"]},
            snapshot={"convo": [{"role": "user", "content": "hi"}], "plan": []})
        sus = _extract_suspend(frame)
        assert sus is not None
        assert sus["pending_call_id"] == "call_ask_1"
        assert sus["pending_kind"] == "question"
        assert sus["payload"]["options"] == ["Staff", "Senior"]
        assert sus["snapshot"]["convo"][0]["content"] == "hi"

    def test_normal_content_frame_is_not_a_suspend(self):
        frame = "data: " + json.dumps(
            {"choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": None}]})
        assert _extract_suspend(frame) is None

    def test_done_and_error_sentinels_are_not_suspends(self):
        assert _extract_suspend("data: [DONE]") is None
        assert _extract_suspend("data: [ERROR] boom") is None

    def test_suspend_without_call_id_is_ignored(self):
        # A malformed frame missing the pending_call_id can't be resumed -> not a suspend.
        frame = "data: " + json.dumps({"careeragent": {"event": "suspend", "pending_kind": "question"}})
        assert _extract_suspend(frame) is None

    def test_a_careeragent_frame_of_another_event_is_ignored(self):
        frame = "data: " + json.dumps({"careeragent": {"event": "progress", "pending_call_id": "x"}})
        assert _extract_suspend(frame) is None

    def test_suspend_frame_carries_no_assistant_content(self):
        # The suspend frame must NOT be scooped into the transcript as answer text.
        frame = _suspend_frame(pending_call_id="c1", pending_kind="approval", payload={}, snapshot={})
        content, done, error = _scan_sse(frame)
        assert content == "" and not error
