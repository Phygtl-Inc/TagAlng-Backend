# TagAlng identity-worker

`POST /identity/extract` — cover text → **Vertex AI (Gemini Flash)** claims → **text-embedding-005** vectors → `user_identity_claims`.

No keyword/rules extraction. Requires Google Cloud + Vertex.

## Env (required)

| Variable | Notes |
|----------|--------|
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_ANON_KEY` | Validates user JWT |
| `SUPABASE_SERVICE_ROLE_KEY` | Writes claims |
| `GCP_VERTEX_PROJECT` | GCP project with Vertex AI enabled |
| `GCP_VERTEX_LOCATION` | e.g. `us-east1` |
| `GOOGLE_APPLICATION_CREDENTIALS` | Service account JSON path (local) |

Optional:

| Variable | Default |
|----------|---------|
| `VERTEX_EXTRACT_MODEL` | `gemini-2.0-flash-001` |
| `VERTEX_EMBED_MODEL` | `text-embedding-005` |

Service account role: **Vertex AI User**.

## Run locally

```bash
cd services/identity-worker
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export SUPABASE_URL=https://rjlcyvwogmfmngemhbmn.supabase.co
export SUPABASE_ANON_KEY=...
export SUPABASE_SERVICE_ROLE_KEY=...
export GCP_VERTEX_PROJECT=your-project
export GCP_VERTEX_LOCATION=us-east1
export GOOGLE_APPLICATION_CREDENTIALS=~/keys/tagalng-vertex.json

uvicorn app.main:app --reload --port 8080
```

## Request

```bash
curl -s -X POST http://localhost:8080/identity/extract \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"cover_text":"Sicilian-American mom of two toddlers. New to Lake Nona. Catholic family, Sunday Mass."}'
```

User must have `home_block_id` set first.

## Read claims

```http
POST /rest/v1/rpc/get_my_identity_claims
Authorization: Bearer {access_token}
```

## Deploy (Cloud Run)

From repo root:

```bash
cp deploy/identity-worker.env.example deploy/identity-worker.env
# fill Supabase keys
./scripts/deploy-identity-worker.sh
```

Cloud Run URL (dev): set in the mobile app / Postman as `identity_worker_url`. Requires Vertex AI on `GCP_VERTEX_PROJECT` and runtime SA **Vertex AI User**.
