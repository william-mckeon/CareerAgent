"""
tests/test_compaction.py — P6 #11 context compaction.

Unit-tests the estimator, the Converse-safe cut, the deterministic briefing (echoes
ONLY real ledger receipts), and compact() (drops the oldest turns, fail-soft). Then
integration: compaction fires over threshold in the loop, the trimmed convo stays a
valid Converse conversation, and a compact->pause snapshot is replay-valid.
"""
import json

from agent import compaction
from agent import loop as agent_loop


# --------------------------------------------------------------------- fakes
class FakeInfra:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.payloads = []

    async def complete(self, payload):
        self.payloads.append(payload)
        r = self._responses[self.calls]
        self.calls += 1
        return r


class BoomInfra:
    async def complete(self, payload):
        raise RuntimeError("summarizer down")


class FakeDossier:
    def __init__(self):
        self.calls = []

    async def read_profile(self):
        self.calls.append("read_profile")
        return 200, {"content": "# Profile", "version": 1}


def _completion(content="", tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg}]}


def _tool_call(name, arguments_json):
    return {"id": f"call_{name}", "type": "function",
            "function": {"name": name, "arguments": arguments_json}}


async def _drain(gen):
    out = b""
    async for chunk in gen:
        out += chunk
    return out


# --------------------------------------------------------------- estimator
def test_estimate_tokens_uses_prior_usage_first():
    est = compaction.estimate_tokens([{"role": "user", "content": "hi"}], None,
                                     prior_usage={"prompt_tokens": 12345})
    assert est == 12345


def test_estimate_tokens_char_fallback_counts_schemas():
    convo = [{"role": "system", "content": "x" * 400}]
    schemas = [{"type": "function", "function": {"name": "t", "description": "d" * 400}}]
    with_schemas = compaction.estimate_tokens(convo, schemas, prior_usage=None)
    without = compaction.estimate_tokens(convo, None, prior_usage=None)
    assert with_schemas > without > 0     # the tool schemas count toward the budget


# ------------------------------------------------------------- the safe cut
def _long_convo(n_pairs):
    convo = [{"role": "system", "content": "SYS"}]
    for i in range(n_pairs):
        convo.append({"role": "user", "content": f"user {i} " + "x" * 200})
        convo.append({"role": "assistant", "content": f"assistant {i} " + "y" * 200})
    return convo


def test_find_compaction_cut_lands_on_a_user_boundary():
    convo = _long_convo(10)
    cut = compaction.find_compaction_cut(convo, keep_recent=6)
    assert cut is not None
    assert convo[cut]["role"] == "user"      # keep-region starts with a user turn (Converse-valid)
    assert cut > 1


def test_find_compaction_cut_none_when_short():
    assert compaction.find_compaction_cut(_long_convo(2), keep_recent=8) is None


def test_current_request_is_the_last_user_turn():
    # The frontend replays the whole history, so the active task is the MOST-RECENT
    # user turn — not the oldest (which would mislabel a superseded task).
    convo = [{"role": "system", "content": "s"},
             {"role": "user", "content": "tailor my resume for Stripe"},
             {"role": "assistant", "content": "done"},
             {"role": "user", "content": "actually, write a cover letter for Meta"}]
    assert compaction.current_request(convo) == "actually, write a cover letter for Meta"


# --------------------------------------------------------------- briefing
def test_build_briefing_echoes_only_real_receipts():
    b = compaction.build_briefing(
        "tailor for Stripe",
        [{"op": "saved_resume", "id": "abc"}, {"op": "updated_application"}],
        "Gathered the JD and 2 projects.")
    assert "tailor for Stripe" in b
    assert "saved_resume" in b and "updated_application" in b
    assert "Gathered the JD" in b


def test_build_briefing_no_completed_line_without_a_ledger():
    b = compaction.build_briefing("do X", [], "summary text")
    assert "Already completed" not in b     # nothing claimed done when the ledger is empty
    assert "summary text" in b


