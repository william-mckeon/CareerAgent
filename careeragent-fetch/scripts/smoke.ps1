# =============================================================================
# careeragent-fetch smoke test (PowerShell) — health + /fetch + /extract.
# Run from careeragent-fetch/ (needs .env with FETCH_API_KEY).
#   $env:URL  = 'https://...'            # optional, a real posting to fetch
#   $env:FILE = 'C:\path\resume.pdf'     # optional, a real file to extract
#   .\scripts\smoke.ps1
# =============================================================================
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')

$key = (Select-String -Path .env -Pattern '^FETCH_API_KEY=').Line -replace '^FETCH_API_KEY=', ''
$base = if ($env:BASE) { $env:BASE } else { 'http://localhost:8008' }

Write-Host '=== /health ==='
Invoke-RestMethod -Uri "$base/health" | ConvertTo-Json

Write-Host ''
Write-Host '=== POST /fetch ==='
$url = if ($env:URL) { $env:URL } else { 'https://example.com/' }
$body = @{ url = $url } | ConvertTo-Json
Invoke-RestMethod -Uri "$base/fetch" -Method Post `
  -Headers @{ 'X-API-Key' = $key } -ContentType 'application/json' -Body $body | ConvertTo-Json

Write-Host ''
Write-Host '=== POST /extract ==='
if ($env:FILE) {
  $form = @{ file = Get-Item $env:FILE }
  Invoke-RestMethod -Uri "$base/extract" -Method Post `
    -Headers @{ 'X-API-Key' = $key } -Form $form | ConvertTo-Json
} else {
  Write-Host '  set $env:FILE = path to a resume.pdf/.docx to test extraction.'
}
