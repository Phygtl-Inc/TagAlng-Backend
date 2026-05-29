-- TagAlng v0.1: remaining RPCs + event_reports

-- 0016: event_reports
create table if not exists public.event_reports (
  id uuid primary key default gen_random_uuid(),
  event_id uuid not null references public.events(id) on delete cascade,
  reporter_id uuid not null references public.users(id),
  reason text not null check (char_length(reason) between 1 and 500),
  status text not null default 'open' check (status in ('open', 'reviewed', 'actioned', 'dismissed')),
  created_at timestamptz not null default now(),
  reviewed_at timestamptz,
  reviewer_id uuid references public.users(id)
);

create index if not exists event_reports_status_idx on public.event_reports (status, created_at);

alter table public.event_reports enable row level security;

drop policy if exists "er_reports_insert_self" on public.event_reports;
create policy "er_reports_insert_self"
  on public.event_reports for insert
  to authenticated
  with check (reporter_id = auth.uid());

drop policy if exists "er_reports_select_self_or_admin" on public.event_reports;
create policy "er_reports_select_self_or_admin"
  on public.event_reports for select
  to authenticated
  using (
    reporter_id = auth.uid()
    or exists (
      select 1 from public.users u
      where u.id = auth.uid() and u.founder_role = 'internal'
    )
  );

-- Peer reads (anon blurred)
create or replace function public.get_cluster_peers(p_cluster_id text)
returns table (
  user_id uuid,
  nickname text,
  avatar_url text,
  match_score int,
  total_threads int,
  is_blurred bool
)
language plpgsql
security definer
set search_path = pg_catalog, public
stable
as $$
declare
  caller uuid := auth.uid();
begin
  if caller is null then
    return query
    select
      null::uuid,
      null::text,
      null::text,
      0,
      0,
      true
    from public.users u
    join public.blocks b on b.id = u.home_block_id
    where b.cluster_id = p_cluster_id
      and u.id is not null
    limit 10;
    return;
  end if;

  return query
  with cluster_users as (
    select u.id, u.nickname, u.profile_photo_url
    from public.users u
    join public.blocks b on b.id = u.home_block_id
    where b.cluster_id = p_cluster_id
      and u.id <> caller
  ),
  caller_concepts as (
    select c.concept
    from public.user_identity_claims c
    where c.user_id = caller
      and c.dismissed_at is null
      and c.disclosure = 'public'
  )
  select
    cu.id,
    cu.nickname,
    cu.profile_photo_url,
    coalesce((
      select count(*)::int
      from public.user_identity_claims pc
      join caller_concepts cc on cc.concept = pc.concept
      where pc.user_id = cu.id
        and pc.dismissed_at is null
        and pc.disclosure = 'public'
    ), 0),
    coalesce((
      select count(distinct er1.event_id)::int
      from public.event_requests er1
      join public.event_requests er2 on er2.event_id = er1.event_id
      where er1.requester_id = caller
        and er2.requester_id = cu.id
        and er1.status in ('approved', 'attended')
        and er2.status in ('approved', 'attended')
    ), 0),
    false
  from cluster_users cu
  order by 4 desc, cu.nickname asc nulls last;
end;
$$;

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
  result jsonb;
