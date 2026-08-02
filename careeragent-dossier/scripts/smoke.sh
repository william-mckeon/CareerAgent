#!/usr/bin/env bash
# =============================================================================
# scripts/smoke.sh — careeragent-dossier smoke test (Bash / Git Bash).
#
# Run:  bash scripts/smoke.sh
#
# Reads DOSSIER_API_KEY from ../.env, then exercises the full tool surface on a
# fresh application: profile seed/edit + versioning, create/get/update, resume
# save/edit + version history + staleness, contacts, and all three lookup modes
# (fuzzy company, full-text, status filter). Requires the stack up on :8006.
# =============================================================================
set -euo pipefail
BASE=http://localhost:8006
KEY=$(grep '^DOSSIER_API_KEY=' "$(dirname "$0")/../.env" | cut -d= -f2)
auth=(-H "X-API-Key: $KEY" -H "Content-Type: application/json")
j() { python -c "import sys,json;d=json.load(sys.stdin);print($1)"; }

echo "== /health (no auth) =="
curl -s "$BASE/health"; echo

echo "== profile: seed (PUT) then edit (PATCH) =="
curl -s "${auth[@]}" -X PUT "$BASE/profile" \
  -d '{"content":"# Master Profile\n\n## Projects\n- Built CareerAgent.\n"}' | j "'  after save  -> version', d['version']"
curl -s "${auth[@]}" -X PATCH "$BASE/profile" \
  -d '{"old_string":"## Projects","new_string":"## Skills\n- Python, Postgres\n\n## Projects"}' | j "'  after edit  -> version', d['version']"
echo -n "  bad edit (nonexistent old_string) -> "
curl -s -o /dev/null -w "HTTP %{http_code}\n" "${auth[@]}" -X PATCH "$BASE/profile" \
  -d '{"old_string":"NOPE","new_string":"x"}'

echo "== create applications =="
CID=$(curl -s "${auth[@]}" -X POST "$BASE/applications" \
  -d '{"company":"Stripe","title":"Applied AI Engineer","job_description":"Build LLM agents for payments."}' | j "d['id']")
echo "  Stripe id=$CID"
curl -s "${auth[@]}" -X POST "$BASE/applications" -d '{"company":"Anthropic","title":"Research Engineer","job_description":"Agentic systems."}' >/dev/null
curl -s "${auth[@]}" -X POST "$BASE/applications" -d '{"company":"Datadog","title":"Backend Engineer","job_description":"Go at scale."}' >/dev/null

echo "== resume: save (v1) then edit (v2) =="
curl -s "${auth[@]}" -X PUT "$BASE/applications/$CID/resume" \
  -d '{"content":"WILLIAM MCKEON\n- Built CareerAgent (multi-service).\n"}' | j "'  save -> version', d['version']"
curl -s "${auth[@]}" -X PATCH "$BASE/applications/$CID/resume" \
  -d '{"old_string":"multi-service","new_string":"5 services on Bedrock"}' | j "'  edit -> version', d['version']"

echo "== contact + status/timeline update =="
curl -s "${auth[@]}" -X POST "$BASE/applications/$CID/contacts" \
  -d '{"name":"Jane Smith","role":"hiring manager","source":"LinkedIn"}' >/dev/null && echo "  contact added"
curl -s "${auth[@]}" -X PATCH "$BASE/applications/$CID" \
  -d '{"status":"interviewing","last_contact":"2026-06-30T12:00:00Z"}' >/dev/null && echo "  status/last_contact set"

echo "== edit profile again -> the Stripe resume is now stale =="
curl -s "${auth[@]}" -X PATCH "$BASE/profile" \
  -d '{"old_string":"Python, Postgres","new_string":"Python, Postgres, Bedrock"}' >/dev/null && echo "  profile bumped"

echo "== get application =="
curl -s "${auth[@]}" "$BASE/applications/$CID" | j "'  company=%s status=%s stale=%s resume_versions=%s contacts=%d'%(d['company'],d['status'],d['stale'],d['resume_versions'],len(d['contacts']))"

echo "== lookup =="
echo -n "  fuzzy company 'stipe' -> "; curl -s "${auth[@]}" "$BASE/applications?company=stipe" | j "[x['company'] for x in d]"
echo -n "  full-text q 'payments' -> "; curl -s "${auth[@]}" "$BASE/applications?q=payments" | j "[x['company'] for x in d]"
echo -n "  status=interviewing -> "; curl -s "${auth[@]}" "$BASE/applications?status=interviewing" | j "[x['company'] for x in d]"
echo -n "  stale=true -> "; curl -s "${auth[@]}" "$BASE/applications?stale=true" | j "[x['company'] for x in d]"

echo -n "== auth: missing key -> "; curl -s -o /dev/null -w "HTTP %{http_code} (expect 401)\n" "$BASE/profile"
echo "== smoke complete =="
