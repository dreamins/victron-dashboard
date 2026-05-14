# deploy_phase9.ps1 — Sync Phase 9 changes to Linux server and run hardware verification.
# Run from D:\projects\victron with:  powershell -File deploy_phase9.ps1
param(
    [string]$Server = "user@192.168.1.x",
    [string]$RemoteDir = "~/victron-dashboard"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "=== Phase 9 deploy: syncing files to $Server ===" -ForegroundColor Cyan

$files = @(
    "ble-bridge/ble_bridge.py",
    "ble-bridge/drivers/litime.py",
    "ble-bridge/tests/test_decoder.py",
    "ble-bridge/tests/test_fixture_replay.py",
    "ble-bridge/fixtures/sites_fixture.json",
    "api/main.py",
    "api/seed_test_data.py",
    "api/tests/test_api.py",
    "api/tests/sites_fixture.json",
    "test_phase9.sh",
    "verify_phase9_hardware.sh"
)

foreach ($file in $files) {
    $remotePath = "$Server`:$RemoteDir/$($file -replace '\\','/')"
    Write-Host "  scp $file -> $remotePath"
    & scp $file $remotePath
    if ($LASTEXITCODE -ne 0) { throw "scp failed for $file" }
}

Write-Host ""
Write-Host "=== Running hardware verification on Linux ===" -ForegroundColor Cyan
& ssh $Server "cd $RemoteDir && bash verify_phase9_hardware.sh 2>&1"
if ($LASTEXITCODE -ne 0) { Write-Host "FAIL: hardware verification failed" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "=== Phase 9 deploy complete ===" -ForegroundColor Green
