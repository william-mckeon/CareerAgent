"""
tests/test_ats.py — the scoring substance (pure functions, no API, no network).

Covers: extraction drops fluff + keeps tech terms; a known resume/JD pair scores
as expected; empty resume -> 0; "java" must not match "javascript"; alias and
fuzzy near-matches (Postgres/PostgreSQL, minor spelling variants).
"""
import ats


# ---------------------------------------------------------------------------
# extraction: keep skill/tech terms, drop stopwords + hiring fluff
# ---------------------------------------------------------------------------
def test_extract_keeps_tech_terms():
    jd = (
        "Responsibilities: build scalable REST APIs in Python. "
        "Requirements: 5+ years experience with Docker and Kubernetes."
    )
    kws = ats.extract_keywords(jd)
    for want in ("python", "docker", "kubernetes"):
        assert want in kws, f"expected {want!r} in {kws}"
    # the common tech bigram is recognized as one keyword
    assert "rest apis" in kws


def test_extract_drops_fluff_and_stopwords():
    jd = (
        "We are looking for a strong team player with excellent communication "
        "skills and years of experience. Responsibilities and requirements apply."
    )
    kws = ats.extract_keywords(jd)
    for junk in (
        "team", "player", "strong", "excellent", "communication", "skills",
        "years", "experience", "responsibilities", "requirements", "looking",
        "we", "are", "for", "a", "with", "of", "and",
    ):
        assert junk not in kws, f"fluff/stopword {junk!r} leaked into {kws}"


def test_extract_is_deterministic():
    jd = "Python developer with Django, Docker, and AWS experience."
    assert ats.extract_keywords(jd) == ats.extract_keywords(jd)


def test_extract_empty_jd_yields_no_keywords():
    assert ats.extract_keywords("") == []
    assert ats.extract_keywords("   \n\t ") == []


# ---------------------------------------------------------------------------
# scoring: a known resume/JD pair -> expected matched / missing / score
# ---------------------------------------------------------------------------
def test_score_known_pair():
    jd = (
        "We are looking for a Python developer with experience in Django, "
        "PostgreSQL, and Docker. Kubernetes and AWS are a plus. Strong "
        "communication skills required."
    )
    resume = (
        "Senior Python engineer. Built web apps with Django and Postgres. "
        "Deployed with Docker containers on AWS."
    )
    result = ats.score_resume(resume, jd)

    # present skills (exact + alias) are matched
    for want in ("python", "django", "postgresql", "docker", "aws"):
        assert want in result.matched, f"{want!r} should be matched: {result}"
    # kubernetes is nowhere in the resume -> missing
    assert "kubernetes" in result.missing
    # "developer" (JD role noun) is absent from the resume ("engineer") -> missing
    assert "developer" in result.missing

    # score is internally consistent with the coverage
    total = len(result.matched) + len(result.missing)
    assert total > 0
    assert result.score == round(100 * len(result.matched) / total)
    assert result.coverage == f"{len(result.matched)}/{total}"
    assert 0 <= result.score <= 100


def test_empty_resume_scores_zero():
    jd = "Python, Docker, Kubernetes, AWS, and Terraform required."
    result = ats.score_resume("", jd)
    assert result.score == 0
    assert result.matched == []
    assert len(result.missing) > 0


def test_jd_of_only_fluff_scores_zero_not_error():
    # Non-empty JD that yields no keywords: total 0 -> score 0 (API 400s only on
    # an EMPTY/whitespace JD; this one is neither).
    result = ats.score_resume("anything", "the and or with a of to")
    assert result.score == 0
    assert result.coverage == "0/0"
    assert result.matched == [] and result.missing == []


# ---------------------------------------------------------------------------
# matching precision: word boundaries, aliases, guarded fuzzy
# ---------------------------------------------------------------------------
def test_java_does_not_match_javascript():
    resume = "Experienced in JavaScript, TypeScript, and React."
    tokens = ats._tokenize(resume)
    assert ats.keyword_matches("java", resume.lower(), tokens) is False
    # sanity: the real thing DOES match
    assert ats.keyword_matches("javascript", resume.lower(), tokens) is True


