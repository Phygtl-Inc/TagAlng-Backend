-- Event Purpose taxonomy (PWA v8 host chips) — mirror cohorts.yaml event_purposes

alter table public.cohorts
  drop constraint if exists cohorts_kind_check;

alter table public.cohorts
  add constraint cohorts_kind_check
  check (kind in ('cohort', 'sport_subtype', 'event_purpose'));

alter table public.cohorts
  add column if not exists emoji text,
  add column if not exists display_order int,
  add column if not exists affinity_concepts text[] not null default '{}';

comment on column public.cohorts.affinity_concepts is
  'For event_purpose kind: claim concept slugs that count as affinity match.';

-- Waitlist cohorts only (sport sub validated separately).
create or replace function public.validate_cohort_ids(p_cohorts text[])
returns boolean
language sql
stable
set search_path = pg_catalog, public
as $$
  select not exists (
    select 1
    from unnest(p_cohorts) as c(id)
    where not exists (
      select 1 from public.cohorts co
      where co.id = c.id and co.kind = 'cohort'
    )
  );
$$;

-- Event cohort_tags: legacy cohort ids, sport subtypes, or event purposes.
create or replace function public.validate_event_cohort_tags(p_tags text[])
returns boolean
language sql
stable
set search_path = pg_catalog, public
as $$
  select cardinality(p_tags) = 0 or not exists (
    select 1
    from unnest(p_tags) as t(id)
    where not exists (
      select 1 from public.cohorts co
      where co.id = t.id
        and co.kind in ('cohort', 'sport_subtype', 'event_purpose')
    )
  );
$$;

create or replace function public.cohort_tag_matches_claim(
  p_tag text,
  p_concept text,
  p_synonyms text[]
)
returns boolean
language sql
stable
set search_path = pg_catalog, public
as $$
  select
    p_concept = p_tag
    or p_tag = any(coalesce(p_synonyms, '{}'))
    or exists (
      select 1
      from public.cohorts co
      where co.id = p_tag
        and co.kind = 'event_purpose'
        and (
          p_concept = any(coalesce(co.affinity_concepts, '{}'))
          or coalesce(co.affinity_concepts, '{}') && coalesce(p_synonyms, '{}')
        )
    );
$$;

insert into public.cohorts (id, label, parent_id, kind, emoji, display_order, affinity_concepts) values
  ('faith_small_group', 'Faith small group', null, 'event_purpose', '⛪', 1, array['faith', 'faith_community']),
  ('running_fitness', 'Running / fitness', null, 'event_purpose', '🏃', 2, array['runner', 'running', 'sports']),
  ('outdoor_adventure', 'Outdoor + adventure', null, 'event_purpose', '🌳', 3, array['outdoor', 'activity']),
  ('coffee_stroller', 'Coffee + stroller', null, 'event_purpose', '☕', 4, array['parents', 'parents_toddlers', 'newcomer']),
  ('heritage_language', 'Heritage / language', null, 'event_purpose', '🌍', 5, array['heritage']),
  ('postpartum_support', 'Postpartum + support', null, 'event_purpose', '🌿', 6, array['parents', 'parents_toddlers', 'postpartum']),
  ('book_club_learning', 'Book club / learning', null, 'event_purpose', '📖', 7, array['creative', 'book_club', 'learning']),
  ('beauty_wellness', 'Beauty + wellness', null, 'event_purpose', '💆', 8, array['wellness', 'activity']),
  ('lifestyle_social', 'Lifestyle + social', null, 'event_purpose', '🍷', 9, array['social', 'interest']),
  ('kids_led_activity', 'Kids-led activity', null, 'event_purpose', '🧸', 10, array['parents', 'parents_toddlers', 'parents_elementary'])
on conflict (id) do update set
  label = excluded.label,
  kind = excluded.kind,
  emoji = excluded.emoji,
  display_order = excluded.display_order,
  affinity_concepts = excluded.affinity_concepts;

create or replace function public.get_event_purposes()
returns table (
  id text,
  label text,
  emoji text,
  display_order int,
  affinity_concepts text[]
)
language sql
stable
security invoker
set search_path = pg_catalog, public
as $$
  select co.id, co.label, co.emoji, co.display_order, co.affinity_concepts
  from public.cohorts co
  where co.kind = 'event_purpose'
  order by co.display_order nulls last, co.label;
$$;

