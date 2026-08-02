#!/usr/bin/env bash
# =============================================================================
# careeragent-fetch smoke test — health + a real /fetch and /extract against the
# running stack. Run from careeragent-fetch/ (needs .env with FETCH_API_KEY).
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

KEY="$(grep '^FETCH_API_KEY=' .env | cut -d= -f2-)"
BASE="${BASE:-http://localhost:8008}"   # override if you publish a host port

echo "=== /health ==="
curl -sf "$BASE/health" | python -m json.tool

echo
echo "=== POST /fetch (set URL='https://...' to fetch a real posting) ==="
URL="${URL:-https://example.com/}"
curl -sf -X POST "$BASE/fetch" \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d "{\"url\": \"$URL\"}" | python -m json.tool

echo
echo "=== POST /extract (set FILE=path/to/resume.pdf to extract a real file) ==="
FILE="${FILE:-}"
if [ -z "$FILE" ]; then
  echo "  set FILE=path/to/resume.pdf (or .docx) to test extraction."
else
  curl -sf -X POST "$BASE/extract" \
    -H "X-API-Key: $KEY" \
    -F "file=@$FILE" | python -m json.tool
fi
