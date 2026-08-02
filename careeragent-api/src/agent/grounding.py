#!/usr/bin/env python3
# ============================================================================
# careeragent-api - The grounding gate (Phase 3 slice 3)
# ============================================================================
#
# The trust layer's second half: after the verified-completion gate makes
# "I saved it" checkable, this makes "here's your resume" checkable — it blocks
# a drafted resume that claims SKILLS or DOMAIN EXPERIENCE the user's own
# dossier doesn't back.
#
# Why it exists: live testing showed the coach, tailoring a resume to a legal-tech
# job that wanted TypeScript, INVENTED a "Legal-Tech Prototype" project and
# asserted "deep expertise in TypeScript" — neither is anywhere in the profile or
# projects library. Persona hardening stopped invented NUMBERS (placeholders) but
# not invented skills/projects. This gate catches them mechanically.
#
# Tier 1 is DETERMINISTIC — no model call:
#   * build an evidence CORPUS from the master profile + every project.
#   * scan the DRAFT for vocabulary-bounded SKILL/TECH terms and DOMAIN markers.
#   * extract PROJECT TITLES from the draft's Projects section and verify each one
#     actually exists in the dossier.
#   * anything present in the draft but absent from the corpus is a PHANTOM.
#
# The project check is NOT optional garnish — it is the only class a vocabulary can
# never catch. An audit replayed this module against the live dossier and found that
# an invented "Quantum Trading Engine — led a team of 40 at Goldman Sachs" shipped
# with grounded=True, because a fabricated project is built from ordinary words. The
# skill/domain vocabularies caught the original live fabrication ("Legal-Tech
# Prototype") only by accident — the word "legal" happened to be in the domain list.
# Rename it "Secure Data Pipelines" (which is what actually persisted to the dossier)
# and the identical fabrication sailed through. Hence _extract_project_titles.
#
# Precision-over-recall by design (a false block is worse than a missed catch the
# slice-4 verifier will get):
#   * the vocabularies EXCLUDE words that double as ordinary English ("go", "rust",
#     "energy", "compliance", "contract"), which an adversarial review showed were
#     flagged in motivational prose ("ready to go the extra mile", "ensured
#     compliance");
#   * matching is ALIAS-aware, so a dossier that says "postgres"/"nodejs" backs a
#     resume that says "PostgreSQL"/"Node.js";
#   * only resume-like drafts are checked (real section headers, single- or
#     multi-word), and DOMAIN claims are read only OUTSIDE the aspirational
#     objective/summary region (stating the industry you're *seeking* is not a
#     claim of experience);
#   * matching is word-boundary aware so "Java" is not satisfied by "JavaScript".
# The wider claim taxonomy (employers, certs, dates, numbers) is deferred to
# slice 4's separate verifier.
# ============================================================================

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Set
from urllib.parse import urlsplit, urlunsplit

# --- vocabularies (bounded on purpose; common-English words deliberately excluded) ---

# Skills/technologies a tech resume commonly claims. Ambiguous everyday words
# (go, r, swift, rust, spark, dart) are intentionally omitted — they matched
# motivational prose, not skill claims.
_TECH_VOCAB: Set[str] = {
    # languages
    "python", "typescript", "javascript", "java", "c++", "cpp", "c#", "csharp",
    "golang", "ruby", "php", "kotlin", "scala", "matlab", "perl", "haskell",
    "elixir", "objective-c", "sql", "bash", "powershell",
    # frameworks / libs
    "react", "angular", "vue", "svelte", "next.js", "node.js", "nodejs", "express",
    "django", "flask", "fastapi", "spring", "rails", "laravel", ".net", "dotnet",
    "pytorch", "tensorflow", "keras", "pandas", "numpy", "scikit-learn", "langchain",
    # cloud / infra / devops
    "aws", "azure", "gcp", "bedrock", "kubernetes", "k8s", "docker", "terraform",
    "ansible", "jenkins", "gitlab", "circleci",
    # data
    "postgresql", "postgres", "mysql", "mongodb", "redis", "elasticsearch", "kafka",
    "rabbitmq", "snowflake", "hadoop", "pgvector", "sqlite",
    # api / protocol
    "graphql", "grpc", "webhooks", "websocket", "oauth", "saml",
}

# Industry/domain markers. Excludes words common in generic prose (compliance,
# contract, energy, retail, insurance, banking).
_DOMAIN_VOCAB: Set[str] = {
    "legal", "legaltech", "legal-tech", "healthcare", "healthtech", "health-tech",
    "medical", "clinical", "fintech", "blockchain", "crypto", "cryptocurrency",
    "ecommerce", "e-commerce", "gaming", "adtech", "ad-tech", "edtech", "logistics",
    "telecom", "defense", "aerospace", "automotive", "biotech", "pharmaceutical",
}

