#!/usr/bin/env bash
# Run lana-worker locally — secrets from deploy/lana-worker.env only.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/deploy/lana-worker.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  if [[ -f "$ROOT/deploy/lana-worker.env.example" ]]; then
    cp "$ROOT/deploy/lana-worker.env.example" "$ENV_FILE"
    echo "Created $ENV_FILE from example."
    echo "Edit it: OPENAI_API_KEY, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY"
    echo "Then re-run: ./scripts/run-lana-worker-local.sh"
    exit 1
  fi
  echo "Missing $ENV_FILE — copy deploy/lana-worker.env.example to that path."
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

if [[ -z "${OPENAI_API_KEY:-}" && -z "${GCP_VERTEX_PROJECT:-}" ]]; then
  echo "Set OPENAI_API_KEY (or GCP_VERTEX_PROJECT) in $ENV_FILE"
  exit 1
fi

for var in SUPABASE_URL SUPABASE_ANON_KEY SUPABASE_SERVICE_ROLE_KEY; do
  if [[ -z "${!var:-}" ]]; then
    echo "Set $var in $ENV_FILE"
    exit 1
  fi
done

cd "$ROOT/services/lana-worker"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

echo "Env: $ENV_FILE"
echo "LLM provider: ${LANA_LLM_PROVIDER:-auto}"
echo "Health: http://127.0.0.1:${PORT:-8081}/health"
exec env PYTHONPATH=. .venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port "${PORT:-8081}"
