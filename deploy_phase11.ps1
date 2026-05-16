# deploy_phase11.ps1 — Deploy Phase 11 (Dashboard multi-site UI) to Linux server.
# Pulls the latest commit, rebuilds solar-api, and runs automated verification.
# Run from D:\projects\victron with:  powershell -File deploy_phase11.ps1
param(
    [string]$Server = "user@192.168.1.x",
    [string]$RemoteDir = "~/victron-dashboard"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "=== Phase 11 deploy: Dashboard multi-site UI ===" -ForegroundColor Cyan
Write-Host "    Target: $Server`:$RemoteDir"
Write-Host ""

# ─── 1. Sync updated files ────────────────────────────────────────────────────

Write-Host "Syncing Phase 11 files..." -ForegroundColor Cyan

$files = @(
    "api/static/index.html",
    "api/tests/test_ui.py",
    "api/tests/test_ui_visual.py",
    "test_phase11.sh"
)

foreach ($file in $files) {
    $remotePath = "$Server`:$RemoteDir/$($file -replace '\\','/')"
    Write-Host "  scp $file"
    & scp $file $remotePath
    if ($LASTEXITCODE -ne 0) { throw "scp failed for $file" }
}

# ─── 2. Rebuild solar-api container (bakes in new index.html) ─────────────────

Write-Host ""
Write-Host "Rebuilding solar-api container..." -ForegroundColor Cyan
& ssh $Server "cd $RemoteDir && docker compose build --no-cache solar-api 2>&1 | tail -5"
if ($LASTEXITCODE -ne 0) { Write-Host "FAIL: docker compose build failed" -ForegroundColor Red; exit 1 }

# ─── 3. Restart solar-api (no downtime for other services) ────────────────────

Write-Host ""
Write-Host "Restarting solar-api..." -ForegroundColor Cyan
& ssh $Server "cd $RemoteDir && docker compose up -d solar-api 2>&1"
if ($LASTEXITCODE -ne 0) { Write-Host "FAIL: docker compose up failed" -ForegroundColor Red; exit 1 }

# Give the container a moment to start
Start-Sleep -Seconds 3

# ─── 4. Run automated Phase 11 checks ────────────────────────────────────────

Write-Host ""
Write-Host "Running Phase 11 automated checks..." -ForegroundColor Cyan
& ssh $Server "cd $RemoteDir && bash test_phase11.sh"
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "FAIL: Phase 11 automated checks failed" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "=== Phase 11 deploy complete ===" -ForegroundColor Green
