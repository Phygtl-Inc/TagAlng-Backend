-- ============================================================================
-- PR7 · GEOLOCATION-AWARE SEARCH
-- Destination: supabase/migrations/20260920120000_geolocation_aware_search.sql
-- Repo:        Phygtl-Inc/TagAlng-Backend
-- Project:     kmetmatfxdkrialwrnzj (PROD — rjlcyvwogmfmngemhbmn is DEV, despite
--              what _CODE_TRUTH_2026-07-30.md says)
-- Author:      generated 2026-07-30, re-cut from PR #121 on 2026-08-03
--
-- SLOT: originally 20260917120000, which COLLIDES with an applied migration
-- (20260917120000_find_activities_zip_gate). db push matches on version, so the
-- file would have been skipped in silence and the deploy would have reported
-- success with none of these functions created. Renumbered above the ledger head
-- (20260919120000_rls_hardening, applied 2026-07-31).
--
-- PROBLEM (standup 2026-07-30)
--   Tommaso: "if my profile is set to my Orlando home, but I'm in Silicon Valley
--   and I say I want to hit the gym, it would show me gyms in Orlando."
--   Asjid:   "it doesn't take user's location, it takes the block location."
--
-- ROOT CAUSE IS CALLER-SIDE, NOT RPC-SIDE.
--   public.get_nearby_activities already accepts p_lat/p_lng and only falls back
--   to the ZIP centroid when they are null. The worker never passes them:
--     * services/lana-worker/app/main.py :: search_places_endpoint calls
--         search_places(query=body.q, block_id=auth.home_block_id, user_id=auth.user_id)
--       and models.PlaceSearchRequest is `class PlaceSearchRequest(BaseModel): q: str`
--       -- there is no lat/lng field to pass.
--     * services/lana-worker/app/places.py :: _centroid() resolves, in order,
--       _BLOCK_FALLBACK[block_id] (hard-coded Lake Nona), the ZIP centroid, then
--       resolve_event_location(user_id) -- which reads users.home_zip /
--       users.home_block_id and ultimately `return 28.3647, -81.2568` (Lake Nona).
--     * services/lana-worker/app/discovery_route.py :: fetch_preview_events_on_block
--       filters `.eq("block_id", block_id)` -- pure equality on the HOME block.
--     * public.get_nearby_activities_authed has ZERO application call sites.
--
-- THIS MIGRATION adds the data-layer primitives the worker needs. It changes no
-- existing function. Everything here is additive.
--
-- DESIGN CONSTRAINTS ENCODED BELOW
--   1. Home block stays the DEFAULT. The device fix is an OVERRIDE.
--   2. The override is NEVER silent. resolve_search_origin() will not switch the
--      origin on its own -- it returns should_offer_away=true and the labels Lana
--      needs to ASK. Only a caller passing p_use_device=true (i.e. after the user
--      said yes) gets a device-anchored origin. Teleporting is not expressible.
--   3. Ephemeral. Nothing here writes. Both functions are STABLE. The device fix
--      is never persisted as a new home.
--   4. Degrades gracefully. No device fix (permission denied) => home origin,
--      should_offer_away=false, zero prompts.
--
-- NON-DESTRUCTIVE: three brand-new functions, no name collisions (verified).
-- See the ROLLBACK section at the foot of this file.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. humanize_distance_text(meters, locale) -> text
--
-- Why this exists: get_nearby_activities hard-codes
--     concat(greatest(1, round(st_distance(...) / 80)::int), ' min walk')
-- with NO radius cap. A user in Silicon Valley querying the Orlando event set
-- gets real rows back rendered as "50000 min walk". That is the visible symptom
-- of the same bug. This helper renders an honest label at any distance.
--
-- Locale set is EN/PT/ES (the launch set). EN uses miles, PT/ES use km.
-- ----------------------------------------------------------------------------
create or replace function public.humanize_distance_text(
  p_meters double precision,
  p_locale text default 'en'
)
returns text
language plpgsql
immutable
set search_path to 'pg_catalog', 'public'
as $function$
declare
  v_lang text := lower(coalesce(nullif(split_part(coalesce(p_locale, 'en'), '-', 1), ''), 'en'));
  v_mins int;
  v_mi numeric;
  v_km numeric;
