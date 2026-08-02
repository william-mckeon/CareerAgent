"""
tests/test_verified_completion.py — Phase 3 slice 2: the verified-completion gate.

A finish_answer whose summary CLAIMS a write (saved/updated/created…) is not
accepted unless a verified dossier write is on record this turn — it's challenged
and the loop continues (bounded by a cap so a stubborn model still finishes). A
plain answer with no write-claim finishes normally. Scripted fake infra + dossier.
See careeragent-api/specs/0005-verified-completion-and-grounding.md.
"""
import json

from agent import loop as agent_loop


class RecordingInfra:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def complete(self, payload):
        r = self._responses[self.calls]
        self.calls += 1
        return r


class FakeDossier:
    def __init__(self):
        self.calls = []

    async def read_profile(self):
        self.calls.append("read_profile")
        return 200, {"content": "# Profile", "version": 1}

    async def save_resume(self, aid, content):
        self.calls.append(("save_resume", aid, content))
        return 200, {"version": 2}                    # a real receipt -> verified write


def _tc(name, args):
    return {"id": f"call_{name}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def _completion(content="", tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg}]}


async def _drain(gen):
    out = b""
    async for chunk in gen:
        out += chunk
    return out


def _content(out: str) -> str:
    """Only the user-facing answer (delta.content) — not the reasoning channel,
    where the 🔧 tool-activity line echoes the finish_answer args."""
    parts = []
    for line in out.splitlines():
        if not line.startswith("data:"):
            continue
        p = line[5:].strip()
        if not p.startswith("{"):
            continue
        try:
            d = ((json.loads(p).get("choices") or [{}])[0]).get("delta") or {}
        except Exception:
            continue
        if isinstance(d.get("content"), str):
            parts.append(d["content"])
    return "".join(parts)


class FakeReview:
    def __init__(self, reviewed=5):
        self._reviewed = reviewed

    async def review_repos(self, repos=None, limit=None, focus=None, force=False):
        return 200, {"reviewed": self._reviewed, "skipped": 0, "errors": 0, "outcomes": []}


def _run(infra, dossier, mode="acceptEdits", review_client=None):
    return agent_loop.run_agent(
        messages=[{"role": "user", "content": "do the task"}], mode=mode,
        persona="CareerAgent.", infra_client=infra, dossier_client=dossier,
        review_client=review_client, max_steps=40)


async def test_unbacked_save_claim_is_challenged_then_completes():
    # finish_answer claims "saved" with NO write on record -> challenged; the model
    # then actually saves and finishes.
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("finish_answer", {"summary": "I've saved your resume."})]),
        _completion(tool_calls=[_tc("save_resume", {"application_id": "a1", "content": "CV"})]),
        _completion(tool_calls=[_tc("finish_answer", {"summary": "Saved."})]),
    ])
    dossier = FakeDossier()
    out = (await _drain(_run(infra, dossier))).decode("utf-8")
    answer = _content(out)
    assert infra.calls == 3                                   # challenged, not accepted at step 0
    assert any(isinstance(c, tuple) and c[0] == "save_resume" for c in dossier.calls)
    assert "completion challenged" in out                    # the gate fired (reasoning channel)
    assert "I've saved your resume." not in answer           # the unbacked claim was NOT the answer
    assert "Saved." in answer                                # finished after the real write


async def test_backed_save_claim_finishes():
    # A real save happened this turn -> the ledger backs the claim -> finish accepted.
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("save_resume", {"application_id": "a1", "content": "CV"})]),
        _completion(tool_calls=[_tc("finish_answer", {"summary": "Saved your resume."})]),
    ])
    dossier = FakeDossier()
    out = (await _drain(_run(infra, dossier))).decode("utf-8")
    assert infra.calls == 2                                   # NOT challenged
    assert "completion challenged" not in out
    assert "Saved your resume." in out


async def test_plain_answer_with_no_write_claim_finishes():
    # No write-claim language -> the gate never fires (no false trigger).
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("finish_answer",
            {"summary": "Your profile looks strong in backend engineering."})]),
    ])
    out = (await _drain(_run(infra, FakeDossier()))).decode("utf-8")
    assert "completion challenged" not in out
    assert "Your profile looks strong in backend engineering." in out


