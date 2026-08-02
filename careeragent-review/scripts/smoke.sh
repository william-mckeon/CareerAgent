#!/usr/bin/env bash
# =============================================================================
# careeragent-review smoke test — health + a real review-batch against the
# running stack. Run from the repo root (needs careeragent-review/.env with keys).
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

KEY="$(grep '^REVIEW_API_KEY=' .env | cut -d= -f2-)"
BASE="${BASE:-http://localhost:8007}"   # override if you publish a host port

echo "=== /health ==="
curl -sf "$BASE/health" | python -m json.tool

echo
echo "=== POST /review-batch (pass repos on the command line: REPOS='owner/a owner/b') ==="
REPOS="${REPOS:-}"
if [ -z "$REPOS" ]; then
  echo "  set REPOS='owner/repo ...' to review specific repos, or omit to enumerate."
  BODY='{"limit": 3}'
else
  arr=$(printf '"%s",' $REPOS | sed 's/,$//')
  BODY="{\"repos\": [$arr]}"
fi
curl -sf -X POST "$BASE/review-batch" \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d "$BODY" | python -m json.tool
