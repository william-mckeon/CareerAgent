"""
tests/test_grounding.py — Phase 3 slice 3: the grounding gate + outcome taxonomy.

Deterministic Tier-1 unit tests (extractor + dossier oracle), then loop-level
tests that an ungrounded resume draft is re-prompted and that the turn's terminal
outcome is recorded honestly (never a blanket "success"). Scripted fake infra +
dossier — no network, no model. See
careeragent-api/specs/0005-verified-completion-and-grounding.md.
"""
import json

from agent import grounding
from agent import loop as agent_loop


# A minimal but resume-LIKE draft (>=2 section headers so the gate engages).
RESUME = """\
## Professional Summary
Backend & AI engineer.

## Core Competencies
Python, TypeScript, C++

## Relevant Experience
Built production services.

## Education
B.S. Computer Science
"""


# ---- gap-note false-positive fix (a skill discussed as a GAP, not claimed) -------
def test_gap_note_skill_is_not_flagged_as_phantom():
    # A resume that CORRECTLY omits Kubernetes but discusses it in a gap note must
    # NOT flag k8s as a phantom skill — the live self-contradicting caveat bug.
    corpus = "python fastapi docker postgresql".lower()
    draft = """\
## Professional Summary
Python backend engineer.

## Technical Skills
Python, FastAPI, Docker, PostgreSQL

## Experience
Built production services.

## Gap note
The job description lists Kubernetes as a must-have skill. Your evidence does not
contain any Kubernetes experience. If you have Kubernetes work not yet recorded,
tell me and I'll add it.
"""
    v = grounding.grounding_verdict(draft, corpus)
    assert v.checked
    assert "kubernetes" not in v.phantom_skills and "k8s" not in v.phantom_skills


def test_genuinely_unbacked_skill_in_the_body_is_still_flagged():
    # No false negative: a skill actually CLAIMED in the skills section (no gap
    # wording) that the corpus doesn't back is still caught.
    corpus = "python fastapi docker".lower()
    draft = """\
## Summary
Backend engineer.

## Technical Skills
Python, FastAPI, Azure, Docker

## Experience
Built services.
"""
    v = grounding.grounding_verdict(draft, corpus)
    assert "azure" in v.phantom_skills


def test_tightened_markers_do_not_strip_real_bullets():
    # "removed"/"missing" are NOT gap markers (they occur in real bullets), so a
    # skill named in such a bullet is still checked — the gate stays strict.
    corpus = "python fastapi docker".lower()
    draft = """\
## Summary
Engineer.

## Technical Skills
Python, FastAPI, Docker

## Experience
Removed the legacy Terraform modules; reduced missing records by 40%.
"""
    v = grounding.grounding_verdict(draft, corpus)
    assert "terraform" in v.phantom_skills   # the "removed" line is NOT stripped


def test_bare_negation_bullet_is_not_stripped():
    # Adversarial-review regression: a normal bullet with a bare negation ("doesn't
    # drop") must NOT be stripped — only specific "does not contain/list/…" gap
    # constructions are. So a fabricated skill in such a bullet is still caught.
    corpus = "python fastapi docker".lower()
    draft = """\
## Summary
Engineer.

## Technical Skills
Python, FastAPI, Docker

## Experience
Built a streaming layer that doesn't drop events at scale, powered by Azure Event Hubs.
"""
    v = grounding.grounding_verdict(draft, corpus)
    assert "azure" in v.phantom_skills       # "doesn't drop" is not a gap marker


# ============================================================ unit: scope guard
def test_looks_like_resume_true_on_resume():
    assert grounding.looks_like_resume(RESUME)


def test_looks_like_resume_false_on_chat_reply():
    # A plain reply that merely MENTIONS a technology must not be gated.
    assert not grounding.looks_like_resume("Sure — you could learn TypeScript for this role!")


def test_non_resume_text_is_not_checked():
    v = grounding.grounding_verdict("You should add TypeScript to your resume.", "python")
    assert v.checked is False
    assert v.grounded is True                       # nothing to block


# ============================================================ unit: skill oracle
def test_phantom_skill_is_flagged():
    corpus = grounding.build_corpus("William McKeon. Skills: Python, C++.", [])
    v = grounding.grounding_verdict(RESUME, corpus)
    assert v.checked and not v.grounded
    assert "typescript" in v.phantom_skills          # in the resume, absent from the profile
    assert "python" not in v.phantom_skills          # backed
    assert "c++" not in v.phantom_skills             # backed (trailing '.' doesn't defeat it)


