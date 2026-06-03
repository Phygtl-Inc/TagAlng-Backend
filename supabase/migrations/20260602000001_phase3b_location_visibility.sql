-- TagAlng Phase 3b: Location precision visibility support
-- Purpose: allow users to choose block or neighborhood-level location disclosure

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_type t
    JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE t.typname = 'location_visibility'
      AND n.nspname = 'public'
  ) THEN
    CREATE TYPE public.location_visibility AS ENUM (
      'block',
      'cluster'
    );
  END IF;
END
$$;
alter table public.users
  add column if not exists home_location_visibility public.location_visibility not null default 'block';
comment on column public.users.home_location_visibility is
  'Controls the precision of location labels shown to other users: block or cluster.';
-- Update get_my_profile to expose the current user setting
create or replace function public.get_my_profile()
returns jsonb
language sql
stable
security definer
set search_path = public
as $$
  select jsonb_build_object(
    'id', u.id,
    'phone', u.phone,
    'nickname', u.nickname,
    'home_block_id', u.home_block_id,
    'block_display_name', b.display_name,
    'block_state', b.state,
    'cluster_id', b.cluster_id,
    'home_location_visibility', u.home_location_visibility::text,
    'created_at', u.created_at
  )
  from public.users u
  left join public.blocks b on b.id = u.home_block_id
  where u.id = auth.uid();
$$;
grant execute on function public.get_my_profile() to authenticated;
-- Allow users to update their own location precision preference
create or replace function public.set_home_location_visibility(
  p_visibility public.location_visibility
)
returns jsonb
language plpgsql
security invoker
set search_path = public
as $$
declare
  updated_visibility text;
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  update public.users
  set home_location_visibility = p_visibility
  where id = auth.uid();

  updated_visibility := p_visibility::text;

  return jsonb_build_object(
    'home_location_visibility', updated_visibility
  );
end;
$$;
comment on function public.set_home_location_visibility(public.location_visibility) is
  'Set the current user''s location disclosure precision. Allowed values: block, cluster.';
grant execute on function public.set_home_location_visibility(public.location_visibility) to authenticated;
-- Helper to return the user's visible location label
create or replace function public.get_user_location_label(
  p_user_id uuid
)
returns table (
  location_label text,
  location_precision text
)
language sql
stable
security definer
set search_path = public
as $$
  select
    case u.home_location_visibility
      when 'block' then b.display_name
      when 'cluster' then b.cluster_id
    end as location_label,
    u.home_location_visibility::text as location_precision
  from public.users u
  left join public.blocks b on b.id = u.home_block_id
  where u.id = p_user_id;
$$;
grant execute on function public.get_user_location_label(uuid) to authenticated, anon;
-- Update get_peer_profile to return label based on peer's chosen precision and hide raw block info when not block
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
      'is_blurred', true,
      'is_matched', false,
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
    'is_blurred', false,
    'is_matched', is_matched,
    -- Always show public claims
    'public_claims', coalesce((
      select jsonb_agg(jsonb_build_object(
        'concept', c.concept,
        'label', c.label,
        'tone', c.tone,
        'confidence', c.confidence
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
            'confidence', c.confidence
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
comment on function public.get_peer_profile(uuid) is
  'Returns peer profile with disclosure-aware claims. Shows public claims to all, mutual claims only to matched users. Returns location label based on peer''s chosen precision and hides home_block_id.';
grant execute on function public.get_peer_profile(uuid) to authenticated, anon;