begin
  if p_meters is null or p_meters < 0 then
    return null;
  end if;

  -- Walking range only. 80 m/min is the existing product constant.
  if p_meters <= 1600 then
    v_mins := greatest(1, round(p_meters / 80)::int);
    return case v_lang
      when 'es' then v_mins || ' min caminando'
      when 'pt' then v_mins || ' min a pé'
      else v_mins || ' min walk'
    end;
  end if;

  -- NOTE: to_char with the FM modifier strips trailing zeros but LEAVES a dangling
  -- '.' on whole numbers ("2548." ). rtrim removes it. Caught in PR7 verification.
  if v_lang in ('es', 'pt') then
    v_km := round((p_meters / 1000.0)::numeric, case when p_meters < 16000 then 1 else 0 end);
    return 'a ' || rtrim(trim(to_char(v_km, 'FM999999990.9')), '.') || ' km';
  end if;

  v_mi := round((p_meters / 1609.344)::numeric, case when p_meters < 16000 then 1 else 0 end);
  return rtrim(trim(to_char(v_mi, 'FM999999990.9')), '.') || ' mi away';
end;
$function$;

comment on function public.humanize_distance_text(double precision, text) is
  'PR7: honest distance label at any range. Replaces the uncapped "N min walk" '
  'string baked into get_nearby_activities, which renders cross-country distances '
  'as absurd walk times. EN=miles, PT/ES=km, walking only under 1600m.';

grant execute on function public.humanize_distance_text(double precision, text)
  to anon, authenticated, service_role;


-- ----------------------------------------------------------------------------
-- 2. resolve_search_origin(...) -> the away-from-home decision
--
-- THE CORE OF THIS PR. One call answers: "where should I search, and do I need
-- to ask first?"
--
-- Returns exactly one row, always. Never raises for a missing device fix or a
-- missing home -- those are normal states with defined answers.
--
-- CONTRACT
--   origin_lat / origin_lng   the point the caller should actually search around.
--   origin_source             'home' | 'device' | 'none'
--                             'device' ONLY when p_use_device = true (the user
--                             confirmed) or when no home exists at all.
--   should_offer_away         true => Lana must ASK before switching. This is the
--                             ONLY way a device origin is ever reached.
--   distance_from_home_meters null when either endpoint is unknown.
--   home_label / away_label   copy fodder for the acknowledgement line, e.g.
--                             "You're away from {home_label} -- want me to look
--                              around {away_label} instead?"
--   device_fix_usable         false when the fix is absent OR too imprecise.
--
-- ACCURACY GUARD (p_device_accuracy_meters)
--   Browser geolocation falls back to IP lookup when GPS/wifi is unavailable,
--   which is routinely 10-50km off and often lands on a datacenter. Acting on
--   that would fire a false "you're away from home!" prompt at a user sitting on
--   their own couch -- the single worst failure mode for this feature. Any fix
--   with accuracy worse than p_max_accuracy_meters is discarded outright, exactly
--   as if permission had been denied.
--
-- IDOR GUARD
--   SECURITY DEFINER + explicit p_user_id. When called with a user JWT
--   (auth.uid() is not null) the id must match the caller. The worker's service
--   role has auth.uid() = null and is trusted to pass any id.
-- ----------------------------------------------------------------------------
create or replace function public.resolve_search_origin(
  p_user_id uuid,
  p_device_lat double precision default null,
  p_device_lng double precision default null,
  p_device_accuracy_meters double precision default null,
  p_use_device boolean default false,
  p_away_threshold_meters double precision default 40000,   -- ~25 miles
  p_max_accuracy_meters double precision default 25000,     -- ~15 miles
  p_locale text default 'en'
)
returns table(
  origin_lat double precision,
  origin_lng double precision,
  origin_source text,
  should_offer_away boolean,
  is_away boolean,
  distance_from_home_meters double precision,
  distance_from_home_text text,
  home_lat double precision,
  home_lng double precision,
  home_zip5 text,
  home_block_id text,
  home_label text,
  away_label text,
  device_fix_usable boolean,
  device_block_id text,
  device_cluster_id text
)
language plpgsql
stable
security definer
set search_path to 'pg_catalog', 'public', 'extensions'
as $function$
declare
  v_caller uuid := auth.uid();
  v_home_zip5 text;
  v_home_block_id text;
  v_home_lat double precision;
  v_home_lng double precision;
  v_home_label text;
  v_device_ok boolean := false;
  v_dist double precision;
  v_away_label text;
  v_dev_block_id text;
  v_dev_cluster_id text;
  v_origin_lat double precision;
  v_origin_lng double precision;
  v_origin_source text;
  v_should_offer boolean := false;
  v_is_away boolean := false;