def test_fully_grounded_resume_passes():
    corpus = grounding.build_corpus("Skills: Python, TypeScript, C++.", [])
    v = grounding.grounding_verdict(RESUME, corpus)
    assert v.checked and v.grounded


def test_word_boundary_java_not_satisfied_by_javascript():
    resume = RESUME.replace("Python, TypeScript, C++", "Java")
    corpus = grounding.build_corpus("Skills: JavaScript.", [])       # has javascript, not java
    v = grounding.grounding_verdict(resume, corpus)
    assert "java" in v.phantom_skills


def test_projects_feed_the_corpus():
    # A skill evidenced only by a PROJECT (not the profile) is still backed.
    corpus = grounding.build_corpus(
        "Skills: Python.",
        [{"name": "X", "tech_stack": "TypeScript", "summary": "a tool"}])
    v = grounding.grounding_verdict(RESUME, corpus)
    assert "typescript" not in v.phantom_skills


# ============================================================ unit: domain oracle
def test_phantom_domain_is_flagged():
    # The exact live failure: a legal-tech claim with no legal evidence anywhere.
    resume = RESUME + "\n## Selected Projects\nLegal-Tech Prototype: contract review tool.\n"
    corpus = grounding.build_corpus("Backend engineer at NUWC (defense).", [])
    v = grounding.grounding_verdict(resume, corpus)
    assert not v.grounded
    assert "legal" in v.phantom_domains or "contract" in v.phantom_domains


def test_backed_domain_not_flagged():
    resume = RESUME + "\n## Experience\nDefense systems at NUWC.\n"
    corpus = grounding.build_corpus("Computer scientist, defense, NUWC.", [])
    v = grounding.grounding_verdict(resume, corpus)
    assert "defense" not in v.phantom_domains        # really in the profile


# ============================================================ loop-level harness
class RecordingInfra:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def complete(self, payload):
        r = self._responses[self.calls]
        self.calls += 1
        return r


class FakeDossier:
    """Profile lists Python + C++ but NOT TypeScript; no legal experience."""
    def __init__(self, projects=None):
        self.calls = []
        self._projects = projects or []

    async def read_profile(self):
        self.calls.append("read_profile")
        return 200, {"content": "William McKeon. Skills: Python, C++. Employer: NUWC (defense).",
                     "version": 1}

    async def search_projects(self, params):
        self.calls.append("search_projects")
        return 200, self._projects


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


def _run(infra, dossier, outcome=None, grounding_enabled=True):
    # These tests exercise the Tier-1 grounding gate in isolation; the Guardian
    # (a separate model call) is covered in test_guardian.py, so disable it here
    # or a clean Tier-1 ship would consume an unscripted verifier call.
    return agent_loop.run_agent(
        messages=[{"role": "user", "content": "write my resume for the TS role"}],
        mode="acceptEdits", persona="CareerAgent.", infra_client=infra,
        dossier_client=dossier, max_steps=40,
        grounding_enabled=grounding_enabled, guardian_enabled=False, outcome=outcome)


async def test_ungrounded_resume_is_challenged_then_fixed():
    # finish_answer ships a resume claiming TypeScript (not in dossier) -> grounding
    # challenge -> the model re-finishes with a grounded resume.
    bad = RESUME                                     # claims TypeScript
    good = RESUME.replace("Python, TypeScript, C++", "Python, C++")
    infra = RecordingInfra([
        _completion(tool_calls=[_tc("finish_answer", {"summary": bad})]),
        _completion(tool_calls=[_tc("finish_answer", {"summary": good})]),
    ])
    outcome = {}
    out = (await _drain(_run(infra, FakeDossier(), outcome=outcome))).decode("utf-8")
    assert infra.calls == 2                          # challenged once, not shipped bad
    assert "grounding challenged" in out
    assert "typescript" not in _content(out).lower() # the phantom skill isn't in the shipped answer
    assert outcome.get("value") == agent_loop.OUTCOME_FINAL


async def test_grounded_resume_ships_first_try_outcome_final():
    good = RESUME.replace("Python, TypeScript, C++", "Python, C++")
    infra = RecordingInfra([_completion(tool_calls=[_tc("finish_answer", {"summary": good})])])
    outcome = {}
    out = (await _drain(_run(infra, FakeDossier(), outcome=outcome))).decode("utf-8")
    assert "grounding challenged" not in out
    assert outcome.get("value") == agent_loop.OUTCOME_FINAL


