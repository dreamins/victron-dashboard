# Phase 7 Work State — Interrupted

## What was done (local Windows repo — committed to git)

Commit: c1bc751 "feat(phase7): multi-site foundation"

### Files changed locally (all committed):
- `.gitignore` — added `config/sites.json`
- `config/sites.json.example` — new schema file (safe)
- `decoder/decoder.py` — supports sites.json + legacy devices.json, wildcard MQTT topics, site tag on writes
- `decoder/fixtures/sites_fixture.json` — new test fixture (safe)
- `decoder/replay.py` — added --site arg, publishes to victron/{site}/raw
- `api/main.py` — added /api/v1/sites endpoint, optional ?site= filter on all endpoints
- `api/seed_test_data.py` — adds site=SITE tag to all seeded points
- `api/tests/test_api.py` — 13 new Phase 7 tests added (total ~35)
- `api/tests/sites_fixture.json` — new test fixture for API container (safe)
- `docker-compose.yml` — DANGER: removed `ports: - "8080:8080"` from solar-api (broke port for prod)
- `docker-compose.test.yml` — updated to use sites_fixture.json, added SITES_FILE env var
- `esp32/victron-bridge.yaml` — parameterized MQTT topics with ${site_id}
- `esp32/secrets.yaml.example` — added site_id field
- `esp32/secrets.yaml` — added `site_id: "home"` (gitignored, local only)
- `migrate_site_tags.py` — migration script (not to be run until Phase 7.5)

## Critical problem in docker-compose.yml (LOCAL + LINUX)

The `ports: - "8080:8080"` line was REMOVED from solar-api in docker-compose.yml.
This must be RESTORED before anything is deployed to production.
Old line to restore:
```yaml
  solar-api:
    build: ./api
    depends_on:
      - influxdb
    ports:
      - "8080:8080"
    environment:
```

## What was copied to Linux (~/victron-dashboard/) — NOT yet restarted

- decoder/decoder.py (new version)
- decoder/replay.py (new version)
- decoder/fixtures/sites_fixture.json (new, harmless)
- api/main.py (new version)
- api/seed_test_data.py (new version)
- api/tests/test_api.py (new version)
- api/tests/sites_fixture.json (new, harmless)
- config/sites.json.example (new, harmless)
- docker-compose.yml (DANGER: missing ports line — NOT yet applied to containers)
- docker-compose.test.yml (new version)
- esp32/victron-bridge.yaml (new version)
- esp32/secrets.yaml.example (new version)
- migrate_site_tags.py (new, harmless)
- multi-site-design.md (new, harmless)

## Linux production containers — UNTOUCHED, still running old code

Production containers were NOT restarted. They are running images built before Phase 7.
The file changes on disk do NOT affect running containers until `docker compose up` is run.

## What was also done on Linux (non-file changes)

- `firmware-atheros` installed and held — KEEP THIS, it's what makes Bluetooth work
- `config/sites.json` CREATED from devices.json — this is NEW and gitignored.
  It is needed for Phase 7 production deployment. DO NOT delete it.
  It contains real MACs/keys, stays gitignored.
- `docker compose build ble-decoder solar-api` was run — built new images but did NOT
  deploy them. Production containers still running old images.

## Linux restore checklist

1. Restore docker-compose.yml (add back `ports: - "8080:8080"` for solar-api)
   - Option A: scp the fixed version from Windows after fixing it locally
   - Option B: edit directly on Linux with sed
2. All other file changes on Linux are harmless (containers not restarted)
3. config/sites.json on Linux — KEEP IT (valid, needed, gitignored)
4. firmware-atheros — KEEP IT

## Next steps (DO BEFORE TOUCHING LINUX AGAIN)

1. Fix docker-compose.yml locally (restore ports line) — DONE BELOW
2. Run tests LOCALLY using Docker Desktop on Windows
3. Only when all tests pass locally, then sync to Linux and restart prod

## How to run tests locally

The test script is test_phase4.sh. On Windows it requires WSL or Git Bash.
Alternative: run docker compose commands directly.

```bash
# From D:\projects\victron in Git Bash / WSL:
MQTT_BIND_IP=127.0.0.1 docker compose --project-name victron-test \
  -f docker-compose.yml -f docker-compose.test.yml \
  up -d --build influxdb solar-api
# wait for health, seed data, run pytest, cleanup
```

Or just run the shell script in Git Bash:
```bash
bash test_phase4.sh
```

## Port conflict root cause (for when tests run)

Base docker-compose.yml has `ports: ["8080:8080"]` for solar-api.
Test docker-compose.test.yml ADDS (not replaces) `ports: ["8081:8080"]`.
Docker Compose merges lists → tries to bind BOTH 8080 AND 8081.
On Linux this conflicts with the running production solar-api on 8080.

FIX (must implement before running tests):
Move ports out of base docker-compose.yml into docker-compose.test.yml only.
Production access to solar-api goes through nginx (container network), not host port.
