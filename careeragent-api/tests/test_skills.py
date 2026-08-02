"""
tests/test_skills.py — P7 #10 loadable coaching skills.

The skill INDEX (name+description) is injected every turn; the BODY loads on demand
via the use_skill READ tool. Verifies the loader, the tool dispatch, and that the
system prompt carries only the index (never the bodies).
"""
from agent import skills, tools
from agent.prompts import build_system_prompt

_ROSTER = {"tailor", "ats-check", "quantify-bullets", "cover-letter"}


def test_skills_load_from_files():
    assert set(skills.skill_names()) >= _ROSTER


def test_index_has_descriptions_not_bodies():
    idx = skills.skills_index()
    assert "`tailor`" in idx
    assert "Tailor the master resume" in idx          # the description
    assert "Tailoring playbook" not in idx            # NOT the body


def test_load_body_and_unknown():
    body = skills.load_body("tailor")
    assert body and "Tailoring playbook" in body
    assert skills.load_body("does-not-exist") is None
    assert skills.load_body("") is None


def test_use_skill_is_a_read_only_tool_in_every_mode():
    assert "use_skill" in tools.READ_TOOLS
    assert "use_skill" not in tools.WRITE_TOOLS
    for mode in ("plan", "default", "acceptEdits", "bypass"):
        names = {s["function"]["name"] for s in tools.schemas_for_mode(mode)}
        assert "use_skill" in names


async def test_use_skill_dispatch_returns_the_body():
    r = await tools.dispatch("use_skill", {"skill": "ats-check"}, None)   # no dossier needed
    assert r.ok and "keyword" in r.content.lower()


async def test_use_skill_unknown_is_a_teaching_error():
    r = await tools.dispatch("use_skill", {"skill": "nope"}, None)
    assert not r.ok
    assert "Available skills" in r.content and "tailor" in r.content


def test_build_system_prompt_injects_index_not_bodies():
    sp = build_system_prompt("persona", "# Profile", "acceptEdits")
    assert "## Skills" in sp
    assert "Tailor the master resume" in sp          # index description injected
    assert "Tailoring playbook" not in sp            # body is NOT injected (lazy via use_skill)