# Spellings that mean the same skill — a claim is backed if ANY alias is evidenced.
_ALIAS_GROUPS: List[FrozenSet[str]] = [
    frozenset({"postgresql", "postgres"}),
    frozenset({"node.js", "nodejs"}),
    frozenset({".net", "dotnet"}),
    frozenset({"kubernetes", "k8s"}),
    frozenset({"c#", "csharp"}),
    frozenset({"c++", "cpp"}),
]
_ALIAS_OF: Dict[str, FrozenSet[str]] = {t: g for g in _ALIAS_GROUPS for t in g}

# Section headers that mark text as an actual resume (single- OR multi-word).
_SECTION_WORDS: Set[str] = {
    "summary", "professional summary", "career summary", "objective", "profile",
    "skills", "technical skills", "core competencies", "competencies", "technologies",
    "experience", "work experience", "relevant experience", "employment history",
    "projects", "selected projects", "education", "certifications", "awards",
}
# Aspirational sections — a domain named here is a target, not experience.
_ASPIRATIONAL: Set[str] = {"objective", "summary", "professional summary",
                           "career summary", "profile"}

# A heading-shaped line: optional markdown '#'s / bullet, a short title, optional ':'.
_HEADER_RE = re.compile(r"^\s{0,3}[#>*\-\s]{0,4}([a-z][a-z &/]{1,38}?)\s*:?\s*$")

# Sections that list PROJECT ENTRIES — where an invented project would be claimed.
_PROJECT_SECTIONS: Set[str] = {"projects", "selected projects"}
# Table-header / boilerplate words that are not project names.
_NOT_A_PROJECT: Set[str] = {
    "project", "projects", "role", "tech", "tech stack", "technologies", "name",
    "description", "summary", "highlights", "stack", "title", "evidence",
}
# An entry title ends at the first ' - ' / ' – ' / ':' / '|' separator.
_TITLE_SPLIT = re.compile(r"\s+[–—-]\s+|\s*\|\s*|:\s+")


@dataclass
class GroundingVerdict:
    """The result of grounding a drafted resume against the dossier."""
    checked: bool = False                       # was the draft resume-like enough to check?
    phantom_skills: List[str] = field(default_factory=list)
    phantom_domains: List[str] = field(default_factory=list)
    phantom_projects: List[str] = field(default_factory=list)

    @property
    def grounded(self) -> bool:
        return not (self.phantom_skills or self.phantom_domains or self.phantom_projects)

    def message(self) -> str:
        """The re-prompt fed back to the model, naming exactly what to fix."""
        parts: List[str] = []
        if self.phantom_projects:
            parts.append(
                "these PROJECTS are listed on the resume but do not exist in the person's "
                f"projects library or profile: {', '.join(self.phantom_projects)}"
            )
        if self.phantom_skills:
            parts.append(
                "these skills/technologies are on the resume but NOT in the person's profile "
                f"or projects: {', '.join(self.phantom_skills)}"
            )
        if self.phantom_domains:
            parts.append(
                "the resume claims experience in domains the profile does not evidence: "
                f"{', '.join(self.phantom_domains)}"
            )
        joined = "; ".join(parts)
        return (
            f"Grounding check — {joined}. Do NOT claim skills, projects, or experience the person "
            "doesn't have. Remove each unsupported item, or replace it with a real, evidenced one "
            "from their profile/projects (use search_projects to find real ones). If a listed "
            "project IS real, restate it using the wording that appears in their profile/projects "
            "so it can be verified. If a job requirement is genuinely missing, name it to the user "
            "as a gap and ask — never assert it as fact."
        )

    def caveat(self) -> str:
        """The user-visible note appended when an ungrounded resume ships anyway (the
        re-prompt cap was hit) — so Tier-1 is as honest to the user as the Guardian,
        never a silent ungrounded pass."""
        parts: List[str] = []
        if self.phantom_projects:
            parts.append("projects not in your library: " + ", ".join(self.phantom_projects))
        if self.phantom_skills:
            parts.append("skills not in your profile/projects: " + ", ".join(self.phantom_skills))
        if self.phantom_domains:
            parts.append("experience the profile doesn't evidence: " + ", ".join(self.phantom_domains))
        if not parts:
            return ""
        return ("\n\n> ⚠️ Unverified — I couldn't confirm these against your profile/projects; "
                "confirm they're true or edit them before sending:\n- " + "\n- ".join(parts))


