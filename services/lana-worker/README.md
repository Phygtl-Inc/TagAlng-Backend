# TagAlng lana-worker

Conversational **Lana** agent: profile intake + event draft host assist.

**v0.4** adds the agent orchestrator (Vertex Claude Haiku router + Sonnet synthesizer) per `docs/LANA_AGENT_ARCHITECTURE_v1.md`.

## Prerequisites

- User signed in (Supabase JWT)
- `assign_home_block` already called (`home_block_required` otherwise)
- `SUPABASE_*`, `GCP_VERTEX_PROJECT`
- **Orchestrator:** enable Claude Haiku/Sonnet in Vertex Model Garden; optional env:
  - `LANA_ORCHESTRATOR=auto` (default) | `legacy` (Gemini-only turns)
  - `VERTEX_CLAUDE_ROUTER_MODEL=claude-haiku-4-5@20251001`
  - `VERTEX_CLAUDE_SYNTH_MODEL=claude-sonnet-4-6`
  - `VERTEX_CLAUDE_REGION=us-east1`
- **Extract on complete:** `VERTEX_EXTRACT_MODEL=gemini-2.5-flash` (unchanged)

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/lana/sessions` | Start session; returns Lana opening message |
| `POST` | `/lana/sessions/{id}/messages` | User message → reply + routing metadata |
| `POST` | `/lana/sessions/{id}/complete` | Extract transcript → claims or event publish |
| `GET` | `/lana/sessions/{id}` | Resume UI (messages) |
| `GET` | `/health` | `orchestrator_enabled`, Claude model ids |

## Local run

```bash
cd services/lana-worker
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export SUPABASE_URL=... SUPABASE_ANON_KEY=... SUPABASE_SERVICE_ROLE_KEY=...
export GCP_VERTEX_PROJECT=...
export LANA_ORCHESTRATOR=auto
uvicorn app.main:app --reload --port 8081
```

## Deploy (Cloud Run)

```bash
gcloud auth login   # if token expired
./scripts/deploy-lana-worker.sh
```

## DB migrations

- `20260603120000_lana_sessions_messages.sql`
- `20260612120000_lana_orchestrator.sql` — `inquiry_signals`, `lana_audit_log`, `core_block`

Frontend can use RPCs `get_active_lana_session`, `get_lana_session_messages` for read-only resume.
