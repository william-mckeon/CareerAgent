# 0001 — careeragent-fetch: the egress + extraction box

> A new microservice that quarantines CareerAgent's two new sources of
> attacker-influenced input: the **first server-side fetch of a user-controlled
> URL** (job postings) and the **first ingestion of untrusted uploaded files**
> (PDF/DOCX resumes). One small, stateless box; **no database, no secrets beyond
> its inbound key, and no model calls**. Port **8008**. Phase 5 ("Reach").

## Goal

The coach (`careeragent-api`) needs to (1) read a job posting the user pastes as
a URL and (2) seed the profile from an uploaded resume. Both are hostile-input
problems:

- A URL fetch inside the coach is **SSRF** — the URL can target cloud metadata
  (`169.254.169.254`), a sibling service on the private network
  (`127.0.0.1:8001`, `careeragent-dossier:8006`), or any RFC1918 host.
- Parsing an uploaded file is **parser-DoS / zip-bomb / XXE**.

The fix is isolation: a dedicated box whose entire job is to do these two things
safely and hand back plain text. If it is ever compromised, it holds nothing and
can reach nothing but the public internet.

## The spine

```
POST /fetch {url}
  → validate_url(url)                 # SSRF control list, BEFORE any socket
  → httpx stream, follow_redirects=False
      each redirect: re-run validate_url on the Location, capped at MAX_REDIRECTS
  → content-type gate (html/xhtml/plain) else 415
  → stream body, abort > MAX_FETCH_BYTES → 413 (enforced during the read)
  → trafilatura main-content extract (bs4 fallback) → cap at MAX_TEXT_CHARS
  → {text, truncated, final_url, title}

POST /extract (multipart file)
  → read actual bytes, abort > MAX_UPLOAD_BYTES → 413
  → sniff MAGIC BYTES: %PDF | PK\x03\x04(+word/document.xml) | OLE(reject) | else 415
  → PDF: pdfplumber, cap MAX_PDF_PAGES, fenced; empty text → 422 (scanned, no OCR)
  → DOCX: zip-bomb guard (members/size/ratio) then python-docx (no XXE)
  → cap at MAX_TEXT_CHARS → {text, truncated, format, chars}
```

## Contract

**`POST /fetch`** (`X-API-Key: FETCH_API_KEY`) → `FetchResponse`
`{text, truncated, final_url, title}`. Body `{url}`.
Errors: `400` (SSRF/invalid), `502` (upstream), `413` (too large), `415` (type).

**`POST /extract`** (`X-API-Key`, multipart `file`) → `ExtractResponse`
`{text, truncated, format, chars}`.
Errors: `415` (unsupported), `413` (too large), `422` (scanned PDF), `400` (corrupt).

**`GET /health`** → `{status, service}` (no auth).

## The SSRF control list (`src/ssrf.py`)

Run **before the first connection** and **again on every redirect hop**:

1. **Scheme allowlist** — `http` / `https` only. `file:`, `ftp:`, `gopher:`,
   `data:`, `ws:`, … → `400`.
2. **Resolve to ALL records** — a literal-IP host is validated directly; a
   hostname is resolved via `socket.getaddrinfo` to **every** A/AAAA record and
   **each** is validated. A record set that mixes a public and a private address
   is rejected.
3. **Block list** (via stdlib `ipaddress`, IPv4-mapped IPv6 unwrapped first):
   unspecified (`0.0.0.0`/`::`), loopback (`127/8`, `::1`), private RFC1918
   (`10/8`, `172.16/12`, `192.168/16`), link-local (`169.254/16`, `fe80::/10`),
   IPv6 ULA (`fc00::/7`), multicast, reserved — **plus an explicit
   cloud-metadata check** (`169.254.169.254`, `fd00:ec2::254`) even though those
   are already link-local / ULA.
4. **Redirects** — `follow_redirects=False`; each `Location` is re-validated and
   the count is capped at `MAX_REDIRECTS` (5).
5. **Timeouts** — httpx connect+read `FETCH_TIMEOUT` (8s).
6. **Size cap** — the body is streamed and aborted the instant it exceeds
   `MAX_FETCH_BYTES` (2 MB) → `413`. An oversized `Content-Length` is refused up
   front, but the cap is **enforced on the actual stream**, never trusted from
   the header alone.
7. **Content-type gate** — only `text/html`, `application/xhtml+xml`,
   `text/plain` (else `415`).

## File-safety limits (`src/extract.py`)

- **Magic bytes, not filename** — PDF must start `%PDF`; DOCX must be a ZIP
  (`PK\x03\x04`) that **contains `word/document.xml`**; legacy `.doc` (OLE
  `\xD0\xCF\x11\xE0`) is explicitly refused (`415`).
- **Byte cap on the real read** — `MAX_UPLOAD_BYTES` (10 MB), on bytes actually
  read, not Content-Length → `413`.
- **PDF** — `pdfplumber` (pdfminer.six, pure-Python), capped at `MAX_PDF_PAGES`
  (30). Parsing is fully fenced (any exception → `400`). Empty/whitespace text →
  `422` with the fixed scanned-PDF message (no OCR in P5).
- **DOCX** — before decompressing any member, guard the zip: member count
  (≤ 2000), summed uncompressed size (≤ 50 MB), and expansion ratio (≤ 200×) →
  `400` if a bomb is suspected. Parsed with `python-docx`, whose lxml parser sets
  `resolve_entities=False`, disabling XXE external-entity expansion.
- **Text cap** — both endpoints cut at `MAX_TEXT_CHARS` (100k) and set
  `truncated`.

## Behaviour rules
1. **Validate the RESOLVED IP, not the hostname string.** The block decision is
   made on the addresses `getaddrinfo` returns.
2. **Re-validate every redirect hop.** No auto-follow.
3. **Enforce the size cap during the read**, not merely from Content-Length.
4. **Trust magic bytes, not the extension or the declared content-type.**
5. **Fail closed on parse errors** (`400`); never hang (page cap + timeouts).

## Residual risks (honest)
- **DNS rebinding.** We resolve+validate, then let httpx connect by hostname
  (which re-resolves). A DNS answer that flips between the two could aim the
  socket at a blocked IP. We shrink the window (per-hop re-validation, short
  timeouts) but do **not** close it. Fully closing it requires pinning the
  validated IP into the socket while preserving TLS SNI/cert verification against
  the hostname — deliberately out of scope for P5. For a first-fetch box behind
  api-only auth, on an isolated network, this is an accepted residual.
- **No OCR.** Scanned/image-only PDFs return `422`; adding OCR is a later phase.
- **In-memory buffering.** Both the fetched body and the upload are held in RAM,
  bounded by their byte caps.

## Non-goals
- Storing anything (the coach writes resume text to `careeragent-dossier`).
- Rendering JavaScript / headless-browser fetches (static HTTP only).
- OCR, image extraction, or `.doc`/`.rtf`/`.odt` parsing.
- Any model call — extraction is deterministic and pure-Python.

## Design decisions
- **Separate service, not in the api agent** — concentrates the SSRF + parser
  blast radius in one box that holds no data and can reach no sibling service.
- **No apt layer** — every dependency ships manylinux wheels on `python:3.11-slim`.
- **trafilatura first, bs4 fallback** — clean main-content text for postings,
  with a robust fallback and a reliable `<title>`.
- **python-docx over docx2txt** — its lxml parser disables entity resolution
  (XXE) by default, and it gives clean paragraph + table text.

---
*careeragent-fetch — part of the CareerAgent system. Internal port 8008.*