def _header_of(line: str) -> Optional[str]:
    """The section-header word a line represents (e.g. 'skills'), or None if the
    line isn't a heading. Lets single-word headers ('Skills', 'Experience') count."""
    m = _HEADER_RE.match((line or "").lower())
    if not m:
        return None
    head = m.group(1).strip()
    return head if head in _SECTION_WORDS else None


def looks_like_resume(text: str) -> bool:
    """True when the text is an actual resume draft — >=2 distinct section headers,
    counting single-word heading lines AND multi-word markers — so a plain advisory
    reply that merely mentions a technology isn't gated."""
    low = (text or "").lower()
    headers: Set[str] = set()
    for line in low.splitlines():
        h = _header_of(line)
        if h:
            headers.add(h)
    for m in _SECTION_WORDS:                     # multi-word markers may sit inline, not on their own line
        if " " in m and m in low:
            headers.add(m)
    return len(headers) >= 2


def _strip_aspirational(text: str) -> str:
    """Drop the objective/summary sections — a domain named there is a target the
    person is *seeking*, not experience they claim."""
    out: List[str] = []
    stripping = False
    for line in (text or "").splitlines():
        h = _header_of(line)
        if h is not None:
            stripping = h in _ASPIRATIONAL
            continue                             # drop the header line itself
        if not stripping:
            out.append(line)
    return "\n".join(out)


# Phrases that mark a line as DISCUSSING a skill's ABSENCE (a gap note, a JD
# requirement, an "I couldn't verify" caveat) rather than CLAIMING it on the resume.
# A resume BULLET never contains these; a coach's gap commentary does. Live bug:
# a re-targeted resume correctly OMITTED Kubernetes, but the coach's own gap note
# ("the JD lists Kubernetes as a must-have; your evidence has none") named "k8s",
# so the skill scan flagged it as an unbacked claim — a self-contradicting caveat.
# Deliberately UNAMBIGUOUS gap-commentary phrases only — never a bare word ("gap",
# "missing", "removed", "lacks") that could appear in a real resume bullet, because
# stripping such a bullet would let a fabricated skill INSIDE it slip past the gate
# (a false negative weakens the trust gate — the worse error). These phrases occur
# in a coach's gap note / a JD-requirement line, essentially never in a resume bullet.
_GAP_MARKERS = (
    "must-have", "must have", "nice-to-have", "nice to have",
    "not in your", "no mention",
    # SPECIFIC "does not <verb>" gap constructions — NOT the bare "does not"/"doesn't",
    # which live in ordinary achievement bullets ("a layer that doesn't drop events,
    # using Kafka") and would strip a real claim (false negative).
    "does not contain", "does not include", "does not show", "does not list",
    "does not mention", "does not evidence", "doesn't contain", "doesn't include",
    "doesn't show", "doesn't list", "doesn't mention",
    "do not have", "don't have", "no experience",
    "not yet recorded", "isn't yet recorded", "not recorded",
    "if you have", "if you do", "unverified", "couldn't confirm", "could not confirm",
    "can't verify", "cannot verify", "confirm these", "confirm they", "[add",
    "job description", "job posting", "the role wants", "the posting lists",
)


def _strip_gap_context(text: str) -> str:
    """Drop lines that DISCUSS a skill as missing / required-but-absent / unverified
    rather than CLAIM it, so a coach's gap note can't turn the very skill it correctly
    left OFF the resume into a phantom-skill false positive."""
    out: List[str] = []
    for line in (text or "").splitlines():
        low = line.lower()
        if any(m in low for m in _GAP_MARKERS):
            continue
        out.append(line)
    return "\n".join(out)


def _section_text(text: str, wanted: Set[str]) -> str:
    """The lines under any header in `wanted`, up to the next header."""
    out: List[str] = []
    inside = False
    for line in (text or "").splitlines():
        h = _header_of(line)
        if h is not None:
            inside = h in wanted
            continue
        if inside:
            out.append(line)
    return "\n".join(out)


def _extract_project_titles(section: str) -> List[str]:
    """Pull the entry TITLE off each line of a Projects section — the leading name
    before a dash/colon/pipe, with bullets, table pipes and markdown stripped.
    Conservative: anything prose-shaped or boilerplate is skipped rather than
    risk calling a sentence a project."""
    titles: List[str] = []
    for raw in (section or "").splitlines():
        line = re.sub(r"^[-*•>\s|]+", "", (raw or "").strip())
        if not line:
            continue
        head = _TITLE_SPLIT.split(line)[0]
        head = head.replace("*", "").replace("`", "").replace("#", "").strip().strip(":").strip()
        if not head or not re.search(r"[A-Za-z]", head):
            continue
        if head.endswith("."):                      # a sentence, not a title
            continue
        if len(head) > 70 or not (1 <= len(head.split()) <= 7):
            continue
        if head.lower() in _NOT_A_PROJECT:          # table header / boilerplate
            continue
        titles.append(head)
    return titles


