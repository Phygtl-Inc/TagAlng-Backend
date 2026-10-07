-- Community search finds a community by what it MEANS, finds chapters, and reaches past
-- "placeless only" when asked to.
--
-- WHY (prod, 2026-10-06): "a club focused on AI / AI ethics", asked for the SJSU pilot,
-- returned nothing — not the Responsible Computing Club, not San Jose State, no offer to look
-- further. Three separate reasons, all in discover_communities_anywhere (the read chat's
-- topic search uses):
--
--   1. It only searched communities with NO location (p_placeless_only, default true). SJSU
--      and RCC both have a point, so a topic ask could never reach them.
--   2. It dropped every chapter (parent_place_ref is null). RCC is a chapter of SJSU, so no
--      wording, distance or account returned it. The same line sits in
--      discover_communities_near.
--   3. It matched English stems only. Nothing in "Responsible Computing Club" stems to "AI"
--      or "ethics". places.blurb_embedding exists (20261231120001) and is indexed, but the
--      only function that reads it — discover_communities — has no caller, and nothing ever
--      wrote one: 36 blurbs on prod, 0 embeddings.
--
-- What changes:
--
--   · discover_communities_anywhere gains a MEANING arm over blurb_embedding (the worker
--     passes the ask's embedding), and an optional radius. With p_radius_meters set, a
--     located community may answer a topic ask too, and comes back with its distance from
--     the caller and its city so the worker can say "near you" or "in San Jose" honestly.
--     A located community qualifies on its NAME or its MEANING only — never on a single
--     shared stem, because "club" would otherwise make every club in the country an answer
--     to "a club about AI ethics". Placeless rows keep the stem arm exactly as before.
--
--   · Chapters are discoverable in both reads, under the 20261214120000 visibility contract
--     (the same rule discover_community_chapters already implements):
--         a member of the parent sees every chapter;
--         a member of only some chapter never sees a SIBLING chapter;
--         a stranger to the family sees the directory.
--     Every chapter row carries parent_place_ref, so the worker can label it
--     "a chapter of San Jose State University" — an unlabelled chapter reads as local.
--
--   · A trigger drops blurb_embedding whenever name or blurb changes in an update that did
--     not also write the embedding, so a vector never describes words the community no
--     longer says. The worker re-embeds anything NULL (app/community_embeddings.py).
--
-- hq_city / hq_lat / hq_lng are still in NO predicate and NO ordering (20261214120000).
-- Distance here is measured from places.lat/lng only, and only to label and partition.
--
-- DEPLOY ORDER: migration first, then worker. The old worker calls
-- (p_user_id, p_query, p_placeless_only, p_limit), which this function still accepts (every
-- new parameter has a default) and answers exactly as before when p_radius_meters is null —
-- apart from chapters now appearing, which the old worker shows unlabelled. The new worker
-- retries without the new arguments if the function has not taken this migration yet.

-- ── 1 · a vector never outlives the words it was made from ─────────────────

create or replace function public.places_drop_stale_blurb_embedding()
returns trigger
language plpgsql
as $$
begin
  if (new.blurb is distinct from old.blurb or new.name is distinct from old.name)
     and new.blurb_embedding is not distinct from old.blurb_embedding then
    new.blurb_embedding := null;
  end if;
  return new;
end;
$$;

revoke all on function public.places_drop_stale_blurb_embedding() from public, anon, authenticated;

drop trigger if exists places_drop_stale_blurb_embedding on public.places;
create trigger places_drop_stale_blurb_embedding
  before update of name, blurb on public.places
  for each row execute function public.places_drop_stale_blurb_embedding();

comment on column public.places.blurb_embedding is
  'Embedding of "<name>. <blurb>" (text-embedding-005, 768). Lets a community be found by '
  'what it is about. Cleared by trigger when name or blurb changes; re-embedded by the '
  'worker (app/community_embeddings.py) whenever it is NULL. Read by '
  'discover_communities_anywhere''s meaning arm (20270119120000).';

-- ── 2 · discover_communities_anywhere · meaning, radius, chapters ────────────

drop function if exists public.discover_communities_anywhere(uuid, text, boolean, int);

create function public.discover_communities_anywhere(
  p_user_id          uuid,
  p_query            text,
  p_placeless_only   boolean default true,
  p_limit            int default 5,
  p_query_embedding  extensions.vector(768) default null,
  p_min_similarity   real default 0.50,
  p_radius_meters    double precision default null
)
returns table (
  place_id          uuid,
  name              text,
  place_type        text,
  hq_city           text,
  hq_lat            double precision,
  hq_lng            double precision,
  member_count      int,
  is_member         boolean,
  matched_on        text,     -- 'name' | 'about' | 'meaning'
  score             real,
  parent_place_ref  uuid,     -- non-null: this row is a chapter of that community
  distance_meters   double precision,  -- null: placeless, or the caller has no origin
  area              text,     -- the city of a located community, for "in San Jose"
  is_placeless      boolean   -- no lat/lng: joinable from anywhere, never "near" or "far"
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, extensions
as $$
declare
  v_q       text := btrim(coalesce(p_query, ''));
  v_ts      tsquery;
  v_origin  extensions.geography;
  v_reach   boolean := p_radius_meters is not null;
begin
  if v_q = '' then
    return;
  end if;

  select to_tsquery('english', string_agg(quote_literal(l) || ':*', ' | '))
    into v_ts
    from unnest(tsvector_to_array(to_tsvector('english', v_q))) as l;

  if v_reach then
    select o.origin into v_origin from public.user_origin_point(p_user_id) o;
  end if;

  return query
  with counted as (
    select vm.place_ref as pid,
           count(distinct vm.user_id)::int as members,
           bool_or(vm.user_id = p_user_id)  as mine
      from public.visible_place_members(p_user_id) vm
     group by vm.place_ref
  ),
  confirmed as (
    select a.place_ref
      from public.circle_affiliations a
     where a.user_id = p_user_id
       and a.status = 'confirmed'
       and a.dismissed_at is null
  ),
  eligible as (
    select p.id, p.name, p.place_type, p.hq_city, p.hq_lat, p.hq_lng,
           p.parent_place_ref, p.zip, p.blurb_embedding,
           c.members, coalesce(c.mine, false) as mine,
           (p.lat is null and p.lng is null) as placeless,
           case
             when v_origin is null or p.lat is null or p.lng is null then null
             else extensions.st_distance(
                    v_origin,
                    extensions.st_setsrid(extensions.st_makepoint(p.lng, p.lat), 4326)
                      ::extensions.geography)
           end as meters,
           to_tsvector('english',
             coalesce(p.name, '') || ' ' || coalesce(p.blurb, '') || ' '
             || coalesce(p.first_action, '')) as doc
      from counted c
      join public.places p on p.id = c.pid
     where c.members > 0
       and not p.is_test
       and p.governance_state is distinct from 'suspended'
       and (not p_placeless_only or v_reach or (p.lat is null and p.lng is null))
       -- Chapters: never a sibling of a chapter the caller is in, unless she is in the
       -- parent too (20261214120000). Her own chapter always.
       and (
         p.parent_place_ref is null
         or coalesce(c.mine, false)
         or exists (select 1 from confirmed f where f.place_ref = p.parent_place_ref)
         or not exists (
           select 1
             from confirmed f
             join public.places s on s.id = f.place_ref
            where s.parent_place_ref = p.parent_place_ref
         )
       )
  ),
  hits as (
    select e.id as pid, 'name'::text as how, 1.0::real as s
      from eligible e
     where e.name ilike '%' || v_q || '%' or v_q ilike '%' || e.name || '%'
    union all
    -- Stems: placeless communities only, as before. A located one answering on one shared
    -- word ("club") is how a chess club in Ohio answers "a club about AI ethics".
    select e.id, 'about'::text, least(0.9, 0.5 + ts_rank(e.doc, v_ts))::real
      from eligible e
     where v_ts is not null and e.doc @@ v_ts and e.placeless
    union all
    select e.id, 'meaning'::text,
           (1 - (e.blurb_embedding <=> p_query_embedding))::real
      from eligible e
     where p_query_embedding is not null
       and e.blurb_embedding is not null
       and (1 - (e.blurb_embedding <=> p_query_embedding)) >= coalesce(p_min_similarity, 0.50)
  ),
  best as (
    select distinct on (h.pid) h.pid, h.how, h.s
      from hits h
     order by h.pid, h.s desc
  )
  select e.id, e.name, e.place_type, e.hq_city, e.hq_lat, e.hq_lng,
         e.members, e.mine, b.how, b.s,
         e.parent_place_ref, e.meters,
         nullif(btrim(coalesce(z.city, '')), ''),
         e.placeless
    from best b
    join eligible e on e.id = b.pid
    left join public.zip_centroids z
      on not e.placeless and z.zip5 = public.normalize_zip5(e.zip)
   order by b.s desc, e.members desc, e.name asc
   limit greatest(1, least(coalesce(p_limit, 5), 20));
end;
$$;

revoke all on function public.discover_communities_anywhere(
  uuid, text, boolean, int, extensions.vector, real, double precision)
  from public, anon, authenticated;
grant execute on function public.discover_communities_anywhere(
  uuid, text, boolean, int, extensions.vector, real, double precision)
  to service_role;

comment on function public.discover_communities_anywhere(
  uuid, text, boolean, int, extensions.vector, real, double precision) is
  'Find communities by what they are: name, own words (stems, placeless only) and meaning '
  '(blurb_embedding, when p_query_embedding is given). Placeless only unless '
  'p_placeless_only is false or p_radius_meters is given; then located communities come back '
  'with distance_meters and area for the caller to label. Chapters included under the '
  '20261214120000 contract, with parent_place_ref. Counts only; never member identities.';

-- ── 3 · discover_communities_near · chapters, same contract ─────────────────
-- Body from 20270104120000, unchanged except the chapter predicate.

create or replace function public.discover_communities_near(
  p_user_id       uuid,
  p_radius_meters double precision default 8000,
  p_limit         integer default 20,
  p_locale        text default 'en'::text,
  p_query         text default null::text,
  p_lat           double precision default null::double precision,
  p_lng           double precision default null::double precision
)
returns table(
  place_id uuid, name text, address text, place_type text, zip text,
  lat double precision, lng double precision, member_count integer,
  member_types text[], distance_meters double precision, distance_text text,
  is_member boolean
)
language plpgsql
stable
security definer
set search_path to 'pg_catalog', 'public', 'extensions'
as $function$
declare
  v_origin extensions.geography;
  v_zip5   text;
begin
  if p_user_id is null then
    raise exception 'user_id_required' using errcode = 'P0001';
  end if;

  if p_lat is not null and p_lng is not null
     and p_lat between -90 and 90
     and p_lng between -180 and 180 then
    v_origin := extensions.st_setsrid(
                  extensions.st_makepoint(p_lng, p_lat), 4326
                )::extensions.geography;
  else
    select o.origin, o.zip5 into v_origin, v_zip5
    from public.user_origin_point(p_user_id) o;
  end if;

  return query
  with visible_members as (
    select vm.place_ref, vm.user_id, vm.circle_type
    from public.visible_place_members(p_user_id) vm
  ),
  counted as (
    select
      vm.place_ref                                        as pid,
      count(distinct vm.user_id)::int                     as members,
      array_agg(distinct vm.circle_type)                  as types,
      bool_or(vm.user_id = p_user_id)                     as mine
    from visible_members vm
    group by vm.place_ref
  ),
  confirmed as (
    select a.place_ref
    from public.circle_affiliations a
    where a.user_id = p_user_id
      and a.status = 'confirmed'
      and a.dismissed_at is null
  ),
  located as (
    select
      c.pid, c.members, c.types, c.mine,
      p.name, p.address, p.place_type, p.zip,
      p.lat, p.lng,
      case
        when v_origin is null or p.lat is null or p.lng is null then null
        else extensions.st_distance(
               v_origin,
               extensions.st_setsrid(extensions.st_makepoint(p.lng, p.lat), 4326)::extensions.geography
             )
      end as meters
    from counted c
    join public.places p on p.id = c.pid
    where c.members > 0
      and (p_query is null or p_query = '' or p.name ilike '%' || p_query || '%')
      and not p.is_test
      -- CHANGED (20270119120000): chapters are listed, never a sibling of the caller's own.
      and (
        p.parent_place_ref is null
        or coalesce(c.mine, false)
        or exists (select 1 from confirmed f where f.place_ref = p.parent_place_ref)
        or not exists (
          select 1
          from confirmed f
          join public.places s on s.id = f.place_ref
          where s.parent_place_ref = p.parent_place_ref
        )
      )
  )
  select
    l.pid,
    l.name,
    l.address,
    l.place_type,
    l.zip,
    l.lat,
    l.lng,
    l.members,
    l.types,
    l.meters,
    public.humanize_distance_text(l.meters, p_locale),
    l.mine
  from located l
  where
    case
      when v_origin is not null then
        (l.meters is not null and l.meters <= p_radius_meters)
        or (l.meters is null and v_zip5 is not null and l.zip = v_zip5
            and l.address is not null)
      else v_zip5 is not null and l.zip = v_zip5 and l.address is not null
    end
  order by l.members desc, coalesce(l.meters, 1e9) asc, l.name asc
  limit greatest(coalesce(p_limit, 20), 1);
end;
$function$;

-- ============================================================================
-- ROLLBACK
--   drop trigger if exists places_drop_stale_blurb_embedding on public.places;
--   drop function if exists public.places_drop_stale_blurb_embedding();
--   drop function public.discover_communities_anywhere(
--     uuid, text, boolean, int, extensions.vector, real, double precision);
--   then re-run 20270110120000 (anywhere) and section 2 of 20270104120000 (near).
--   Embeddings written by the worker are harmless to leave in place.
-- ============================================================================
