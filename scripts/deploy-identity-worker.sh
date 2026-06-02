#!/usr/bin/env bash
# Deploy identity-worker to Cloud Run (silver-bridge-381702 by default).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/deploy/identity-worker.env}"

PROJECT="${GCP_PROJECT:-silver-bridge-381702}"
REGION="${GCP_REGION:-us-east1}"
SERVICE="${CLOUD_RUN_SERVICE:-tagalng-identity-worker}"
SA="${GCP_RUN_SERVICE_ACCOUNT:-tagalng-identity-worker@${PROJECT}.iam.gserviceaccount.com}"
AR_REPO="${ARTIFACT_REGISTRY_REPO:-cloud-run-source-deploy}"

_die_ar_permission() {
  local acct
  acct="$(gcloud config get-value account 2>/dev/null || echo 'your-account')"
  echo ""
  echo "PERMISSION_DENIED: cannot create Artifact Registry repo in $PROJECT ($REGION)."
  echo "Logged in as: $acct"
  echo ""
  echo "Ask a project Owner to do ONE of:"
  echo "  A) IAM → $acct → add role **Artifact Registry Administrator**"
  echo "     (or broader: **Cloud Run Admin** + **Artifact Registry Administrator**)"
  echo "  B) Create the repo once (then rerun this script):"
  echo "     gcloud artifacts repositories create $AR_REPO \\"
  echo "       --repository-format=docker --location=$REGION --project=$PROJECT"
  echo ""
  exit 1
}

_ensure_artifact_registry() {
  if gcloud artifacts repositories describe "$AR_REPO" \
    --location="$REGION" --project="$PROJECT" &>/dev/null; then
    echo "Artifact Registry repo exists: $AR_REPO ($REGION)"
    return 0
  fi
  echo "Creating Artifact Registry repo: $AR_REPO ($REGION) ..."
  if ! gcloud artifacts repositories create "$AR_REPO" \
    --repository-format=docker \
    --location="$REGION" \
    --project="$PROJECT" \
    --description="Cloud Run source deploys for TagAlng"; then
    _die_ar_permission
  fi
}

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
else
  echo "Missing $ENV_FILE"
  echo "  cp deploy/identity-worker.env.example deploy/identity-worker.env"
  echo "  # fill Supabase keys from Dashboard → Project Settings → API"
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
EMBED_MODEL="${VERTEX_EMBED_MODEL:-text-embedding-005}"
CORS="${CORS_ALLOW_ORIGINS:-*}"

case "$EXTRACT_MODEL" in
  gemini-2.0-flash-001|gemini-1.5-flash-002|gemini-1.5-flash-001)
    echo "WARN: $EXTRACT_MODEL is retired on Vertex — deploying with gemini-2.5-flash"
    echo "       Update VERTEX_EXTRACT_MODEL in $ENV_FILE"
    EXTRACT_MODEL="gemini-2.5-flash"
    ;;
esac

echo "Project: $PROJECT  Region: $REGION  Service: $SERVICE"
echo "Vertex: $VERTEX_LOCATION / $EXTRACT_MODEL"
echo "Vertex SA: $SA"

gcloud config set project "$PROJECT" >/dev/null

echo "Enabling APIs (idempotent)..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  aiplatform.googleapis.com \
  --project="$PROJECT"

_ensure_artifact_registry

echo "Deploying from services/identity-worker ..."
gcloud run deploy "$SERVICE" \
  --source "$ROOT/services/identity-worker" \
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
  --set-env-vars "^@^SUPABASE_URL=${SUPABASE_URL}@SUPABASE_ANON_KEY=${SUPABASE_ANON_KEY}@SUPABASE_SERVICE_ROLE_KEY=${SUPABASE_SERVICE_ROLE_KEY}@GCP_VERTEX_PROJECT=${GCP_VERTEX_PROJECT}@GCP_VERTEX_LOCATION=${VERTEX_LOCATION}@VERTEX_EXTRACT_MODEL=${EXTRACT_MODEL}@VERTEX_EMBED_MODEL=${EMBED_MODEL}@CORS_ALLOW_ORIGINS=${CORS}"

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --project "$PROJECT" --format='value(status.url)')"
echo ""
echo "Deployed: $URL"
echo "Health:   $URL/health"
echo ""
echo "Postman: set identity_worker_url to $URL in TagAlng-tagalng-dev environment."
