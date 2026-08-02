# careeragent-ats

> **The deterministic ATS keyword-coverage scorer for CareerAgent.** Given a
> resume's text and a job description, it reports how many of the JD's important
> keywords the resume covers — a fast, explainable, **model-free** signal. NO
> model, NO database, NO network egress. Holds **none** of the user's data.
> Port **8010**.

---

## Why it exists

The coach (`careeragent-api`) wants to tell the user *"your resume covers 8 of
the 12 skills this posting emphasizes; it's missing Kubernetes, Terraform,
GraphQL, and CI/CD."* That is a **deterministic text problem** — matching the
posting's keywords against the resume — and it should not cost a model call, a
round-trip, or any of the user's stored data.

So it lives in its own tiny box: pure text in, a coverage report out. No model,
no DB, no egress, no file parsing (the resume/JD arrive as text the caller
already fetched, e.g. via `careeragent-fetch`).

```
careeragent-api ──POST /ats-score──▶ careeragent-ats
                 {resume_text, job_description}   (pure text analysis)
                ◀── {score, coverage, matched, missing}
```

## What it does

### `POST /ats-score` — a resume + a JD in, a coverage report out
1. **Extract keywords from the job description** — hard skills, tools,
   technologies, frameworks, languages, certs, and role nouns. Unigrams **and**
   common tech bigrams (`machine learning`, `rest api`, `ci/cd`). Tech tokens
   like `c++`, `c#`, `node.js`, `.net` survive intact.
2. **Filter out stopwords + hiring fluff** — `team`, `player`, `fast-paced`,
   `responsibilities`, `experience`, `strong`, `excellent`, articles,
   prepositions, pronouns, … The goal is skill-like terms, not prose.
3. **Dedupe + rank + cap** — a bounded set (≤ 40), preferring multi-word tech
   phrases and capitalized / known-tech tokens.
4. **Match each keyword against the resume** — case-insensitive **word-boundary**
   match, a small **alias map** (`Postgres`↔`PostgreSQL`, `K8s`↔`Kubernetes`,
   `AWS`↔`Amazon Web Services`), and a guarded **rapidfuzz** near-match for minor
   spelling variants. Short/ambiguous tokens (`go`, `js`) are matched *exactly*
   only — `java` never matches `javascript`, `go` never matches `google`.
5. **Score** — `round(100 × matched / total)`.

## API

`POST /ats-score` (`X-API-Key: ATS_API_KEY`)
```json
{ "resume_text": "Senior Python engineer …", "job_description": "We are hiring a …" }
```
→
```json
{ "score": 67, "coverage": "8/12",
  "matched": ["python", "django", "docker", "aws", "..."],
  "missing": ["kubernetes", "terraform", "graphql", "ci/cd"] }
```

| Condition | Status | Body |
|---|---|---|
| success | `200` | `{score, coverage, matched, missing}` |
| empty / whitespace `job_description` | `400` | `{"detail": "job_description is required to score against."}` |
| `ATS_API_KEY` unset | `503` | — |
| bad / missing `X-API-Key` | `401` | — |

An **empty `resume_text` is allowed** → score `0`, everything missing.

`GET /health` → `{ "status": "ok", "service": "careeragent-ats" }` (no auth).

## Setup

```bash
docker network create careeragent-network        # once, shared by all services
cp .env.example .env                              # set ATS_API_KEY
docker compose up -d --build
docker logs careeragent-ats                       # "careeragent-ats ready."
```

Then wire `careeragent-api` (its `.env`): `ATS_URL=http://careeragent-ats:8010`
and `ATS_API_KEY=…`, and restart it — the coach gains the ATS-scoring tool.

## Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `ATS_API_KEY` | — | inbound auth (only caller: careeragent-api) |
| `LOG_LEVEL` | `INFO` | log verbosity |

## How the scoring works (and its honest limits)

The extraction + matching functions live in `src/ats.py` and are **pure and
deterministic** — the same inputs always give the same output, and every
`matched`/`missing` decision is explainable.

This is a **keyword-coverage heuristic, not a real applicant-tracking system.**
It does not parse a real ATS's proprietary rules, does not weight keywords by
importance, does not understand semantics or seniority, and can be gamed by
keyword-stuffing. It is a useful, transparent gap-finder — *"the posting stresses
these terms; your resume is missing these"* — not a hiring verdict. See
`docs/DATASHEET.md` and `specs/0001-ats.md` for the full method and limits.

The container runs unprivileged (uid 1000). No host port is published.

## Tests

`pytest` (hermetic — no network, no server):
- `test_ats.py` — extraction drops fluff / keeps tech terms; a known resume/JD
  pair scores as expected; empty resume → 0; `java`↛`javascript`; alias + fuzzy
  near-matches.
- `test_api.py` — inbound auth (401/503), the empty-JD 400, the response shape,
  and the unauthenticated `/health`.

---
*careeragent-ats — part of the CareerAgent system. Internal port 8010.*
