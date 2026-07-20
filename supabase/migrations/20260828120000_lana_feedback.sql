-- Thumbs up/down on Lana output, for the team to review and learn from.
--
-- Two rateable things: an assistant chat reply (a lana_messages row) and a rapport
-- "By the way…" question (a rapport_gaps row). One row per (user, target) — tapping the
-- other thumb flips the rating in place, tapping the same thumb again clears it (the
-- worker deletes the row). content_snapshot copies the rated text at rating time so the
-- team reads feedback without joins and it survives the target being deleted — which is
-- also why the FKs are `set null`, not cascade: the feedback outlives the message.
-- Service-role only: the worker writes; no anon/authenticated access.
create table if not exists public.lana_feedback (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users(id) on delete cascade,
  target_kind text not null check (target_kind in ('message', 'rapport_question')),
  message_id uuid references public.lana_messages(id) on delete set null,
  gap_row_id uuid references public.rapport_gaps(gap_row_id) on delete set null,
  rating text not null check (rating in ('up', 'down')),
  content_snapshot text,
  context jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint lana_feedback_context_is_object check (jsonb_typeof(context) = 'object')
);

-- Toggle semantics need "the row for this user+target" to be unique. Partial (not a
-- composite unique on both columns) because exactly one of the two ids is set per row.
create unique index if not exists lana_feedback_user_message_uq
  on public.lana_feedback (user_id, message_id) where message_id is not null;
create unique index if not exists lana_feedback_user_gap_uq
  on public.lana_feedback (user_id, gap_row_id) where gap_row_id is not null;

-- Team review reads newest-first, optionally filtered to one thumb.
create index if not exists lana_feedback_created_idx
  on public.lana_feedback (created_at desc);
create index if not exists lana_feedback_rating_created_idx
  on public.lana_feedback (rating, created_at desc);

comment on table public.lana_feedback is
  'Thumbs up/down a user gave a Lana reply (lana_messages) or a rapport tile question (rapport_gaps). One row per user+target; cleared ratings are deleted.';

alter table public.lana_feedback enable row level security;
-- No policies: RLS denies anon/authenticated by default; service_role bypasses RLS.
