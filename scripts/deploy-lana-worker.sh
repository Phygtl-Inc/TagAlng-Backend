#!/usr/bin/env bash
# Deploy lana-worker to Cloud Run.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/deploy/lana-worker.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  if [[ -f "$ROOT/deploy/lana-worker.env.example" ]]; then
    cp "$ROOT/deploy/lana-worker.env.example" "$ENV_FILE"
    echo "Created $ENV_FILE from example."
    echo "Edit it: OPENAI_API_KEY, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY"
    echo "Then re-run: ./scripts/deploy-lana-worker.sh"
    exit 1
  fi
  echo "Missing $ENV_FILE — copy deploy/lana-worker.env.example to that path."
  exit 1
fi

PROJECT="${GCP_PROJECT:-silver-bridge-381702}"
REGION="${GCP_REGION:-us-east1}"
SERVICE="${CLOUD_RUN_SERVICE:-tagalng-lana-worker}"
SA="${GCP_RUN_SERVICE_ACCOUNT:-tagalng-identity-worker@${PROJECT}.iam.gserviceaccount.com}"
AR_REPO="${ARTIFACT_REGISTRY_REPO:-cloud-run-source-deploy}"

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

required=(SUPABASE_URL SUPABASE_ANON_KEY SUPABASE_SERVICE_ROLE_KEY)
for var in "${required[@]}"; do
  if [[ -z "${!var:-}" ]]; then
    echo "Set $var in $ENV_FILE"
    exit 1
  fi
done

LANA_LLM_PROVIDER="${LANA_LLM_PROVIDER:-gemini}"
if [[ "$LANA_LLM_PROVIDER" == "openai" || -n "${OPENAI_API_KEY:-}" ]]; then
  if [[ -z "${OPENAI_API_KEY:-}" ]]; then
    echo "Set OPENAI_API_KEY in $ENV_FILE (LANA_LLM_PROVIDER=openai)"
    exit 1
  fi
  LANA_LLM_PROVIDER=openai
elif [[ -z "${GCP_VERTEX_PROJECT:-}" ]]; then
  echo "Set GCP_VERTEX_PROJECT or OPENAI_API_KEY in $ENV_FILE"
  exit 1
fi

VERTEX_LOCATION="${GCP_VERTEX_LOCATION:-us-central1}"
EXTRACT_MODEL="${VERTEX_EXTRACT_MODEL:-gemini-2.5-flash}"
LANA_MODEL="${VERTEX_LANA_MODEL:-$EXTRACT_MODEL}"
EMBED_MODEL="${VERTEX_EMBED_MODEL:-text-embedding-005}"
CORS="${CORS_ALLOW_ORIGINS:-*}"
LANA_ORCHESTRATOR="${LANA_ORCHESTRATOR:-auto}"
OPENAI_ROUTER="${OPENAI_ROUTER_MODEL:-gpt-4o-mini}"
OPENAI_SYNTH="${OPENAI_SYNTH_MODEL:-gpt-4o}"
LANA_ROUTER="${VERTEX_LANA_ROUTER_MODEL:-gemini-2.5-flash}"
LANA_SYNTH="${VERTEX_LANA_SYNTH_MODEL:-gemini-2.5-pro}"

# Retired on Vertex
case "$EXTRACT_MODEL" in
  gemini-2.0-flash-001|gemini-1.5-flash-002|gemini-1.5-flash-001)
    echo "WARN: $EXTRACT_MODEL is retired on Vertex — deploying with gemini-2.5-flash"
    echo "       Update VERTEX_EXTRACT_MODEL in $ENV_FILE to avoid this warning."
    EXTRACT_MODEL="gemini-2.5-flash"
    LANA_MODEL="gemini-2.5-flash"
    ;;
esac

echo "Project: $PROJECT  Region: $REGION  Service: $SERVICE"
if [[ "$LANA_LLM_PROVIDER" == "openai" ]]; then
  echo "LLM: openai router=$OPENAI_ROUTER synth=$OPENAI_SYNTH"
else
  echo "Vertex: $VERTEX_LOCATION / extract=$EXTRACT_MODEL legacy_lana=$LANA_MODEL"
  echo "Orchestrator: $LANA_ORCHESTRATOR · LLM $LANA_LLM_PROVIDER router=$LANA_ROUTER synth=$LANA_SYNTH"
fi
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
# Use env-vars-file — Claude model ids contain '@' which breaks --set-env-vars ^@^ delimiter.
ENV_VARS_FILE="$(mktemp)"
trap 'rm -f "$ENV_VARS_FILE"' EXIT
cat >"$ENV_VARS_FILE" <<EOF
SUPABASE_URL: "${SUPABASE_URL}"
SUPABASE_ANON_KEY: "${SUPABASE_ANON_KEY}"
SUPABASE_SERVICE_ROLE_KEY: "${SUPABASE_SERVICE_ROLE_KEY}"
GCP_VERTEX_PROJECT: "${GCP_VERTEX_PROJECT:-}"
GCP_VERTEX_LOCATION: "${VERTEX_LOCATION}"
VERTEX_EXTRACT_MODEL: "${EXTRACT_MODEL}"
VERTEX_LANA_MODEL: "${LANA_MODEL}"
VERTEX_EMBED_MODEL: "${EMBED_MODEL}"
LANA_ORCHESTRATOR: "${LANA_ORCHESTRATOR}"
LANA_LATENT_EXTRACT: "${LANA_LATENT_EXTRACT:-0}"
LANA_REC_PERSONALIZE: "${LANA_REC_PERSONALIZE:-0}"
LANA_RAPPORT_MIN_HOURS: "${LANA_RAPPORT_MIN_HOURS:-0}"
LANA_RAPPORT_MAX_PER_7D: "${LANA_RAPPORT_MAX_PER_7D:-0}"
LANA_LLM_PROVIDER: "${LANA_LLM_PROVIDER}"
VERTEX_LANA_ROUTER_MODEL: "${LANA_ROUTER}"
VERTEX_LANA_SYNTH_MODEL: "${LANA_SYNTH}"
OPENAI_API_KEY: "${OPENAI_API_KEY:-}"
OPENAI_ROUTER_MODEL: "${OPENAI_ROUTER}"
OPENAI_SYNTH_MODEL: "${OPENAI_SYNTH}"
CORS_ALLOW_ORIGINS: "${CORS}"
GOOGLE_MAPS_API_KEY: "${GOOGLE_MAPS_API_KEY:-}"
AMPLITUDE_API_KEY: "${AMPLITUDE_API_KEY:-}"
VAPID_PUBLIC_KEY: "${VAPID_PUBLIC_KEY:-}"
VAPID_PRIVATE_KEY: "${VAPID_PRIVATE_KEY:-}"
VAPID_SUBJECT: "${VAPID_SUBJECT:-}"
RESEND_API_KEY: "${RESEND_API_KEY:-}"
RESEND_FROM: "${RESEND_FROM:-}"
APP_BASE_URL: "${APP_BASE_URL:-}"
EOF

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
  --env-vars-file "$ENV_VARS_FILE"

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --project "$PROJECT" --format='value(status.url)')"
echo ""
echo "Deployed: $URL"
echo "Health:   $URL/health"
echo ""
echo "Postman / app: set lana_worker_url to $URL"
echo "Apply DB migrations if not done: supabase db push (lana_sessions + lana_orchestrator)"