# --------------------------------------------------------------- compact()
async def test_compact_drops_oldest_keeps_recent_and_stays_converse_valid():
    convo = _long_convo(10)
    orig_len = len(convo)
    infra = FakeInfra([_completion(content="Running summary of the early turns.")])
    summary = await compaction.compact(convo, infra, keep_recent=6, effort="low")
    assert summary == "Running summary of the early turns."
    assert len(convo) < orig_len                      # oldest turns dropped
    assert convo[0]["role"] == "system"
    assert convo[1]["role"] == "user"                 # trimmed convo starts with a user turn
    assert infra.payloads[0]["tools"] == []           # summarizer is tools-disabled


async def test_compact_is_fail_soft_on_summarizer_error():
    convo = _long_convo(10)
    orig = list(convo)
    summary = await compaction.compact(convo, BoomInfra(), keep_recent=6, effort="low")
    assert summary is None
    assert convo == orig                              # a failed summarize leaves the convo intact


async def test_compact_none_when_nothing_to_trim():
    convo = _long_convo(2)
    assert await compaction.compact(convo, FakeInfra([]), keep_recent=8, effort="low") is None


# ----------------------------------------------------------- in the loop
def _big_history(n):
    msgs = []
    for i in range(n):
        msgs.append({"role": "user", "content": f"turn {i}: " + "x" * 3000})
        msgs.append({"role": "assistant", "content": f"reply {i}: " + "y" * 3000})
    msgs.append({"role": "user", "content": "now finish the task"})
    return msgs


async def test_compaction_fires_over_threshold_and_trims_the_coach_payload():
    infra = FakeInfra([
        _completion(content="Summary of the earlier turns."),   # the summarizer call
        _completion(content="All done."),                        # the coach's turn
    ])
    out = (await _drain(agent_loop.run_agent(
        messages=_big_history(12),
        mode="acceptEdits", persona="p", infra_client=infra, dossier_client=FakeDossier(),
        compact_token_threshold=2000, compact_keep_recent=6,
    ))).decode("utf-8")
    assert "compacted context" in out
    # The coach turn is the payload WITH tools; its messages were trimmed and start
    # with system then a user turn (Converse-valid).
    coach_payloads = [p for p in infra.payloads if p.get("tools")]
    assert coach_payloads, "expected a coach turn with a tool catalog"
    coach_msgs = coach_payloads[0]["messages"]
    assert coach_msgs[0]["role"] == "system"
    assert coach_msgs[1]["role"] == "user"
    assert len(coach_msgs) < len(_big_history(12)) + 1     # fewer than the full replayed history


def _parse_suspend_snapshot(out_text):
    for line in out_text.split("\n"):
        line = line.strip()
        if not line.startswith("data: "):
            continue
        try:
            obj = json.loads(line[len("data: "):])
        except (json.JSONDecodeError, ValueError):
            continue
        ca = obj.get("careeragent")
        if isinstance(ca, dict) and ca.get("event") == "suspend":
            return ca.get("snapshot")
    return None


async def test_compact_then_pause_snapshot_is_replay_valid():
    infra = FakeInfra([
        _completion(content="Summary of the earlier turns."),   # summarizer
        _completion(tool_calls=[_tool_call("ask_user",
                    '{"question": "Which role should I target?", "options": ["A", "B"]}')]),
    ])
    out = (await _drain(agent_loop.run_agent(
        messages=_big_history(12),
        mode="acceptEdits", persona="p", infra_client=infra, dossier_client=FakeDossier(),
        compact_token_threshold=2000, compact_keep_recent=6,
    ))).decode("utf-8")
    snap = _parse_suspend_snapshot(out)
    assert snap is not None
    convo = snap["convo"]
    # Post-compaction suspend snapshot must be a valid Converse conversation:
    # starts with a user turn, ends with the assistant's (still-pending) ask_user call.
    assert convo[0]["role"] == "user"
    assert convo[-1]["role"] == "assistant"
    assert any((tc.get("function") or {}).get("name") == "ask_user"
               for tc in (convo[-1].get("tool_calls") or []))
