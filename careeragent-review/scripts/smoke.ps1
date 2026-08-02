# =============================================================================
# careeragent-review smoke test (PowerShell). Run from careeragent-review/.
#   $env:REPOS = "owner/a owner/b"   # optional; else the service enumerates
#   .\scripts\smoke.ps1
# =============================================================================
$ErrorActionPreference = "Stop"
$key  = (Select-String -Path .env -Pattern '^REVIEW_API_KEY=').Line -replace '^REVIEW_API_KEY=', ''
$base = if ($env:BASE) { $env:BASE } else { "http://localhost:8007" }

Write-Host "=== /health ==="
Invoke-RestMethod "$base/health" | ConvertTo-Json

Write-Host "`n=== POST /review-batch ==="
if ($env:REPOS) {
    $body = @{ repos = ($env:REPOS -split '\s+') } | ConvertTo-Json
} else {
    $body = @{ limit = 3 } | ConvertTo-Json
}
Invoke-RestMethod -Method Post "$base/review-batch" `
    -Headers @{ "X-API-Key" = $key; "Content-Type" = "application/json" } `
    -Body $body | ConvertTo-Json -Depth 6
