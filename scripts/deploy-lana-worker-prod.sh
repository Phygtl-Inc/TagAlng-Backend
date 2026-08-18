#!/usr/bin/env bash
# Deploy lana-worker to Cloud Run against the PROD Supabase project (tagalng-prod).
# Thin wrapper over deploy-lana-worker.sh — same build, different env + service name.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/deploy/lana-worker-prod.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE — copy deploy/lana-worker.env and swap in the tagalng-prod"
  echo "SUPABASE_URL / SUPABASE_ANON_KEY / SUPABASE_SERVICE_ROLE_KEY."
  exit 1
fi

if grep -q "<PROD_" "$ENV_FILE"; then
  echo "$ENV_FILE still has <PROD_...> placeholders."
  echo "Paste the real values from tagalng-prod → Project Settings → API, then re-run."
  exit 1
fi

export ENV_FILE
export CLOUD_RUN_SERVICE="${CLOUD_RUN_SERVICE:-tagalng-lana-worker-prod}"

# Real users open this one. Keep an instance warm: at min-instances 0 the service scaled
# to zero after a quiet spell and whoever opened the app next waited through a container
# boot (Python + supabase/openai/vertex imports) before their first query even ran —
# which is why prod felt slower than a laptop hitting the same database (2026-08-18).
# It also keeps post-response background writes (place blurbs) alive long enough to land.
# Dev stays at 0: nothing there is worth paying for an idle instance.
export MIN_INSTANCES="${MIN_INSTANCES:-1}"
export CPU_BOOST="${CPU_BOOST:-1}"

exec "$ROOT/scripts/deploy-lana-worker.sh"
