-- In-session feedback (bug 2026-07-14 #5 — MUST HAVE): let a user flag an issue the
-- moment it happens, anchored to the exact session/message so triage replays the
-- context instead of guessing from a vague report later.
--
-- Writes go through the worker (service role) via POST /lana/feedback — it stamps the
-- turn's routing snapshot into context so every report carries its own repro state.

create table if not exists public.lana_feedback (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users (id) on delete cascade,
  session_id uuid references public.lana_sessions (id) on delete set null,
  message_id uuid references public.lana_messages (id) on delete set null,
  category text not null default 'other'
    check (category in ('bug', 'confusing', 'wrong_answer', 'idea', 'other')),
  body text not null check (char_length(body) between 1 and 2000),
  -- Worker-stamped repro context: last_routing, routing_phase, active_intent, lang, app build.
  context jsonb not null default '{}'::jsonb check (jsonb_typeof(context) = 'object'),
  created_at timestamptz not null default now(),
  resolved_at timestamptz
);

comment on table public.lana_feedback is
  'User-filed in-session feedback, anchored to the session/message where the issue happened. '
  'Written by lana-worker (POST /lana/feedback); context carries the turn routing snapshot.';

create index if not exists lana_feedback_created_idx
  on public.lana_feedback (created_at desc);
create index if not exists lana_feedback_user_idx
  on public.lana_feedback (user_id, created_at desc);

alter table public.lana_feedback enable row level security;

-- Owner may read their own reports; all writes go through the service role (lana-worker).
create policy lana_feedback_select_own on public.lana_feedback
  for select to authenticated
  using (user_id = auth.uid());

create policy lana_feedback_no_client_write on public.lana_feedback
  for all to authenticated using (false) with check (false);