begin
  if p_user_id is null then
    raise exception 'user_id_required' using errcode = 'P0001';
  end if;
  -- A user JWT may only resolve its own origin. Service role (auth.uid() null) is trusted.
  if v_caller is not null and v_caller <> p_user_id then
    raise exception 'forbidden' using errcode = 'P0001';
  end if;

  -- ---- Resolve HOME (the default, unchanged behaviour) ---------------------
  select public.normalize_zip5(u.home_zip), u.home_block_id
    into v_home_zip5, v_home_block_id
  from public.users u
  where u.id = p_user_id;

  -- Preference order mirrors places.py::_centroid and event_location.py::
  -- resolve_event_location: ZIP centroid first, then the block centroid.
  if v_home_zip5 is not null then
    select z.lat, z.lng into v_home_lat, v_home_lng
    from public.zip_centroids z
    where z.zip5 = v_home_zip5;
  end if;

  if (v_home_lat is null or v_home_lng is null) and v_home_block_id is not null then
    select extensions.st_y(b.centroid::extensions.geometry),
           extensions.st_x(b.centroid::extensions.geometry)
      into v_home_lat, v_home_lng
    from public.blocks b
    where b.id = v_home_block_id and b.centroid is not null;
  end if;

  -- Human label for the home area: the block's display name beats a bare ZIP.
  if v_home_block_id is not null then
    select nullif(trim(b.display_name), '') into v_home_label
    from public.blocks b where b.id = v_home_block_id;
  end if;
  v_home_label := coalesce(v_home_label, v_home_zip5);

  -- ---- Validate the DEVICE fix --------------------------------------------
  -- Absent, out-of-range, or too imprecise => treated exactly like "permission
  -- denied": we silently keep home and never prompt.
  v_device_ok := p_device_lat is not null
             and p_device_lng is not null
             and p_device_lat between -90 and 90
             and p_device_lng between -180 and 180
             and not (p_device_lat = 0 and p_device_lng = 0)   -- null island
             and (p_device_accuracy_meters is null
                  or p_device_accuracy_meters <= greatest(0, coalesce(p_max_accuracy_meters, 25000)));

  -- ---- Distance + away decision -------------------------------------------
  if v_device_ok and v_home_lat is not null and v_home_lng is not null then
    v_dist := extensions.st_distance(
      extensions.st_setsrid(extensions.st_makepoint(v_home_lng, v_home_lat), 4326)::extensions.geography,
      extensions.st_setsrid(extensions.st_makepoint(p_device_lng, p_device_lat), 4326)::extensions.geography
    )::double precision;
    v_is_away := v_dist > greatest(0, coalesce(p_away_threshold_meters, 40000));
  end if;

  -- ---- Name where the device actually is (copy fodder for the ask) --------
  -- NOTE: deliberately NOT using public.resolve_nearest_block_id() here. That
  -- function is hard-pinned to p_cluster_id default 'lake-nona', so for a device
  -- in Silicon Valley it cheerfully returns an Orlando block -- the exact class
  -- of bug this PR exists to kill. We search blocks globally instead.
  if v_device_ok then
    select b.id, b.cluster_id
      into v_dev_block_id, v_dev_cluster_id
    from public.blocks b
    where b.centroid is not null
    order by extensions.st_distance(
      b.centroid,
      extensions.st_setsrid(extensions.st_makepoint(p_device_lng, p_device_lat), 4326)::extensions.geography
    )
    limit 1;

    -- Only claim the nearest block as a label if it is genuinely nearby.
    select nullif(trim(b.display_name), '') into v_away_label
    from public.blocks b
    where b.id = v_dev_block_id
      and extensions.st_distance(
            b.centroid,
            extensions.st_setsrid(extensions.st_makepoint(p_device_lng, p_device_lat), 4326)::extensions.geography
          ) <= 40000;

    -- Fall back to the nearest known ZIP, still only when genuinely nearby.
    if v_away_label is null then
      select z.zip5 into v_away_label
      from public.zip_centroids z
      where extensions.st_distance(
              extensions.st_setsrid(extensions.st_makepoint(z.lng, z.lat), 4326)::extensions.geography,
              extensions.st_setsrid(extensions.st_makepoint(p_device_lng, p_device_lat), 4326)::extensions.geography
            ) <= 40000
      order by extensions.st_distance(
              extensions.st_setsrid(extensions.st_makepoint(z.lng, z.lat), 4326)::extensions.geography,
              extensions.st_setsrid(extensions.st_makepoint(p_device_lng, p_device_lat), 4326)::extensions.geography
            )
      limit 1;
    end if;
    -- v_away_label may stay null: Lana then says "where you are" instead of a
    -- place name. Never invent a location name we cannot substantiate.
  end if;

  -- ---- Choose the origin ---------------------------------------------------
  if v_home_lat is null or v_home_lng is null then
    -- No home at all. A device fix is strictly better than nothing, and there is
    -- nothing to "override", so no prompt is warranted.
    if v_device_ok then
      v_origin_lat := p_device_lat;
      v_origin_lng := p_device_lng;
      v_origin_source := 'device';
    else
      v_origin_source := 'none';
    end if;
  elsif v_device_ok and v_is_away and coalesce(p_use_device, false) then
    -- The user was asked and said yes. This is the ONLY confirmed-override path.
    v_origin_lat := p_device_lat;
    v_origin_lng := p_device_lng;
    v_origin_source := 'device';
  else
    -- DEFAULT. Home wins. If they are far away we flag it so Lana can ask, but
    -- we do NOT move them. No silent teleporting.
    v_origin_lat := v_home_lat;
    v_origin_lng := v_home_lng;
    v_origin_source := 'home';
    v_should_offer := v_device_ok and v_is_away and not coalesce(p_use_device, false);
  end if;

  return query select
    v_origin_lat,
    v_origin_lng,
    v_origin_source,
    v_should_offer,
    v_is_away,
    v_dist,
    public.humanize_distance_text(v_dist, p_locale),
    v_home_lat,
    v_home_lng,
    v_home_zip5,
    v_home_block_id,
    v_home_label,
    v_away_label,
    v_device_ok,
    v_dev_block_id,
    v_dev_cluster_id;