def _norm_phrase(s: str) -> str:
    """Lowercase, punctuation-collapsed, space-delimited — so 'OpenAgent-os' and
    'OpenAgent OS' compare equal, with word boundaries preserved for phrase match."""
    return " " + re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip() + " "


def _project_backed(title: str, corpus_norm: str) -> bool:
    """Is a claimed project title evidenced anywhere in the dossier? Exact phrase
    containment (deliberately strict): token-overlap scoring would call an invented
    'Secure Data Pipelines' backed just because 'secure', 'data' and 'pipelines' each
    appear somewhere — which is precisely the fabrication we must catch. A real
    project restated in profile wording will match; the re-prompt tells the model to
    do exactly that, so a false flag self-corrects in one bounded loop."""
    t = _norm_phrase(title)
    if len(t.strip()) < 4:                          # too short to judge -> don't flag
        return True
    return t in corpus_norm


def _matches(term: str, corpus: str) -> bool:
    """Word-boundary-aware membership: 'java' must NOT be satisfied by 'javascript',
    while 'c++'/'c#'/'node.js' still match — and a trailing '.'/',' (end of a
    sentence or list) must NOT defeat the match. Only alphanumerics and tech
    symbols (+, #) are token-internal; '.'/'-' are left out so 'go.'/'c++.' match."""
    pat = r"(?<![a-z0-9+#])" + re.escape(term) + r"(?![a-z0-9+#])"
    return re.search(pat, corpus) is not None


def _backed(term: str, corpus: str) -> bool:
    """Is a claimed term evidenced by the corpus, under any of its alias spellings?"""
    return any(_matches(a, corpus) for a in _ALIAS_OF.get(term, frozenset({term})))


def _present_terms(vocab: Set[str], text: str) -> Set[str]:
    low = (text or "").lower()
    return {t for t in vocab if _matches(t, low)}


def _canonical(term: str) -> str:
    """A stable display label per alias group (so 'postgres' and 'postgresql'
    don't both appear in a phantom list)."""
    group = _ALIAS_OF.get(term)
    return min(group) if group else term


def build_corpus(profile_content: str, projects: Optional[List[Dict[str, Any]]]) -> str:
    """The evidence blob claims are checked against: the master profile plus every
    project's textual fields, lowercased into one searchable string."""
    parts: List[str] = [profile_content or ""]
    for p in (projects or []):
        if not isinstance(p, dict):
            continue
        for k in ("name", "summary", "role", "tech_stack", "highlights", "languages", "external_id"):
            v = p.get(k)
            if isinstance(v, str):
                parts.append(v)
            elif isinstance(v, (list, tuple)):
                parts.extend(str(x) for x in v)
    return "\n".join(parts).lower()


def grounding_verdict(draft: str, corpus: str) -> GroundingVerdict:
    """Pure, deterministic Tier-1 check: which skill/domain claims in the draft
    have no backing in the evidence corpus. Only resume-like drafts are checked;
    an EMPTY corpus (dossier unavailable) means we cannot verify -> don't block."""
    if not looks_like_resume(draft) or not (corpus or "").strip():
        return GroundingVerdict(checked=False)
    # Strip gap/requirement/unverified commentary FIRST so a skill the resume
    # correctly omitted (but the coach discussed as a gap) isn't flagged as a claim.
    grounded_text = _strip_gap_context(draft)
    skills_in_draft = _present_terms(_TECH_VOCAB, grounded_text)
    domains_in_draft = _present_terms(_DOMAIN_VOCAB, _strip_aspirational(grounded_text))
    phantom_skills = sorted({_canonical(t) for t in skills_in_draft if not _backed(t, corpus)})
    phantom_domains = sorted({t for t in domains_in_draft if not _backed(t, corpus)})
    # Invented PROJECTS were the motivating failure and are the one class a
    # vocabulary can never catch (a fabricated project uses ordinary words).
    corpus_norm = _norm_phrase(corpus)
    titles = _extract_project_titles(_section_text(draft, _PROJECT_SECTIONS))
    phantom_projects = sorted({t for t in titles if not _project_backed(t, corpus_norm)})
    return GroundingVerdict(checked=True,
                            phantom_skills=phantom_skills,
                            phantom_domains=phantom_domains,
                            phantom_projects=phantom_projects)


