-- ============================================================================
-- event_allows_attendee_share: NULL -> false for non-open events
--
-- PROBLEM
--   20260718120000_event_join_enforcement.sql defined it as
--       select coalesce(allow_attendee_share, false)
--       from public.events
--       where id = p_event_id and status = 'open';
--   A `language sql` scalar select that matches NO ROW returns NULL, not the
--   coalesce default -- the coalesce only guards a null COLUMN on a row that
--   matched. So a cancelled event, a completed event, or a bad uuid all answer
--   NULL where the contract (and the FE handoff doc) promise false.
--
--   SQL callers would be safe by accident: NULL is not true, so a policy USING
--   clause fails closed. A JS caller writing `if (allowed !== false)` gets
--   `null !== false` -> true -> the Share affordance appears on a cancelled meet.
--
-- BLAST RADIUS TODAY: none. Verified 2026-08-03 -- this function has zero
-- callers in the backend, the PWA, or the admin portal, and no RLS policy
-- references it. It is exposed to anon + authenticated and never invoked. This
-- is a latent trap being closed before the first caller, not a live incident.
--
-- FIX: wrap the row lookup in a subquery so "no row" coalesces too. Signature,
-- volatility, security mode and grants are unchanged.
-- ============================================================================

create or replace function public.event_allows_attendee_share(p_event_id uuid)
returns boolean
language sql
security definer
set search_path = pg_catalog, public
stable
as $$
  -- The subquery yields NULL when no open event matches; the OUTER coalesce
  -- turns that into false. Do not flatten this back into a bare select --
  -- that is precisely the bug.
  select coalesce(
    (
      select e.allow_attendee_share
      from public.events e
      where e.id = p_event_id and e.status = 'open'
    ),
    false
  );
$$;

comment on function public.event_allows_attendee_share(uuid) is
  'True only when the event exists, is open, and the host allowed attendee '
  'sharing. Returns FALSE — never null — for cancelled, completed, or unknown '
  'events (fixed 20260925120000; the original returned null for a missing row). '
  'Callers may compare with === true or rely on falsiness; both are now correct.';

-- Grants unchanged from 20260718120000; restated so a fresh apply is complete.
revoke execute on function public.event_allows_attendee_share(uuid) from public;
grant execute on function public.event_allows_attendee_share(uuid) to anon, authenticated;


-- ============================================================================
-- ROLLBACK — restore the 20260718120000 body (re-introduces the null):
--   create or replace function public.event_allows_attendee_share(p_event_id uuid)
--   returns boolean language sql security definer
--   set search_path = pg_catalog, public stable as $$
--     select coalesce(allow_attendee_share, false)
--     from public.events where id = p_event_id and status = 'open';
--   $$;
-- ============================================================================
