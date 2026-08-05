-- The recommendation cascade (frontend §12 / issues #68): a neighbor row on a `looking.tip`
-- turn has to carry the REC ITSELF, not just the neighbor. Today find_neighbor_tips
-- (20261001120000) answers with the tip text and a coarse label only, so the worker can
-- speak the rec in prose but cannot attach it to a person, attribute it, or offer a
-- reach-out. This replaces that function with the join the ranker actually needs.
--
-- WHAT IS NEWLY EXPOSED, AND WHY THAT IS OK
--   The v1 comment said "never the peer's user_id — identity is earned through the block log
--   after a real match". That reasoning holds for a tip_SEEK (a question the asker has not
--   agreed to broadcast) and for claim-based peer matching (an inferred affinity the peer
--   never published). A tip_SHARE is neither: the neighbor deliberately posted a
--   recommendation *for their block to see*, and v1 already returned their nickname. What
--   v1 withheld was only the ability to say thank-you — the row could not be attributed or
--   replied to. So this version returns signal_id, peer_user_id and avatar_url for
--   tip_share authors ONLY. No other intent is readable through this function, and the
--   `status = 'listening'` + `expires_at > now()` filters mean a withdrawn tip
--   (close_local_signal) disappears from the cascade immediately.
--
-- ALSO FIXED HERE: v1 did not honour lana_is_blocked, so a blocked neighbor's tip could
-- still surface. Blocked in either direction is now excluded.
--
-- NON-DESTRUCTIVE: only find_neighbor_tips changes. The old 4-arg version is dropped
-- because a RETURNS TABLE shape cannot be altered in place; the new signature keeps those
-- four parameters positionally identical, so existing callers are unaffected.

-- ---------------------------------------------------------------------------
-- find_neighbor_tips v2 — the rec, its author, and how far away they are
-- ---------------------------------------------------------------------------
-- p_radius_meters is the widen lever ("Find more people" in the frame). NULL keeps the v1
-- behaviour exactly: candidates are the caller's own block. A value switches the candidate
-- set to st_dwithin on the two coarse points, the same block-centroid-then-ZIP-centroid
-- resolution match_peers_within_radius uses (20260921120000). Distance is always computed
-- when both points resolve, so a block-scoped call still gets an honest "a few minutes
-- away" — block precision means same-block neighbors legitimately read as 0 m.
drop function if exists public.find_neighbor_tips(text, text, text, int);

create or replace function public.find_neighbor_tips(
  p_block_id text,
  p_query text,
  p_category text default null,
  p_limit int default 3,
  p_locale text default 'en',
  p_radius_meters double precision default null
)
returns table (
  signal_id uuid,
  detail_text text,
  category text,
  match_strength real,
  neighbor_label text,
  peer_user_id uuid,
  avatar_url text,
  affinity_tags text[],
  distance_meters double precision,
  distance_text text,
  created_at timestamptz
)
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
stable
as $$
declare
  v_me uuid := auth.uid();
  v_origin extensions.geography;
  v_radius double precision;
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  -- Radius mode does not need a block; block mode does. Neither is an error state.
  if p_radius_meters is null
     and (p_block_id is null or length(trim(p_block_id)) = 0) then
    return;
  end if;

  select o.origin into v_origin from public.user_origin_point(v_me) o;

  if p_radius_meters is not null then
    v_radius := greatest(100, least(p_radius_meters, 200000));
    -- Asked to widen but we cannot locate the caller → fall back to their block rather
    -- than returning the whole table.
    if v_origin is null then
      v_radius := null;
      if p_block_id is null or length(trim(p_block_id)) = 0 then
        return;
      end if;
    end if;
  end if;

  return query
  with peer_points as (
    select
      u.id as peer_id,
      coalesce(
        b.centroid,
        extensions.st_setsrid(extensions.st_makepoint(z.lng, z.lat), 4326)::extensions.geography
      ) as pt
    from public.users u
    left join public.blocks b
      on b.id = u.home_block_id and b.centroid is not null
    left join public.zip_centroids z
      on z.zip5 = public.normalize_zip5(u.home_zip)
    where u.id <> v_me
  )
  select
    s.id,
    s.detail_text,
    s.category,
    public._signal_match_strength(p_category, p_query, s.category, s.detail_text) as strength,
    coalesce(u.nickname, 'A neighbor on your block') as neighbor_label,
    s.user_id,
    u.profile_photo_url,
    coalesce(s.affinity_tags, '{}')::text[],
    case
      when v_origin is null or pp.pt is null then null
      else extensions.st_distance(pp.pt, v_origin)::double precision
    end as dist_m,
    case
      when v_origin is null or pp.pt is null then null
      else public.humanize_distance_text(
             extensions.st_distance(pp.pt, v_origin)::double precision,
             coalesce(p_locale, 'en')
           )
    end as dist_text,
    s.created_at
  from public.local_signals s
  join public.users u on u.id = s.user_id
  left join peer_points pp on pp.peer_id = s.user_id
  where s.intent = 'tip_share'
    and s.status = 'listening'
    and s.expires_at > now()
    and s.user_id <> v_me
    and not public.lana_is_blocked(v_me, s.user_id)
    and (
      case
        when v_radius is not null
          then pp.pt is not null and extensions.st_dwithin(pp.pt, v_origin, v_radius)
        else s.block_id = p_block_id
      end
    )
    -- Floor is 0.74, deliberately stricter than the matcher's 0.65: _signal_match_strength
    -- returns exactly 0.72 for a bare category match (health == health), which would let ANY
    -- health tip answer "know a good doctor?". 0.74 requires real word overlap (0.68 + 0.08
    -- per shared word), so a rec is only shown when the neighbor actually wrote about this.
    and public._signal_match_strength(p_category, p_query, s.category, s.detail_text) >= 0.74
  -- Ordinals, not aliases: `match_strength` and `created_at` are also RETURNS TABLE
  -- variables in scope here, so naming them in ORDER BY is an ambiguous reference.
  order by 4 desc, 9 asc nulls last, 11 desc
  limit greatest(1, least(coalesce(p_limit, 3), 20));
end;
$$;

revoke all on function public.find_neighbor_tips(text, text, text, int, text, double precision)
  from public, anon;
grant execute on function public.find_neighbor_tips(text, text, text, int, text, double precision)
  to authenticated;

comment on function public.find_neighbor_tips(text, text, text, int, text, double precision) is
  'Read-only neighbor tip_share matches for a query, with the rec text, its author '
  '(tip_share is a deliberate broadcast — seeks and claim matches stay anonymous), an '
  'honest distance label, and the tip''s affinity tags. p_radius_meters widens past the '
  'caller''s block. No writes: Lana answers a recommendation ask without posting a tip_seek.';

-- ---------------------------------------------------------------------------
-- ROLLBACK
-- ---------------------------------------------------------------------------
--   drop function if exists public.find_neighbor_tips(text, text, text, int, text, double precision);
--   then re-run §1 of 20261001120000_tip_ask_consent.sql to restore the v1 shape.
-- The worker degrades on its own if this is not applied: the extra columns are read with
-- .get(), so a v1 response simply yields rows with no author, no distance and no chips.