async def test_stubborn_ungrounded_resume_ships_after_cap_outcome_ungrounded():
    # The model keeps shipping the phantom skill -> after the cap it's let through,
    # but the turn is logged 'ungrounded', never a clean success.
    bad = _completion(tool_calls=[_tc("finish_answer", {"summary": RESUME})])
    infra = RecordingInfra([bad] * (agent_loop.GROUNDING_CHALLENGE_CAP + 1))
    outcome = {}
    out = (await _drain(_run(infra, FakeDossier(), outcome=outcome))).decode("utf-8")
    assert infra.calls == agent_loop.GROUNDING_CHALLENGE_CAP + 1
    assert outcome.get("value") == agent_loop.OUTCOME_UNGROUNDED
    # …and it ships with a user-visible caveat, not a silent ungrounded pass (Finding 3).
    body = _content(out)
    assert "Unverified" in body and "typescript" in body.lower()


async def test_gate_disabled_ships_ungrounded_without_challenge():
    infra = RecordingInfra([_completion(tool_calls=[_tc("finish_answer", {"summary": RESUME})])])
    out = (await _drain(_run(infra, FakeDossier(), grounding_enabled=False))).decode("utf-8")
    assert "grounding challenged" not in out


async def test_plain_chat_reply_is_not_grounding_gated():
    # A normal reply that mentions TypeScript (not a resume) ships untouched.
    infra = RecordingInfra([_completion(content="You could highlight TypeScript if you know it.")])
    out = (await _drain(_run(infra, FakeDossier()))).decode("utf-8")
    assert "grounding challenged" not in out
    assert "TypeScript" in _content(out)


async def test_plain_reply_unbacked_write_claim_is_unverified():
    # "I've saved your resume." with no tool call -> not a resume draft, but an
    # over-claimed write -> the turn is logged 'unverified', not a clean 'final'.
    infra = RecordingInfra([_completion(content="I've saved your resume.")])
    outcome = {}
    await _drain(_run(infra, FakeDossier(), outcome=outcome))
    assert outcome.get("value") == agent_loop.OUTCOME_UNVERIFIED


# ============================= review-fix regressions (adversarial pass) =========
def test_generic_prose_is_not_flagged():
    # Confirmed false positives: 'go'/'energy'/'compliance' as ordinary English in a
    # summary must NOT be treated as skill/domain CLAIMS (they're out of the vocab,
    # and domain scanning skips the summary region anyway).
    resume = ("## Professional Summary\nA high-energy engineer ready to go the extra mile who "
              "ensured compliance with security standards.\n"
              "## Skills\nPython\n## Experience\nBuilt services.\n## Education\nB.S.\n")
    corpus = grounding.build_corpus("Skills: Python.", [])
    v = grounding.grounding_verdict(resume, corpus)
    assert v.grounded, (v.phantom_skills, v.phantom_domains)


def test_alias_spellings_are_backed():
    # A dossier that says 'postgres/nodejs/dotnet' must back a resume that writes the
    # formal 'PostgreSQL/Node.js/.NET' — no false phantom from spelling divergence.
    resume = "## Skills\nPostgreSQL, Node.js, .NET\n## Experience\nx\n## Education\ny\n"
    corpus = grounding.build_corpus("Stack: postgres, nodejs, dotnet.", [])
    v = grounding.grounding_verdict(resume, corpus)
    assert v.grounded, (v.phantom_skills,)


def test_single_word_headers_engage_the_gate():
    # Single-word 'Summary/Skills/Experience/Projects' headers are the common layout —
    # they must still count as a resume, or fabrication slips through unchecked.
    resume = "Summary\nEngineer.\nSkills\nPython, TypeScript\nExperience\nx\nEducation\ny\n"
    assert grounding.looks_like_resume(resume)
    v = grounding.grounding_verdict(resume, grounding.build_corpus("Skills: Python.", []))
    assert "typescript" in v.phantom_skills


def test_domain_in_objective_is_not_experience():
    # Stating the industry you're SEEKING (in an Objective) is not a claim of experience.
    resume = "Objective\nSeeking a fintech role.\nSkills\nPython\nEducation\nB.S.\n"
    v = grounding.grounding_verdict(resume, grounding.build_corpus("Backend engineer (defense). Python.", []))
    assert "fintech" not in v.phantom_domains     # 'seeking fintech' is aspiration, not experience
    assert v.grounded                             # (Python is backed, no phantoms)


