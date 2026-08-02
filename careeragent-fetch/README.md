# careeragent-fetch

> **The egress + extraction box for CareerAgent.** The system's *first*
> server-side fetch of a user-controlled URL and its *first* handler of untrusted
> uploaded files — deliberately isolated into one small, stateless service so
> that blast radius never touches the coach. Holds **none** of the user's data.
> Port **8008**.

---

## Why it exists

Two new capabilities in Phase 5 ("Reach") both mean handling attacker-influenced
input:

1. **Fetch a job posting from a URL** the user pastes. A naïve `httpx.get(url)`
   inside `careeragent-api` is a textbook **SSRF** — the URL could point at
   `169.254.169.254` (cloud metadata), `127.0.0.1:8001` (a sibling service), or a
   private-range host.
2. **Read an uploaded resume** (PDF/DOCX). Parsing an untrusted file is a
   textbook **parser-DoS / zip-bomb / XXE** surface.

Rather than spread that risk through the coach, it is quarantined here: a tiny
box with one job each, no database, no secrets beyond its own inbound key, and
**no model calls at all**.

```
careeragent-api ──POST /fetch────▶ careeragent-fetch ──▶ the public internet
                ──POST /extract──▶                        (SSRF-validated egress)
```

## What it does

### `POST /fetch` — a URL in, clean text out
- Validates the URL against the **SSRF control list before every connection**
  (and re-validates on **every redirect hop** — redirects are followed manually).
- Streams the body with a **hard byte cap** (enforced during the read, not from
  Content-Length) and gates the content-type to HTML / XHTML / plain text.
- Returns trafilatura-extracted main-content text (BeautifulSoup fallback), the
  final URL, and the page title.

### `POST /extract` — a resume file in, its text out
- Decides the format by **magic bytes**, never the filename: PDF (`%PDF`), DOCX
  (a ZIP containing `word/document.xml`). Legacy `.doc` (OLE) is refused.
- Caps the **actual bytes read**, caps PDF **pages**, and guards DOCX against
  **zip bombs** (member count / uncompressed-size / expansion ratio) before any
  member is decompressed.
- A scanned/image-only PDF (no extractable text) returns **422** — there is no
  OCR in P5.

## API

`POST /fetch` (`X-API-Key: FETCH_API_KEY`)
```json
{ "url": "https://careers.example.com/jobs/123" }
```
→ `{ "text": "...", "truncated": false, "final_url": "https://...", "title": "..." }`

| Failure | Status |
|---|---|
| SSRF-blocked / invalid URL / bad scheme | `400` |
| upstream timeout / transport error / bad status | `502` |
| body over `MAX_FETCH_BYTES` | `413` |
| non-HTML/-text content-type | `415` |

`POST /extract` (`X-API-Key: FETCH_API_KEY`, multipart `file=...`)
→ `{ "text": "...", "truncated": false, "format": "pdf", "chars": 1234 }`

| Failure | Status |
|---|---|
| not a PDF/DOCX (or legacy `.doc`) | `415` |
| over `MAX_UPLOAD_BYTES` | `413` |
| scanned/image-only PDF (no text) | `422` |
| malformed / corrupt file | `400` |

`GET /health` → `{ "status": "ok", "service": "careeragent-fetch" }` (no auth).

## Setup

```bash
docker network create careeragent-network        # once, shared by all services
cp .env.example .env                              # set FETCH_API_KEY
docker compose up -d --build
docker logs careeragent-fetch                     # "careeragent-fetch ready."
```

Then wire `careeragent-api` (its `.env`): `FETCH_URL=http://careeragent-fetch:8008`
and `FETCH_API_KEY=…`, and restart it — the coach gains the fetch/extract tools.

## Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `FETCH_API_KEY` | — | inbound auth (only caller: careeragent-api) |
| `MAX_FETCH_BYTES` | `2000000` | response body cap (streamed) → 413 |
| `FETCH_TIMEOUT` | `8` | httpx connect+read timeout (s) |
| `MAX_REDIRECTS` | `5` | manual, re-validated redirect hops |
| `MAX_UPLOAD_BYTES` | `10000000` | upload byte cap (actual read) → 413 |
| `MAX_PDF_PAGES` | `30` | PDF pages parsed per document |
| `MAX_TEXT_CHARS` | `100000` | returned-text cap; sets `truncated` |

## Security model

- **SSRF**: scheme allowlist (http/https only); resolve the host to **all** A/AAAA
  records and reject if **any** is loopback / private / link-local / ULA /
  multicast / reserved / unspecified / a cloud-metadata IP; validate **literal
  IPs** directly; **manual, re-validated** redirects; streamed size cap;
  content-type gate. See `src/ssrf.py` and `specs/0001-fetch.md`.
- **File safety**: magic-byte validation (not extension), byte cap on the real
  read, PDF page cap, DOCX zip-bomb guard, `resolve_entities=False` (no XXE).
- **Residual risks** (documented honestly in the spec): a small **DNS-rebinding
  window** between validate and connect (mitigated by re-validation + short
  timeouts, not fully closed), and **no OCR** (scanned PDFs → 422).

The container runs unprivileged (uid 1000). No host port is published.

## Tests

`pytest` (hermetic — monkeypatched DNS, in-test PDF/DOCX fixtures, no network):
the SSRF validator (`test_ssrf.py`), file-safety + extraction (`test_extract.py`),
and inbound auth + the upload cap (`test_api.py`).

---
*careeragent-fetch — part of the CareerAgent system. Internal port 8008.*