async def test_challenge_cap_lets_a_stubborn_finish_through():
    # The model keeps claiming a save without doing it -> after the cap it finishes
    # (no dead loop).
    claim = _completion(tool_calls=[_tc("finish_answer", {"summary": "I saved your resume."})])
    infra = RecordingInfra([claim, claim, claim])
    out = (await _drain(_run(infra, FakeDossier()))).decode("utf-8")
    assert infra.calls == 1 + agent_loop.COMPLETION_CHALLENGE_CAP   # CAP challenges, then accept
    assert "I saved your resume." in _content(out)


async def test_advisory_reply_verb_without_dossier_noun_is_not_challenged():
    # A write verb with NO dossier noun ("updated my recommendation") is advice, not a claim.
    infra = RecordingInfra([_completion(tool_calls=[_tc("finish_answer",
        {"summary": "I updated my recommendation and added a few suggestions below."})])])
    out = (await _drain(_run(infra, FakeDossier()))).decode("utf-8")
    assert "completion challenged" not in out


async def test_review_repos_write_backs_the_claim():
    # review_repos filed projects -> a verified write -> "saved N projects" is NOT challenged
    # (regression for the review's ship-blocking finding).
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("review_repos", {})]),
        _completion(tool_calls=[_tc("finish_answer",
            {"summary": "Reviewed your repos and saved 5 projects."})]),
    ])
    out = (await _drain(_run(infra, FakeDossier(), review_client=FakeReview(reviewed=5)))).decode("utf-8")
    assert "completion challenged" not in out
    assert "saved 5 projects" in _content(out)


async def test_finish_alongside_write_in_same_batch_is_not_challenged():
    # A [finish_answer, save_resume] batch where the write LANDS a receipt -> the
    # ledger backs the claim (the gate now evaluates the final ledger, not the mere
    # presence of a write name in the batch) -> no false challenge.
    infra = RecordingInfra([_completion(tool_calls=[
        _tc("finish_answer", {"summary": "Saved your resume."}),
        _tc("save_resume", {"application_id": "a1", "content": "CV"})])])
    dossier = FakeDossier()
    out = (await _drain(_run(infra, dossier))).decode("utf-8")
    assert "completion challenged" not in out
    assert any(isinstance(c, tuple) and c[0] == "save_resume" for c in dossier.calls)
    assert "Saved your resume." in _content(out)


async def test_finish_alongside_a_write_that_does_not_land_is_challenged():
    # P6-review fix: a [finish_answer(claims save), save_resume] batch where the write
    # returns NO receipt (didn't actually land) must STILL be challenged. The old
    # name-based `batch_has_write` laundered this into a clean finish; the gate now
    # runs after the batch against the FINAL (empty) ledger.
    class NoReceiptDossier(FakeDossier):
        async def save_resume(self, aid, content):
            self.calls.append(("save_resume", aid, content))
            return 200, {}          # 2xx but NO receipt -> verified=False -> not in ledger

    infra = RecordingInfra([
        _completion(tool_calls=[
            _tc("finish_answer", {"summary": "I've saved your resume."}),
            _tc("save_resume", {"application_id": "a1", "content": "CV"})]),
        _completion(content="On reflection that write didn't go through — let me retry."),
    ])
    dossier = NoReceiptDossier()
    out = (await _drain(_run(infra, dossier))).decode("utf-8")
    assert "completion challenged" in out                       # NOT laundered by the write name
    assert any(isinstance(c, tuple) and c[0] == "save_resume" for c in dossier.calls)
    assert "didn't go through" in _content(out)


def test_render_claim_vocabulary_is_covered():
    # P7 #16: the gate must recognize a render over-claim (a falsely-claimed PDF
    # after a failed render, with an empty ledger), and NOT challenge a backed one.
    from agent import loop
    # unbacked render claims (empty ledger) -> challenged
    assert loop._claims_unbacked_write("I generated your résumé as a downloadable PDF.", []) is True
    assert loop._claims_unbacked_write("Rendered your resume to a Word document.", []) is True
    # backed by a rendered_resume receipt -> NOT challenged
    assert loop._claims_unbacked_write(
        "Your PDF is ready to download.", [{"op": "rendered_resume", "artifact_id": "x"}]) is False
    # a render verb without a dossier/artifact noun -> no false trigger
    assert loop._claims_unbacked_write("I generated a few ideas for you.", []) is False