def test_empty_corpus_is_not_checked():
    # Dossier unavailable -> empty corpus -> we cannot verify -> do NOT block.
    v = grounding.grounding_verdict(RESUME, "")
    assert v.checked is False and v.grounded


# ===================== invented-PROJECT regressions (live-audit findings) =========
# An audit replayed the deployed module against the real dossier and found that a
# fabricated PROJECT shipped with grounded=True — a vocabulary can't catch it,
# because an invented project is built from ordinary words. These lock the fix.

_REAL_CORPUS = grounding.build_corpus(
    "William McKeon. Backend & AI Engineer at NUWC (defense). Skills: Python, C++, SQL, Azure. "
    "Active DoD Secret Clearance. B.S. Computer Science, University of Rhode Island 2022.",
    [{"name": "OpenAgent", "summary": "agentic AI platform", "tech_stack": "Python, FastAPI, Docker"},
     {"name": "OpenAgent-os", "summary": "microservice OS", "tech_stack": "Python, TypeScript",
      "languages": "Python 70%, TypeScript 20%"},
     {"name": "datasetforge", "summary": "synthetic data tool", "tech_stack": "Python"}])


def test_invented_project_is_caught():
    # The audit's smoking gun: this exact draft previously returned grounded=True.
    draft = ("## Professional Summary\nBackend engineer.\n## Selected Projects\n"
             "| Project | Role | Tech Stack |\n| Quantum Trading Engine | Lead | Python |\n"
             "## Education\nB.S.\n")
    v = grounding.grounding_verdict(draft, _REAL_CORPUS)
    assert not v.grounded
    assert "Quantum Trading Engine" in v.phantom_projects


def test_domain_neutral_invented_project_is_caught():
    # 'Secure Data Pipelines' is the fabrication that actually reached the dossier.
    # It uses only ordinary words, so the skill/domain vocabularies never see it.
    draft = ("## Summary\nEngineer.\n## Selected Projects\n"
             "**Secure Data Pipelines** – built high-throughput ingestion\n## Education\nB.S.\n")
    v = grounding.grounding_verdict(draft, _REAL_CORPUS)
    assert not v.grounded
    assert "Secure Data Pipelines" in v.phantom_projects


def test_real_projects_are_not_flagged():
    # False-positive guard: the user's actual projects must survive untouched.
    draft = ("## Summary\nEngineer.\n## Selected Projects\n"
             "**OpenAgent** – Open-source agentic AI platform\n"
             "**OpenAgent-os** – microservice OS\n"
             "**datasetforge** – synthetic data tool\n## Education\nB.S.\n")
    v = grounding.grounding_verdict(draft, _REAL_CORPUS)
    assert v.grounded, (v.phantom_projects, v.phantom_skills, v.phantom_domains)


def test_typescript_backed_by_a_project_is_not_flagged():
    # TypeScript IS in the corpus via OpenAgent-os. The corpus-starvation bug (dossier
    # search_projects omitting languages/summary) is what made this look phantom.
    draft = ("## Professional Summary\nEngineer with Python and TypeScript.\n"
             "## Core Competencies\nPython, TypeScript, C++\n## Education\nB.S.\n")
    v = grounding.grounding_verdict(draft, _REAL_CORPUS)
    assert "typescript" not in v.phantom_skills


def test_table_headers_are_not_treated_as_projects():
    # A markdown table's header row must not be read as a project named "Project".
    draft = ("## Summary\nEngineer.\n## Selected Projects\n"
             "| Project | Role | Tech Stack |\n|---|---|---|\n"
             "| OpenAgent | Creator | Python |\n## Education\nB.S.\n")
    v = grounding.grounding_verdict(draft, _REAL_CORPUS)
    assert v.grounded, v.phantom_projects


def test_projects_outside_a_projects_section_are_not_extracted():
    # Only the Projects section is scanned — experience prose isn't mined for titles.
    draft = ("## Summary\nEngineer.\n## Experience\nBuilt a Quantum Trading Engine thing.\n"
             "## Education\nB.S.\n")
    v = grounding.grounding_verdict(draft, _REAL_CORPUS)
    assert v.phantom_projects == []


