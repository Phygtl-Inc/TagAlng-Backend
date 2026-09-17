-- An eighth bucket, `other`, and chips that only offer what actually exists.
--
-- TWO changes, one story: the category row on Find-a-rec is only honest if every
-- recommendation has a bucket AND every bucket shown has rows in it.
--
-- 1. `other`. The taxonomy covers what a neighbour DOES with a recommendation — eat there,
--    cook it, buy it, visit it, book them, hire them, do it yourself — and real asks fall
--    outside all six: the fast bus to the airport, the Lake Nona moms group, an insurance
--    broker, "don't use the west gate at 5pm". The extractor was allowed to answer null for
--    those, and a null type is the worst outcome available: no question set is generated
--    (app/tip_share.py), and the row is invisible to every category read — a recommendation
--    nobody can find. `other` gives the model somewhere to land, so null stops being a legal
--    answer (the app now writes 'other' rather than nothing, whatever the model returned).
--
--    The column stays NULLABLE by design: local_signals holds meet/swap/seek rows that have
--    no type at all, and a tip's type is written by set_signal_reco one step AFTER the row is
--    inserted — a NOT NULL or a table CHECK would reject that first insert. The floor is in
--    the one code path that ever saves a tip_share.
--
-- 2. neighbor_tip_type_counts. The chip row was a fixed list, so "Recipes" invited a tap
--    when no recipe existed within reach — and "Restaurants" invited one when the only
--    restaurant tip was the READER'S OWN, which the search is required to hide. So the count
--    has to come from the search's own predicate, not from a simpler one: same intent,
--    status, expiry, block/radius/community scope, same `user_id <> me`, same block list.
--    Only the query scoring is dropped, because a census answers "what is here", not "what
--    answers this sentence".
--
--    Returned as counts, not booleans: a chip that says "Services · 2" sets the expectation
--    before the tap, and the number is a floor — an ask that widens can find more.

-- ---------------------------------------------------------------------------
-- 1. `other` joins the closed taxonomy.
-- ---------------------------------------------------------------------------
create or replace function public.set_signal_reco(
  p_signal_id        uuid,
  p_reco_type        text default null,
  p_reco_fields      jsonb default null,
  p_reco_subject     text default null,
  p_reco_name        text default null,
  p_reco_place       text default null,
  p_reco_description text default null
)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  if p_reco_type is not null and p_reco_type not in (
    'professional', 'restaurant', 'recipe', 'product', 'location', 'service', 'diy',
    'other'
  ) then
    raise exception 'invalid_reco_type' using errcode = 'P0001';
  end if;

  if p_reco_fields is not null and jsonb_typeof(p_reco_fields) <> 'array' then
    raise exception 'reco_fields_must_be_array' using errcode = 'P0001';
  end if;

  update public.local_signals
     set reco_type = coalesce(p_reco_type, reco_type),
         reco_fields = coalesce(p_reco_fields, reco_fields),
         reco_subject = coalesce(nullif(btrim(lower(p_reco_subject)), ''), reco_subject),
         reco_name = coalesce(nullif(btrim(p_reco_name), ''), reco_name),
         reco_place = coalesce(nullif(btrim(p_reco_place), ''), reco_place),
         reco_description = coalesce(nullif(btrim(p_reco_description), ''), reco_description),
         updated_at = now()
   where id = p_signal_id
     and user_id = v_me;
end;
$$;

comment on function public.set_signal_reco(uuid, text, jsonb, text, text, text, text) is
  'Attach the recommendation card fields to your own tip. reco_type is the closed 8-value '
  'taxonomy (7 kinds + other); anything else raises invalid_reco_type.';

-- ---------------------------------------------------------------------------
-- 2. The census behind the chip row.
-- ---------------------------------------------------------------------------
create or replace function public.neighbor_tip_type_counts(
  p_block_id        text default null,
  p_radius_meters   double precision default null,
  p_circle_place_id uuid default null
)
returns table (
  reco_type text,
  n         int
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
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  -- Same three-way scope rule as find_neighbor_tips: a community read needs no geography,
  -- an area read needs either a block or a radius.
  if p_circle_place_id is null
     and p_radius_meters is null
     and (p_block_id is null or length(trim(p_block_id)) = 0) then
    return;
  end if;

  -- Members-only, exactly as the roster and the tip search are.
  if p_circle_place_id is not null and not exists (
    select 1 from public.circle_affiliations ca
     where ca.user_id = v_me
       and ca.place_ref = p_circle_place_id
       and ca.status in ('confirmed', 'curious')
  ) then
    return;
  end if;

  select o.origin into v_origin from public.user_origin_point(v_me) o;

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
  )
  select s.reco_type, count(*)::int
  from public.local_signals s
  join public.users u on u.id = s.user_id
  left join peer_points pp on pp.peer_id = s.user_id
  where s.intent = 'tip_share'
    and s.status = 'listening'
    and s.expires_at > now()
    and s.user_id <> v_me
    and s.reco_type is not null
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
  group by s.reco_type
  order by 2 desc, 1;
end;
$function$;

comment on function public.neighbor_tip_type_counts(text, double precision, uuid) is
  'How many neighbour recommendations of each reco_type this caller can actually see, under '
  'find_neighbor_tips own visibility rules (own rows excluded). Drives the Find-a-rec chip '
  'row so it never offers an empty category.';

revoke all on function public.neighbor_tip_type_counts(text, double precision, uuid)
  from public, anon;
grant execute on function public.neighbor_tip_type_counts(text, double precision, uuid)
  to authenticated;
