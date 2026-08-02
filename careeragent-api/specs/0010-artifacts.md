# 0010 — Phase 7 #16: Artifact generation (render + ATS)

> Promoted from the P7 scaffold (0009 #16). Turn "edited text in a DB" into "a polished PDF that covers
> 8/12 of the JD's keywords." Two new services on the careeragent-fetch/review mold, delegated like
> `review_repos`/`fetch_url`. **Split build:** `ats_score` (light, read-only, pure JSON) ships first;
> `render_resume` (heavy — binary transport) second.

**Status:** ✅ built — careeragent-ats (`ats_score`, commit `175c962`) + careeragent-render
(`render_resume`, commit `841ae4e`); both adversarially reviewed (7 + 8 findings), live-verified
end-to-end. · **Depends on:** P3 (ledger), P5 (service mold), P7 #19 (the typed SSE channel)
· **Last updated:** 2026-07-21

## Shape (ratified)
- **Two new services, both stateless, holding no user data** (the api reads the résumé/JD from dossier
  and passes text down): `careeragent-ats` (port **8010**) and `careeragent-render` (port **8009**).
- **`ats_score` is a READ tool** — deterministic JD-keyword coverage; no model, no DB, no egress. Ships
  first (no binary-transport problem).
- **`render_resume` is a WRITE tool** — markdown résumé → PDF/DOCX bytes via **reportlab** (pure-Python,
  keeps the image thin; no weasyprint apt stack). It must emit a **machine-checkable receipt**
  `{op: "rendered_resume", artifact_id}` (like `review_repos`) or the P3 gate challenges "I rendered your
  PDF."
- **Binary transport (the hard part):** bytes CANNOT ride a `ToolResult` (fed to the model as text) nor
  the `/chat` SSE content stream. So: the artifact is **stored in dossier** (`resume_artifacts` bytea),
  `render_resume` returns only the receipt, the api emits a **`KIND_ARTIFACT`** typed frame (P7 #19
  channel), and the frontend renders a native **`st.download_button`** that GETs the bytes from an api
  **download proxy** `GET /applications/{id}/artifact` — re-rendered from history so it survives rerun.

## careeragent-ats (port 8010) — the light half, first
`POST /ats-score {resume_text, job_description}` → `{score, matched[], missing[], coverage}` +
`GET /health`. X-API-Key. Deterministic: extract JD keywords (hard skills / tools / role nouns; drop
fluff), normalize, exact + fuzzy (rapidfuzz) match against the résumé, score = matched/total. An empty JD
→ a clear 400 (never a 0/0 score). Mold: clone careeragent-fetch minus egress (security.py, schemas.py,
Dockerfile pure slim, compose, README/DATASHEET/specs/0001-ats.md, hygiene files).

**api wiring (ats):** `client/ats.py` (mirror `client/fetch.py`); `ats_score` in `READ_TOOLS` +
`_READ_SCHEMAS` + a dispatch branch; thread `ats_client` through the dispatch sites + `run_agent`; config
triple `ATS_URL/ATS_API_KEY/ATS_ENABLED` + fail-soft lifespan client.

## careeragent-render (port 8009) — the heavy half, second
`POST /render {resume, format: pdf|docx, options?}` → `{content_b64, format, bytes}` (or a binary body) +
`GET /health`. X-API-Key. **reportlab** for PDF, **python-docx** for DOCX; markdown→layout. Typed
`RenderProblem.status_code`. Mold: clone careeragent-fetch.

**api wiring (render):** `client/render.py`; `render_resume` in `WRITE_TOOLS` + `MUTATING` +
`_WRITE_OPS` (`rendered_resume`) + `_WRITE_SCHEMAS` + a dispatch branch returning the receipt; thread
`render_client`; config triple + lifespan; **download proxy** `GET /applications/{id}/artifact`.

**dossier:** `database/migrations/0005_resume_artifacts.sql` (+ init.sql) — `resume_artifacts`
(application_id FK, version, format, content bytea, ats_score/matched/missing, created_at); `store.save_artifact`/
`get_artifact`; `POST` + `GET .../artifact` (StreamingResponse); `client/dossier.py` methods.

**frontend:** `sse_decoder.py` `KIND_ARTIFACT` parse; `app.py` a `st.download_button` fed by the api
proxy, stashed in `session_state` and re-rendered from history.

## Acceptance
- [x] `ats_score` returns a deterministic coverage score against a JD; an empty JD fails clearly.
- [x] `ats_score` is a read tool, usable in plan mode; egress-free service.
- [x] `render_resume` produces a real PDF/DOCX, stored + downloadable via the proxy; the tool returns a
      verified receipt (P3-safe); a `KIND_ARTIFACT` frame reaches the frontend which shows a download button.
- [x] The binary never rides a tool result or the SSE content stream.

## Known limitation (ratified scope: "survives rerun")
The frontend download button is **session-local**: it survives Streamlit reruns (re-drawn
from `st.session_state.messages`, bytes re-fetched from the download proxy by id), but a
**full page reload or conversation switch** loses it — careeragent-sessions persists only
`{role, content}` per message, not the `KIND_ARTIFACT` metadata, so a rehydrated transcript
has no button. The rendered document is **not lost** (the bytes persist in dossier
`resume_artifacts`); the user just re-renders to get a fresh button. **Follow-up (durable
downloads):** add an `artifacts jsonb` column to the sessions `messages` table, capture the
`KIND_ARTIFACT` frame in the /chat relay (as `_scan_sse` does for the suspend frame), return
it from `GET /conversations/{id}`, and carry it through the frontend's `_bind_transcript`.

## Non-goals
ATS *rewriting* automation beyond the coverage report; multi-template rendering; the jobs/cron path (#18).

*careeragent-api — Phase 7 #16 (artifacts). Part of the CareerAgent system. Ports 8009 (render), 8010 (ats).*
