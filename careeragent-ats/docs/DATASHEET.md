# careeragent-ats — Datasheet

> Precise contract reference. The README is the narrative; this is the contract.

## Quick Reference

| Item | Value |
|---|---|
| Role | Deterministic ATS keyword-coverage scorer (resume vs. job description) |
| Port / path | `8010` — internal only, no host port |
| Kind | FastAPI; no DB; **no model calls**; **no network egress**; pure text analysis |
| Inbound | `POST /ats-score` (X-API-Key: ATS_API_KEY), `GET /health` |
| Sole client | `careeragent-api` (the coach's ATS-scoring tool) |
| Outbound | **None.** No sibling calls, no internet. |
| Holds secrets | `ATS_API_KEY` only. Holds **none** of the user's data. |

## API reference

### `POST /ats-score`
Body (`AtsRequest`): `{ "resume_text": "<str>", "job_description": "<str>" }`.

Success `200` (`AtsResponse`):
```json
{ "score": 67, "coverage": "8/12",
  "matched": ["python", "django", "docker"],
  "missing": ["kubernetes", "terraform"] }
```
- `score` — `int` 0-100, `round(100 * matched / total)` over the JD's extracted
  keywords. `0` when the JD yields no keywords at all.
- `coverage` — `"<matched>/<total>"`, e.g. `"8/12"`.
- `matched` / `missing` — the JD keywords found / not found in the resume, in
  extraction (salience) order. Lowercased; multi-word phrases are space-joined.

| Condition | Status | Body |
|---|---|---|
| success | `200` | `{score, coverage, matched, missing}` |
| `job_description` empty or whitespace-only | `400` | `{"detail": "job_description is required to score against."}` |
| `resume_text` empty | `200` | score `0`, everything missing (allowed, not an error) |
| `ATS_API_KEY` unset | `503` | — |
| bad/missing `X-API-Key` | `401` | — |

A `0/0` "score" is never returned for a real request: an empty/whitespace JD is a
`400`, and a keyword-yielding JD has `total > 0`.

### `GET /health` (no auth)
`{"status": "ok", "service": "careeragent-ats"}`

## Scoring method (`src/ats.py`)

All pure, deterministic functions — unit-testable without the API.

1. **Tokenize** — lowercased tokens that preserve tech punctuation, so `c++`,
   `c#`, `node.js`, `ci/cd`, `.net`, `k8s` survive as single tokens.
2. **`extract_keywords(job_description)`**
   - Detect known multi-word tech phrases (`machine learning`, `rest api`, …).
   - Collect unigrams; drop stopwords + hiring fluff and pure numbers.
   - Overlap-dedupe (a phrase's constituent words are dropped unless independently
     salient), salience-rank (multi-word phrase > tech-punctuation > known-tech >
     role noun > capitalized, with frequency/length tie-breakers), cap at 40.
3. **`keyword_matches(keyword, resume)`** — matched if, in order:
   - exact case-insensitive **word-boundary** match (boundaries defined against
     alphanumerics so tech punctuation works and `java` ≠ `javascript`);
   - any **alias-group** member matches (e.g. `kubernetes`↔`k8s`);
   - a **rapidfuzz** near-match (`fuzz.ratio ≥ 88`), attempted only for keywords
     ≥ 4 chars — short tokens (`go`, `js`, `c#`) are matched exactly only.
     `fuzz.ratio` (not `partial_ratio`) is used so a substring like `java` inside
     `javascript` does **not** score as a match.
4. **Score** — `round(100 * len(matched) / len(keywords))`.

## Ownership

### Owns
| Domain | Artifact |
|---|---|
| Keyword extraction (skills/tech/phrases, fluff filtering, ranking) | `src/ats.py::extract_keywords` |
| Keyword→resume matching (boundary + alias + guarded fuzzy) | `src/ats.py::keyword_matches` |
| The coverage score | `src/ats.py::score_resume` |
| Inbound auth | `src/security.py` |
| Empty-JD validation | `backend.api.ats_score` |

### Does NOT own
| Concern | Owner |
|---|---|
| Fetching the JD / extracting resume text | `careeragent-fetch` (the coach calls it first) |
| When to score / how to phrase the advice | `careeragent-api` (the coach's tool) |
| Where resume text is stored | `careeragent-dossier` |
| The model | not involved — this service makes no model calls |

## Residual limits (honest)

| Limit | Status |
|---|---|
| **Not a real ATS.** No proprietary ATS ruleset, no per-keyword weighting, no semantic/seniority understanding. | By design — a transparent coverage heuristic, not a hiring verdict. |
| **Keyword-stuffable.** A resume can raise its score by listing terms it doesn't back up. | Accepted; the score is a gap-finder, not proof of skill. |
| **Extraction is heuristic.** An unusual skill phrased oddly, or a rare acronym, may be mis-extracted or mis-matched. | Bounded by the curated vocab + alias map; extends easily. |
| **English-oriented.** Stopword/fluff lists and tech vocab are English. | Accepted for P7. |

## Container / deployment
- `python:3.11-slim`; **no apt layer** (all deps — fastapi, uvicorn, pydantic,
  python-dotenv, rapidfuzz — ship manylinux wheels); non-root uid 1000;
  `PYTHONPATH=/app/src`; `uvicorn backend.api:app` on `:8010`.
- Compose: single service on the external `careeragent-network`; no host port;
  stdlib `/health` healthcheck.

## Cross-references
- `specs/0001-ats.md` — design, the extraction/matching rules, honest limits
- `careeragent-api` — the ATS-scoring client + coach tool (to be written)

---
*careeragent-ats — part of the CareerAgent system. Internal port 8010.*
