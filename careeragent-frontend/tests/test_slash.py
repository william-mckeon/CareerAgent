"""
tests/test_slash.py — the /slash command expansion (pure; no Streamlit/network).

Verifies expand_slash() maps each command to the right request, appends trailing
task detail, carries the per-turn mode ONLY for /plan, and passes non-command text
through untouched.
"""
from frontend.slash import SLASH_COMMANDS, expand_slash


class TestExpandSlash:
    def test_non_command_passthrough(self):
        assert expand_slash("just a normal message") == ("just a normal message", None)

    def test_empty_and_none(self):
        assert expand_slash("") == ("", None)
        assert expand_slash(None) == (None, None)

    def test_unknown_command_passthrough(self):
        # A leading slash that isn't a known command is left alone (not swallowed).
        assert expand_slash("/notacommand do stuff") == ("/notacommand do stuff", None)

    def test_skill_command_says_use_skill(self):
        text, mode = expand_slash("/tailor")
        assert "Use your `tailor` skill." in text
        assert mode is None

    def test_appends_trailing_detail(self):
        text, _ = expand_slash("/ats-check the JD is: Senior PM at Acme")
        assert text.startswith("Run an ATS keyword-coverage check")
        assert text.endswith("the JD is: Senior PM at Acme")
        assert "\n\n" in text            # detail is on its own block

    def test_review_repos_is_a_background_request(self):
        text, mode = expand_slash("/review-repos")
        assert "background job" in text
        assert mode is None              # not a mode command
        assert "Use your" not in text    # action command, no skill body

    def test_reminders_asks_for_due_and_stale(self):
        text, mode = expand_slash("/reminders")
        assert "follow-up" in text and "stale" in text
        assert mode is None

    def test_linkedin_review_invokes_skill(self):
        text, mode = expand_slash("/linkedin-review")
        assert "linkedin-review` skill" in text
        assert mode is None

    def test_recommend_jobs_invokes_skill(self):
        text, mode = expand_slash("/recommend-jobs")
        assert "recommend-jobs` skill" in text
        assert mode is None

    def test_deep_review_invokes_skill(self):
        text, mode = expand_slash("/deep-review william-mckeon/openagent-code")
        assert "deep-code-review` skill" in text
        assert text.endswith("william-mckeon/openagent-code")   # repo appended as detail
        assert mode is None

    def test_content_ideas_invokes_skill(self):
        text, mode = expand_slash("/content-ideas")
        assert "code-content-ideas` skill" in text
        assert mode is None

    def test_fetch_passes_url_through(self):
        text, mode = expand_slash("/fetch https://greenhouse.io/acme/jobs/123")
        assert text.startswith("Fetch this job posting")
        assert text.endswith("https://greenhouse.io/acme/jobs/123")
        assert mode is None
        assert "Use your" not in text     # action command, no skill body

    def test_plan_sets_plan_mode(self):
        text, mode = expand_slash("/plan rewrite all 6 bullets")
        assert mode == "plan"                       # the whole point: read-only plan mode
        assert text.startswith("Plan this task")
        assert text.endswith("rewrite all 6 bullets")

    def test_case_insensitive_and_trimmed(self):
        text, mode = expand_slash("  /PLAN  do the thing  ")
        assert mode == "plan"
        assert text.endswith("do the thing")

    def test_only_plan_carries_a_mode(self):
        # Guard against a stray mode leaking onto a non-/plan command.
        for name, spec in SLASH_COMMANDS.items():
            _, mode = expand_slash(f"/{name}")
            assert (mode == "plan") == (name == "plan"), name

    def test_every_command_expands_nonempty(self):
        for name in SLASH_COMMANDS:
            text, _ = expand_slash(f"/{name}")
            assert text and not text.startswith("/"), name
