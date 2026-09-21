-- When a gap was last skipped, so repetition can be rationed by time rather than by count.
--
-- increment_skip_and_reopen (20260806120000) already bumps skipped_count and sets the row
-- back to 'open' in the SAME statement, so a skipped question is re-servable on the very
-- next render. The only penalty is the third strike, which expires it. Between the first
-- skip and the third the user can be shown the same question they just dismissed, twice.
--
-- skipped_count cannot carry the window because it records how often, never when. A
-- timestamp can: the ranker holds a skipped gap back for 72h and a served one for 24h,
-- falling back to them only if the alternative is an empty tile.
alter table public.rapport_gaps
  add column if not exists skipped_at timestamptz;

comment on column public.rapport_gaps.skipped_at is
  'When this gap was last skipped. Powers the ranker''s 72h repetition window — skipped_count '
  'says how often, never when. Null for gaps skipped before 20261217120000.';

create index if not exists rapport_gaps_user_skipped_idx
  on public.rapport_gaps (user_id, skipped_at)
  where skipped_at is not null;

create or replace function public.increment_skip_and_reopen(p_gap_row_id uuid)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  -- Keep asked_at so the 24h "one ask per day" cap still counts a skipped ask; the gap
  -- simply becomes eligible again (status='open') and the ranker re-scores it later.
  -- skipped_at is what makes "later" mean later: without it the ranker cannot tell a gap
  -- dismissed a minute ago from one dismissed last week.
  update public.rapport_gaps
     set skipped_count = skipped_count + 1,
         status = case when skipped_count + 1 >= 3 then 'expired' else 'open' end,
         skipped_at = now(),
         updated_at = now()
   where gap_row_id = p_gap_row_id;
end;
$$;

comment on function public.increment_skip_and_reopen(uuid) is
  'Rapport: bump a gap''s skip count, stamp skipped_at; reopen, or expire it once skipped 3×. '
  'Service role only.';

revoke all on function public.increment_skip_and_reopen(uuid) from public, anon, authenticated;
grant execute on function public.increment_skip_and_reopen(uuid) to service_role;

-- ============================================================================
-- ROLLBACK
-- ----------------------------------------------------------------------------
-- Additive: the column is nullable and the function keeps its old signature and behaviour
-- apart from the new stamp. A ranker that does not read skipped_at is unaffected.
--
--   drop index if exists public.rapport_gaps_user_skipped_idx;
--   alter table public.rapport_gaps drop column if exists skipped_at;
--   -- then restore increment_skip_and_reopen from 20260806120000.
-- ============================================================================
