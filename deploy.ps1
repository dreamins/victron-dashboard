#!/usr/bin/env pwsh
# deploy.ps1 — Deploy changed files to production and VERIFY each one.
#
# Usage:
#   .\deploy.ps1                   # deploy all changed files
#   .\deploy.ps1 -Only dashboard   # dashboard only
#   .\deploy.ps1 -Only api         # solar-api python files only
#   .\deploy.ps1 -Only ble         # ble-bridge only
#
# Why this script exists: docker compose up -d recreates containers from the
# built image, wiping any docker cp changes. This script always does:
#   scp → docker cp → grep verify
# and aborts if verification fails so you are never silently running old code.

param(
    [ValidateSet("all","dashboard","api","ble")]
    [string]$Only = "all"
)

$ErrorActionPreference = "Stop"

$ServerFile = Join-Path $PSScriptRoot ".server"
if (-not (Test-Path $ServerFile)) {
    Write-Error ".server file not found. Create it: echo 'user@host' > .server"
}
$Server = (Get-Content $ServerFile).Trim()

function Deploy-File {
    param($Local, $Remote, $Container, $ContainerPath, $VerifyPattern)
    Write-Host "  scp $Local → $Server`:$Remote" -ForegroundColor Cyan
    scp $Local "${Server}:${Remote}"
    Write-Host "  docker cp → $ContainerPath" -ForegroundColor Cyan
    ssh $Server "docker cp ${Remote} ${Container}:${ContainerPath}"
    $count = ssh $Server "docker exec ${Container} grep -c '$VerifyPattern' '${ContainerPath}'"
    if ($count -eq 0 -or $null -eq $count) {
        Write-Error "VERIFY FAILED: '$VerifyPattern' not found in ${Container}:${ContainerPath}. Deploy aborted."
    }
    Write-Host "  VERIFIED ($count match)" -ForegroundColor Green
}

# ── Dashboard (index.html) ────────────────────────────────────────────────────
if ($Only -eq "all" -or $Only -eq "dashboard") {
    Write-Host "`n[Dashboard]" -ForegroundColor Yellow
    Deploy-File `
        "api/static/index.html" `
        "~/victron-dashboard/api/static/index.html" `
        "victron-solar-api-1" `
        "/app/static/index.html" `
        "victron_battery_sense"   # unique to current build
    Write-Host "  Hard-refresh browser (Ctrl+Shift+R) to pick up changes."
}

# ── Solar API python files ────────────────────────────────────────────────────
if ($Only -eq "all" -or $Only -eq "api") {
    Write-Host "`n[Solar API]" -ForegroundColor Yellow
    $apiFiles = @(
        @{ Local="api/main.py";       Remote="~/victron-dashboard/api/main.py";       Path="/app/main.py";       Pattern="type_map" },
        @{ Local="api/repository.py"; Remote="~/victron-dashboard/api/repository.py"; Path="/app/repository.py"; Pattern="battery_voltage" },
        @{ Local="api/config.py";     Remote="~/victron-dashboard/api/config.py";     Path="/app/config.py";     Pattern="BMS_MEASUREMENT" }
    )
    foreach ($f in $apiFiles) {
        if (Test-Path $f.Local) {
            Deploy-File $f.Local $f.Remote "victron-solar-api-1" $f.Path $f.Pattern
        }
    }
    Write-Host "  Restarting solar-api..." -ForegroundColor Cyan
    ssh $Server "docker restart victron-solar-api-1"
    Write-Host "  solar-api restarted." -ForegroundColor Green
}

# ── BLE bridge ────────────────────────────────────────────────────────────────
if ($Only -eq "all" -or $Only -eq "ble") {
    Write-Host "`n[BLE Bridge]" -ForegroundColor Yellow
    $bleFiles = @(
        @{ Local="ble-bridge/ble_bridge.py"; Remote="~/victron-dashboard/ble-bridge/ble_bridge.py"; Path="/app/ble_bridge.py"; Pattern="_last_adv_time" }
    )
    foreach ($f in $bleFiles) {
        if (Test-Path $f.Local) {
            Deploy-File $f.Local $f.Remote "victron-ble-bridge-1" $f.Path $f.Pattern
        }
    }
    Write-Host "  Restarting ble-bridge..." -ForegroundColor Cyan
    ssh $Server "docker restart victron-ble-bridge-1"
    Write-Host "  ble-bridge restarted." -ForegroundColor Green
    Write-Host "  If scanner hangs NotReady: sudo hciconfig hci1 reset && sudo hciconfig hci1 up"
}

Write-Host "`nDeploy complete." -ForegroundColor Green
