# install-hooks.ps1 — Install the pre-commit MAC-address hook on Windows.
#
# Run once after cloning:  .\install-hooks.ps1
# The hook blocks `git commit` if staged files contain MAC-address patterns,
# preventing accidental commits of real device addresses from sites.json.

$ErrorActionPreference = "Stop"

$hookDir = Join-Path (git rev-parse --show-toplevel 2>$null) ".git\hooks"
if (-not $hookDir -or -not (Test-Path (Split-Path $hookDir -Parent))) {
    Write-Error "Not inside a git repository. Run this script from the project root."
    exit 1
}

New-Item -ItemType Directory -Path $hookDir -Force | Out-Null

$hookContent = @'
#!/bin/sh
# Block commits containing real MAC addresses (xx:xx:xx:xx:xx:xx pattern).
# Excludes known test/fixture paths where dummy MACs are expected.
MAC_PATTERN='[0-9a-fA-F]{2}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}'
EXCLUDE_PREFIXES="ble-bridge/fixtures/ ble-bridge/tests/ api/tests/"

staged=$(git diff --cached --name-only --diff-filter=ACMR 2>/dev/null)
[ -z "$staged" ] && exit 0

filtered=""
for f in $staged; do
    skip=0
    for excl in $EXCLUDE_PREFIXES; do
        case "$f" in $excl*) skip=1; break ;; esac
    done
    [ "$skip" -eq 0 ] && filtered="$filtered$f
"
done
[ -z "$filtered" ] && exit 0

if echo "$filtered" | tr -d ' ' | grep -v '^$' | xargs -d '\n' grep -lE "$MAC_PATTERN" 2>/dev/null | head -1 | grep -q .; then
    echo "" >&2
    echo "  ERROR: Staged file(s) contain MAC addresses." >&2
    echo "  Real device MACs must stay in config/sites.json (which is gitignored)." >&2
    echo "  Use 'git commit --no-verify' only if you are sure this is a test fixture." >&2
    echo "" >&2
    exit 1
fi
exit 0
'@

$hookPath = Join-Path $hookDir "pre-commit"
# Write with Unix line endings (LF) so Git on WSL/Linux reads it correctly
$hookContent -replace "`r`n", "`n" | Set-Content -Path $hookPath -Encoding utf8 -NoNewline

Write-Host "Pre-commit hook installed: $hookPath"
Write-Host "The hook will reject commits containing MAC-address patterns."
Write-Host "Test fixture MACs in ble-bridge/fixtures/, ble-bridge/tests/, api/tests/ are excluded."