grant execute on function public.get_event_purposes() to anon, authenticated;

alter function public.validate_event_cohort_tags(text[]) set search_path = pg_catalog, public;
alter function public.cohort_tag_matches_claim(text, text, text[]) set search_path = pg_catalog, public;
alter function public.get_event_purposes() set search_path = pg_catalog, public;

create or replace function public.create_event(p_fields jsonb)
returns uuid
language plpgsql
security invoker
set search_path = pg_catalog, public, extensions
as $$
declare
  new_id uuid;
  v_lat double precision;
  v_lng double precision;
  v_tags text[];
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  v_lat := (p_fields->>'lat')::double precision;
  v_lng := (p_fields->>'lng')::double precision;

  if v_lat is null or v_lng is null then
    raise exception 'location_required' using errcode = 'P0001';
  end if;

  if p_fields->>'title' is null or char_length(p_fields->>'title') < 1 then
    raise exception 'title_required' using errcode = 'P0001';
  end if;

  select coalesce(array_agg(t), '{}')
  into v_tags
  from jsonb_array_elements_text(coalesce(p_fields->'cohort_tags', '[]'::jsonb)) as t;

  if not public.validate_event_cohort_tags(v_tags) then
    raise exception 'invalid_cohort' using errcode = 'P0001';
  end if;

  insert into public.events (
    host_id,
    cluster_id,
    block_id,
    title,
    description,
    starts_at,
    ends_at,
    location,
    venue_name,
    cohort_tags,
    max_attendees,
    cover_image_url
  )
  values (
    auth.uid(),
    coalesce(p_fields->>'cluster_id', 'lake-nona'),
    p_fields->>'block_id',
    p_fields->>'title',
    p_fields->>'description',
    coalesce((p_fields->>'starts_at')::timestamptz, now() + interval '7 days'),
    (p_fields->>'ends_at')::timestamptz,
    extensions.st_setsrid(extensions.st_makepoint(v_lng, v_lat), 4326)::extensions.geography,
    p_fields->>'venue_name',
    v_tags,
    (p_fields->>'max_attendees')::integer,
    p_fields->>'cover_image_url'
  )
  returning id into new_id;

  return new_id;
end;
$$;

create or replace function public.update_event(p_event_id uuid, p_fields jsonb)
returns void
language plpgsql
security invoker
set search_path = pg_catalog, public, extensions
as $$
declare
  v_lat double precision;
  v_lng double precision;
  v_tags text[];
begin
  v_lat := (p_fields->>'lat')::double precision;
  v_lng := (p_fields->>'lng')::double precision;

  if p_fields ? 'cohort_tags' then
    select coalesce(array_agg(t), '{}')
    into v_tags
    from jsonb_array_elements_text(p_fields->'cohort_tags') as t;

    if not public.validate_event_cohort_tags(v_tags) then
      raise exception 'invalid_cohort' using errcode = 'P0001';
    end if;
  end if;

  update public.events e
  set
    title = coalesce(p_fields->>'title', e.title),
    description = coalesce(p_fields->>'description', e.description),
    starts_at = coalesce((p_fields->>'starts_at')::timestamptz, e.starts_at),
    ends_at = case when p_fields ? 'ends_at' then (p_fields->>'ends_at')::timestamptz else e.ends_at end,
    venue_name = coalesce(p_fields->>'venue_name', e.venue_name),
    cohort_tags = case
      when p_fields ? 'cohort_tags' then v_tags
      else e.cohort_tags
    end,
    max_attendees = coalesce((p_fields->>'max_attendees')::integer, e.max_attendees),
    cover_image_url = coalesce(p_fields->>'cover_image_url', e.cover_image_url),
    location = case
      when v_lat is not null and v_lng is not null then
        extensions.st_setsrid(extensions.st_makepoint(v_lng, v_lat), 4326)::extensions.geography
      else e.location
    end
  where e.id = p_event_id;
end;
$$;