end;
$function$;

comment on function public.resolve_search_origin(uuid, double precision, double precision, double precision, boolean, double precision, double precision, text) is
  'PR7: decides whether a search anchors on the user home block (default) or an '
  'ephemeral device fix (override). Never switches on its own -- returns '
  'should_offer_away=true so Lana asks first. Nothing is persisted. Discards '
  'device fixes worse than p_max_accuracy_meters (IP-geolocation noise).';

grant execute on function public.resolve_search_origin(uuid, double precision, double precision, double precision, boolean, double precision, double precision, text)
  to authenticated, service_role;


-- ----------------------------------------------------------------------------
-- 3. get_activities_near_point(...) -> radius-capped activity search
--
-- The lat/lng-honest sibling of get_nearby_activities, with the two fixes the
-- original needs but cannot receive without a breaking signature change:
--
--   (a) A REAL RADIUS CAP. get_nearby_activities has none -- it orders the whole
--       open-event set by distance and returns the top N. For a user in Silicon
--       Valley that means Orlando events, ranked "nearest first", 4,000km away.
--   (b) An honest distance label via humanize_distance_text().
--
-- It is also NOT block-scoped, which is the fix for
-- discovery_route.fetch_preview_events_on_block's `.eq("block_id", block_id)`:
-- events on an adjacent block that are physically closer than events on your own
-- block are currently invisible. Radius beats equality.
--
-- Output columns are a superset-compatible reshaping of get_nearby_activities;
-- the original is left untouched for its existing caller (look_meet.py:456).
-- ----------------------------------------------------------------------------
create or replace function public.get_activities_near_point(
  p_lat double precision,
  p_lng double precision,
  p_radius_meters double precision default 40000,
  p_window interval default '14 days'::interval,
  p_locale text default 'en',
  p_limit integer default 20
)
returns table(
  id uuid,
  host_id uuid,
  title text,
  description text,
  starts_at timestamp with time zone,
  has_time boolean,
  ends_at timestamp with time zone,
  duration_minutes integer,
  venue_name text,
  block_id text,
  cohort_tags text[],
  max_attendees integer,
  status text,
  cover_image_url text,
  cover_emoji text,
  distance_meters double precision,
  distance_text text,
  participant_count integer,
  maybe_count integer
)
language plpgsql
stable
security definer
set search_path to 'pg_catalog', 'public', 'extensions'
as $function$
declare
  v_point extensions.geography;
  v_radius double precision := greatest(100, least(coalesce(p_radius_meters, 40000), 200000));
  -- Region-stripped locale. humanize_distance_text() normalizes internally, so
  -- without this the same p_locale means two different things in one call:
  -- 'pt-BR' would pick the Portuguese distance label while missing every
  -- pt translation and silently falling back to the English title.
  v_lang text := lower(coalesce(nullif(split_part(coalesce(p_locale, 'en'), '-', 1), ''), 'en'));
