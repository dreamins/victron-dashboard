# Install git hooks for the project (PowerShell version)

$hooksDir = git rev-parse --git-path hooks
Copy-Item "scripts/pre-commit" "$hooksDir/pre-commit"

Write-Host "Git hooks installed successfully."
