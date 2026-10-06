-- Communities that are not anywhere can be found · every one of them gets a headquarters
--
-- WHY (Asjid, 2026-10-06: "I created a community for podcasters … now it's undiscoverable?")
--
--   A community made in chat without a place (podcasters, a book club, a creator's audience)
--   has no lat/lng. Every way of finding a community was location-based:
--     · discover_communities_near — radius / ZIP, so a placeless row can never qualify;
--     · Lana's "find <name>" — the caller's own communities, then the near list;
--     · discover_communities_semantic — built for exactly this, but no caller anywhere, it
--       matched only members' profile claims (never the community's own name), and it was
--       creator-type only by default.
--   So the community worked, and nobody but its link-holders could ever reach it.
--
-- WHAT
--   1. discover_communities_anywhere: communities matched on their OWN words (name, blurb,
--      what-to-do-first; English stems, so "podcasters" finds "Podcast Club") and on what
--      their members say about themselves (public, self-subject claims — the same privacy
--      rules as discover_communities_semantic). Placeless communities only by default, so a
--      gym three states away never answers "any communities for climbers?"; a named lookup
--      passes p_placeless_only = false so "SJSU" is found from Orlando.
--      Visibility rules are the near read's: confirmed members > 0, not is_test, not a
--      chapter (chapters are found from their parent).
--   2. _community_manage_status / set_community_hq_for: the operator, or whoever started a
--      still-unverified community, records where it is RUN FROM. hq_* stays display-only —
--      a label and a pin, never a distance and never a discovery predicate (20261214120000).
--
-- Nothing here gives a placeless community lat/lng: a point there is what the near read
-- measures against, and it would list a global community as a neighbour's gym.

-- ── 1 · search ──────────────────────────────────────────────────────────────────

