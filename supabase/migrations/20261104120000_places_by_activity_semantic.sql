-- "Where can I take the kids to read?" -> the library neighbors already go to.
--
-- 20261103120000 could only LABEL a place Google happened to return. Asked for a reading
-- spot, Google answered with three coffee shops, and Orlando Public Library -- a real
-- community, with a member who listed "Weekly reading session" -- was never in the list to
-- be labelled. A labeller with nothing to label does nothing. This FINDS.
--
-- MATCH ON WHAT PEOPLE DO, NOT ON NAMES. Nothing about "Orlando Public Library" contains
-- the word "read", and nothing about "Florida Game Rooms" contains "pool". The member's
-- activity is the text worth matching, and place_activities already mirrors every activity
-- into user_identity_claims -- embedding included (app/place_activities._mirror_claim). So
-- the place inherits a vector for free: no new column, no backfill, no second embed path.
--
-- EVERY nearby community, not just the caller's. A user who belongs to nothing should see
-- "3 neighbors read at Orlando Public Library" -- that is the most useful thing we can show
-- them, and it is what a community is FOR. Counts and activity labels only: who belongs
-- where is the roster's business, and the roster is members-only.
--
-- Weaker proof than a neighbor's posted tip (nobody said "go here", they just go) and the
-- caller ranks it accordingly -- see app/tip_rec_cascade.
--
-- ponytail: borrows the member's claim embedding rather than embedding the label itself.
-- If they dismiss that claim the place loses the signal; give place_activities its own
-- embedding column when activity labels start diverging from claim labels.

create or replace function public.find_places_by_activity_semantic(
  p_user_id          uuid,
  p_query_embedding  extensions.vector(768),
  p_radius_meters    double precision default 8000,
  p_min_similarity   real default 0.55,
  p_limit            int default 3
)
returns table (
  place_id         uuid,
  google_place_id  text,
  name             text,
  address          text,
  place_type       text,
  member_count     int,
  is_member        boolean,
  matched_label    text,
  similarity       real,
  distance_meters  double precision,
  activity_labels  text[]
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, extensions
as $function$
declare
  v_origin extensions.geography;
begin
  if p_user_id is null or p_query_embedding is null then
    return;
  end if;

  select o.origin into v_origin from public.user_origin_point(p_user_id) o;

  return query
  with visible as (
    -- The one definition of "member here" (20261103120000): confirmed, not dismissed,
    -- blocked users excluded. Never a second copy of the predicate.
    select vm.place_ref, vm.user_id
    from public.visible_place_members(p_user_id) vm
  ),
  counted as (
    select
      v.place_ref                      as pid,
      count(distinct v.user_id)::int   as members,
      bool_or(v.user_id = p_user_id)   as mine
    from visible v
    group by v.place_ref
  ),
  -- A member's activity, scored against the ask through the claim it was mirrored into.
  scored as (
    select
      pa.place_id                                          as pid,
      pa.label                                             as label,
      (1 - (c.embedding <=> p_query_embedding))::real      as sim
    from public.place_activities pa
    join visible v
      on v.place_ref = pa.place_id and v.user_id = pa.user_id
    join public.user_identity_claims c
      on c.user_id = pa.user_id
     and c.concept = pa.concept
     and c.dismissed_at is null
     and c.embedding is not null
  ),
  best as (
    select distinct on (s.pid) s.pid, s.label, s.sim
    from scored s
    where s.sim >= p_min_similarity
    order by s.pid, s.sim desc
  ),
  -- What ELSE members do there, for the card's second line. Not scored, just context.
  labels as (
    select pa.place_id as pid, array_agg(distinct pa.label order by pa.label) as labels
    from public.place_activities pa
    join visible v on v.place_ref = pa.place_id and v.user_id = pa.user_id
    group by pa.place_id
  ),
  located as (
    select
      b.pid, b.label, b.sim, c.members, c.mine,
      p.google_place_id, p.name, p.address, p.place_type,
      coalesce(l.labels, '{}'::text[]) as labels,
      case
        when v_origin is null or p.lat is null or p.lng is null then null
        else extensions.st_distance(
               v_origin,
               extensions.st_setsrid(extensions.st_makepoint(p.lng, p.lat), 4326)::extensions.geography
             )
      end as meters
    from best b
    join counted c on c.pid = b.pid
    join public.places p on p.id = b.pid
    left join labels l on l.pid = b.pid
  )
  select
    lo.pid, lo.google_place_id, lo.name, lo.address, lo.place_type,
    lo.members, lo.mine, lo.label, lo.sim, lo.meters, lo.labels
  from located lo
  -- A place with no resolvable point is kept only when we cannot place the CALLER either;
  -- claiming an unlocatable spot is "near you" is the one thing this must not do.
  where v_origin is null
     or (lo.meters is not null and lo.meters <= p_radius_meters)
  order by lo.sim desc, lo.members desc, coalesce(lo.meters, 1e9) asc
  limit greatest(1, least(coalesce(p_limit, 3), 10));
end;
$function$;

comment on function public.find_places_by_activity_semantic(uuid, extensions.vector, double precision, real, int) is
  'Nearby communities whose MEMBERS do something close to the ask, scored on the activity '
  'claim embeddings we already store ("read with kids" -> "Weekly reading session"). Every '
  'visible community, not just the caller''s. Returns counts and activity labels, never '
  'member identities. matched_label is the activity that scored, for an honest proof line.';

revoke all on function public.find_places_by_activity_semantic(uuid, extensions.vector, double precision, real, int) from public, anon;
grant execute on function public.find_places_by_activity_semantic(uuid, extensions.vector, double precision, real, int)
  to authenticated, service_role;
