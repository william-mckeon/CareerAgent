#!/usr/bin/env python3
# ============================================================================
# careeragent-ats - deterministic ATS keyword-coverage scorer (the substance)
# ============================================================================
#
# NO model, NO database, NO network. Pure text analysis. Given a resume and a
# job description, extract the JD's important keywords and report how many of
# them the resume covers.
#
# The two public entry points are PURE FUNCTIONS (unit-testable without the API):
#
#   extract_keywords(job_description) -> list[str]
#       Deterministically pull skill/tech-like keywords out of a JD: hard skills,
#       tools, technologies, frameworks, languages, certs, role nouns; unigrams +
#       common tech bigrams. Stopwords and hiring fluff are filtered out. The set
#       is deduped, salience-ranked, and capped.
#
#   score_resume(resume_text, job_description) -> AtsResult
#       For each extracted keyword, decide MATCHED if it appears in the resume
#       (case-insensitive word-boundary match + a small alias map + a guarded
#       rapidfuzz near-match). Returns score, matched, missing.
#
# Design notes / honest limits are in specs/0001-ats.md and docs/DATASHEET.md:
# this is a keyword-coverage HEURISTIC, not a real applicant-tracking system.
# ============================================================================

from __future__ import annotations

import re
from typing import List, NamedTuple, Optional

from rapidfuzz import fuzz

# ---------------------------------------------------------------------------
# Tuning knobs
# ---------------------------------------------------------------------------
# Max keywords kept from a JD. Bounds the denominator so one enormous posting
# can't dilute the score into meaninglessness; salience ranking keeps the best.
MAX_KEYWORDS = 40
# rapidfuzz similarity (0-100) at/above which a fuzzy near-match counts as a hit.
# 90 (not 88): a 4-char keyword that is a subsequence of a 5-char token scores
# exactly 800/9 = 88.9 on fuzz.ratio — so 88 let 'rust' match 'trust', 'ruby'
# match 'rugby', 'perl' match 'pearl'. 90 closes that boundary while still catching
# real typos in longer terms (kubernetes/kubernets = 94.7).
FUZZY_THRESHOLD = 90
# Fuzzy matching is only attempted for keywords at least this long. Short tokens
# ("go", "js", "ml", "c#", "rust", "ruby", "java") are matched EXACTLY / by alias
# only — fuzzy on a short token lights up on unrelated words (go/google,
# rust/trust, scala/scalar). Their real variants live in the alias map. 6, not 4:
# every confirmed fuzzy false-positive was on a 4–5 char token.
MIN_FUZZY_LEN = 6

# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------
# One token = an alphanumeric run that may carry internal/trailing tech
# punctuation, so real tech tokens survive intact:
#   c++, c#, node.js, ci/cd, rest-api, .net, asp.net, 3d, k8s
# Breakdown:
#   \.?               optional leading dot  -> ".net"
#   [a-z0-9]+         an alphanumeric run   -> "node"
#   (?:[+#./-][a-z0-9]+)*  internal groups  -> ".js", "/cd", "-api"
#   [+#]*             trailing + or #       -> "c++", "c#"
_TOKEN_RE = re.compile(r"\.?[a-z0-9]+(?:[+#./\-][a-z0-9]+)*[+#]*", re.IGNORECASE)


