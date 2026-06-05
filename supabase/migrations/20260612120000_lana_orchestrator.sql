-- Lana orchestrator v0.1: inquiry capture, audit log, core_block on sessions

alter table public.lana_sessions
  add column if not exists core_block jsonb not null default '{}'::jsonb;

comment on column public.lana_sessions.core_block is
  'Always-in-context memory block (identity, block, session goal). Updated by orchestrator post-turn.';

alter table public.lana_sessions
  add constraint lana_sessions_core_block_is_object
  check (jsonb_typeof(core_block) = 'object');

create table if not exists public.inquiry_signals (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users (id) on delete cascade,
  block_id text references public.blocks (id) on delete set null,
  session_id uuid references public.lana_sessions (id) on delete set null,
  captured_at timestamptz not null default now(),
  category text not null,
  free_text text not null,
  embedding extensions.vector(768),
  urgency text not null default 'low',
  sentiment text not null default 'neutral',
  opt_in_followup boolean not null default false,
  source_module text not null default 'companionship',
  sensitive_flag boolean not null default false,
  cluster_id uuid,
  status text not null default 'open'
    check (status in ('open', 'reviewed', 'addressed', 'launched'))
);

comment on table public.inquiry_signals is
  'Out-of-scope and unmet-need signals from Lana capture_inquiry tool. Product roadmap input.';

create index if not exists inquiry_signals_user_captured_idx
  on public.inquiry_signals (user_id, captured_at desc);

create index if not exists inquiry_signals_block_category_idx
  on public.inquiry_signals (block_id, category);

create index if not exists inquiry_signals_embedding_idx
  on public.inquiry_signals
  using hnsw (embedding extensions.vector_cosine_ops);

alter table public.inquiry_signals enable row level security;

create policy "inquiry_signals_select_own"
  on public.inquiry_signals for select
  to authenticated
  using (user_id = auth.uid());

create policy "inquiry_signals_no_client_write"
  on public.inquiry_signals for all
  to authenticated
  using (false)
  with check (false);

create table if not exists public.lana_audit_log (
  id uuid primary key default gen_random_uuid(),
  session_id uuid references public.lana_sessions (id) on delete set null,
  user_id uuid references public.users (id) on delete set null,
  event_type text not null,
  module text,
  utterance_redacted text,
  response_redacted text,
  guardrail_result jsonb not null default '{}'::jsonb,
  routing jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  constraint lana_audit_guardrail_is_object check (jsonb_typeof(guardrail_result) = 'object'),
  constraint lana_audit_routing_is_object check (jsonb_typeof(routing) = 'object')
);

comment on table public.lana_audit_log is
  'Per-turn Lana routing, guardrails, captures. Service role writes only.';

create index if not exists lana_audit_log_session_created_idx
  on public.lana_audit_log (session_id, created_at desc);

alter table public.lana_audit_log enable row level security;

create policy "lana_audit_log_no_client_access"
  on public.lana_audit_log for all
  to authenticated
  using (false)
  with check (false);