-- Affinity: event_purpose tags match via affinity_concepts on cohorts row.
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
  ends_at timestamptz,
  duration_minutes int,
  venue_name text,
  cohort_tags text[],
  max_attendees integer,
  status text,
  cover_image_url text,
  distance_meters double precision,
  distance_text text,
  affinity_match_count int,
  affinity_total_count int,
  affinity_match_label text,
  participant_count int,
  my_request_status text,
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
    extensions.st_distance(e.location, v_point)::double precision as distance_meters,
    concat(
      greatest(1, round(extensions.st_distance(e.location, v_point) / 80)::int),
      ' min walk'
    ) as distance_text,
    am.matched as affinity_match_count,
    cardinality(coalesce(e.cohort_tags, '{}')) as affinity_total_count,
    case
      when cardinality(coalesce(e.cohort_tags, '{}')) = 0 then null
      else concat(am.matched, '/', cardinality(coalesce(e.cohort_tags, '{}')), ' affinities match')
    end as affinity_match_label,
    coalesce((
      select count(*)
      from public.event_requests er
      where er.event_id = e.id
        and er.status in ('approved', 'attended')
    ), 0) as participant_count,
    (
      select er.status
      from public.event_requests er
      where er.event_id = e.id
        and er.requester_id = v_caller
    ) as my_request_status,
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
        order by event_count desc, u.created_at asc
        limit 6
      ) p
    ), '[]'::jsonb) as participant_preview
  from public.events e
  left join lateral (
    select count(distinct tag)::int as matched
    from unnest(coalesce(e.cohort_tags, '{}')) as tag
    where exists (
      select 1
      from public.user_identity_claims c
      where c.user_id = v_caller
        and c.dismissed_at is null
        and c.disclosure = 'public'
        and public.cohort_tag_matches_claim(tag, c.concept, c.synonyms)
    )
  ) am on true
  where e.status = 'open'
    and e.location is not null
    and e.starts_at between now() and now() + p_window
  order by distance_meters asc
  limit greatest(1, least(coalesce(p_limit, 20), 50));
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
  v_my_status text;
begin
  if v_caller is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  select
    e.id, e.host_id,
    coalesce(e.title_translations->>p_locale, e.title) as title,
    coalesce(e.description_translations->>p_locale, e.description) as description,
    e.starts_at, e.ends_at, e.location, e.venue_name, e.cohort_tags,
    e.max_attendees, e.status, e.cover_image_url
  into v_event
  from public.events e
  where e.id = p_event_id
    and e.status = 'open';

  if not found then
    raise exception 'event_not_found' using errcode = 'P0001';
  end if;

  if p_lat is not null and p_lng is not null then
    v_point := extensions.st_setsrid(extensions.st_makepoint(p_lng, p_lat), 4326)::extensions.geography;
    v_distance := extensions.st_distance(v_event.location, v_point)::double precision;
    v_distance_text := concat(greatest(1, round(v_distance / 80)::int), ' min walk');
  else
    v_distance := null;
    v_distance_text := null;
  end if;

  v_total := cardinality(coalesce(v_event.cohort_tags, '{}'));

  select count(distinct tag)::int
  into v_matched
  from unnest(coalesce(v_event.cohort_tags, '{}')) as tag
  where exists (
    select 1
    from public.user_identity_claims c
    where c.user_id = v_caller
      and c.dismissed_at is null
      and c.disclosure = 'public'
      and public.cohort_tag_matches_claim(tag, c.concept, c.synonyms)
  );

  select er.status
  into v_my_status
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
    order by event_count desc, u.created_at asc
    limit 20
  ) p;

  return jsonb_build_object(
    'event_id', v_event.id,
    'host_id', v_event.host_id,
    'title', v_event.title,
    'description', v_event.description,
    'starts_at', v_event.starts_at,
    'ends_at', v_event.ends_at,
    'duration_minutes', case when v_event.ends_at is null then null else greatest(round(extract(epoch from v_event.ends_at - v_event.starts_at) / 60)::int, 1) end,
    'venue_name', v_event.venue_name,
    'cohort_tags', v_event.cohort_tags,
    'max_attendees', v_event.max_attendees,
    'status', v_event.status,
    'cover_image_url', v_event.cover_image_url,
    'distance_meters', v_distance,
    'distance_text', v_distance_text,
    'affinity_match_count', v_matched,
    'affinity_total_count', v_total,
    'affinity_match_label', case when v_total = 0 then null else concat(v_matched, '/', v_total, ' affinities match') end,
    'is_authenticated', true,
    'my_request_status', v_my_status,
    'participant_count', coalesce((select count(*) from public.event_requests er where er.event_id = p_event_id and er.status in ('approved', 'attended')), 0),
    'participants', v_participants
  );
end;
$$;
