-- Rapport · Ring C ("By the way…") · the open-gap queue the ranker picks from.
-- Spec: LANA_RAPPORT_BACKEND_v1 §3.2, reconciled to this codebase.
--
-- Deviations from the spec's schema (intentional):
--   * `covers_concept` / `parent_bucket` replace the spec's `parent_facet` — they join the
--     REAL user_identity_claims contract (single-token `concept`, `bucket` enum), not the
--     doc's 7-domain slashed gap ids that the concept-format CHECK would reject.
--   * `unique (user_id, gap_id)` — one row per gap per user, status transitions in place —
--     replaces the spec's fragile `(user_id, gap_id, status)` triple key.
--   * No `capability_embedding` here: semantic suppression is a fast-follow; v1 suppresses on
--     exact concept / normalized label, app-side. No new provider, no 1536-dim vectors.

create table if not exists public.rapport_gaps (
  gap_row_id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users (id) on delete cascade,
  gap_id text not null,                                   -- joins app/rapport_gap_tree.py
  parent_bucket text not null,                            -- claim bucket → chip color / grouping
  covers_concept text not null,                           -- concept whose capture closes this gap
  why_frame text not null,                                -- tile copy, rendered from the triggering claim
  opened_at timestamptz not null default now(),
  opened_from_message_id uuid references public.lana_messages (id) on delete set null,
  unlock_score real not null default 0.5,
  asked_at timestamptz,
  answered_at timestamptz,
  answer_claim_id uuid references public.user_identity_claims (id) on delete set null,
  skipped_count int not null default 0,
  status text not null default 'open'
    check (status in ('open', 'asked', 'answered', 'skipped', 'muted_by_user', 'expired')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, gap_id)
);

comment on table public.rapport_gaps is
  'Rapport Ring C: per-user follow-up questions ("gaps") opened from captured identity claims. '
  'The ranker (app/rapport_ranker.py) picks the highest-scoring open gap for the home tile.';

create index if not exists rapport_gaps_user_status_score_idx
  on public.rapport_gaps (user_id, status, unlock_score desc);

create index if not exists rapport_gaps_user_opened_idx
  on public.rapport_gaps (user_id, opened_at desc);

alter table public.rapport_gaps enable row level security;

-- Owner may read her own gaps; all writes go through the service role (lana-worker).
create policy rapport_gaps_select_own on public.rapport_gaps
  for select to authenticated
  using (user_id = auth.uid());

create policy rapport_gaps_no_client_write on public.rapport_gaps
  for all to authenticated using (false) with check (false);

-- Skip → reopen, or expire after 3 skips (mirrors spec §8). Service role only.
create or replace function public.increment_skip_and_reopen(p_gap_row_id uuid)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  -- Keep asked_at so the 24h "one ask per day" cap still counts a skipped ask; the gap
  -- simply becomes eligible again (status='open') and the ranker re-scores it later.
  update public.rapport_gaps
     set skipped_count = skipped_count + 1,
         status = case when skipped_count + 1 >= 3 then 'expired' else 'open' end,
         updated_at = now()
   where gap_row_id = p_gap_row_id;
end;
$$;

comment on function public.increment_skip_and_reopen(uuid) is
  'Rapport: bump a gap''s skip count; reopen, or expire it once skipped 3×. Service role only.';

revoke all on function public.increment_skip_and_reopen(uuid) from public, anon, authenticated;
grant execute on function public.increment_skip_and_reopen(uuid) to service_role;
