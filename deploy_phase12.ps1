# deploy_phase12.ps1 — Push Phase 12 to GitHub, deploy to production server, run tests.
#
# The server-side test script handles everything safely:
#   - backs up sites.json, .env, and InfluxDB before touching anything
#   - rebuilds only ble-bridge and solar-api (influxdb/mosquitto untouched)
#   - rolls back automatically on test failure
#
# Prerequisites:
#   1. Create a .server file in the project root (gitignored):
#         echo "username@server-ip" > .server
#      Example: echo "user@192.168.1.x" > .server
#   2. SSH key auth to the server must be set up (no password prompt).
#
# Usage: .\deploy_phase12.ps1
# Skip BLE hardware scan: .\deploy_phase12.ps1 -SkipScan

param(
    [switch]$SkipScan
)

$ErrorActionPreference = "Stop"

# ── Read server connection from .server (gitignored) ──────────────────────────
$ServerFile = Join-Path $PSScriptRoot ".server"
if (-not (Test-Path $ServerFile)) {
    Write-Error @"
.server file not found. Create it once:

    echo "username@server-ip" > .server

Example: echo "user@192.168.1.x" > .server
"@
    exit 1
}

$ServerConnection = (Get-Content $ServerFile -Raw).Trim()
if ($ServerConnection -notmatch '^[a-zA-Z0-9_.-]+@[a-zA-Z0-9_.-]+$') {
    Write-Error ".server must contain exactly 'username@host'. Got: $ServerConnection"
    exit 1
}

$ServerDir = "~/victron-dashboard"
$ScanEnv   = if ($SkipScan) { "SKIP_SCAN=true " } else { "" }

Write-Host ""
Write-Host "Phase 12 deployment" -ForegroundColor Cyan -NoNewline
Write-Host " → push to GitHub → deploy to $ServerConnection → test" -ForegroundColor White
Write-Host ""

# ── 1. Push to GitHub ─────────────────────────────────────────────────────────
Write-Host "Pushing to GitHub..." -ForegroundColor Yellow
git push origin main
if (-not $?) {
    Write-Error "git push failed — check git remote and credentials"
    exit 1
}
Write-Host "Pushed." -ForegroundColor Green

# ── 2. SSH to server: pull + backup + rebuild + test + auto-rollback on fail ──
Write-Host ""
Write-Host "Connecting to $ServerConnection..." -ForegroundColor Yellow
$RemoteCmd = "cd $ServerDir && ${ScanEnv}bash test_phase12.sh 2>&1"

ssh $ServerConnection $RemoteCmd
$ExitCode = $LASTEXITCODE

Write-Host ""
if ($ExitCode -eq 0) {
    Write-Host "Phase 12 deployment PASSED" -ForegroundColor Green
} else {
    Write-Host "Phase 12 deployment FAILED" -ForegroundColor Red
    Write-Host "Rollback was applied automatically on the server." -ForegroundColor Yellow
    Write-Host "To investigate:"
    Write-Host "  ssh $ServerConnection"
    Write-Host "  cd $ServerDir"
    Write-Host "  docker compose logs ble-bridge --tail 50"
    Write-Host "  docker compose logs solar-api  --tail 50"
    Write-Host "  # If rollback didn't run: bash rollback_phase12.sh"
    exit 1
}