create or replace function public.discover_communities_anywhere(
  p_user_id          uuid,
  p_query            text,
  p_query_embedding  extensions.vector(768) default null,
  p_placeless_only   boolean default true,
  p_limit            int default 5,
  p_min_similarity   real default 0.55
)
returns table (
  place_id      uuid,
  name          text,
  place_type    text,
  hq_city       text,
  hq_lat        double precision,
  hq_lng        double precision,
  member_count  int,
  is_member     boolean,
  matched_on    text,     -- 'name' | 'about' | 'members'
  matched_label text,     -- the member claim that matched, for 'members'
  score         real
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, extensions
as $$
declare
  v_q   text := btrim(coalesce(p_query, ''));
  v_ts  tsquery;
begin
  if v_q = '' and p_query_embedding is null then
    return;
  end if;

  -- OR of the ask's stems, each as a prefix: "podcasting groups" → 'podcast':* | 'group':*.
  -- Stop words drop out, so "a community for podcasters" is just the subject.
  if v_q <> '' then
    select to_tsquery('english', string_agg(quote_literal(l) || ':*', ' | '))
      into v_ts
      from unnest(tsvector_to_array(to_tsvector('english', v_q))) as l;
  end if;

  return query
  with visible as (
    select vm.place_ref, vm.user_id
      from public.visible_place_members(p_user_id) vm
  ),
  counted as (
    select v.place_ref as pid,
           count(distinct v.user_id)::int as members,
           bool_or(v.user_id = p_user_id)  as mine
      from visible v
     group by v.place_ref
  ),
  eligible as (
    select p.id, p.name, p.place_type, p.hq_city, p.hq_lat, p.hq_lng,
           c.members, coalesce(c.mine, false) as mine,
           to_tsvector('english',
             coalesce(p.name, '') || ' ' || coalesce(p.blurb, '') || ' '
             || coalesce(p.first_action, '')) as doc
      from counted c
      join public.places p on p.id = c.pid
     where c.members > 0
       and not p.is_test
       and p.parent_place_ref is null
       and (not p_placeless_only or (p.lat is null and p.lng is null))
  ),
  by_name as (
    -- The name itself, either way round: "Podcasters Hub" for "podcasters hub", and
    -- "SJSU" inside "the SJSU community".
    select e.id as pid, 'name'::text as how, null::text as label, 1.0::real as s
      from eligible e
     where v_q <> ''
       and (e.name ilike '%' || v_q || '%' or v_q ilike '%' || e.name || '%')
  ),
  by_words as (
    select e.id, 'about'::text, null::text,
           least(0.9, 0.5 + ts_rank(e.doc, v_ts))::real
      from eligible e
     where v_ts is not null and e.doc @@ v_ts
  ),
  by_members as (
    -- Public, self-subject claims only — the rules of discover_communities_semantic: a
    -- discovery surface is strangers, and "my kid does karate" is not about the member.
    select distinct on (e.id) e.id, 'members'::text, cl.label,
           (1 - (cl.embedding <=> p_query_embedding))::real
      from eligible e
      join visible v on v.place_ref = e.id
      join public.user_identity_claims cl
        on cl.user_id = v.user_id
       and cl.dismissed_at is null
       and cl.transient = false
       and cl.disclosure = 'public'
       and cl.subject_kind = 'self'
       and cl.embedding is not null
     where p_query_embedding is not null
       and (1 - (cl.embedding <=> p_query_embedding)) >= p_min_similarity
     order by e.id, (cl.embedding <=> p_query_embedding)
  ),
  hits as (
    select * from by_name
    union all select * from by_words
    union all select * from by_members
  ),
  best as (
    select distinct on (h.pid) h.pid, h.how, h.label, h.s
      from hits h
     order by h.pid, h.s desc
  )
  select e.id, e.name, e.place_type, e.hq_city, e.hq_lat, e.hq_lng,
         e.members, e.mine, b.how, b.label, b.s
    from best b
    join eligible e on e.id = b.pid
   order by b.s desc, e.members desc, e.name asc
   limit greatest(1, least(coalesce(p_limit, 5), 20));
end;
$$;

revoke all on function public.discover_communities_anywhere(
  uuid, text, extensions.vector, boolean, int, real) from public, anon, authenticated;
grant execute on function public.discover_communities_anywhere(
  uuid, text, extensions.vector, boolean, int, real) to service_role;

comment on function public.discover_communities_anywhere(
  uuid, text, extensions.vector, boolean, int, real) is
  'Find communities by what they ARE (name, blurb, first action) and by what their members '
  'say about themselves (public self claims). Placeless communities by default — the path '
  'a community with no location has. hq_* are returned for rendering only, never ranked on. '
  'Counts only; never member identities.';

-- ── 2 · headquarters ────────────────────────────────────────────────────────────

-- Null when p_user_id may manage p_place_id's community details; otherwise why not.
-- Same people as the handle claim (20270108120000), minus the email requirement: where a
-- community is run from is not a claim on a name.
create or replace function public._community_manage_status(p_user_id uuid, p_place_id uuid)
returns text
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select case
    when p_user_id is null then 'sign_in_required'
    when p.id is null then 'place_not_found'
    when public.is_community_operator(p.id, p_user_id) then null
    when p.governance_state = 'community_started' and p.created_by = p_user_id then null
    else 'not_eligible'
  end
  from (select 1) one
  left join public.places p on p.id = p_place_id;
$$;

revoke all on function public._community_manage_status(uuid, uuid)
  from public, anon, authenticated;

create or replace function public.set_community_hq_for(
  p_user_id  uuid,
  p_place_id uuid,
  p_city     text,
  p_lat      double precision,
  p_lng      double precision
)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public
as $$
declare
  v_city text := btrim(coalesce(p_city, ''));
  v_why  text := public._community_manage_status(p_user_id, p_place_id);
begin
  if v_why is not null then
    return jsonb_build_object('status', v_why);
  end if;
  if v_city = '' or length(v_city) > 120 then
    return jsonb_build_object('status', 'invalid', 'reason', 'city');
  end if;
  -- Both or neither (places_hq_point_complete); a label with no pin is allowed.
  if (p_lat is null) <> (p_lng is null)
     or (p_lat is not null and (p_lat not between -90 and 90 or p_lng not between -180 and 180))
  then
    return jsonb_build_object('status', 'invalid', 'reason', 'point');
  end if;

  update public.places
     set hq_city = v_city, hq_lat = p_lat, hq_lng = p_lng, updated_at = now()
   where id = p_place_id;

  return jsonb_build_object('status', 'saved', 'placeId', p_place_id, 'hqCity', v_city);
end;
$$;

revoke all on function public.set_community_hq_for(uuid, uuid, text, double precision, double precision)
  from public, anon, authenticated;
grant execute on function public.set_community_hq_for(uuid, uuid, text, double precision, double precision)
  to service_role;

comment on function public.set_community_hq_for(uuid, uuid, text, double precision, double precision) is
  'Record where a community is RUN FROM. Operator, or the starter of a still-unverified '
  'community. Display-only: never a distance, never a discovery predicate.';

-- ============================================================================
-- ROLLBACK
--   drop function public.set_community_hq_for(uuid, uuid, text, double precision, double precision);
--   drop function public._community_manage_status(uuid, uuid);
--   drop function public.discover_communities_anywhere(uuid, text, extensions.vector, boolean, int, real);
-- ============================================================================
