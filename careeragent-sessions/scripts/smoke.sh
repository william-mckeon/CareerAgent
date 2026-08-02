#!/usr/bin/env bash
# =============================================================================
# scripts/smoke.sh — careeragent-sessions smoke test (Bash / Git Bash).
#
# Run:  bash scripts/smoke.sh
#
# Reads SESSIONS_API_KEY from ../.env, then exercises /health, two /chat turns
# in ONE conversation, the conversation list, and the restored transcript.
# Requires the careeragent-sessions stack up on :8005.
# =============================================================================
set -euo pipefail
BASE=http://localhost:8005
KEY=$(grep '^SESSIONS_API_KEY=' "$(dirname "$0")/../.env" | cut -d= -f2)

echo "== /health =="
curl -s "$BASE/health"; echo

echo "== chat turn 1 =="
CID=$(curl -s -D - -o /dev/null -X POST "$BASE/chat" \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Give me one strong resume action verb."}],"reasoning_effort":"low"}' \
  | grep -i x-conversation-id | tr -d '\r' | awk '{print $2}')
echo "conversation_id = $CID"; sleep 2

echo "== chat turn 2 (continue same conversation) =="
curl -s -o /dev/null -X POST "$BASE/chat" \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d "{\"messages\":[{\"role\":\"user\",\"content\":\"Give one more.\"}],\"reasoning_effort\":\"low\",\"conversation_id\":\"$CID\"}"
sleep 2

echo; echo "== conversations (list) =="
curl -s -H "X-API-Key: $KEY" "$BASE/conversations"
echo; echo "== transcript ($CID) =="
curl -s -H "X-API-Key: $KEY" "$BASE/conversations/$CID"; echo
