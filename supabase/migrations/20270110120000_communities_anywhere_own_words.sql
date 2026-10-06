-- Community search matches what a community says about ITSELF, not its members' hobbies
--
-- WHY (prod, 2026-10-06): "any communities for podcasters?" returned Tommaso, Gym Fans,
-- Asjid Fellas and a triathlon community — and not "Podcasters". The run of
-- discover_communities_anywhere(…, 'podcasting', null, …) with the member arm switched off
-- returned exactly one row: Podcasters, matched on its own description (0.56). Everything
-- else came in through the member arm (20270109120000), which let ONE member's public
-- "podcaster" claim qualify every community they belong to — and scored those cosine
-- matches above the community's own words, so the real answer was outranked.
--
-- A member's hobby is not what a community is about. The member arm is removed; matching is
-- the community's own name, blurb and first action. That also drops an embedding call from
-- every search.
--
-- DEPLOY ORDER: worker first, then this migration. The new worker calls with
-- (p_user_id, p_query, p_placeless_only, p_limit), which the OLD function also accepts (its
-- other parameters have defaults). The old worker's call would not resolve against this
-- signature and would degrade to "nothing found" until the worker is deployed.

drop function if exists public.discover_communities_anywhere(
  uuid, text, extensions.vector, boolean, int, real);

create function public.discover_communities_anywhere(
  p_user_id        uuid,
  p_query          text,
  p_placeless_only boolean default true,
  p_limit          int default 5
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
  matched_on    text,     -- 'name' | 'about'
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
  if v_q = '' then
    return;
  end if;

  -- OR of the ask's stems, each as a prefix: "podcasting groups" → 'podcast':* | 'group':*.
  select to_tsquery('english', string_agg(quote_literal(l) || ':*', ' | '))
    into v_ts
    from unnest(tsvector_to_array(to_tsvector('english', v_q))) as l;

  return query
  with counted as (
    select vm.place_ref as pid,
           count(distinct vm.user_id)::int as members,
           bool_or(vm.user_id = p_user_id)  as mine
      from public.visible_place_members(p_user_id) vm
     group by vm.place_ref
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
  hits as (
    -- The name itself, either way round: "Podcasters Hub" / "SJSU" inside a longer ask.
    select e.id as pid, 'name'::text as how, 1.0::real as s
      from eligible e
     where e.name ilike '%' || v_q || '%' or v_q ilike '%' || e.name || '%'
    union all
    select e.id, 'about'::text, least(0.9, 0.5 + ts_rank(e.doc, v_ts))::real
      from eligible e
     where v_ts is not null and e.doc @@ v_ts
  ),
  best as (
    select distinct on (h.pid) h.pid, h.how, h.s
      from hits h
     order by h.pid, h.s desc
  )
  select e.id, e.name, e.place_type, e.hq_city, e.hq_lat, e.hq_lng,
         e.members, e.mine, b.how, b.s
    from best b
    join eligible e on e.id = b.pid
   order by b.s desc, e.members desc, e.name asc
   limit greatest(1, least(coalesce(p_limit, 5), 20));
end;
$$;

revoke all on function public.discover_communities_anywhere(uuid, text, boolean, int)
  from public, anon, authenticated;
grant execute on function public.discover_communities_anywhere(uuid, text, boolean, int)
  to service_role;

comment on function public.discover_communities_anywhere(uuid, text, boolean, int) is
  'Find communities by what they say about themselves: name, blurb, first action (English '
  'stems). Never by members'' claims — one member''s hobby is not the community''s topic '
  '(20270110120000). Placeless communities by default; a named lookup passes false. hq_* '
  'are for rendering only. Counts only; never member identities.';

-- ============================================================================
-- ROLLBACK
--   drop function public.discover_communities_anywhere(uuid, text, boolean, int);
--   then re-run the function section of 20270109120000.
-- ============================================================================
