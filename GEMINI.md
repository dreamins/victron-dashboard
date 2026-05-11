# Gemini Project Instructions

## Working Style
* **Testing:** ALWAYS add new automated tests (unit or UI) with every code change. Every commit must be backed by new or updated tests.
* **No Manual Commands:** Never give the user a list of commands to run manually. Wrap operations in scripts so the user runs one thing and gets a PASS/FAIL result.
* **Physical Verification:** A task is not "complete" until verified against real system state (e.g., checking live API on the Linux machine). Tests are a sub-step, not the finish line.

## Architectural Constraints
* **Bucket Stitching:** The API (`/api/v1/history`) MUST stitch data from `victron` (1s), `victron_medium` (5m), and `victron_hourly` (1h) based on the requested range.
* **500-Point Ceiling:** API responses are capped at 500 points per series. Do not increase this; it is a performance and UX constraint for the dashboard.
* **Credentials:** Never hardcode production credentials. They must be referenced via environment variables or file mounts.

## Git Workflow
* **Commit Tagging:** When making commits to this repository, always include `[Gemini]` or `gemini:` at the start of the commit message. 