def test_model_error_placeholder_is_not_replayed_as_history():
    # During the 2026-07-16 outage the canned "couldn't reach the model" reply was
    # stored by the frontend and fed back as the assistant's prior turn. It is not an
    # answer and must be stripped from incoming history.
    kept = agent_loop._strip_system([
        {"role": "system", "content": "ignore me"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": agent_loop.MODEL_ERROR_TEXT},
        {"role": "user", "content": "hello"},
    ])
    assert {"role": "assistant", "content": agent_loop.MODEL_ERROR_TEXT} not in kept
    assert [m["role"] for m in kept] == ["user", "user"]     # system + placeholder dropped


async def test_real_assistant_replies_survive_history_stripping():
    # Guard the above: a genuine assistant turn must NOT be dropped.
    kept = agent_loop._strip_system([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Here's your resume feedback."},
    ])
    assert len(kept) == 2 and kept[1]["content"] == "Here's your resume feedback."


# ---------------------------------------------------------------------------
# Web-citation grounding (P7 /fetch) — a cited URL must be one the coach fetched
# this turn or one already in the person's dossier corpus.
# ---------------------------------------------------------------------------
class TestNormUrl:
    def test_lowercases_scheme_and_host_drops_fragment_and_trailing_slash(self):
        assert grounding.norm_url("HTTPS://Greenhouse.IO/Acme/Job/1/#apply") == \
            "https://greenhouse.io/Acme/Job/1"

    def test_keeps_query(self):
        # A job posting often lives at ?gh_jid=123 — the query must survive.
        assert grounding.norm_url("https://boards.greenhouse.io/x?gh_jid=99") == \
            "https://boards.greenhouse.io/x?gh_jid=99"

    def test_strips_trailing_sentence_punctuation(self):
        assert grounding.norm_url("https://example.com/jobs.") == "https://example.com/jobs"

    def test_rejects_non_http(self):
        assert grounding.norm_url("mailto:x@y.com") == ""
        assert grounding.norm_url("ftp://host/f") == ""
        assert grounding.norm_url("not a url") == ""
        assert grounding.norm_url("") == ""


class TestCitedUrls:
    def test_extracts_urls_from_prose(self):
        text = "See the role at https://acme.com/jobs/1 and https://globex.io/careers?id=2 today."
        assert grounding.cited_urls(text) == {
            "https://acme.com/jobs/1", "https://globex.io/careers?id=2"}

    def test_url_in_parens_not_swallowed(self):
        # The trailing ')' must not become part of the URL.
        assert grounding.cited_urls("(source: https://acme.com/x)") == {"https://acme.com/x"}

    def test_no_urls(self):
        assert grounding.cited_urls("no links here") == set()


class TestWebCitationVerdict:
    def test_clean_when_no_urls(self):
        v = grounding.web_citation_verdict("plain answer, no links", set())
        assert v.clean and v.phantom_urls == []

    def test_fetched_url_is_clean(self):
        fetched = {"https://acme.com/jobs/1"}
        v = grounding.web_citation_verdict("The role at https://acme.com/jobs/1 wants Python.", fetched)
        assert v.clean

    def test_uncited_unfetched_url_is_phantom(self):
        v = grounding.web_citation_verdict(
            "The posting at https://acme.com/jobs/9 requires 5 years of Rust.", set())
        assert not v.clean
        assert v.phantom_urls == ["https://acme.com/jobs/9"]
        assert "did NOT fetch" in v.message()
        assert "acme.com/jobs/9" in v.caveat()

    def test_dossier_corpus_url_is_allowed(self):
        # The user's OWN GitHub link (in the corpus) must not be flagged a phantom.
        corpus = "Projects: my API — repo https://github.com/me/api"
        v = grounding.web_citation_verdict(
            "Your project at https://github.com/me/api shows strong Go work.", set(), corpus)
        assert v.clean

    def test_final_url_after_redirect_matches(self):
        # fetched_urls holds the post-redirect URL; a cite of it is clean.
        fetched = {"https://boards.greenhouse.io/acme/jobs/7"}
        v = grounding.web_citation_verdict(
            "Per https://boards.greenhouse.io/acme/jobs/7 they want Kubernetes.", fetched)
        assert v.clean

    def test_mixed_one_phantom_one_ok(self):
        fetched = {"https://acme.com/a"}
        v = grounding.web_citation_verdict(
            "From https://acme.com/a and https://evil.test/b ...", fetched)
        assert v.phantom_urls == ["https://evil.test/b"]

    def test_dossier_corpus_MIXEDCASE_url_is_allowed(self):
        # build_corpus lowercases the corpus in prod; the coach cites the user's OWN
        # link in original mixed case (GitHub org/repo names commonly have uppercase).
        # Matching MUST be case-insensitive or this false-flags the user's own repo.
        corpus = "projects: portfolio https://github.com/islander-intel/resume-helper"
        v = grounding.web_citation_verdict(
            "Your repo https://github.com/Islander-Intel/Resume-Helper shows strong Go work.",
            set(), corpus)
        assert v.clean

    def test_fetched_url_matches_case_insensitively(self):
        fetched = {"https://acme.com/Jobs/PM-Role"}
        v = grounding.web_citation_verdict(
            "The role at https://acme.com/jobs/pm-role wants Python.", fetched)
        assert v.clean

    def test_paren_in_path_records_and_cites_consistently(self):
        # cited_urls stops at ')', so RECORDING via cited_urls keeps the recorded and
        # cited forms identical — no phantom for a genuinely-fetched parenthesised URL.
        recorded = grounding.cited_urls("https://en.wikipedia.org/wiki/Role_(backend)")
        v = grounding.web_citation_verdict(
            "Per https://en.wikipedia.org/wiki/Role_(backend) they want Kubernetes.", recorded)
        assert v.clean


# --- resume-seed: a page fetched before a suspend still counts after resume -----
def test_seed_fetched_from_convo_rebuilds_ledger_on_resume():
    convo = [
        {"role": "user", "content": "look at the posting"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "t1", "function": {
             "name": "fetch_url",
             "arguments": '{"url": "https://boards.greenhouse.io/acme/jobs/123"}'}}]},
        {"role": "tool", "tool_call_id": "t1",
         "content": "Fetched: https://boards.greenhouse.io/acme/jobs/123\n"
                    ">>> BEGIN FETCHED PAGE\nbody\n>>> END FETCHED PAGE"},
        {"role": "user", "content": "yes track it"},
    ]
    seeded: set = set()
    agent_loop._seed_fetched_from_convo(convo, seeded)
    assert "https://boards.greenhouse.io/acme/jobs/123" in seeded


