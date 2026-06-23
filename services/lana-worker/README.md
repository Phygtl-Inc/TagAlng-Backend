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

## Frontend contract changes (latest)

Handoff notes for the PWA/mobile dev. These change the `POST /lana/sessions/{id}/messages`
response contract and post-publish behaviour.

### 1. New `ui_intent: "collect_email"` (identifier step)

The app is email-auth, but the worker used to emit the phone-era token `collect_phone`
for the email step (and `routing_phase` still says `await_*_phone`). It now emits
**`collect_email`** for the identifier step:

| `routing_phase` | old `ui_intent` | new `ui_intent` |
|-----------------|-----------------|-----------------|
| `await_signup_phone` | `collect_phone` | **`collect_email`** |
| `await_login_phone`  | `collect_phone` | **`collect_email`** |
| `gate_verify`        | `collect_phone` | **`collect_email`** |

- FE: render the **email input** for `ui_intent === "collect_email"`.
- `collect_phone` is **deprecated but still valid** — keep treating it as the email step
  for back-compat (older sessions / cached turns). `routing_phase` stays `await_*_phone`
  (legacy naming, unchanged) so existing `routing_phase` fallbacks still work.
- OTP step is unchanged: `ui_intent: "collect_otp"`.

### 2. `event_created` no longer returns server `ui_actions`

Publishing an event used to return `ui_actions` pills (`"Send to a mom"` / `"Maybe later"`).
Those are **removed** (`ui_actions: []` on `event_created` turns) because the post-publish
CTAs need to *navigate* / open the *share sheet*, which a message-sending `ui_action`
can't do. The FE renders these natively instead:

- **Open the meet up** → navigate to `/meet/{event_id}`
- **Share with a mom** → native share / clipboard of `/meet/{event_id}`

`event_id` is on the turn when `ui_intent === "event_created"`.

### 3. Event publish can now fail loudly

`_auto_publish_event` no longer fakes success. If `create_event` is rejected, the turn
comes back with `event_id: null` and an honest `assistant_message` (e.g. "verify your
email first" / "pick a place I can map") instead of an "all set!" line. Host mode stays
active so a retry republishes. No new fields — just don't assume `event_created` implies
a real `event_id`; gate the completion card on `event_id` being present.

### FE files already updated to match (for review/ownership)

I made the matching FE edits in `tagalng-pwa-main`; review or take them over:
- `src/lib/lana.ts` — added `'collect_email'` to `LanaUiIntent`.
- `src/features/voice/components/lana-conversation.tsx` — `authStageOf` maps `collect_email`
  (and legacy `collect_phone`) → email stage.
- `src/features/voice/components/event-draft-card.tsx` — exported `EventCreatedActions`
  (Open the meet up / Share with a mom); removed the CTAs from inside the receipt card.
- `src/features/voice/components/thought-chat.tsx` — renders `EventCreatedActions` in the
  action-pill row on `event_created` turns.

### Related DB migration (must be applied)

`supabase/migrations/20260717120000_email_verify_unlocks_gates.sql` — email verification
now satisfies the legacy `phone_verified_at` gates, so an email-verified host can actually
create events (previously `create_event` returned 403). Run `supabase db push`.

## Env reference

| Variable | Required (OpenAI) | Description |
|----------|-------------------|-------------|
| `OPENAI_API_KEY` | yes | Server-only secret |
| `LANA_LLM_PROVIDER` | yes | Set `openai` |
| `OPENAI_ROUTER_MODEL` | no | Default `gpt-4o-mini` |
| `OPENAI_SYNTH_MODEL` | no | Default `gpt-4o` |
| `LANA_ORCHESTRATOR` | no | `auto` (default) |
| `SUPABASE_*` | yes | Same as always |
