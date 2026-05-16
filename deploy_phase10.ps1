# deploy_phase10.ps1 — Sync Phase 10 changes to Linux server and run multi-site API tests.
# Run from D:\projects\victron with:  powershell -File deploy_phase10.ps1
param(
    [string]$Server = "user@192.168.1.x",
    [string]$RemoteDir = "~/victron-dashboard"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "=== Phase 10 deploy: syncing files to $Server ===" -ForegroundColor Cyan

$files = @(
    "api/tests/sites_fixture.json",
    "api/tests/test_api.py",
    "api/seed_test_data.py",
    "test_phase10.sh"
)

foreach ($file in $files) {
    $remotePath = "$Server`:$RemoteDir/$($file -replace '\\','/')"
    Write-Host "  scp $file -> $remotePath"
    & scp $file $remotePath
    if ($LASTEXITCODE -ne 0) { throw "scp failed for $file" }
}

Write-Host ""
Write-Host "=== Running Phase 10 tests on Linux ===" -ForegroundColor Cyan
& ssh $Server "cd $RemoteDir && bash test_phase10.sh 2>&1"
if ($LASTEXITCODE -ne 0) { Write-Host "FAIL: Phase 10 tests failed" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "=== Phase 10 deploy complete ===" -ForegroundColor Green