begin
  select u.id, u.nickname, u.profile_photo_url, u.home_block_id
  into peer
  from public.users u
  where u.id = p_user_id;

  if not found then
    raise exception 'peer_not_found' using errcode = 'P0001';
  end if;

  if caller is null then
    return jsonb_build_object(
      'user_id', null,
      'nickname', null,
      'avatar_url', null,
      'is_blurred', true,
      'public_claims', '[]'::jsonb,
      'shared_claim_count', 0,
      'upcoming_shared_events', '[]'::jsonb
    );
  end if;

  result := jsonb_build_object(
    'user_id', peer.id,
    'nickname', peer.nickname,
    'avatar_url', peer.profile_photo_url,
    'is_blurred', false,
    'public_claims', coalesce((
      select jsonb_agg(jsonb_build_object(
        'concept', c.concept,
        'label', c.label,
        'tone', c.tone
      ) order by c.confidence desc)
      from public.user_identity_claims c
      where c.user_id = peer.id
        and c.dismissed_at is null
        and c.disclosure = 'public'
    ), '[]'::jsonb),
    'shared_claim_count', (
      select count(*)::int
      from public.user_identity_claims c1
      join public.user_identity_claims c2
        on c1.concept = c2.concept
      where c1.user_id = caller
        and c2.user_id = peer.id
        and c1.dismissed_at is null
        and c2.dismissed_at is null
        and c1.disclosure = 'public'
        and c2.disclosure = 'public'
    ),
    'upcoming_shared_events', coalesce((
      select jsonb_agg(jsonb_build_object(
        'event_id', e.id,
        'title', e.title,
        'starts_at', e.starts_at
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

-- Event host RPCs
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

create or replace function public.create_event_from_description(p_text text)
returns uuid
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
begin
  raise exception 'wire_to_aki_pipeline' using errcode = 'P0001';
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
begin
  v_lat := (p_fields->>'lat')::double precision;
  v_lng := (p_fields->>'lng')::double precision;

  update public.events e
  set
    title = coalesce(p_fields->>'title', e.title),
    description = coalesce(p_fields->>'description', e.description),
    starts_at = coalesce((p_fields->>'starts_at')::timestamptz, e.starts_at),
    ends_at = case when p_fields ? 'ends_at' then (p_fields->>'ends_at')::timestamptz else e.ends_at end,
    venue_name = coalesce(p_fields->>'venue_name', e.venue_name),
    cohort_tags = case
      when p_fields ? 'cohort_tags' then (
        select coalesce(array_agg(t), '{}')
        from jsonb_array_elements_text(p_fields->'cohort_tags') as t
      )
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

create or replace function public.cancel_event(p_event_id uuid)
returns void
language sql
security invoker
set search_path = pg_catalog, public
as $$
  update public.events
  set status = 'cancelled'
  where id = p_event_id;
$$;

-- Nudges list
create or replace function public.get_my_nudges(p_direction text default 'received')
returns table (
  id uuid,
  other_user_id uuid,
  nickname text,
  avatar_url text,
  sent_at timestamptz,
  shared_count int
)
language sql
security invoker
set search_path = pg_catalog, public
stable
as $$
  select
    n.id,
    case when p_direction = 'sent' then n.recipient_id else n.sender_id end,
    u.nickname,
    u.profile_photo_url,
    n.sent_at,
    0
  from public.nudges n
  join public.users u on u.id = case when p_direction = 'sent' then n.recipient_id else n.sender_id end
  where (
    (p_direction = 'received' and n.recipient_id = auth.uid())
    or (p_direction = 'sent' and n.sender_id = auth.uid())
  )
  order by n.sent_at desc;
$$;

-- Reports (Slack hook deferred)
create or replace function public.report_event(p_event_id uuid, p_reason text)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  report_id uuid;
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  if char_length(p_reason) < 1 then
    raise exception 'reason_required' using errcode = 'P0001';
  end if;

  insert into public.event_reports (event_id, reporter_id, reason)
  values (p_event_id, auth.uid(), p_reason)
  returning id into report_id;

  return report_id;
end;
$$;

-- Grants
revoke execute on function public.get_cluster_peers(text) from public;
grant execute on function public.get_cluster_peers(text) to anon, authenticated;

revoke execute on function public.get_peer_profile(uuid) from public;
grant execute on function public.get_peer_profile(uuid) to anon, authenticated;

revoke execute on function public.create_event(jsonb) from public, anon;
grant execute on function public.create_event(jsonb) to authenticated;

revoke execute on function public.create_event_from_description(text) from public, anon;
grant execute on function public.create_event_from_description(text) to authenticated;

revoke execute on function public.update_event(uuid, jsonb) from public, anon;
grant execute on function public.update_event(uuid, jsonb) to authenticated;

revoke execute on function public.cancel_event(uuid) from public, anon;
grant execute on function public.cancel_event(uuid) to authenticated;

revoke execute on function public.get_my_nudges(text) from public, anon;
grant execute on function public.get_my_nudges(text) to authenticated;

revoke execute on function public.report_event(uuid, text) from public, anon;
grant execute on function public.report_event(uuid, text) to authenticated;
