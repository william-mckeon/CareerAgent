# =============================================================================
# scripts/smoke.ps1 — careeragent-sessions smoke test (PowerShell-native).
#
# Run (from the careeragent-sessions folder or anywhere):
#   powershell -ExecutionPolicy Bypass -File scripts\smoke.ps1
#
# Reads SESSIONS_API_KEY from ..\.env, then exercises /health, two /chat turns
# in ONE conversation, the conversation list, and the restored transcript.
# Requires the careeragent-sessions stack up on :8005 (docker compose up -d).
# =============================================================================
$ErrorActionPreference = "Stop"
$base = "http://localhost:8005"

$envPath = Join-Path $PSScriptRoot "..\.env"
$key = ((Get-Content $envPath | Where-Object { $_ -match '^SESSIONS_API_KEY=' }) -replace '^SESSIONS_API_KEY=', '').Trim()
if (-not $key) { Write-Error "SESSIONS_API_KEY not found in $envPath"; exit 1 }
$headers = @{ "X-API-Key" = $key }

Write-Host "== /health =="
Invoke-RestMethod -Uri "$base/health" -UseBasicParsing | ConvertTo-Json -Compress

Write-Host "`n== chat turn 1 =="
$body1 = @'
{"messages":[{"role":"user","content":"Give me one strong resume action verb."}],"reasoning_effort":"low"}
'@
$r1 = Invoke-WebRequest -Uri "$base/chat" -Method Post -Headers $headers -ContentType "application/json" -Body $body1 -UseBasicParsing
$cid = $r1.Headers["X-Conversation-Id"]
Write-Host "conversation_id = $cid"
Start-Sleep -Seconds 2

Write-Host "== chat turn 2 (continue same conversation) =="
$body2 = @"
{"messages":[{"role":"user","content":"Give one more."}],"reasoning_effort":"low","conversation_id":"$cid"}
"@
Invoke-WebRequest -Uri "$base/chat" -Method Post -Headers $headers -ContentType "application/json" -Body $body2 -UseBasicParsing | Out-Null
Start-Sleep -Seconds 2

Write-Host "`n== conversations (list) =="
Invoke-RestMethod -Uri "$base/conversations" -Headers $headers -UseBasicParsing | ConvertTo-Json -Depth 6

Write-Host "`n== transcript ($cid) =="
Invoke-RestMethod -Uri "$base/conversations/$cid" -Headers $headers -UseBasicParsing | ConvertTo-Json -Depth 6
