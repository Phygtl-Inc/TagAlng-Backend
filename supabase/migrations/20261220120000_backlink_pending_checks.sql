-- "I'll add the link in a minute."
--
-- 20261216120000 wrote an identity row only on SUCCESS, and claim_backlink checked once.
-- That serves exactly one creator: the one who had already put the link up before
-- submitting. Everyone else — and it is most people, because the natural order is claim
-- the handle, THEN go edit your bio — got BACKLINK_NOT_FOUND and a dead end.
--
-- So a pending claim now has a row from the start, with ownership_verified_at null. The
-- row carries which URL we are watching, how many times we have looked, and why the last
-- look failed. Re-checking is then an UPDATE, the user can ask us to look again, and a
-- sweep can retry on its own.
--
-- WHY A ROW RATHER THAN A QUEUE
--   The unique indexes on (provider, provider_account_id) and (provider, lower(username))
--   already exist and already mean "this account belongs to one community". Holding the
--   row from the start extends that to pending claims — two people cannot race on the
--   same Instagram account while both are waiting. The cost is that an abandoned claim
--   would squat an account, which is what expiry below is for.

alter table public.external_community_identities
  add column if not exists check_attempts    int not null default 0,
  add column if not exists last_check_reason text,
  add column if not exists next_check_after  timestamptz,
  add column if not exists claim_id          uuid references public.place_claims(id) on delete set null;

comment on column public.external_community_identities.check_attempts is
  'How many times we have fetched the profile looking for our link. Bounded: a creator '
  'who never adds it must not be retried forever, and an unbounded retry against someone '
  'else''s site is a bad neighbour.';

comment on column public.external_community_identities.last_check_reason is
  'Reason code from the most recent check (BACKLINK_NOT_FOUND, PROFILE_NOT_PUBLIC, ...). '
  'This is what the UI renders while a claim is pending — "we looked and could not see it '
  'yet" is a different message from "that profile is private", and the creator can only '
  'act on the difference if we keep it.';

comment on column public.external_community_identities.next_check_after is
  'Earliest time the sweep may look again. Backs off so a pending claim does not hammer a '
  'third-party profile. Null means the sweep will not pick it up (verified, or given up).';

-- Pending rows the sweep should look at.
create index if not exists eci_pending_check_idx
  on public.external_community_identities(next_check_after)
  where ownership_verified_at is null and next_check_after is not null;

-- ---------------------------------------------------------------------------
-- Expiry: an unverified identity must not squat an account forever.
--
-- Tied to the reservation, not to a fixed age — the reservation is already the thing that
-- bounds a claim, and two clocks that can disagree is one clock too many.
-- ---------------------------------------------------------------------------
create or replace function public.release_stale_backlink_claims()
returns int
language sql
security definer
set search_path = pg_catalog, public
as $$
  with gone as (
    delete from public.external_community_identities e
    where e.ownership_verified_at is null
      and not exists (
        select 1
        from public.place_handle_reservations r
        where r.place_id = e.place_id
          and r.status = 'active'
          and (r.expires_at is null or r.expires_at > now())
      )
    returning 1
  )
  select count(*)::int from gone;
$$;

revoke all on function public.release_stale_backlink_claims() from public, anon, authenticated;
grant execute on function public.release_stale_backlink_claims() to service_role;

comment on function public.release_stale_backlink_claims() is
  'Drops unverified identity rows whose reservation is gone, freeing the external account '
  'for someone else. Run alongside whatever already expires reservations — the two must '
  'move together or a lapsed claim keeps holding an Instagram account it never proved.';

-- ============================================================================
-- ROLLBACK
--   drop function if exists public.release_stale_backlink_claims();
--   drop index if exists public.eci_pending_check_idx;
--   alter table public.external_community_identities
--     drop column if exists claim_id,
--     drop column if exists next_check_after,
--     drop column if exists last_check_reason,
--     drop column if exists check_attempts;
-- ============================================================================
