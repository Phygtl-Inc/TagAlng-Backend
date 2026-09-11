-- A lit category chip is the ask, so the match score ranks inside it — never gates it.
--
-- v7 filtered on reco_type but kept v6's `strength > 0` gate, and the two fight each other
-- on the ask the chips exist to serve. Tapping Professionals and typing "any professional
-- recommednation" scored 0 against every row — `professional` appears in no tip's prose,
-- the typo matches nothing lexically, and a meta-phrase with no subject falls under the
-- cosine floor — so a block holding Dr. Sarah AND Jacas Barber answered empty and fell
-- through to Google, which then pitched doctors off the reader's own "is a doctor" claim
-- (prod log, 2026-09-10).
--
-- v8 keeps the filter and moves the score: with a bucket picked, every row of that type is
-- an answer and `order by strength desc, created_at desc` decides which comes first — a
-- real word/meaning match still leads, and the rest of the bucket follows instead of
-- vanishing. Nothing changes for an unfiltered ask: there the gate is the only thing
-- separating an answer from every tip in range.
--
-- Signature unchanged, so this is a plain `create or replace` — no drop, no re-grant.

create or replace function public.find_neighbor_tips(
  p_block_id        text default null,
  p_category        text default null,
  p_query           text default null,
  p_limit           int default 5,
  p_locale          text default 'en',
  p_radius_meters   double precision default null,
  p_circle_place_id uuid default null,
  p_query_embedding extensions.vector(768) default null,
  p_min_similarity  real default 0.55,
  p_reco_types      text[] default null
)
returns table (
  signal_id        uuid,
  detail_text      text,
  category         text,
  match_strength   real,
  neighbor_label   text,
  peer_user_id     uuid,
  avatar_url       text,
  affinity_tags    text[],
  distance_meters  double precision,
  distance_text    text,
  created_at       timestamptz,
  shared_circles   jsonb,
  same_block       boolean,
  vouch_count      int,
  helpful_count    int,
  i_vouched        boolean
)
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
stable
as $function$
declare
  v_me uuid := auth.uid();
  v_origin extensions.geography;
  v_radius double precision;
  v_my_block text;
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  -- Inside a community the roster IS the audience, so neither a block nor a radius is
  -- required to have a read at all — that early return is a geography rule.
  if p_circle_place_id is null
     and p_radius_meters is null
     and (p_block_id is null or length(trim(p_block_id)) = 0) then
    return;
  end if;

  -- Same bar as the roster: a community read is members-only, so this can never be
  -- used to enumerate a place the caller has nothing to do with.
  if p_circle_place_id is not null and not exists (
    select 1 from public.circle_affiliations ca
     where ca.user_id = v_me
       and ca.place_ref = p_circle_place_id
       and ca.status in ('confirmed', 'curious')
  ) then
    return;
  end if;

  select o.origin into v_origin from public.user_origin_point(v_me) o;
  select u.home_block_id into v_my_block from public.users u where u.id = v_me;

  if p_radius_meters is not null then
    v_radius := greatest(100, least(p_radius_meters, 200000));
    if v_origin is null then
      v_radius := null;
      if p_circle_place_id is null
         and (p_block_id is null or length(trim(p_block_id)) = 0) then
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
    left join public.blocks b on b.id = u.home_block_id and b.centroid is not null
    left join public.zip_centroids z on z.zip5 = public.normalize_zip5(u.home_zip)
    where u.id <> v_me
  ),
  visible as (
    select vm.place_ref, vm.user_id from public.visible_place_members(v_me) vm
  ),
  mine as (
    select v.place_ref from visible v where v.user_id = v_me
  ),
  overlap as (
    select
      v.user_id as peer_id,
      jsonb_agg(
        jsonb_build_object('place_id', p.id, 'name', p.name, 'circle_type', p.place_type)
        order by p.name
      ) as circles
    from visible v
    join mine m on m.place_ref = v.place_ref
    join public.places p on p.id = v.place_ref
    where v.user_id <> v_me
    group by v.user_id
  )
  select
    s.id,
    s.detail_text,
    s.category,
    public._tip_match_strength(
      p_query, s.detail_text, s.affinity_tags, p_query_embedding, s.embedding, p_min_similarity
    ) as strength,
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
    s.created_at,
    coalesce(o.circles, '[]'::jsonb),
    (v_my_block is not null and u.home_block_id = v_my_block),
    (select count(*)::int from public.tip_vouches tv where tv.signal_id = s.id),
    (select count(*)::int from public.tip_helpful th where th.signal_id = s.id),
    exists (select 1 from public.tip_vouches tv where tv.signal_id = s.id and tv.user_id = v_me)
  from public.local_signals s
  join public.users u on u.id = s.user_id
  left join peer_points pp on pp.peer_id = s.user_id
  left join overlap o on o.peer_id = s.user_id
  where s.intent = 'tip_share'
    and s.status = 'listening'
    and s.expires_at > now()
    and s.user_id <> v_me
    and not public.lana_is_blocked(v_me, s.user_id)
    -- Scope, not filter: the community read ignores geography entirely, and the area
    -- read never shows a tip that was meant for a community.
    and (
      case
        when p_circle_place_id is not null then s.circle_place_ref = p_circle_place_id
        else s.circle_place_ref is null
             and case
                   when v_radius is not null
                     then pp.pt is not null and extensions.st_dwithin(pp.pt, v_origin, v_radius)
                   else s.block_id = p_block_id
                 end
      end
    )
    -- The category toggle on the Find-a-rec screen ("Recipes", "Services"). One coarse
    -- taxonomy bucket per tip (local_signals.reco_type), so this is equality over a list:
    -- the client's "Services" chip covers both professional and service, and "Others"
    -- covers location. NULL/empty = no toggle picked = every type, as before.
    and (
      p_reco_types is null
      or cardinality(p_reco_types) = 0
      or s.reco_type = any (p_reco_types)
    )
    -- With a chip lit, the chip IS the ask, so the score RANKS and does not gate. An ask
    -- of "any professional recommendation" has no subject to match on — no 4-char word
    -- lands in "Dr. Sarah, gentle with kids" and a meta-phrase like that sits under the
    -- cosine floor — so the strength was 0 for every row and a bucket holding two live
    -- recommendations read back as empty, then answered out of Google (prod 2026-09-10).
    -- Unfiltered asks keep the gate: without a bucket to browse, a zero-score row is
    -- simply not an answer to the question.
    and (
      (p_reco_types is not null and cardinality(p_reco_types) > 0)
      or public._tip_match_strength(
           p_query, s.detail_text, s.affinity_tags, p_query_embedding, s.embedding,
           p_min_similarity
         ) > 0
    )
  order by strength desc, s.created_at desc
  limit greatest(1, least(coalesce(p_limit, 5), 20));
end;
$function$;

comment on function public.find_neighbor_tips(text, text, text, int, text, double precision, uuid, extensions.vector, real, text[]) is
  'Neighbor tips matching an ask. v8 = v7 with the match score as a RANKER when p_reco_types '
  'is set (the chip is the ask) and still a GATE when it is not. p_category unused since v6.';
