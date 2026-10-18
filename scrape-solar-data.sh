#!/bin/bash
# Keep one local Enphase scraper running.
# Credentials come from secrets.env (not committed). The Envoy JWT is cached in
# secrets/envoy-token.json and refreshed before it expires.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p secrets
exec 9>secrets/scrape.lock
if ! flock -n 9; then
  echo "$(date '+%F %T') scraper already running" >&2
  exit 0
fi
exec python3 -u scrape_local.py "$@"
