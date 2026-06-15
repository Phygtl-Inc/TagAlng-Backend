# TagAlng lana-worker

Conversational **Lana** agent: unified chat, discovery routing, orchestrator tools.

## LLM providers

| Provider | Env | Router | Synthesizer |
|----------|-----|--------|-------------|
| **OpenAI** (ATPR default) | `LANA_LLM_PROVIDER=openai` + `OPENAI_API_KEY` | `gpt-4o-mini` | `gpt-4o` |
| Vertex Gemini | `LANA_LLM_PROVIDER=gemini` + `GCP_VERTEX_PROJECT` | `gemini-2.5-flash` | `gemini-2.5-pro` |
| Vertex Claude | `LANA_LLM_PROVIDER=claude` + `GCP_VERTEX_PROJECT` | Haiku | Sonnet |

Discovery slots, orchestrator router, and synthesizer all call `app/orchestrator/llm.py` — one provider switch.

Profile/event **complete** extract still uses Vertex (`VERTEX_EXTRACT_MODEL`) unless migrated.

## Where to put your OpenAI key

**Do not commit the key.** Add it here:

```bash
cp deploy/lana-worker.env.example deploy/lana-worker.env
```

Edit **`deploy/lana-worker.env`** (gitignored):

```bash
LANA_LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...          # ← your key here
OPENAI_ROUTER_MODEL=gpt-4o-mini
OPENAI_SYNTH_MODEL=gpt-4o

SUPABASE_URL=https://rjlcyvwogmfmngemhbmn.supabase.co
SUPABASE_ANON_KEY=...
SUPABASE_SERVICE_ROLE_KEY=...
```

Cloud Run deploy reads the same file via `./scripts/deploy-lana-worker.sh`.

## Local run

```bash
chmod +x scripts/run-lana-worker-local.sh
./scripts/run-lana-worker-local.sh
```

Verify:

```bash
curl -s http://127.0.0.1:8081/health | python3 -m json.tool
```

Expect `"llm_provider": "openai"`, `"openai_configured": true`, `"router_model": "gpt-4o-mini"`.

## Deploy (Cloud Run)

```bash
gcloud auth login
./scripts/deploy-lana-worker.sh
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/lana/sessions` | Start session |
| `POST` | `/lana/sessions/{id}/messages` | User message → reply + `ui_intent` |
| `POST` | `/lana/sessions/{id}/complete` | Extract transcript → claims or event |
| `GET` | `/lana/sessions/{id}` | Resume UI |
| `GET` | `/lana/users/{id}/block-log` | Block log (RADAR tab) |
| `GET` | `/health` | LLM provider + model ids |

## Env reference

| Variable | Required (OpenAI) | Description |
|----------|-------------------|-------------|
| `OPENAI_API_KEY` | yes | Server-only secret |
| `LANA_LLM_PROVIDER` | yes | Set `openai` |
| `OPENAI_ROUTER_MODEL` | no | Default `gpt-4o-mini` |
| `OPENAI_SYNTH_MODEL` | no | Default `gpt-4o` |
| `LANA_ORCHESTRATOR` | no | `auto` (default) |
| `SUPABASE_*` | yes | Same as always |
