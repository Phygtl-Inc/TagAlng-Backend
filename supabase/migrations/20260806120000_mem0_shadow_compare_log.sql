
-- mem0 SHADOW / A-B TRIAL · comparison ledger (see services/lana-worker/app/mem0_shadow.py).
-- Temporary evaluation substrate: lets us answer "is mem0 better than native recall?" with data
-- instead of opinion. Every turn (both arms) logs the native top-k and the mem0 top-k for the SAME
-- query, so an offline LLM-judge can score retrieval quality head-to-head. Drop this table when the
-- trial concludes (it holds no product data — pure telemetry, like lana_audit_log).
--
-- A/B design (mirrors mem0_shadow.py):
--   arm 'A' = control  -> native MemGPT recall injected into the prompt
--   arm 'B' = treatment -> mem0 recall injected instead
-- BOTH arms dual-write to mem0 and log here; only arm B changes what the LLM sees. Injecting both
-- sources at once is deliberately avoided — it would entangle them and make attribution impossible.

create table if not exists public.mem0_compare_log (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references public.users (id) on delete cascade,
  session_id uuid references public.lana_sessions (id) on delete set null,
  turn_id uuid references public.lana_messages (id) on delete set null,
  arm text not null check (arm in ('A', 'B')),
  query text not null,                               -- PII-scrubbed utterance (scrub_pii already applied)

  -- Retrieval snapshots for the same query. Shapes kept raw-ish for offline analysis:
  --   native_results: [{source_type, content, similarity, scope}, ...]
  --   mem0_results:   [{memory, score, categories}, ...]
  native_results jsonb not null default '[]'::jsonb,
  mem0_results jsonb not null default '[]'::jsonb,

  native_count int not null default 0,
  mem0_count int not null default 0,
  native_latency_ms int,
  mem0_latency_ms int,
  mem0_error text,                                   -- null on success; set on timeout/exception

  -- Offline LLM-judge verdict (filled later by mem0_shadow.judge_pending(); null until scored).
  judge_verdict text check (judge_verdict in ('native', 'mem0', 'tie', 'both_empty')),
  judge_reason text,
  judge_model text,
  judged_at timestamptz,

  created_at timestamptz not null default now(),
  constraint mem0_compare_log_native_is_array check (jsonb_typeof(native_results) = 'array'),
  constraint mem0_compare_log_mem0_is_array check (jsonb_typeof(mem0_results) = 'array')
);

comment on table public.mem0_compare_log is
  'mem0 A/B trial telemetry. One row per turn: native vs mem0 retrieval for the same query, plus an '
  'offline judge verdict. Temporary — drop when the trial concludes. Service-role only.';

-- The judge query scans for unscored rows; the readout scans by arm.
create index if not exists mem0_compare_log_unjudged_idx
  on public.mem0_compare_log (created_at)
  where judged_at is null;

create index if not exists mem0_compare_log_arm_idx
  on public.mem0_compare_log (arm, created_at desc);

alter table public.mem0_compare_log enable row level security;

-- Pure telemetry: service-role only, no client access (like lana_audit_log / recommendation_impressions).
create policy mem0_compare_log_no_client_access on public.mem0_compare_log
  for all to authenticated using (false) with check (false);
