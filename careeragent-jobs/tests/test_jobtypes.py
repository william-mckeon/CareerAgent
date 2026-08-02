"""
tests/test_jobtypes.py — the job-kind handlers (pure; fake leaf clients).

Verifies review_repos builds the friendly summary from a 2xx review-batch result
and RAISES on any non-success so the worker's retry_or_fail can act, and that the
#18b reminder kinds (follow_up_scan, resume_freshness) return a message when work
is due, the EMPTY string when nothing is due (→ worker skips the inject), and RAISE
on a dossier failure / missing client. No network.
"""
import pytest

from jobtypes import (
    Deps,
    handle_follow_up_scan,
    handle_resume_freshness,
    handle_review_repos,
)


class FakeReview:
    """Records the review_batch call and returns a canned (status, body)."""

    def __init__(self, status, body):
        self._status = status
        self._body = body
        self.calls = []

    async def review_batch(self, repos=None, limit=None, focus=None, force=False):
        self.calls.append({"repos": repos, "limit": limit, "focus": focus, "force": force})
        return self._status, self._body


class TestHandleReviewRepos:
    async def test_builds_summary_from_counts(self):
        review = FakeReview(200, {"reviewed": 3, "skipped": 1, "errors": 0, "outcomes": []})
        summary = await handle_review_repos(
            {"repos": ["a/b"], "limit": 5, "focus": "backend", "force": True},
            Deps(review=review),
        )
        assert "reviewed 3" in summary
        assert "skipped 1" in summary
        assert "0 error(s)" in summary
        assert "projects library is updated" in summary
        # spec was forwarded verbatim to the review client.
        assert review.calls == [
            {"repos": ["a/b"], "limit": 5, "focus": "backend", "force": True}
        ]

    async def test_missing_spec_fields_default_cleanly(self):
        review = FakeReview(200, {"reviewed": 0, "skipped": 0, "errors": 0})
        summary = await handle_review_repos({}, Deps(review=review))
        assert "reviewed 0" in summary
        assert review.calls == [{"repos": None, "limit": None, "focus": None, "force": False}]

    async def test_non_2xx_raises(self):
        review = FakeReview(500, {"detail": "review harness exploded"})
        with pytest.raises(Exception) as exc:
            await handle_review_repos({}, Deps(review=review))
        assert "review harness exploded" in str(exc.value)

    async def test_transport_error_raises(self):
        # ReviewClient surfaces a transport failure as (0, {"error": ...}).
        review = FakeReview(0, {"error": "ConnectError: refused"})
        with pytest.raises(Exception) as exc:
            await handle_review_repos({}, Deps(review=review))
        assert "refused" in str(exc.value)

    async def test_2xx_without_counts_raises(self):
        # A 200 that doesn't carry the expected shape is treated as a failure.
        review = FakeReview(200, {"unexpected": True})
        with pytest.raises(Exception):
            await handle_review_repos({}, Deps(review=review))


class FakeDossier:
    """Records the search_applications call and returns a canned (status, body)."""

    def __init__(self, status, body):
        self._status = status
        self._body = body
        self.calls = []

    async def search_applications(self, status=None, stale=None,
                                  follow_up_due=None, limit=200):
        self.calls.append({"status": status, "stale": stale,
                           "follow_up_due": follow_up_due, "limit": limit})
        return self._status, self._body


def _deps(dossier):
    # review is unused by the reminder kinds but Deps requires it.
    return Deps(review=None, dossier=dossier)


class TestHandleFollowUpScan:
    async def test_due_apps_build_reminder(self):
        dossier = FakeDossier(200, [
            {"company": "Acme", "title": "PM", "status": "applied"},
            {"company": "Globex", "title": "Sr PM", "status": "interviewing"},
        ])
        msg = await handle_follow_up_scan({}, _deps(dossier))
        assert "Follow-up reminder" in msg
        assert "2 applications" in msg
        assert "• Acme — PM (applied)" in msg
        assert "• Globex — Sr PM (interviewing)" in msg
        # It queried ONLY the follow-up-due filter.
        assert dossier.calls == [{"status": None, "stale": None,
                                  "follow_up_due": True, "limit": 200}]

    async def test_singular_grammar(self):
        dossier = FakeDossier(200, [{"company": "Acme", "title": "PM", "status": "applied"}])
        msg = await handle_follow_up_scan({}, _deps(dossier))
        assert "1 application is due" in msg

    async def test_none_due_returns_empty_string(self):
        # Empty result -> worker skips the inject (no "nothing to do" noise).
        dossier = FakeDossier(200, [])
        assert await handle_follow_up_scan({}, _deps(dossier)) == ""

    async def test_caps_the_inline_list(self):
        rows = [{"company": f"Co{i}", "title": "PM", "status": "applied"} for i in range(15)]
        msg = await handle_follow_up_scan({}, _deps(FakeDossier(200, rows)))
        assert "15 applications" in msg           # true total in the header
        assert "…and 5 more." in msg              # only 10 listed inline
        assert "• Co0 — PM" in msg and "• Co9 — PM" in msg
        assert "• Co10 — PM" not in msg

    async def test_dossier_error_raises(self):
        dossier = FakeDossier(503, {"detail": "dossier down"})
        with pytest.raises(Exception) as exc:
            await handle_follow_up_scan({}, _deps(dossier))
        assert "dossier down" in str(exc.value)

    async def test_missing_dossier_client_raises(self):
        with pytest.raises(Exception) as exc:
            await handle_follow_up_scan({}, Deps(review=None, dossier=None))
        assert "dossier" in str(exc.value).lower()


class TestHandleResumeFreshness:
    async def test_stale_apps_build_reminder(self):
        dossier = FakeDossier(200, [{"company": "Acme", "title": "PM", "status": "applied"}])
        msg = await handle_resume_freshness({}, _deps(dossier))
        assert "Résumé freshness" in msg
        assert "• Acme — PM (applied)" in msg
        # It queried ONLY the stale filter.
        assert dossier.calls == [{"status": None, "stale": True,
                                  "follow_up_due": None, "limit": 200}]

    async def test_none_stale_returns_empty_string(self):
        assert await handle_resume_freshness({}, _deps(FakeDossier(200, []))) == ""

    async def test_dossier_error_raises(self):
        with pytest.raises(Exception):
            await handle_resume_freshness({}, _deps(FakeDossier(0, {"error": "refused"})))