async def build_corpus_from_dossier(profile_content: str, dossier_client: Any) -> str:
    """Fetch the projects once and assemble the evidence corpus. Never raises —
    a corpus of just the profile is still useful, and a gate that crashes must
    not take the turn down with it."""
    projects: List[Dict[str, Any]] = []
    try:
        status, body = await dossier_client.search_projects({})
        if 200 <= status < 300 and isinstance(body, list):
            projects = body
    except Exception:
        projects = []
    return build_corpus(profile_content, projects)


# ============================================================================
# Web-citation grounding (P7 /fetch) — a page the answer CITES must be one the
# coach actually FETCHED this turn (or a URL already in the person's dossier).
#
# The coach's one door to the outside world is `fetch_url`. Without this, it can
# assert "the posting requires 5 years of Rust" citing a link it never opened —
# a fabricated source the P3 resume gate (which only checks profile/projects
# evidence) never sees. This is the network analogue of the phantom-project
# check: a cited http(s) URL that was neither fetched this turn NOR present in
# the dossier corpus (the user's own repo/portfolio/LinkedIn links) is a PHANTOM
# citation. Precision-over-recall: allowing corpus URLs prevents flagging the
# user's own links, which the coach legitimately references all the time.
# ============================================================================

# http(s) URLs in prose; the class stops at whitespace, quotes, and the common
# closing brackets so a trailing ')' or '.' in a sentence isn't swallowed (norm_url
# strips any residual trailing punctuation).
_URL_RE = re.compile(r'https?://[^\s<>"\'\)\]\}]+', re.IGNORECASE)
_URL_TRAILING = '.,;:!?'


def norm_url(u: str) -> str:
    """Normalize a URL for ledger matching: lowercase scheme+host, drop the
    fragment and a trailing slash, strip trailing sentence punctuation. Query is
    KEPT (a job posting often lives at ?gh_jid=123). Returns '' for a non-http(s)
    string so callers can filter."""
    u = (u or "").strip().rstrip(_URL_TRAILING)
    if not u:
        return ""
    try:
        p = urlsplit(u)
    except ValueError:
        return ""
    if p.scheme.lower() not in ("http", "https") or not p.netloc:
        return ""
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), p.query, ""))


def cited_urls(text: str) -> Set[str]:
    """Every normalized http(s) URL that appears in ``text`` (empty set if none)."""
    out: Set[str] = set()
    for raw in _URL_RE.findall(text or ""):
        n = norm_url(raw)
        if n:
            out.add(n)
    return out


@dataclass
class WebCitationVerdict:
    """Which cited URLs were neither fetched this turn nor backed by the dossier."""
    phantom_urls: List[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.phantom_urls

    def message(self) -> str:
        """Re-prompt fed back to the coach, naming exactly what to fix."""
        urls = "\n".join(f"- {u}" for u in self.phantom_urls)
        return (
            "You cited web page(s) you did NOT fetch this turn (and that aren't in the "
            "person's profile):\n" + urls + "\n"
            "Call fetch_url on the exact URL before you cite it or state what it says, or "
            "remove the link — never attribute a claim to a page you didn't actually read."
        )

    def caveat(self) -> str:
        """User-visible note when a phantom citation ships anyway (re-prompt cap hit)."""
        if not self.phantom_urls:
            return ""
        return (
            "\n\n> ⚠️ I referenced page(s) I didn't fetch this turn ("
            + ", ".join(self.phantom_urls)
            + ") — I can't confirm what they say, so verify those sources yourself."
        )


def web_citation_verdict(draft: str, fetched_urls: Set[str], corpus: str = "") -> WebCitationVerdict:
    """A cited URL is legitimate if it was FETCHED this turn or appears in the
    dossier ``corpus`` (the user's own links). Anything else cited is a phantom.

    Matching is case-INSENSITIVE: build_corpus lowercases the whole corpus, but a
    cited/fetched URL keeps its path+query case (norm_url lowercases only
    scheme+host). A case-sensitive compare would therefore spuriously flag the
    user's OWN mixed-case links (e.g. github.com/Islander-Intel/Resume-Helper),
    which is exactly the class the corpus allow-list exists to protect."""
    cited = cited_urls(draft)
    if not cited:
        return WebCitationVerdict()
    allowed = {u.lower() for u in fetched_urls} | {u.lower() for u in cited_urls(corpus)}
    phantom = sorted(u for u in cited if u.lower() not in allowed)
    return WebCitationVerdict(phantom_urls=phantom)
