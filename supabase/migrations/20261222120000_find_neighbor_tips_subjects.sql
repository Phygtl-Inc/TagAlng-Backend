-- The ask, answered by SUBJECT: find_neighbor_tips carries what each tip is about.
--
-- Stages 1-2 fill local_signals.subject_ref and nothing reads it. This is the read half:
-- the same rows, each carrying its subject, so the caller can return ONE card per
-- recommended thing with how many neighbours stand behind it.
--
-- WHY NOT A SECOND RPC THAT GROUPS IN SQL. The visibility predicate here is long and
-- load-bearing — blocked users, community scope vs area scope, expiry, the reco_type
-- chips, the strength gate. A grouping RPC would have to restate it, and a second copy is
-- precisely how the rules in this repo rot (20261103120000's own words, about the
-- membership predicate it had to extract). So the rows stay row-shaped and Python groups
-- them: one definition of who may see what, forever.
--
-- THE COUNT IS NOT THE WINDOW. Grouping a LIMIT-ed page in Python would undercount a
-- subject whose other contributions fell off the end — "3 vouched" would silently become
-- "2 vouched" on page two. So subject_vouch_count is computed in SQL across EVERY visible
-- live contribution to that subject, independent of this page. A count a stranger is meant
-- to trust cannot be an artefact of pagination.
--
-- YOUR OWN RECOMMENDATION COUNTS. The row list still excludes the caller (you are not a
-- neighbour to introduce yourself to), but the COUNT includes you, because "3 people
-- recommend this" is either true or it is not. `i_contributed` says so, and lets the card
-- read "you and 2 neighbours" instead of quietly crediting your own voice to strangers.

