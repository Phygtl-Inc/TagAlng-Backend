-- ============================================================================
-- Meet read surfaces: honest distance back on the previews, the matched threads and a
-- ranked fit on every meet, and the community a meet is FOR on three more payloads.
--
-- §52  get_event_preview[_authed] lost the honest-distance fix. 20261015120000 copied
--      both bodies off a pre-20260926120000 source and brought back
--          concat(greatest(1, round(v_distance / 80)::int), ' min walk')
--      ('164069 min walk' for a meet 13,000 km away). Nothing after it redefines either
--      function, so it is in force. Re-applied: public.humanize_distance_text(m, locale).
--
-- §50(a) get_event_preview_authed shipped only the CARDINALITY of the viewer/meet
--      intersection. It now ships the intersection itself, `affinity_matched_tags`
--      (text[], label-mapped like cohort_tags, in the meet's tag order). Anon sends [].
--
-- §54  `fit_score` (0-1, null = unscored) on get_nearby_activities_authed and on the
--      authed preview, and — through public.score_events_fit_for_user — on
--      /lana/circles/profile's upcoming_events. All three read ONE helper,
--      public.event_viewer_fit, so a meet carries the same score by radius and by
--      community, and the meter can never claim more than the chips.
--
--      THE SCORE. A ladder over the PROVEN shared threads, the same one the peer card's
--      badge uses (peer_discovery_surface.match_badge: 1 = FIT, 2 = STRONG, 3+ = PERFECT
--      FIT), placed so the PWA's scoreBand lands on the same bands:
--          0 shared -> 0.0   1 -> 0.6   2 -> 0.8   3+ -> 1.0
--      It is a rank, not matched/total: one tag out of one is a FIT, not a perfect fit,
--      and a meet sharing three threads with her outranks a one-tag meet whatever their
--      sizes. null when the viewer holds no public claims (nothing to score — the
--      acceptance says null, not 0) or the meet has no tags (nothing to score against).
--
--      ONE MATCH RULE. The nearby RPC matched with cohort_tag_matches_claim (concept,
--      synonym, or an event-purpose cohort's affinity_concepts); the preview matched on
--      concept/synonym only. The same meet could read 1/3 on the marker and 0/3 on its
--      card. Both now use cohort_tag_matches_claim via the helper, so the preview's
--      affinity_match_count can only go UP for a meet tagged with an event-purpose cohort.
--
-- §26(b) get_my_contributions: `community` (event_community's object, null on signals and
--      on plain neighbourhood meets) and `cover_emoji` (never projected here, although the
--      PWA's contributionRowSchema has parsed it since 20260817) on every meet row.
-- §26(c) get_peer_profile.upcoming_shared_events[]: `community`, same object.
--
-- Every redefined body was extracted verbatim from the latest migration that defines it
-- and edited with counted, asserted substitutions — nothing else moves.
--
-- Sources: get_nearby_activities_authed      <- 20260926120000_honest_distance_labels
--          get_event_preview / _authed       <- 20261015120000_event_community_surface
--          get_my_contributions              <- 20261206120000_contributions_reco_title
--          get_peer_profile                  <- 20261102120000_peer_profile_restore_portrait
--
-- ROLLBACK: re-run the five source definitions above (get_nearby_activities_authed needs
-- its drop first: the return type shrinks), then
--   drop function if exists public.score_events_fit_for_user(uuid, uuid[]);
--   drop function if exists public.event_viewer_fit(text[], uuid);
-- ============================================================================

-- ---------------------------------------------------------------------------
-- event_viewer_fit — one viewer against one meet's tags: what they share, and a rank.
-- ---------------------------------------------------------------------------
create or replace function public.event_viewer_fit(
  p_tags text[],
  p_viewer uuid
)
returns table (
  matched_tags text[],
  matched_count int,
  total_count int,
  fit_score numeric
)
language sql
stable
set search_path = pg_catalog, public
as $$
  with claims as (
    select c.concept, c.synonyms
    from public.user_identity_claims c
    where c.user_id = p_viewer
      and c.dismissed_at is null
      and c.disclosure = 'public'
  ),
  tags as (
    select t.tag, min(t.ord) as ord
    from unnest(coalesce(p_tags, '{}'::text[])) with ordinality as t(tag, ord)
    group by t.tag
  ),
  hit as (
    select tg.tag, tg.ord
    from tags tg
    where exists (
      select 1
      from claims c
      where public.cohort_tag_matches_claim(tg.tag, c.concept, c.synonyms)
    )
  )
  select
    coalesce((select array_agg(h.tag order by h.ord) from hit h), '{}'::text[]),
    (select count(*)::int from hit),
    cardinality(coalesce(p_tags, '{}'::text[])),
    case
      when p_viewer is null or not exists (select 1 from claims) then null
      when cardinality(coalesce(p_tags, '{}'::text[])) = 0 then null
      else case least((select count(*) from hit), 3)
        when 0 then 0.0
        when 1 then 0.6
        when 2 then 0.8
        else 1.0
      end
    end::numeric;
$$;

comment on function public.event_viewer_fit(text[], uuid) is
  'The viewer''s public claims against a meet''s cohort_tags (cohort_tag_matches_claim): '
  'matched raw tags in the meet''s order, their count, the meet''s tag count, and fit_score '
  '— 0/0.6/0.8/1.0 for 0/1/2/3+ proven shared threads, null when the viewer has no public '
  'claims or the meet no tags. The ONE intersection behind every meet''s count, chips and '
  'score (get_nearby_activities_authed, get_event_preview_authed, score_events_fit_for_user).';

-- Takes an arbitrary viewer id, so it must never be client-callable: it would let anyone
-- probe someone else's claims against tags of their choosing. SECURITY DEFINER callers run
-- it as their owner.
revoke all on function public.event_viewer_fit(text[], uuid) from public, anon, authenticated;
grant execute on function public.event_viewer_fit(text[], uuid) to service_role;

-- ---------------------------------------------------------------------------
-- score_events_fit_for_user — the worker's batch read of the same helper, for meets it
-- lists itself (/lana/circles/profile upcoming_events, the event fit line).
-- ---------------------------------------------------------------------------
create or replace function public.score_events_fit_for_user(
  p_user_id uuid,
  p_event_ids uuid[]
)
returns table (
  event_id uuid,
  affinity_matched_tags text[],
  affinity_match_count int,
  affinity_total_count int,
  fit_score numeric
)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select
    e.id,
    public.cohort_tag_labels(f.matched_tags),
    f.matched_count,
    f.total_count,
    f.fit_score
  from public.events e
  cross join lateral public.event_viewer_fit(e.cohort_tags, p_user_id) f
  where e.id = any(coalesce(p_event_ids, '{}'::uuid[]));
$$;

comment on function public.score_events_fit_for_user(uuid, uuid[]) is
  'Per-meet viewer fit for meets the worker lists itself: label-mapped matched threads, '
  'counts and fit_score, from public.event_viewer_fit. service_role only.';

revoke all on function public.score_events_fit_for_user(uuid, uuid[]) from public, anon, authenticated;
grant execute on function public.score_events_fit_for_user(uuid, uuid[]) to service_role;

-- ---------------------------------------------------------------------------
-- §54 get_nearby_activities_authed — body verbatim from 20260926120000 apart from the
-- lateral (now public.event_viewer_fit) and the new fit_score column. The return type
-- grows, so drop + create + re-grant, exactly as 20260829120000 had to.
-- ---------------------------------------------------------------------------
drop function if exists public.get_nearby_activities_authed(double precision, double precision, text, interval, text, int);

create or replace function public.get_nearby_activities_authed(
  p_lat double precision default null,
  p_lng double precision default null,
  p_zip text default null,
  p_window interval default '14 days',
  p_locale text default 'en',
  p_limit int default 20
)
returns table (
  id uuid,
  host_id uuid,
  title text,
  description text,
  starts_at timestamptz,
  has_time boolean,
  ends_at timestamptz,
  duration_minutes int,
  venue_name text,
  cohort_tags text[],
  max_attendees integer,
  status text,
  cover_image_url text,
  cover_emoji text,
  distance_meters double precision,
  distance_text text,
  affinity_match_count int,
  affinity_total_count int,
  affinity_match_label text,
  fit_score numeric,
  participant_count int,
  maybe_count int,
  my_request_status text,
  my_rsvp_status text,
  participant_preview jsonb
)
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
as $$
declare
  v_caller uuid := auth.uid();
  v_lat double precision := p_lat;
  v_lng double precision := p_lng;
  v_zip5 text;
  v_point extensions.geography;
begin
  if v_caller is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  if v_lat is null or v_lng is null then
    v_zip5 := public.normalize_zip5(p_zip);
    if v_zip5 is null then
      raise exception 'location_required' using errcode = 'P0001';
    end if;

    select z.lat, z.lng
    into v_lat, v_lng
    from public.zip_centroids z
    where z.zip5 = v_zip5;

    if not found then
      raise exception 'zip_not_found' using errcode = 'P0001';
    end if;
  end if;

  v_point := extensions.st_setsrid(extensions.st_makepoint(v_lng, v_lat), 4326)::extensions.geography;

  return query
  select
    e.id,
    e.host_id,
    coalesce(e.title_translations->>p_locale, e.title) as title,
    coalesce(e.description_translations->>p_locale, e.description) as description,
    e.starts_at,
    e.has_time,
    e.ends_at,
    case
      when e.ends_at is null then null
      else greatest(round(extract(epoch from e.ends_at - e.starts_at) / 60)::int, 1)
    end as duration_minutes,
    e.venue_name,
    e.cohort_tags,
    e.max_attendees,
    e.status,
    e.cover_image_url,
    e.cover_emoji,
    extensions.st_distance(e.location, v_point)::double precision as distance_meters,
    public.humanize_distance_text(
      extensions.st_distance(e.location, v_point)::double precision, p_locale
    ) as distance_text,
    am.matched_count as affinity_match_count,
    cardinality(coalesce(e.cohort_tags, '{}')) as affinity_total_count,
    case
      when cardinality(coalesce(e.cohort_tags, '{}')) = 0 then null
      else concat(am.matched_count, '/', cardinality(coalesce(e.cohort_tags, '{}')), ' affinities match')
    end as affinity_match_label,
    -- 0-1 over the same proven intersection as affinity_match_count; null = unscored
    -- (no public claims, or an untagged meet). See public.event_viewer_fit.
    am.fit_score as fit_score,
    coalesce((
      select count(*)::int
      from public.event_requests er
      where er.event_id = e.id
        and er.status in ('approved', 'attended')
        and er.rsvp_status = 'going'
    ), 0) as participant_count,
    coalesce((
      select count(*)::int
      from public.event_requests er
      where er.event_id = e.id
        and er.status in ('approved', 'attended')
        and er.rsvp_status = 'maybe'
    ), 0) as maybe_count,
    (
      select er.status
      from public.event_requests er
      where er.event_id = e.id
        and er.requester_id = v_caller
    ) as my_request_status,
    (
      select er.rsvp_status
      from public.event_requests er
      where er.event_id = e.id
        and er.requester_id = v_caller
    ) as my_rsvp_status,
    coalesce((
      select jsonb_agg(jsonb_build_object(
        'user_id', p.id,
        'nickname', p.nickname,
        'avatar_url', p.avatar_url,
        'is_blurred', false,
        'event_count', coalesce(p.event_count, 0),
        'weeks_here', p.weeks_here,
        'about_tags', p.about_tags,
        'shared_claim_count', p.shared_claim_count
      ) order by p.shared_claim_count desc, p.event_count desc, p.weeks_here desc)
      from (
        select
          u.id,
          u.nickname,
          u.profile_photo_url as avatar_url,
          (select count(*) from public.event_requests er2
            where er2.requester_id = u.id
              and er2.status in ('approved','attended')) as event_count,
          floor(extract(epoch from now() - u.created_at) / 604800)::int as weeks_here,
          coalesce((
            select jsonb_agg(sub.label order by sub.confidence desc)
            from (
              select distinct c.label, c.confidence
              from public.user_identity_claims c
              where c.user_id = u.id
                and c.dismissed_at is null
                and c.disclosure = 'public'
              order by c.confidence desc
              limit 5
            ) sub
          ), '[]'::jsonb) as about_tags,
          coalesce((
            select count(*)::int
            from public.user_identity_claims c1
            join public.user_identity_claims c2 on c1.concept = c2.concept
            where c1.user_id = v_caller
              and c2.user_id = u.id
              and c1.dismissed_at is null
              and c2.dismissed_at is null
              and c1.disclosure = 'public'
              and c2.disclosure = 'public'
          ), 0) as shared_claim_count
        from public.users u
        join public.event_requests er on er.requester_id = u.id
        where er.event_id = e.id
          and er.status in ('approved', 'attended')
          and er.rsvp_status = 'going'
        order by event_count desc, u.created_at asc
        limit 6
      ) p
    ), '[]'::jsonb) as participant_preview
  from public.events e
  left join lateral public.event_viewer_fit(e.cohort_tags, v_caller) am on true
  where e.status = 'open'
    and e.location is not null
    and e.starts_at between now() and now() + p_window
  order by distance_meters asc
  limit greatest(1, least(coalesce(p_limit, 20), 50));
end;
$$;

revoke all on function public.get_nearby_activities_authed(double precision, double precision, text, interval, text, int) from public, anon;
grant execute on function public.get_nearby_activities_authed(double precision, double precision, text, interval, text, int) to authenticated;

-- ---------------------------------------------------------------------------
-- §52 + §50(a) get_event_preview / _authed — bodies verbatim from 20261015120000 (the
-- definition in force; nothing later redefines either) apart from humanize_distance_text
-- and the affinity_matched_tags / fit_score keys. Same signatures: grants carry over.
-- ---------------------------------------------------------------------------
create or replace function public.get_event_preview(
  p_event_id uuid,
  p_lat double precision default null,
  p_lng double precision default null,
  p_locale text default 'en'
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
as $$
declare
  v_event record;
  v_point extensions.geography;
  v_distance double precision;
  v_distance_text text;
  v_participants jsonb;
  v_total int;
begin
  begin
    perform public.roll_recurring_events(p_event_id);
  exception when others then null;
  end;

  select
    e.id, e.host_id,
    coalesce(e.title_translations->>p_locale, e.title) as title,
    coalesce(e.description_translations->>p_locale, e.description) as description,
    e.starts_at, e.has_time, e.ends_at, e.location, e.venue_name, e.venue_address, e.place_id, e.cohort_tags,
    e.max_attendees, e.bring_items, e.status, e.cover_image_url, e.cover_emoji,
    e.recurrence, e.recurrence_until, e.circle_place_ref
  into v_event
  from public.events e
  where e.id = p_event_id
    and e.status in ('open', 'cancelled');

  if not found then
    raise exception 'event_not_found' using errcode = 'P0001';
  end if;

  if p_lat is not null and p_lng is not null then
    v_point := extensions.st_setsrid(extensions.st_makepoint(p_lng, p_lat), 4326)::extensions.geography;
    v_distance := extensions.st_distance(v_event.location, v_point)::double precision;
    v_distance_text := public.humanize_distance_text(v_distance, p_locale);
  else
    v_distance := null;
    v_distance_text := null;
  end if;

  v_total := cardinality(coalesce(v_event.cohort_tags, '{}'));

  select coalesce(jsonb_agg(jsonb_build_object(
      'user_id', null,
      'nickname', null,
      'avatar_url', null,
      'is_blurred', true,
      'event_count', p.event_count,
      'weeks_here', p.weeks_here,
      'about_tags', p.about_tags,
      'shared_claim_count', 0
    ) order by p.event_count desc, p.weeks_here desc), '[]'::jsonb)
  into v_participants
  from (
    select
      u.id,
      (select count(*) from public.event_requests er2
        where er2.requester_id = u.id
          and er2.status in ('approved', 'attended')) as event_count,
      floor(extract(epoch from now() - u.created_at) / 604800)::int as weeks_here,
      coalesce((
        select jsonb_agg(sub.label order by sub.confidence desc)
        from (
          select distinct c.label, c.confidence
          from public.user_identity_claims c
          where c.user_id = u.id
            and c.dismissed_at is null
            and c.disclosure = 'public'
          order by c.confidence desc
          limit 5
        ) sub
      ), '[]'::jsonb) as about_tags
    from public.users u
    join public.event_requests er on er.requester_id = u.id
    where er.event_id = p_event_id
      and er.status in ('approved', 'attended')
      and er.rsvp_status = 'going'
    order by event_count desc, u.created_at asc
    limit 20
  ) p;

  return jsonb_build_object(
    'event_id', v_event.id,
    'host_id', v_event.host_id,
    'title', v_event.title,
    'description', v_event.description,
    'starts_at', v_event.starts_at,
    'has_time', v_event.has_time,
    'ends_at', v_event.ends_at,
    'duration_minutes', case when v_event.ends_at is null then null else greatest(round(extract(epoch from v_event.ends_at - v_event.starts_at) / 60)::int, 1) end,
    'venue_name', v_event.venue_name,
    'venue_address', v_event.venue_address,
    'place_id', v_event.place_id,
    'cohort_tags', public.cohort_tag_labels(v_event.cohort_tags),
    'max_attendees', v_event.max_attendees,
    'bring_items', coalesce(v_event.bring_items, '{}'),
    'status', v_event.status,
    'cover_image_url', v_event.cover_image_url,
    'cover_emoji', v_event.cover_emoji,
    'recurrence', v_event.recurrence,
    'recurrence_until', v_event.recurrence_until,
    'community', public.event_community(v_event.circle_place_ref, v_event.host_id),
    'distance_meters', v_distance,
    'distance_text', v_distance_text,
    'affinity_match_count', v_total,
    'affinity_match_label', case when v_total > 0 then concat(v_total, ' affinities') else null end,
    -- A signed-out viewer has no claims to intersect: no threads, no score — never the
    -- event's own tags dressed up as a match (20260901120000).
    'affinity_matched_tags', '[]'::jsonb,
    'fit_score', null,
    'is_authenticated', false,
    'participant_count', coalesce((select count(*) from public.event_requests er where er.event_id = p_event_id and er.status in ('approved', 'attended') and er.rsvp_status = 'going'), 0),
    'maybe_count', coalesce((select count(*) from public.event_requests er where er.event_id = p_event_id and er.status in ('approved', 'attended') and er.rsvp_status = 'maybe'), 0),
    'participants', v_participants
  );
end;
$$;

create or replace function public.get_event_preview_authed(
  p_event_id uuid,
  p_lat double precision default null,
  p_lng double precision default null,
  p_locale text default 'en'
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
as $$
declare
  v_caller uuid := auth.uid();
  v_event record;
  v_point extensions.geography;
  v_distance double precision;
  v_distance_text text;
  v_participants jsonb;
  v_total int;
  v_matched int;
  v_matched_tags text[];
  v_fit numeric;
  v_my_status text;
  v_my_rsvp text;
begin
  if v_caller is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  begin
    perform public.roll_recurring_events(p_event_id);
  exception when others then null;
  end;

  select
    e.id, e.host_id,
    coalesce(e.title_translations->>p_locale, e.title) as title,
    coalesce(e.description_translations->>p_locale, e.description) as description,
    e.starts_at, e.has_time, e.ends_at, e.location, e.venue_name, e.venue_address, e.place_id, e.cohort_tags,
    e.max_attendees, e.bring_items, e.status, e.cover_image_url, e.cover_emoji,
    e.recurrence, e.recurrence_until, e.circle_place_ref
  into v_event
  from public.events e
  where e.id = p_event_id
    and e.status in ('open', 'cancelled');

  if not found then
    raise exception 'event_not_found' using errcode = 'P0001';
  end if;

  if p_lat is not null and p_lng is not null then
    v_point := extensions.st_setsrid(extensions.st_makepoint(p_lng, p_lat), 4326)::extensions.geography;
    v_distance := extensions.st_distance(v_event.location, v_point)::double precision;
    v_distance_text := public.humanize_distance_text(v_distance, p_locale);
  else
    v_distance := null;
    v_distance_text := null;
  end if;

  v_total := cardinality(coalesce(v_event.cohort_tags, '{}'));

  -- One intersection for the count, the chips and the score — the same helper the map's
  -- get_nearby_activities_authed and /lana/circles/profile read, so the card, the marker
  -- and the community-scoped marker can never disagree about the same meet.
  select f.matched_count, f.matched_tags, f.fit_score
  into v_matched, v_matched_tags, v_fit
  from public.event_viewer_fit(v_event.cohort_tags, v_caller) f;

  select er.status, er.rsvp_status
  into v_my_status, v_my_rsvp
  from public.event_requests er
  where er.event_id = p_event_id
    and er.requester_id = v_caller;

  select coalesce(jsonb_agg(jsonb_build_object(
      'user_id', p.id,
      'nickname', p.nickname,
      'avatar_url', p.avatar_url,
      'is_blurred', false,
      'event_count', p.event_count,
      'weeks_here', p.weeks_here,
      'about_tags', p.about_tags,
      'shared_claim_count', p.shared_claim_count
    ) order by p.shared_claim_count desc, p.event_count desc, p.weeks_here desc), '[]'::jsonb)
  into v_participants
  from (
    select
      u.id,
      u.nickname,
      u.profile_photo_url as avatar_url,
      (select count(*) from public.event_requests er2
        where er2.requester_id = u.id
          and er2.status in ('approved', 'attended')) as event_count,
      floor(extract(epoch from now() - u.created_at) / 604800)::int as weeks_here,
      coalesce((
        select jsonb_agg(sub.label order by sub.confidence desc)
        from (
          select distinct c.label, c.confidence
          from public.user_identity_claims c
          where c.user_id = u.id
            and c.dismissed_at is null
            and c.disclosure = 'public'
          order by c.confidence desc
          limit 5
        ) sub
      ), '[]'::jsonb) as about_tags,
      coalesce((
        select count(*)::int
        from public.user_identity_claims c1
        join public.user_identity_claims c2 on c1.concept = c2.concept
        where c1.user_id = v_caller
          and c2.user_id = u.id
          and c1.dismissed_at is null
          and c2.dismissed_at is null
          and c1.disclosure = 'public'
          and c2.disclosure = 'public'
      ), 0) as shared_claim_count
    from public.users u
    join public.event_requests er on er.requester_id = u.id
    where er.event_id = p_event_id
      and er.status in ('approved', 'attended')
      and er.rsvp_status = 'going'
    order by event_count desc, u.created_at asc
    limit 20
  ) p;

  return jsonb_build_object(
    'event_id', v_event.id,
    'host_id', v_event.host_id,
    'title', v_event.title,
    'description', v_event.description,
    'starts_at', v_event.starts_at,
    'has_time', v_event.has_time,
    'ends_at', v_event.ends_at,
    'duration_minutes', case when v_event.ends_at is null then null else greatest(round(extract(epoch from v_event.ends_at - v_event.starts_at) / 60)::int, 1) end,
    'venue_name', v_event.venue_name,
    'venue_address', v_event.venue_address,
    'place_id', v_event.place_id,
    'cohort_tags', public.cohort_tag_labels(v_event.cohort_tags),
    'max_attendees', v_event.max_attendees,
    'bring_items', coalesce(v_event.bring_items, '{}'),
    'status', v_event.status,
    'cover_image_url', v_event.cover_image_url,
    'cover_emoji', v_event.cover_emoji,
    'recurrence', v_event.recurrence,
    'recurrence_until', v_event.recurrence_until,
    'community', public.event_community(v_event.circle_place_ref, v_event.host_id),
    'distance_meters', v_distance,
    'distance_text', v_distance_text,
    'affinity_match_count', v_matched,
    'affinity_total_count', v_total,
    'affinity_match_label', case when v_total = 0 then null else concat(v_matched, '/', v_total, ' affinities match') end,
    -- The intersection itself, label-mapped like cohort_tags, in the event's tag order.
    'affinity_matched_tags', to_jsonb(public.cohort_tag_labels(coalesce(v_matched_tags, '{}'))),
    'fit_score', v_fit,
    'is_authenticated', true,
    'my_request_status', v_my_status,
    'my_rsvp_status', v_my_rsvp,
    'participant_count', coalesce((select count(*) from public.event_requests er where er.event_id = p_event_id and er.status in ('approved', 'attended') and er.rsvp_status = 'going'), 0),
    'maybe_count', coalesce((select count(*) from public.event_requests er where er.event_id = p_event_id and er.status in ('approved', 'attended') and er.rsvp_status = 'maybe'), 0),
    'participants', v_participants
  );
end;
$$;

grant execute on function public.get_event_preview(uuid, double precision, double precision, text) to anon, authenticated;
revoke all on function public.get_event_preview_authed(uuid, double precision, double precision, text) from public, anon;
grant execute on function public.get_event_preview_authed(uuid, double precision, double precision, text) to authenticated;

-- ---------------------------------------------------------------------------
-- §26(b) get_my_contributions — body verbatim from 20261206120000 plus two columns on
-- every arm: cover_emoji and community (null on the signal arm).
-- ---------------------------------------------------------------------------
create or replace function public.get_my_contributions(p_since timestamptz default null)
returns jsonb
language sql
security definer
set search_path = pg_catalog, public
stable
as $$
  with mine as (
    -- My local signals: offers, seeks, tips, casual host asks.
    select
      'signal'::text       as kind,
      s.id                 as id,
      s.intent             as intent,
      coalesce(nullif(btrim(s.reco_name), ''), s.detail_text) as title,
      s.category           as category,
      s.status             as status,
      s.created_at         as created_at,
      s.photo_url          as photo_url,
      s.reco_type          as reco_type,
      nullif(btrim(s.reco_place), '')       as reco_place,
      nullif(btrim(s.reco_description), '') as reco_description,
      null::text           as cover_emoji,
      null::jsonb          as community,
      null::uuid           as event_id,
      null::timestamptz    as starts_at,
      null::int            as yes_count,
      null::int            as capacity,
      (
        select u.nickname
        from public.block_log_entries b
        join public.users u on u.id = b.peer_user_id
        where b.my_signal_id = s.id and b.peer_user_id is not null
        order by b.created_at desc
        limit 1
      )                    as peer_label
    from public.local_signals s
    where s.user_id = auth.uid()
      and (p_since is null or s.created_at >= p_since)

    union all

    -- Meets I host or co-host (published events) — title, when, N-of-capacity going.
    -- cohost_meet lets the Radar card badge "CO-HOSTING" instead of "HOSTING".
    select
      'event'::text        as kind,
      e.id                 as id,
      case when e.cohost_id = auth.uid() and e.host_id <> auth.uid()
           then 'cohost_meet' else 'host_meet' end::text as intent,
      e.title              as title,
      null::text           as category,
      e.status             as status,
      e.created_at         as created_at,
      null::text           as photo_url,
      null::text           as reco_type,
      null::text           as reco_place,
      null::text           as reco_description,
      e.cover_emoji        as cover_emoji,
      public.event_community(e.circle_place_ref, e.host_id) as community,
      e.id                 as event_id,
      e.starts_at          as starts_at,
      (select count(*)::int from public.event_requests er
        where er.event_id = e.id
          and er.status in ('approved', 'attended')
          and er.rsvp_status = 'going') as yes_count,
      e.max_attendees      as capacity,
      null::text           as peer_label
    from public.events e
    where (e.host_id = auth.uid() or e.cohost_id = auth.uid())
      and (p_since is null or e.created_at >= p_since)
      and not exists (
        select 1 from public.event_dismissals d
        where d.event_id = e.id and d.user_id = auth.uid()
      )

    union all

    -- Meets I asked to join — status is the REQUEST's status (pending/approved/attended).
    select
      'request'::text      as kind,
      e.id                 as id,
      'joined_meet'::text  as intent,
      e.title              as title,
      null::text           as category,
      er.status            as status,
      er.created_at        as created_at,
      null::text           as photo_url,
      null::text           as reco_type,
      null::text           as reco_place,
      null::text           as reco_description,
      e.cover_emoji        as cover_emoji,
      public.event_community(e.circle_place_ref, e.host_id) as community,
      e.id                 as event_id,
      e.starts_at          as starts_at,
      null::int            as yes_count,
      e.max_attendees      as capacity,
      null::text           as peer_label
    from public.event_requests er
    join public.events e on e.id = er.event_id
    where er.requester_id = auth.uid()
      and er.status in ('pending', 'approved', 'attended')
      and (p_since is null or er.created_at >= p_since)
      and not exists (
        select 1 from public.event_dismissals d
        where d.event_id = e.id and d.user_id = auth.uid()
      )
  )
  select coalesce(jsonb_agg(to_jsonb(m) order by m.created_at desc), '[]'::jsonb)
  from mine m;
$$;

revoke execute on function public.get_my_contributions(timestamptz) from public, anon;
grant execute on function public.get_my_contributions(timestamptz) to authenticated;

-- ---------------------------------------------------------------------------
-- §26(c) get_peer_profile — body verbatim from 20261102120000 plus `community` on each
-- upcoming_shared_events[] row. Same signature: grants and comment carry over.
-- ---------------------------------------------------------------------------
create or replace function public.get_peer_profile(p_user_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
stable
as $$
declare
  caller uuid := auth.uid();
  peer record;
  is_matched boolean;
  location_label text;
  location_precision text;
  result jsonb;
begin
  -- Fetch peer profile
  select u.id, u.nickname, u.profile_photo_url, u.home_block_id, u.home_location_visibility,
         u.public_portrait,
         b.display_name, b.cluster_id
  into peer
  from public.users u
  left join public.blocks b on b.id = u.home_block_id
  where u.id = p_user_id;

  if not found then
    raise exception 'peer_not_found' using errcode = 'P0001';
  end if;

  -- Anonymous visitor: blurred profile (no sensitive data)
  if caller is null then
    return jsonb_build_object(
      'user_id', null,
      'nickname', null,
      'avatar_url', null,
      'is_matched', false,
      'portrait', null,
      'public_claims', '[]'::jsonb,
      'mutual_claims', '[]'::jsonb,
      'shared_claim_count', 0,
      'location_label', null,
      'location_precision', null,
      'block_name', null,
      'upcoming_shared_events', '[]'::jsonb
    );
  end if;

  -- Choose label based on peer preference
  location_label := case peer.home_location_visibility
    when 'block' then peer.display_name
    when 'cluster' then peer.cluster_id
  end;
  location_precision := peer.home_location_visibility::text;

  -- Check if caller and peer are matched (same event or shared public claims)
  is_matched := public.are_users_matched(caller, p_user_id);

  -- Build authenticated response
  result := jsonb_build_object(
    'user_id', peer.id,
    'nickname', peer.nickname,
    'avatar_url', peer.profile_photo_url,
    'is_matched', is_matched,
    -- Written from the public claims listed directly below, and from nothing else.
    'portrait', peer.public_portrait,
    -- Always show public claims
    'public_claims', coalesce((
      select jsonb_agg(jsonb_build_object(
        'concept', c.concept,
        'label', c.label,
        'tone', c.tone,
        'confidence', c.confidence,
        -- NEW: what kind of thread it is, and when it was learned.
        'bucket', c.bucket,
        'subject_kind', c.subject_kind,
        'created_at', c.created_at
      ) order by c.confidence desc)
      from public.user_identity_claims c
      where c.user_id = peer.id
        and c.dismissed_at is null
        and c.disclosure = 'public'
    ), '[]'::jsonb),
    -- Show mutual claims only if matched (silent omission otherwise)
    'mutual_claims', case
      when is_matched then
        coalesce((
          select jsonb_agg(jsonb_build_object(
            'concept', c.concept,
            'label', c.label,
            'tone', c.tone,
            'confidence', c.confidence,
            'bucket', c.bucket,
        'subject_kind', c.subject_kind,
            'created_at', c.created_at
          ) order by c.confidence desc)
          from public.user_identity_claims c
          where c.user_id = peer.id
            and c.dismissed_at is null
            and c.disclosure = 'mutual'
        ), '[]'::jsonb)
      else '[]'::jsonb
    end,
    'shared_claim_count', (
      select count(*)::int
      from public.user_identity_claims c1
      join public.user_identity_claims c2
        on c1.concept = c2.concept
       and c1.subject_kind = c2.subject_kind
      where c1.user_id = caller
        and c2.user_id = peer.id
        and c1.dismissed_at is null
        and c2.dismissed_at is null
        and c1.disclosure = 'public'
        and c2.disclosure = 'public'
    ),
    'location_label', location_label,
    'location_precision', location_precision,
    'block_name', case when peer.home_location_visibility = 'block' then peer.display_name else null end,
    -- home_block_id is HIDDEN from peer views (only owner sees it via get_my_profile)
    'upcoming_shared_events', coalesce((
      select jsonb_agg(jsonb_build_object(
        'event_id', e.id,
        'title', e.title,
        'starts_at', e.starts_at,
        -- The community the meet is FOR, same object as get_event_preview's; null on a
        -- plain neighbourhood meet. Both people are going, so the place is no news.
        'community', public.event_community(e.circle_place_ref, e.host_id)
      ) order by e.starts_at asc)
      from public.events e
      where e.status = 'open'
        and e.starts_at > now()
        and exists (
          select 1 from public.event_requests r
          where r.event_id = e.id
            and r.requester_id = caller
            and r.status in ('approved', 'attended')
        )
        and exists (
          select 1 from public.event_requests r
          where r.event_id = e.id
            and r.requester_id = peer.id
            and r.status in ('approved', 'attended')
        )
    ), '[]'::jsonb)
  );

  return result;
end;
$$;

-- end 20270110130000
