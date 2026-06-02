# TagAlng lana-worker

Conversational **Lana** agent for signup profile intake: product context + user/block context + chat history → Vertex Gemini → `user_identity_claims`.

## Prerequisites

- User signed in (Supabase JWT)
- `assign_home_block` already called (`home_block_required` otherwise)
- Same env as identity-worker: `SUPABASE_*`, `GCP_VERTEX_PROJECT`, optional `VERTEX_LANA_MODEL`

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/lana/sessions` | Start session; returns Lana opening message |
| `POST` | `/lana/sessions/{id}/messages` | User message → warm reply + `continue` or `ready_to_complete` |
| `POST` | `/lana/sessions/{id}/complete` | Extract transcript → claims + embeddings |
| `GET` | `/lana/sessions/{id}` | Resume UI (messages) |

## Local run

```bash
cd services/lana-worker
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export SUPABASE_URL=... SUPABASE_ANON_KEY=... SUPABASE_SERVICE_ROLE_KEY=...
export GCP_VERTEX_PROJECT=...
uvicorn app.main:app --reload --port 8081
```

## Deploy (Cloud Run)

From repo root (reuses `deploy/identity-worker.env`):

```bash
gcloud auth login   # if token expired
./scripts/deploy-lana-worker.sh
```

Uses the same GCP project, region, and service account as identity-worker (`tagalng-identity-worker@...`).

## DB

Apply migration `20260603120000_lana_sessions_messages.sql`.

Frontend can also use RPCs `get_active_lana_session`, `get_lana_session_messages` for read-only resume.
