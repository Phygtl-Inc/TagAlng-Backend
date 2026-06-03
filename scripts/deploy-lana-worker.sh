#!/usr/bin/env bash
# Deploy lana-worker to Cloud Run (same GCP project as identity-worker).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/deploy/identity-worker.env}"
if [[ ! -f "$ENV_FILE" && -f "$ROOT/deploy/lana-worker.env" ]]; then
  ENV_FILE="$ROOT/deploy/lana-worker.env"
fi

PROJECT="${GCP_PROJECT:-silver-bridge-381702}"
REGION="${GCP_REGION:-us-east1}"
SERVICE="${CLOUD_RUN_SERVICE:-tagalng-lana-worker}"
SA="${GCP_RUN_SERVICE_ACCOUNT:-tagalng-identity-worker@${PROJECT}.iam.gserviceaccount.com}"
AR_REPO="${ARTIFACT_REGISTRY_REPO:-cloud-run-source-deploy}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
else
  echo "Missing env file. Use identity-worker secrets:"
  echo "  cp deploy/identity-worker.env.example deploy/identity-worker.env"
  echo "  # or: cp deploy/lana-worker.env.example deploy/lana-worker.env"
  exit 1
fi

required=(SUPABASE_URL SUPABASE_ANON_KEY SUPABASE_SERVICE_ROLE_KEY GCP_VERTEX_PROJECT)
for var in "${required[@]}"; do
  if [[ -z "${!var:-}" ]]; then
    echo "Set $var in $ENV_FILE"
    exit 1
  fi
done

VERTEX_LOCATION="${GCP_VERTEX_LOCATION:-us-central1}"
EXTRACT_MODEL="${VERTEX_EXTRACT_MODEL:-gemini-2.5-flash}"
LANA_MODEL="${VERTEX_LANA_MODEL:-$EXTRACT_MODEL}"
EMBED_MODEL="${VERTEX_EMBED_MODEL:-text-embedding-005}"
CORS="${CORS_ALLOW_ORIGINS:-*}"

# Retired on Vertex (e.g. gemini-2.0-flash-001 discontinued 2026-06-01). Override stale deploy/*.env.
case "$EXTRACT_MODEL" in
  gemini-2.0-flash-001|gemini-1.5-flash-002|gemini-1.5-flash-001)
    echo "WARN: $EXTRACT_MODEL is retired on Vertex — deploying with gemini-2.5-flash"
    echo "       Update VERTEX_EXTRACT_MODEL in $ENV_FILE to avoid this warning."
    EXTRACT_MODEL="gemini-2.5-flash"
    LANA_MODEL="gemini-2.5-flash"
    ;;
esac

echo "Project: $PROJECT  Region: $REGION  Service: $SERVICE"
echo "Vertex: $VERTEX_LOCATION / $LANA_MODEL"
echo "Vertex SA: $SA (shared with identity-worker)"

gcloud config set project "$PROJECT" >/dev/null

gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  aiplatform.googleapis.com \
  --project="$PROJECT" \
  --quiet

echo "Deploying from services/lana-worker ..."
gcloud run deploy "$SERVICE" \
  --source "$ROOT/services/lana-worker" \
  --project "$PROJECT" \
  --region "$REGION" \
  --platform managed \
  --quiet \
  --service-account "$SA" \
  --allow-unauthenticated \
  --memory 512Mi \
  --cpu 1 \
  --timeout 120 \
  --concurrency 20 \
  --min-instances 0 \
  --max-instances 10 \
  --set-env-vars "^@^SUPABASE_URL=${SUPABASE_URL}@SUPABASE_ANON_KEY=${SUPABASE_ANON_KEY}@SUPABASE_SERVICE_ROLE_KEY=${SUPABASE_SERVICE_ROLE_KEY}@GCP_VERTEX_PROJECT=${GCP_VERTEX_PROJECT}@GCP_VERTEX_LOCATION=${VERTEX_LOCATION}@VERTEX_EXTRACT_MODEL=${EXTRACT_MODEL}@VERTEX_LANA_MODEL=${LANA_MODEL}@VERTEX_EMBED_MODEL=${EMBED_MODEL}@CORS_ALLOW_ORIGINS=${CORS}"

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --project "$PROJECT" --format='value(status.url)')"
echo ""
echo "Deployed: $URL"
echo "Health:   $URL/health"
echo ""
echo "Postman / app: set lana_worker_url to $URL"
echo "Apply DB migration first if not done: supabase db push (20260603120000_lana_sessions_messages.sql)"