drop function if exists public.find_neighbor_tips(
  text, text, text, int, text, double precision, uuid, extensions.vector, real, text[]
);

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
  signal_id           uuid,
  detail_text         text,
  category            text,
  match_strength      real,
  neighbor_label      text,
  peer_user_id        uuid,
  avatar_url          text,
  affinity_tags       text[],
  distance_meters     double precision,
  distance_text       text,
  created_at          timestamptz,
  shared_circles      jsonb,
  same_block          boolean,
  vouch_count         int,
  helpful_count       int,
  i_vouched           boolean,
  -- The author's own card fields. Carried because the 80/20 clustering reads what each
  -- neighbour actually SAID, and detail_text is a joined recap, not their words.
  reco_name           text,
  reco_description    text,
  reco_fields         jsonb,
  reco_type           text,
  -- The subject, and how its contributions may be combined.
  subject_ref         uuid,
  subject_name        text,
  subject_category    text,
  subject_locality    text,
  subject_lat         double precision,
  subject_lng         double precision,
  -- How far the THING is, which is not how far its recommender lives. The card reads
  -- "Pediatric clinic · 0.2 mi" about the clinic; the recommender's own distance stays on
  -- their row, where it belongs. Null until a subject is grounded, so a card falls back to
  -- the recommender's distance rather than inventing one.
  subject_distance_meters double precision,
  subject_distance_text   text,
  subject_merge_mode  text,
  subject_vouch_count int,
  i_contributed       boolean
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

  if p_circle_place_id is null
     and p_radius_meters is null
     and (p_block_id is null or length(trim(p_block_id)) = 0) then
    return;
  end if;

  if p_circle_place_id is not null and not exists (
    select 1 from public.circle_affiliations ca
    where ca.place_ref = p_circle_place_id
      and ca.user_id = v_me
      and ca.status = 'confirmed'
      and ca.dismissed_at is null
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
    exists (select 1 from public.tip_vouches tv where tv.signal_id = s.id and tv.user_id = v_me),
    s.reco_name,
    s.reco_description,
    coalesce(s.reco_fields, '[]'::jsonb),
    s.reco_type,
    s.subject_ref,
    rs.display_name,
    rs.category,
    rs.locality,
    rs.lat,
    rs.lng,
    case
      when v_origin is null or rs.lat is null or rs.lng is null then null
      else extensions.st_distance(
             extensions.st_setsrid(extensions.st_makepoint(rs.lng, rs.lat), 4326)::extensions.geography,
             v_origin
           )::double precision
    end,
    case
      when v_origin is null or rs.lat is null or rs.lng is null then null
      else public.humanize_distance_text(
             extensions.st_distance(
               extensions.st_setsrid(extensions.st_makepoint(rs.lng, rs.lat), 4326)::extensions.geography,
               v_origin
             )::double precision,
             coalesce(p_locale, 'en')
           )
    end,
    rs.merge_mode,
    -- Across every visible live contribution, not just this page (see the header). Same
    -- block rule as the row itself: a contribution from someone the caller blocked must
    -- not be counted any more than it may be shown.
    case
      when s.subject_ref is null then 1
      else (
        select count(*)::int
        from public.local_signals ls
        where ls.subject_ref = s.subject_ref
          and ls.intent = 'tip_share'
          and ls.status = 'listening'
          and ls.expires_at > now()
          and not public.lana_is_blocked(v_me, ls.user_id)
      )
    end,
    -- Did the caller contribute to this subject? Their row is excluded from the list but
    -- counted above, so the card can say "you and 2 neighbours" rather than miscrediting.
    case
      when s.subject_ref is null then false
      else exists (
        select 1 from public.local_signals ls
        where ls.subject_ref = s.subject_ref
          and ls.user_id = v_me
          and ls.intent = 'tip_share'
          and ls.status = 'listening'
      )
    end
  from public.local_signals s
  join public.users u on u.id = s.user_id
  left join public.reco_subjects rs on rs.id = s.subject_ref
  left join peer_points pp on pp.peer_id = s.user_id
  left join overlap o on o.peer_id = s.user_id
  where s.intent = 'tip_share'
    and s.status = 'listening'
    and s.expires_at > now()
    and s.user_id <> v_me
    and not public.lana_is_blocked(v_me, s.user_id)
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
    and (
      p_reco_types is null
      or cardinality(p_reco_types) = 0
      or s.reco_type = any (p_reco_types)
    )
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
  'Neighbor tips matching an ask. v8 = v7 plus the author''s own card fields and the '
  'SUBJECT each tip is about (ref, name, locality, coordinates, merge_mode), with a '
  'vouch count computed across every visible live contribution rather than this page, and '
  'i_contributed so the caller''s own voice is counted without being miscredited.';

revoke all on function public.find_neighbor_tips(text, text, text, int, text, double precision, uuid, extensions.vector, real, text[]) from public, anon;
grant execute on function public.find_neighbor_tips(text, text, text, int, text, double precision, uuid, extensions.vector, real, text[])
  to authenticated, service_role;


-- ----------------------------------------------------------------------------
-- The 80/20 digest cache.
--
-- Standup 2026-09-22: within one subject, aggregate the majority saying much the same
-- thing and show the distinctive minority standalone. That grouping is an LLM read over
-- what each neighbour wrote, so it is cached for the same reasons peer_rec_lines is: a
-- reload must not cost a model call, and the text a card shows has to be readable back
-- from the DB rather than trusted from a client.
--
-- Keyed by basis_sig — a fingerprint of the contributions it was built from — so a NEW
-- recommendation about the same subject produces a NEW digest instead of serving a stale
-- one that cannot know about it.
-- ----------------------------------------------------------------------------
create table if not exists public.reco_subject_digests (
  id          uuid primary key default gen_random_uuid(),
  subject_ref uuid not null references public.reco_subjects (id) on delete cascade,
  lang        text not null default 'en',
  basis_sig   text not null,
  -- [{label, n, quote, signal_ids[]}] ordered by n desc. `n` out of `total` is what a card
  -- renders as "Gentle with anxious kids 8/10".
  themes      jsonb not null default '[]'::jsonb,
  total       int not null default 0,
  created_at  timestamptz not null default now(),
  constraint reco_subject_digests_uq unique (subject_ref, lang, basis_sig)
);

comment on table public.reco_subject_digests is
  'The Pareto read of one subject: themes most contributors share, each with its count and '
  'a representative quote, outliers kept as their own low-count themes rather than blended '
  'away. Cached per (subject, language, contribution fingerprint).';

alter table public.reco_subject_digests enable row level security;
-- No policies: the worker (service_role) is the only reader/writer, same as peer_rec_lines.
revoke all on public.reco_subject_digests from anon, authenticated;