def test_go_does_not_match_google():
    resume = "Worked at Google on search infrastructure."
    tokens = ats._tokenize(resume)
    assert ats.keyword_matches("go", resume.lower(), tokens) is False


def test_alias_postgres_matches_postgresql():
    resume = "Managed Postgres databases in production."
    tokens = ats._tokenize(resume)
    assert ats.keyword_matches("postgresql", resume.lower(), tokens) is True


def test_alias_k8s_matches_kubernetes():
    resume = "Ran workloads on K8s clusters."
    tokens = ats._tokenize(resume)
    assert ats.keyword_matches("kubernetes", resume.lower(), tokens) is True


def test_fuzzy_matches_minor_spelling_variant():
    # A near-spelling (not an alias) is caught by rapidfuzz.
    resume = "Deep experience with Kubernets orchestration."  # typo
    tokens = ats._tokenize(resume)
    assert ats.keyword_matches("kubernetes", resume.lower(), tokens) is True


def test_tech_tokens_survive_tokenization():
    toks = ats._tokenize("C++, C#, Node.js, CI/CD and .NET")
    for t in ("c++", "c#", "node.js", "ci/cd", ".net"):
        assert t in toks, f"{t!r} missing from {toks}"


# ---------------------------------------------------------------------------
# regressions from the P7 #16 adversarial review
# ---------------------------------------------------------------------------
def test_fuzzy_does_not_credit_short_lookalikes():
    # rust/trust, ruby/rugby, perl/pearl all score fuzz.ratio 88.9 — under the old
    # 88 threshold + 4-char MIN they were false hits. The résumé names none of the
    # three languages; only the role noun 'engineer' is real.
    resume = "Engineer who built trust with teams, played rugby, and wore a pearl."
    tokens = ats._tokenize(resume)
    tset = set(tokens)
    for lang in ("rust", "ruby", "perl"):
        assert ats.keyword_matches(lang, resume.lower(), tokens, tset) is False, \
            f"{lang!r} should NOT fuzzy-match a lookalike word"


def test_go_does_not_match_hyphenated_fluff():
    # The tokenizer treats a hyphen as internal, so 'go-getter' is one token; the
    # old boundary-regex saw the hyphen as a boundary and matched 'go'.
    for fluff in ("a self-described go-getter", "drove our go-to-market", "planned the go-live"):
        tokens = ats._tokenize(fluff)
        assert ats.keyword_matches("go", fluff.lower(), tokens, set(tokens)) is False, \
            f"'go' should NOT match {fluff!r}"
    # sanity: a clean mention still matches
    tokens = ats._tokenize("Wrote services in Go and Python.")
    assert ats.keyword_matches("go", "wrote services in go and python.", tokens, set(tokens)) is True


def test_hyphenated_hiring_fluff_is_not_a_keyword():
    jd = "Seeking a detail-oriented, self-motivated, results-driven engineer who knows Python."
    kws = ats.extract_keywords(jd)
    for fluff in ("detail-oriented", "self-motivated", "results-driven"):
        assert fluff not in kws, f"hyphenated fluff {fluff!r} leaked into {kws}"
    assert "python" in kws and "engineer" in kws     # the real terms survive


def test_need_is_a_stopword():
    assert "need" not in ats.extract_keywords("We need a Python developer.")


def test_rest_api_not_triple_counted():
    # 'rest api' is one concept — it must not also emit bare 'rest' and 'api'.
    kws = ats.extract_keywords("Build REST APIs and web services in Python.")
    assert "rest apis" in kws
    assert "rest" not in kws and "api" not in kws and "apis" not in kws


def test_phrase_list_is_deterministically_ordered():
    # The alphabetical secondary key removes the frozenset-iteration (hash-seed)
    # dependence that made the SCORE vary across container restarts.
    assert ats._PHRASE_LIST == sorted(ats._KNOWN_PHRASES, key=lambda p: (-len(p.split()), p))
