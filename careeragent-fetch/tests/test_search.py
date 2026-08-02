"""
tests/test_search.py — web-search provider parsing + run_search dispatch/validation.

Pure + hermetic: parse_tavily is network-free; run_search's validation and provider
dispatch are exercised with a fake provider (no real HTTP).
"""
import pytest

import search
from search import (
    SearchOutcome, SearchHit, SearchProblem, _clamp_results, parse_tavily, run_search,
)


class TestParseTavily:
    def test_maps_results_and_answer(self):
        body = {
            "results": [
                {"title": "Acme PM", "url": "https://acme.com/jobs/1", "content": "We want PMs", "score": 0.9},
                {"title": "Globex", "url": "https://globex.io/careers", "content": "Sr PM", "score": 0.5},
            ],
            "answer": "Two PM roles found.",
        }
        out = parse_tavily(body)
        assert out.provider == "tavily"
        assert [h.url for h in out.results] == ["https://acme.com/jobs/1", "https://globex.io/careers"]
        assert out.results[0].title == "Acme PM"
        assert out.results[0].snippet == "We want PMs"
        assert out.results[0].score == 0.9
        assert out.answer == "Two PM roles found."

    def test_drops_result_without_url(self):
        out = parse_tavily({"results": [{"title": "no url", "content": "x"}, {"url": "https://a.com"}]})
        assert [h.url for h in out.results] == ["https://a.com"]

    def test_tolerates_missing_and_wrong_types(self):
        out = parse_tavily({"results": [{"url": "https://a.com", "score": "high"}]})
        assert out.results[0].score == 0.0          # non-numeric score coerced to 0.0
        assert parse_tavily({}).results == []
        assert parse_tavily("garbage").results == []

    def test_empty_answer_is_none(self):
        assert parse_tavily({"answer": ""}).answer is None
        assert parse_tavily({"answer": "  hi  "}).answer == "hi"

    def test_hostile_non_list_results_does_not_crash(self):
        # A truthy non-list 'results' (e.g. an int) must not raise TypeError.
        assert parse_tavily({"results": 5, "answer": "x"}).results == []
        assert parse_tavily({"results": "nope"}).results == []

    def test_hostile_huge_score_is_swallowed(self):
        # A pathological huge-int score would OverflowError on float() — caught → 0.0.
        out = parse_tavily({"results": [{"url": "https://a.com", "score": 10 ** 400}]})
        assert out.results[0].score == 0.0


class TestClampResults:
    def test_defaults_and_bounds(self):
        assert _clamp_results(None) == 5     # default
        assert _clamp_results("nan") == 5
        assert _clamp_results(0) == 1        # floor
        assert _clamp_results(999) == 10     # ceil
        assert _clamp_results(3) == 3


class TestRunSearch:
    async def test_empty_query_is_400(self):
        with pytest.raises(SearchProblem) as exc:
            await run_search("   ", provider="tavily", api_key="k")
        assert exc.value.status_code == 400

    async def test_unknown_provider_is_503(self):
        with pytest.raises(SearchProblem) as exc:
            await run_search("pm jobs", provider="bing", api_key="k")
        assert exc.value.status_code == 503

    async def test_tavily_without_key_is_503(self):
        # The tavily provider refuses before any network when no key is set.
        with pytest.raises(SearchProblem) as exc:
            await run_search("pm jobs", provider="tavily", api_key="")
        assert exc.value.status_code == 503

    async def test_dispatches_trimmed_and_clamped(self, monkeypatch):
        seen = {}

        async def fake(query, max_results, api_key, timeout, include_answer):
            seen.update(query=query, max_results=max_results, api_key=api_key)
            return SearchOutcome(results=[SearchHit("t", "https://a.com", "s")], provider="tavily")

        monkeypatch.setitem(search._PROVIDERS, "tavily", fake)
        out = await run_search("  pm jobs  ", provider="tavily", api_key="key", max_results=99)
        assert seen["query"] == "pm jobs"     # trimmed
        assert seen["max_results"] == 10      # clamped to the max
        assert seen["api_key"] == "key"
        assert out.results[0].url == "https://a.com"
