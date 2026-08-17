#!/usr/bin/env bash
# Production deploy of shade-engine (push-to-deploy via GitHub Actions).
# Triggered by the restricted SSH key (forced command= in authorized_keys).
# Source of truth: deploy/deploy.sh in the repo; installed on the server as
# /usr/local/bin/deploy_shade during provisioning:
#   sudo install -m 755 /opt/shade/deploy/deploy.sh /usr/local/bin/deploy_shade
set -euo pipefail

APP_DIR=/opt/shade
COMPOSE="docker compose -f compose.yml"

log() { echo "[deploy_shade $(date -Iseconds)] $*"; }

cd "$APP_DIR"

log "fetch + reset -> origin/main"
git fetch origin main
git reset --hard origin/main

# Bootstrap only, never a sync. The configs this machine serves live in
# live/cities/ and belong to `shade-engine publish` (ADR-025); a deploy has no
# opinion about which cities are up. Seeding an empty directory is what makes a
# fresh machine work at all -- but copying over a populated one would revert a
# published config under artifacts that stayed new, silently, which is the exact
# bug this directory was split out to prevent.
if [ -z "$(ls -A live/cities 2>/dev/null)" ]; then
  log "bootstrap live/cities from the repo (first deploy on this machine)"
  mkdir -p live/cities
  cp -a cities/. live/cities/
fi

log "build image"
$COMPOSE build api

log "run migrations (blocking)"
$COMPOSE run --rm migrate

log "up api"
$COMPOSE up -d api

log "local smoke"
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS -o /dev/null http://127.0.0.1:8003/healthz; then
    log "api healthy on attempt $i"
    exit 0
  fi
  sleep 3
done

log "ERROR: api did not answer in 30s"
$COMPOSE logs --tail=80 api
exit 1