def _tokenize(text: str) -> List[str]:
    """Lowercased tokens, preserving tech punctuation (see _TOKEN_RE)."""
    return [m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")]


# ---------------------------------------------------------------------------
# Stopwords + hiring fluff — the terms we do NOT want as keywords. The goal is
# to keep skill/tech-like terms, not prose. (Curated, intentionally broad.)
# ---------------------------------------------------------------------------
_STOPWORDS = frozenset(
    # articles / prepositions / conjunctions / pronouns / aux verbs
    "a an the and or but if then else of to in on at by for with without from "
    "as is are was were be been being am do does did done have has had having "
    "will would shall should can could may might must this that these those it "
    "its we our us you your they them their he she his her i me my mine ours "
    "who whom which what when where why how not no nor so than too very just "
    "also into over under out up down off about above below between through "
    "during before after again further once here there all any both each few "
    "more most other some such only own same s t re ve ll d m o "
    # generic hiring / JD fluff
    "team teams player teamwork collaborate collaboration collaborative "
    "fast-paced paced environment culture responsibility responsibilities "
    "requirement requirements qualification qualifications experience "
    "experienced years year ability able abilities strong excellent great good "
    "work working works worked join looking seeking seek want wants role "
    "position job opportunity opportunities candidate candidates applicant "
    "etc including include includes included e.g eg ie plus preferred required "
    "must-have nice minimum maximum least well highly proven track record "
    "help helping helps ensure ensuring provide providing support supporting "
    "across within using use used uses new existing various multiple different "
    "day days daily week weekly month monthly time full part company companies "
    "business businesses organization organizations customer customers client "
    "clients stakeholder stakeholders partner partners world class worldclass "
    "passion passionate motivated driven self starter detail oriented "
    "need needs needed results goal focused "
    "communication communicate skills skill knowledge understanding familiar "
    "familiarity proficiency proficient expertise expert level senior junior "
    "mid lead principal staff entry apply application applications hire hiring "
    "please email resume cv cover letter benefits salary compensation eeo "
    "equal employer diversity inclusive inclusion location remote onsite hybrid "
    "office based per etc.".split()
)

# ---------------------------------------------------------------------------
# Role nouns — common job-title words. NOT fluff: a resume that lacks the role
# noun genuinely lacks a keyword. Kept AND treated as salient.
# ---------------------------------------------------------------------------
_ROLE_NOUNS = frozenset(
    "engineer engineering developer development programmer architect analyst "
    "scientist administrator specialist consultant designer manager director "
    "lead researcher devops sre technician operator strategist coordinator".split()
)

# ---------------------------------------------------------------------------
# Known tech vocabulary — languages, frameworks, tools, platforms, DBs, certs,
# methodologies. Used to (a) boost salience, (b) recognize tech bigrams. This
# does NOT limit extraction (unknown skills still get pulled); it just ranks.
# ---------------------------------------------------------------------------
_KNOWN_TECH = frozenset(
    # languages
    "python java javascript typescript c c++ c# go golang rust ruby php swift "
    "kotlin scala perl r matlab bash shell powershell sql html css "
    # frontend / frameworks
    "react angular vue svelte jquery redux next.js nuxt node node.js nodejs "
    "django flask fastapi rails spring express laravel dotnet .net asp.net "
    "tailwind bootstrap sass webpack vite "
    # data / ml
    "pandas numpy scipy sklearn scikit-learn tensorflow pytorch keras spark "
    "hadoop kafka airflow dbt snowflake databricks tableau powerbi looker "
    "pytest jupyter "
    # cloud / infra / devops
    "aws azure gcp ec2 s3 lambda docker kubernetes k8s terraform ansible "
    "jenkins gitlab github git ci/cd cicd helm prometheus grafana nginx "
    "linux unix windows serverless microservices "
    # databases
    "postgresql postgres mysql mongodb redis elasticsearch cassandra dynamodb "
    "sqlite oracle mssql sqlserver graphql rest restful grpc "
    # concepts / methods / certs
    "agile scrum kanban tdd oop api apis sdk cli saas etl ml ai nlp llm "
    "cybersecurity pmp cissp aws-certified comptia scrum-master "
    "3d opengl unity unreal".split()
)

# ---------------------------------------------------------------------------
# Common multi-word tech phrases to recognize as single keywords (bigrams and a
# few trigrams). Detected as contiguous token runs in the JD.
# ---------------------------------------------------------------------------
_KNOWN_PHRASES = frozenset(
    {
        "machine learning", "deep learning", "data science", "data engineering",
        "data analysis", "data analytics", "data pipeline", "data pipelines",
        "computer vision", "natural language processing", "artificial intelligence",
        "rest api", "rest apis", "restful api", "restful apis", "web services",
        "unit testing", "integration testing", "test automation", "version control",
        "object oriented", "distributed systems", "software engineering",
        "software development", "continuous integration", "continuous deployment",
        "cloud computing", "big data", "business intelligence", "project management",
        "product management", "user experience", "user interface", "front end",
        "back end", "full stack", "source control", "amazon web services",
        "google cloud", "google cloud platform", "microsoft azure",
        "infrastructure as code", "message queue", "time series", "a/b testing",
    }
)

# Longest phrases first, so "google cloud platform" wins over "google cloud".
# Secondary key `p` (alphabetical) is LOAD-BEARING: without it, ties among the many
# equal-word-count phrases resolve by frozenset iteration order, which is
# PYTHONHASHSEED-randomized per process — so the SAME resume+JD could score
# differently across container restarts once the 40-keyword cap slices an
# equal-salience cluster. The alphabetical tiebreak makes extraction fully
# deterministic (the Dockerfile also pins PYTHONHASHSEED=0 as defense-in-depth).
_PHRASE_LIST = sorted(_KNOWN_PHRASES, key=lambda p: (-len(p.split()), p))

# Phrase FRAGMENTS — words that are in _KNOWN_TECH (so the overlap-dedupe would
# otherwise force-keep them) but carry no standalone signal once their parent
# phrase is present. Dropping them stops "rest api" from also emitting bare "rest"
# and "api" as separate keywords (triple-counting one concept, and putting a bare
# "api" in `missing` when the résumé says "REST APIs").
_PHRASE_FRAGMENTS = frozenset({"rest", "api", "apis", "restful"})

# ---------------------------------------------------------------------------
# Alias groups — sets of terms treated as equivalent for MATCHING. If a JD
# keyword is in a group, the resume matches when it contains ANY member of the
# group (exact, word-boundary). Handles the cases rapidfuzz can't (abbreviations
# whose spelling differs entirely: k8s/kubernetes, aws/amazon web services).
# ---------------------------------------------------------------------------
_ALIAS_GROUPS = [
    {"postgresql", "postgres", "psql"},
    {"kubernetes", "k8s"},
    {"javascript", "js"},
    {"typescript", "ts"},
    {"node.js", "nodejs", "node"},
    {"ci/cd", "cicd", "ci-cd"},
    {"rest api", "rest apis", "restful", "restful api"},
    {".net", "dotnet", "asp.net"},
    {"golang", "go"},
    {"c#", "csharp"},
    {"c++", "cpp"},
    {"amazon web services", "aws"},
    {"google cloud platform", "google cloud", "gcp"},
    {"microsoft azure", "azure"},
    {"scikit-learn", "sklearn"},
    {"artificial intelligence", "ai"},
    {"machine learning", "ml"},
    {"natural language processing", "nlp"},
    {"continuous integration", "ci"},
    {"tensorflow", "tf"},
]

# keyword -> the OTHER members of its group (built once)
_ALIASES: dict[str, set[str]] = {}
for _grp in _ALIAS_GROUPS:
    for _term in _grp:
        _ALIASES.setdefault(_term, set()).update(_grp - {_term})


# ---------------------------------------------------------------------------
# Keyword extraction
# ---------------------------------------------------------------------------
def _is_number(tok: str) -> bool:
    """True for bare quantities like "5", "3.5", "5+", "24/7" — NOT for tech
    tokens that merely contain a digit ("c++", "3d", "s3", "log4j")."""
    stripped = re.sub(r"[+#./\-]", "", tok)
    return stripped.isdigit()


def _keep_unigram(tok: str) -> bool:
    """Whether a single token is worth keeping as a candidate keyword."""
    if tok in _STOPWORDS:
        return False
    # Hyphenated hiring fluff — "detail-oriented", "self-motivated", "results-driven"
    # — tokenizes as ONE token that isn't literally in _STOPWORDS, then lands in
    # `missing` (uncoverable) and dilutes an otherwise-strong score. Drop it when
    # every hyphen-split part is itself a stopword. (A hyphenated TECH token like
    # "ci-cd" or "scikit-learn" survives — its parts aren't stopwords.)
    if "-" in tok:
        parts = [p for p in tok.split("-") if p]
        if parts and all(p in _STOPWORDS for p in parts):
            return False
    if _is_number(tok):
        return False
    # Drop bare single characters (a, i, x) — except real single-char tech that
    # carries punctuation is already >1 char (c#, c++). "c"/"r" alone are too
    # ambiguous to score on.
    if len(tok) < 2:
        return False
    return True


def _salience(kw: str, count: int, capitalized: bool) -> int:
    """Rank score for a candidate keyword. Higher = more likely a real skill.

    Preferences (per the spec): multi-word tech phrases, then capitalized /
    known-tech tokens. Frequency and length are light tie-breakers.
    """
    parts = kw.split()
    score = 0
    if kw in _KNOWN_TECH or all(p in _KNOWN_TECH for p in parts):
        score += 100
    if kw in _KNOWN_PHRASES:
        score += 90
    if any(c in kw for c in "+#/.") and not _is_number(kw):
        score += 55  # tech punctuation -> almost certainly a tech token
    if " " in kw:
        score += 45  # multi-word phrase
    if kw in _ROLE_NOUNS:
        score += 30
    if capitalized:
        score += 20  # Proper-Cased / UPPER in the original JD
    score += min(count, 5) * 3  # repeated -> emphasized by the JD
    score += min(len(kw), 12)
    return score


def extract_keywords(job_description: str) -> List[str]:
    """Extract deduped, salience-ranked, capped keywords from a job description.

    Pure + deterministic: the same JD always yields the same ordered list.
    Returns lowercased keywords; multi-word phrases are space-joined.
    """
    text = job_description or ""
    tokens = _tokenize(text)
    if not tokens:
        return []

    # Which lowercased tokens appeared capitalized somewhere in the original?
    # (Proper nouns / acronyms are salience signals: "Python", "AWS", "React".)
    capitalized: set[str] = set()
    for m in _TOKEN_RE.finditer(text):
        raw = m.group(0)
        if raw[:1].isupper() or (raw.isupper() and any(c.isalpha() for c in raw)):
            capitalized.add(raw.lower())

    counts: dict[str, int] = {}
    order: List[str] = []  # first-seen order, for stable output
    cap_flag: dict[str, bool] = {}

    def _add(kw: str, is_cap: bool) -> None:
        if kw not in counts:
            counts[kw] = 0
            order.append(kw)
            cap_flag[kw] = is_cap
        counts[kw] += 1
        if is_cap:
            cap_flag[kw] = True

    # 1) Known multi-word phrases (scan contiguous token runs, longest first).
    joined = " " + " ".join(tokens) + " "
    phrase_hits: dict[str, int] = {}
    for phrase in _PHRASE_LIST:
        needle = " " + phrase + " "
        c = joined.count(needle)
        if c:
            phrase_hits[phrase] = c

    # 2) Unigrams.
    for tok in tokens:
        if _keep_unigram(tok):
            _add(tok, tok in capitalized)

    # 3) Add the detected phrases as keywords.
    for phrase, c in phrase_hits.items():
        is_cap = all(p in capitalized for p in phrase.split())
        if phrase not in counts:
            counts[phrase] = 0
            order.append(phrase)
            cap_flag[phrase] = is_cap
        counts[phrase] += c
        if is_cap:
            cap_flag[phrase] = True

    # 4) Overlap dedupe: if a multi-word phrase is kept, drop its constituent
    #    unigrams UNLESS a constituent is independently salient (known tech / a
    #    role noun). Prevents "machine learning" + "machine" + "learning" from
    #    triple-counting the same concept.
    phrase_words: set[str] = set()
    for kw in order:
        if " " in kw:
            for w in kw.split():
                # Drop a constituent unigram if it's a known phrase-fragment (rest/api)
                # OR it isn't independently salient (not known-tech, not a role noun).
                if w in _PHRASE_FRAGMENTS or (w not in _KNOWN_TECH and w not in _ROLE_NOUNS):
                    phrase_words.add(w)
    kept = [kw for kw in order if not (" " not in kw and kw in phrase_words)]

    # 5) Salience-rank and cap.
    ranked = sorted(
        kept,
        key=lambda kw: (-_salience(kw, counts[kw], cap_flag[kw]), order.index(kw)),
    )
    return ranked[:MAX_KEYWORDS]


# ---------------------------------------------------------------------------
# Matching a keyword against the resume
# ---------------------------------------------------------------------------
def _boundary_pattern(term: str) -> re.Pattern:
    """A word-boundary-ish matcher for `term` that works with tech punctuation.

    Standard \\b is unreliable next to +, #, ., / — so we require the match not
    be flanked by an alphanumeric. This is what stops "java" from matching
    "javaSCRIPT" and "go" from matching "GOogle": the char right after must not
    be alphanumeric.
    """
    return re.compile(
        r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", re.IGNORECASE
    )


def _appears_exact(term: str, resume_lower: str) -> bool:
    return bool(_boundary_pattern(term).search(resume_lower))


def _term_present(term: str, resume_lower: str, tokens_set: set) -> bool:
    """Is a single term present in the resume, respecting the tokenizer's rules?

    A term WITH a space (a phrase, e.g. "rest api") is matched with the
    word-boundary regex over the raw lowercased resume. A single-token term is
    matched by EXACT membership in the resume's token SET — which is what stops
    "go" from matching "go-getter" (the tokenizer treats the hyphen as internal,
    so "go-getter" is one token != "go"), where the old boundary regex saw the
    hyphen as a boundary and matched. Token equality errs toward NOT crediting a
    hyphen-compounded skill (e.g. "aws" won't match a résumé that only writes
    "aws-certified") — the safe direction for an anti-fabrication coverage tool.
    """
    if " " in term:
        return _appears_exact(term, resume_lower)
    return term in tokens_set


def keyword_matches(
    keyword: str, resume_lower: str, resume_tokens: List[str],
    resume_tokens_set: Optional[set] = None,
) -> bool:
    """Is `keyword` covered by the resume? (case-insensitive)

    Order of checks, cheapest/strictest first:
      1) exact match (token-set membership for a single token; word-boundary regex
         for a multi-word phrase),
      2) any alias-group member (same rule),
      3) guarded rapidfuzz near-match (only for keywords >= MIN_FUZZY_LEN; uses
         fuzz.ratio, NOT partial_ratio — partial would make "java" match
         "javascript" at 100). `resume_tokens_set` is prebuilt once per score by
         the caller; a direct caller (tests) may omit it and it's derived here.
    """
    tokens_set = resume_tokens_set if resume_tokens_set is not None else set(resume_tokens)

    # 1) exact
    if _term_present(keyword, resume_lower, tokens_set):
        return True

    # 2) aliases (e.g. keyword "kubernetes" matches resume "k8s")
    for alias in _ALIASES.get(keyword, ()):  # membership only; order irrelevant
        if _term_present(alias, resume_lower, tokens_set):
            return True

    # 3) fuzzy near-match, guarded by length (see MIN_FUZZY_LEN) — catches minor
    #    spelling variants like kubernetes/kubernets. Iterate the DISTINCT tokens
    #    (bounds the work for a large résumé) and short-circuit on the first hit.
    if len(keyword.replace(" ", "")) < MIN_FUZZY_LEN:
        return False
    for tok in tokens_set:
        # fuzz.ratio is symmetric edit-distance similarity: a near-spelling scores
        # high, but a mere substring ("java" in "javascript") does NOT.
        if fuzz.ratio(keyword, tok) >= FUZZY_THRESHOLD:
            return True
    return False


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
class AtsResult(NamedTuple):
    score: int          # 0-100
    matched: List[str]  # JD keywords found in the resume (extraction order)
    missing: List[str]  # JD keywords NOT found (extraction order)

    @property
    def total(self) -> int:
        return len(self.matched) + len(self.missing)

    @property
    def coverage(self) -> str:
        return f"{len(self.matched)}/{self.total}"


def score_resume(resume_text: str, job_description: str) -> AtsResult:
    """Deterministic keyword-coverage score of a resume against a JD.

    score = round(100 * matched / total) over the JD's extracted keywords;
    0 when the JD yields no keywords at all (e.g. a JD of only stopwords —
    note the API rejects an EMPTY/whitespace JD with 400 before we get here).
    An empty resume simply matches nothing → score 0, everything missing.
    """
    keywords = extract_keywords(job_description)
    if not keywords:
        return AtsResult(score=0, matched=[], missing=[])

    resume_lower = (resume_text or "").lower()
    resume_tokens = _tokenize(resume_text or "")
    resume_tokens_set = set(resume_tokens)   # built ONCE; reused for every keyword

    matched: List[str] = []
    missing: List[str] = []
    for kw in keywords:
        if keyword_matches(kw, resume_lower, resume_tokens, resume_tokens_set):
            matched.append(kw)
        else:
            missing.append(kw)

    score = round(100 * len(matched) / len(keywords))
    return AtsResult(score=score, matched=matched, missing=missing)
