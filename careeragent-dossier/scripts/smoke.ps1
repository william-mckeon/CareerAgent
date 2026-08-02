# =============================================================================
# scripts/smoke.ps1 — careeragent-dossier smoke test (PowerShell-native).
#
# Run:  powershell -ExecutionPolicy Bypass -File scripts\smoke.ps1
#
# Reads DOSSIER_API_KEY from ..\.env, then exercises the full tool surface:
# profile seed/edit + versioning, create/get/update, resume save/edit + version
# history + staleness, contacts, and lookup (fuzzy, full-text, status filter).
# Requires the stack up on :8006 (docker compose up -d --build).
# =============================================================================
$ErrorActionPreference = "Stop"
$base = "http://localhost:8006"

$envPath = Join-Path $PSScriptRoot "..\.env"
$key = ((Get-Content $envPath | Where-Object { $_ -match '^DOSSIER_API_KEY=' }) -replace '^DOSSIER_API_KEY=', '').Trim()
if (-not $key) { Write-Error "DOSSIER_API_KEY not found in $envPath"; exit 1 }
$h = @{ "X-API-Key" = $key }
function Body($o) { $o | ConvertTo-Json -Compress }

Write-Host "== /health (no auth) =="
Invoke-RestMethod -Uri "$base/health" | ConvertTo-Json -Compress

Write-Host "`n== profile: seed then edit =="
$r = Invoke-RestMethod -Uri "$base/profile" -Method Put -Headers $h -ContentType "application/json" -Body (Body @{ content = "# Master Profile`n`n## Projects`n- Built CareerAgent.`n" })
Write-Host "  after save  -> version $($r.version)"
$r = Invoke-RestMethod -Uri "$base/profile" -Method Patch -Headers $h -ContentType "application/json" -Body (Body @{ old_string = "## Projects"; new_string = "## Skills`n- Python, Postgres`n`n## Projects" })
Write-Host "  after edit  -> version $($r.version)"
try {
  Invoke-RestMethod -Uri "$base/profile" -Method Patch -Headers $h -ContentType "application/json" -Body (Body @{ old_string = "NOPE"; new_string = "x" }) | Out-Null
} catch { Write-Host "  bad edit -> HTTP $($_.Exception.Response.StatusCode.value__) (expect 422)" }

Write-Host "`n== create applications =="
$cid = (Invoke-RestMethod -Uri "$base/applications" -Method Post -Headers $h -ContentType "application/json" -Body (Body @{ company = "Stripe"; title = "Applied AI Engineer"; job_description = "Build LLM agents for payments." })).id
Write-Host "  Stripe id=$cid"
Invoke-RestMethod -Uri "$base/applications" -Method Post -Headers $h -ContentType "application/json" -Body (Body @{ company = "Anthropic"; title = "Research Engineer"; job_description = "Agentic systems." }) | Out-Null
Invoke-RestMethod -Uri "$base/applications" -Method Post -Headers $h -ContentType "application/json" -Body (Body @{ company = "Datadog"; title = "Backend Engineer"; job_description = "Go at scale." }) | Out-Null

Write-Host "`n== resume: save (v1) then edit (v2) =="
$r = Invoke-RestMethod -Uri "$base/applications/$cid/resume" -Method Put -Headers $h -ContentType "application/json" -Body (Body @{ content = "WILLIAM MCKEON`n- Built CareerAgent (multi-service).`n" })
Write-Host "  save -> version $($r.version)"
$r = Invoke-RestMethod -Uri "$base/applications/$cid/resume" -Method Patch -Headers $h -ContentType "application/json" -Body (Body @{ old_string = "multi-service"; new_string = "5 services on Bedrock" })
Write-Host "  edit -> version $($r.version)"

Write-Host "`n== contact + status/timeline update =="
Invoke-RestMethod -Uri "$base/applications/$cid/contacts" -Method Post -Headers $h -ContentType "application/json" -Body (Body @{ name = "Jane Smith"; role = "hiring manager"; source = "LinkedIn" }) | Out-Null
Write-Host "  contact added"
Invoke-RestMethod -Uri "$base/applications/$cid" -Method Patch -Headers $h -ContentType "application/json" -Body (Body @{ status = "interviewing"; last_contact = "2026-06-30T12:00:00Z" }) | Out-Null
Write-Host "  status/last_contact set"

Write-Host "`n== edit profile again -> Stripe resume now stale =="
Invoke-RestMethod -Uri "$base/profile" -Method Patch -Headers $h -ContentType "application/json" -Body (Body @{ old_string = "Python, Postgres"; new_string = "Python, Postgres, Bedrock" }) | Out-Null
Write-Host "  profile bumped"

Write-Host "`n== get application =="
$a = Invoke-RestMethod -Uri "$base/applications/$cid" -Headers $h
Write-Host "  company=$($a.company) status=$($a.status) stale=$($a.stale) resume_versions=$($a.resume_versions) contacts=$($a.contacts.Count)"

Write-Host "`n== lookup =="
Write-Host "  fuzzy 'stipe'    -> $((Invoke-RestMethod -Uri "$base/applications?company=stipe" -Headers $h).company -join ', ')"
Write-Host "  full-text 'payments' -> $((Invoke-RestMethod -Uri "$base/applications?q=payments" -Headers $h).company -join ', ')"
Write-Host "  status=interviewing  -> $((Invoke-RestMethod -Uri "$base/applications?status=interviewing" -Headers $h).company -join ', ')"
Write-Host "  stale=true       -> $((Invoke-RestMethod -Uri "$base/applications?stale=true" -Headers $h).company -join ', ')"

Write-Host "`n== auth: missing key =="
try { Invoke-RestMethod -Uri "$base/profile" | Out-Null }
catch { Write-Host "  HTTP $($_.Exception.Response.StatusCode.value__) (expect 401)" }
Write-Host "== smoke complete =="
