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

exec "$ROOT/scripts/deploy-lana-worker.sh"
