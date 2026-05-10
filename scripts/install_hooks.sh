#!/bin/sh
# Install git hooks for the project.

HOOKS_DIR=$(git rev-parse --git-path hooks)
cp scripts/pre-commit "$HOOKS_DIR/pre-commit"
chmod +x "$HOOKS_DIR/pre-commit"

echo "Git hooks installed successfully."