def test_seed_ignores_urls_in_the_page_body():
    # Only the 'Fetched:' header URL is taken — a URL inside the page BODY was not
    # fetched by the coach and must not be allow-listed.
    convo = [{"role": "tool", "tool_call_id": "t1",
              "content": "Fetched: https://acme.com/jobs/1\n>>> BEGIN FETCHED PAGE\n"
                         "Apply at https://evil.example.com/phish\n>>> END FETCHED PAGE"}]
    seeded: set = set()
    agent_loop._seed_fetched_from_convo(convo, seeded)
    assert seeded == {"https://acme.com/jobs/1"}


def test_seed_is_a_noop_on_a_plain_turn():
    empty: set = set()
    agent_loop._seed_fetched_from_convo(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}], empty)
    assert empty == set()


# --- web_search feeds the same web-citation ledger (P7 web search) --------------
def test_record_fetched_registers_web_search_urls():
    class R:
        ok = True
        structured = {"op": "searched", "urls": ["https://a.com/1", "https://b.io/careers"]}
    s: set = set()
    agent_loop._record_fetched("web_search", {"query": "x"}, R(), s)
    assert s == {"https://a.com/1", "https://b.io/careers"}


def test_seed_from_convo_registers_only_result_urls_not_query_or_snippet():
    # Resume-seed must register ONLY the surfaced result URLs (each at column 0 as
    # "N. <url>"), matching the live path — NOT a URL in the model-authored query
    # header or a snippet, which would launder a fabricated source past the gate.
    convo = [{"role": "tool", "tool_call_id": "t1",
              "content": "Web search results for: comp per https://evil.example/fake  (2 result(s))\n"
                         ">>> BEGIN WEB SEARCH RESULTS\n"
                         "1. https://acme.com/1\n   Acme PM\n   see also https://snippet.example/x\n"
                         "2. https://globex.io/careers\n   Globex\n"
                         ">>> END WEB SEARCH RESULTS"}]
    s: set = set()
    agent_loop._seed_fetched_from_convo(convo, s)
    assert s == {"https://acme.com/1", "https://globex.io/careers"}
    assert "https://evil.example/fake" not in s       # query-header URL not laundered
    assert "https://snippet.example/x" not in s        # snippet URL not registered
