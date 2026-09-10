-- Rapport cold start: what do this user's NEIGHBORS already claim?
--
-- A brand-new user has zero claims, and every rapport opener is claim-triggered:
-- the extractor needs something of theirs to follow up on, and
-- rapport_uncovered_claims() correctly returns empty (there is nothing uncovered
-- when there is nothing). So the queue cannot start itself and the "By the way…"
-- tile has nothing to show — the emptiest state in the product, and the one most
-- internal testing goes through.
--
-- Guessing at questions is the obvious fix and the wrong one, because of how
-- matching actually works. score_onion_candidates_for_user (20260914120000) awards
-- +1 per SHARED public concept and drops any pair scoring 0. A shared interest
-- therefore only scores IF A NEIGHBOR NEARBY HOLDS IT TOO: ask a new user their
-- hobbies, hear "I collect vinyl", and if nobody within reach shares it that
-- answer is worth exactly nothing to the matcher. True, warm, and useless.
--
-- So the seed questions are drawn from real local claim supply instead. Every
-- answer to a question sourced this way has a guaranteed counterpart, because the
-- concept was read off the people we would be matching them with.
--
-- Proximity is block-first, ZIP-fallback — the same order peer_radius_match uses
-- (20260921120000), and for the same reason: block is the finer signal, but 23 of
-- 37 prod users had no home_block_id, so ZIP has to carry them.
--
-- DISCLOSURE. This returns concept/label only, aggregated, with a holder COUNT and
-- never a user id — the same shape the tile already shows. It also mirrors the
-- matcher's own filters exactly (disclosure = 'public', not dismissed), so it can
-- never surface a thread the matcher would refuse to score, and never a 'mutual'
-- claim (faith, recovery, LGBTQ+ — forced mutual by the extractor). p_min_holders
-- keeps a single neighbor's private-feeling niche from being read back to a
-- stranger as a local trend.

create or replace function public.rapport_local_supply(
  p_user_id uuid,
  p_limit int default 8,
  p_min_holders int default 2
) returns table (concept text, label text, bucket text, holders int)
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
stable
as $$
declare
  v_block_id text;
  v_zip5 text;
begin
  if p_user_id is null then
    raise exception 'user_id_required' using errcode = 'P0001';
  end if;

  select u.home_block_id, public.normalize_zip5(u.home_zip)
    into v_block_id, v_zip5
    from public.users u
   where u.id = p_user_id;

  if v_block_id is null and v_zip5 is null then
    return;  -- no location yet: nothing to be near. The caller falls back to seeds.
  end if;

  return query
  with neighbors as (
    select u.id
    from public.users u
    where u.id <> p_user_id
      and (
        (v_block_id is not null and u.home_block_id = v_block_id)
        or (v_zip5 is not null and public.normalize_zip5(u.home_zip) = v_zip5)
      )
      and not public.lana_is_blocked(p_user_id, u.id)
  )
  select c.concept,
         -- The most common phrasing of the thread, so the question Lana writes uses
         -- the words neighbors actually used rather than a slug.
         (array_agg(c.label order by c.created_at))[1] as label,
         (array_agg(c.bucket order by c.created_at))[1] as bucket,
         count(distinct c.user_id)::int as holders
  from public.user_identity_claims c
  join neighbors n on n.id = c.user_id
  where c.dismissed_at is null
    and c.disclosure = 'public'          -- mirrors the matcher; excludes 'mutual'
    and coalesce(c.transient, false) = false
    and c.concept is not null
    -- Never hand back something the user already holds: that is not a seed, and
    -- open_semantic_gap would dedupe it anyway one round-trip later.
    and not exists (
      select 1 from public.user_identity_claims own
      where own.user_id = p_user_id
        and own.dismissed_at is null
        and own.concept = c.concept
    )
  group by c.concept
  having count(distinct c.user_id) >= p_min_holders
  order by count(distinct c.user_id) desc, c.concept asc
  limit greatest(p_limit, 1);
end;
$$;

comment on function public.rapport_local_supply(uuid, int, int) is
  'Rapport cold start: public, non-transient concepts held by >= p_min_holders '
  'neighbors near p_user_id (block first, ZIP fallback), excluding concepts the user '
  'already holds. Aggregated counts only — never a peer user id. Mirrors the onion '
  'matcher''s filters so a seeded question always has a scoreable counterpart. '
  'Service role only.';

revoke all on function public.rapport_local_supply(uuid, int, int) from public, anon, authenticated;
grant execute on function public.rapport_local_supply(uuid, int, int) to service_role;

-- The neighbors CTE filters users by block or ZIP; neither column was indexed for
-- this access pattern (home_block_id had no index at all).
create index if not exists users_home_block_idx
  on public.users (home_block_id) where home_block_id is not null;
create index if not exists users_home_zip_idx
  on public.users (home_zip) where home_zip is not null;
