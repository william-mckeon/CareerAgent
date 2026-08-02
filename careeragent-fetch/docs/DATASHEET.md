# careeragent-fetch — Datasheet

> Precise contract reference. The README is the narrative; this is the contract.

## Quick Reference

| Item | Value |
|---|---|
| Role | Egress + extraction box (fetch a URL; extract an uploaded resume) |
| Port / path | `8008` — internal only, no host port |
| Kind | FastAPI; no DB; **no model calls**; pure-Python parsers |
| Inbound | `POST /fetch`, `POST /extract` (X-API-Key: FETCH_API_KEY), `GET /health` |
| Sole client | `careeragent-api` (the coach's fetch/extract tools) |
| Outbound | Only the user-controlled URL on `/fetch` (SSRF-validated). No sibling calls. |
| Holds secrets | `FETCH_API_KEY` only. Holds **none** of the user's data. |

## API reference

### `POST /fetch`
Body (`FetchRequest`): `{ "url": "<http/https string>" }`.

Success `200` (`FetchResponse`):
```json
{ "text": "<clean text>", "truncated": false,
  "final_url": "https://careers.example.com/jobs/123", "title": "Senior Engineer" }
```
`title` may be `null`. `truncated` is `true` when `text` was cut at `MAX_TEXT_CHARS`.

| Condition | Status | Body |
|---|---|---|
| SSRF-blocked / invalid URL / non-http(s) scheme / no host | `400` | `{"detail": "<reason>"}` |
| upstream timeout / transport error / status ≥ 400 / redirect loop | `502` | `{"detail": "<reason>"}` |
| Content-Length or streamed body over `MAX_FETCH_BYTES` | `413` | `{"detail": "<reason>"}` |
| content-type not html/xhtml/plain | `415` | `{"detail": "<reason>"}` |
| `FETCH_API_KEY` unset | `503` | — |
| bad/missing `X-API-Key` | `401` | — |

### `POST /extract`
Multipart upload: `file=@resume.pdf` (FastAPI `UploadFile = File(...)`; needs
`python-multipart`).

Success `200` (`ExtractResponse`):
```json
{ "text": "<extracted text>", "truncated": false, "format": "pdf", "chars": 1234 }
```
`format` ∈ `pdf | docx`, decided by **magic bytes**.

| Condition | Status | Body |
|---|---|---|
| not a PDF/DOCX, or legacy `.doc` (OLE), or a zip that isn't a DOCX | `415` | `{"detail": ...}` |
| actual bytes read over `MAX_UPLOAD_BYTES` | `413` | `{"detail": ...}` |
| scanned/image-only PDF (no extractable text) | `422` | `{"detail": "This PDF appears to be scanned/image-only; no text could be extracted (OCR is not supported)."}` |
| malformed / corrupt file (bad zip, unparseable PDF, zip bomb) | `400` | `{"detail": ...}` |

### `GET /health` (no auth)
`{"status": "ok", "service": "careeragent-fetch"}`

## Ownership

### Owns
| Domain | Artifact |
|---|---|
| The SSRF control list + safe streamed fetch | `src/ssrf.py` |
| Magic-byte validation + fenced PDF/DOCX extraction | `src/extract.py` |
| The upload byte cap (actual read) | `backend.api._read_capped` |
| Inbound auth | `src/security.py` |

### Does NOT own
| Concern | Owner |
|---|---|
| When to fetch / when to ingest a resume | `careeragent-api` (the coach's tools) |
| Where the resume text is stored | `careeragent-dossier` (the coach writes it) |
| The model | not involved — this service makes no model calls |

## Security controls (summary)

| Control | Where |
|---|---|
| Scheme allowlist (http/https only) | `ssrf.validate_url` |
| Resolve ALL A/AAAA records; block loopback/private/link-local/ULA/multicast/reserved/unspecified/metadata | `ssrf._reason_blocked` |
| Literal-IP hosts validated directly | `ssrf.validate_url` |
| Manual, per-hop re-validated redirects (`follow_redirects=False`) | `ssrf.fetch_url` |
| Streamed body size cap (not Content-Length trust) | `ssrf.fetch_url` |
| Content-type gate | `ssrf.fetch_url` |
| Upload byte cap on actual read | `api._read_capped` |
| Magic-byte format decision | `extract._sniff` |
| PDF page cap + fenced parse | `extract._extract_pdf` |
| DOCX zip-bomb guard (members/size/ratio) + `resolve_entities=False` | `extract._extract_docx` |

## Residual risks (honest)

| Risk | Status |
|---|---|
| **DNS rebinding** — the answer can flip between validate and connect | Mitigated (per-hop re-validation + short timeouts), **not fully closed**. Closing it needs IP-pinning with TLS SNI preserved — out of scope for P5. |
| **No OCR** — scanned/image-only PDFs | By design → `422` with a clear message. |
| **In-memory buffering** — body/upload held in RAM up to the caps | Bounded by `MAX_FETCH_BYTES` / `MAX_UPLOAD_BYTES`. |

## Container / deployment
- `python:3.11-slim`; **no apt layer** (all deps ship manylinux wheels); non-root
  uid 1000; `PYTHONPATH=/app/src`; `uvicorn backend.api:app` on `:8008`.
- Compose: single service on the external `careeragent-network`; no host port;
  stdlib `/health` healthcheck.

## Cross-references
- `specs/0001-fetch.md` — design, the full control list, residual risks
- `careeragent-api` — the fetch/extract client + coach tools (to be written)

---
*careeragent-fetch — part of the CareerAgent system. Internal port 8008.*