begin
  if p_lat is null or p_lng is null then
    raise exception 'location_required' using errcode = 'P0001';
  end if;
  if p_lat not between -90 and 90 or p_lng not between -180 and 180 then
    raise exception 'invalid_location' using errcode = 'P0001';
  end if;

  v_point := extensions.st_setsrid(extensions.st_makepoint(p_lng, p_lat), 4326)::extensions.geography;

  return query
  select
    e.id,
    e.host_id,
    -- Exact tag first ('pt-BR' if it was ever stored that way), then the base
    -- language, then the English-canonical column.
    coalesce(
      e.title_translations->>p_locale,
      e.title_translations->>v_lang,
      e.title
    ) as title,
    coalesce(
      e.description_translations->>p_locale,
      e.description_translations->>v_lang,
      e.description
    ) as description,
    e.starts_at,
    e.has_time,
    e.ends_at,
    case
      when e.ends_at is null then null
      else greatest(round(extract(epoch from e.ends_at - e.starts_at) / 60)::int, 1)
    end as duration_minutes,
    e.venue_name,
    e.block_id,
    e.cohort_tags,
    e.max_attendees,
    e.status,
    e.cover_image_url,
    e.cover_emoji,
    extensions.st_distance(e.location, v_point)::double precision as distance_meters,
    public.humanize_distance_text(
      extensions.st_distance(e.location, v_point)::double precision, p_locale
    ) as distance_text,
    coalesce((
      select count(*)::int from public.event_requests er
      where er.event_id = e.id
        and er.status in ('approved', 'attended')
        and er.rsvp_status = 'going'
    ), 0) as participant_count,
    coalesce((
      select count(*)::int from public.event_requests er
      where er.event_id = e.id
        and er.status in ('approved', 'attended')
        and er.rsvp_status = 'maybe'
    ), 0) as maybe_count
  from public.events e
  where e.status = 'open'
    and e.location is not null
    and e.starts_at between now() and now() + p_window
    -- THE CAP. st_dwithin is index-assisted (GiST on geography).
    and extensions.st_dwithin(e.location, v_point, v_radius)
  order by extensions.st_distance(e.location, v_point) asc, e.starts_at asc
  limit greatest(1, least(coalesce(p_limit, 20), 50));
end;
$function$;

comment on function public.get_activities_near_point(double precision, double precision, double precision, interval, text, integer) is
  'PR7: radius-capped, block-agnostic activity search. Fixes two gaps in '
  'get_nearby_activities: no radius cap (returns cross-country events ranked '
  '"nearest") and an uncapped "N min walk" label. Not block-scoped, so it also '
  'supersedes fetch_preview_events_on_block''s .eq("block_id") equality filter.';

-- anon is deliberate, for parity with the existing get_nearby_activities (also
-- SECURITY DEFINER, also anon-granted) which the signed-out preview relies on.
-- Consequence to be aware of: definer bypasses RLS, so an anonymous caller can
-- enumerate open events anywhere by feeding arbitrary coordinates. Drop anon here
-- if that preview ever stops needing it.
grant execute on function public.get_activities_near_point(double precision, double precision, double precision, interval, text, integer)
  to anon, authenticated, service_role;


-- ============================================================================
-- ROLLBACK
-- ----------------------------------------------------------------------------
-- All three functions are NEW (verified: zero name collisions in public before
-- this migration). No existing object is altered, so rollback is a clean drop
-- and cannot restore-clobber anything.
--
--   drop function if exists public.get_activities_near_point(
--     double precision, double precision, double precision, interval, text, integer);
--   drop function if exists public.resolve_search_origin(
--     uuid, double precision, double precision, double precision, boolean,
--     double precision, double precision, text);
--   drop function if exists public.humanize_distance_text(double precision, text);
--
-- Drop in that order (get_activities_near_point and resolve_search_origin both
-- depend on humanize_distance_text).
--
-- Worker-side rollback is independent: reverting the lana-worker deploy restores
-- the home-block behaviour regardless of whether these functions exist, because
-- nothing existing calls them.
-- ============================================================================
