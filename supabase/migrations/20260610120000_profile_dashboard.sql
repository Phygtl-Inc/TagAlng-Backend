-- Profile dashboard: one RPC for PWA "My profile" screen.
-- handle = nickname (@display). full_name = separate display name.

alter table public.users
  add column if not exists full_name text;

comment on column public.users.full_name is
  'Display full name (e.g. Sofia Russo). @handle in UI maps to nickname, not this column.';

-- Extended header fields for existing callers.
create or replace function public.get_my_profile()
returns jsonb
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select jsonb_build_object(
    'id', u.id,
    'full_name', u.full_name,
    'nickname', u.nickname,
    'handle', u.nickname,
    'phone', u.phone,
    'phone_verified_at', u.phone_verified_at,
    'profile_photo_url', u.profile_photo_url,
    'home_block_id', u.home_block_id,
    'home_zip', u.home_zip,
    'block_display_name', b.display_name,
    'block_state', b.state,
    'cluster_id', b.cluster_id,
    'home_location_visibility', u.home_location_visibility::text,
    'locale', u.locale,
    'created_at', u.created_at
  )
  from public.users u
  left join public.blocks b on b.id = u.home_block_id
  where u.id = auth.uid();
$$;

comment on function public.get_my_profile() is
  'Own profile header. handle duplicates nickname for @handle UI.';

-- Single load for profile tab: profile + mapped_summary + claims + stats.
create or replace function public.get_my_profile_dashboard()
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
stable
as $$
declare
  v_caller uuid := auth.uid();
  v_profile jsonb;
  v_summary text;
  v_spans jsonb;
  v_claims jsonb;
  v_stats jsonb;
  v_events_hosted int;
  v_events_attended int;
  v_check_ins int;
  v_peers_met int;
begin
  if v_caller is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  select public.get_my_profile() into v_profile;

  select
    s.context->>'mapped_summary',
    coalesce(s.context->'spans', '[]'::jsonb)
  into v_summary, v_spans
  from public.lana_sessions s
  where s.user_id = v_caller
    and s.purpose = 'profile_intake'
    and s.status = 'completed'
  order by s.completed_at desc nulls last, s.updated_at desc
  limit 1;

  select coalesce(jsonb_agg(
    jsonb_build_object(
      'id', c.id,
      'concept', c.concept,
      'label', c.label,
      'tone', c.tone,
      'confidence', c.confidence,
      'disclosure', c.disclosure,
      'synonyms', c.synonyms,
      'source_quote', c.source_quote,
      'bucket', c.bucket,
      'created_at', c.created_at
    ) order by c.confidence desc, c.created_at desc
  ), '[]'::jsonb)
  into v_claims
  from public.user_identity_claims c
  where c.user_id = v_caller
    and c.dismissed_at is null;

  select count(*)::int
  into v_events_hosted
  from public.events e
  where e.host_id = v_caller
    and e.status in ('open', 'completed');

  select count(*)::int
  into v_events_attended
  from public.event_requests er
  where er.requester_id = v_caller
    and er.status in ('approved', 'attended');

  select count(*)::int
  into v_check_ins
  from public.thread_events te
  where te.actor_id = v_caller
    and te.event_type = 'check_in';

  select count(distinct er2.requester_id)::int
  into v_peers_met
  from public.event_requests er1
  join public.event_requests er2
    on er2.event_id = er1.event_id
   and er2.requester_id <> v_caller
  where er1.requester_id = v_caller
    and er1.status in ('approved', 'attended')
    and er2.status in ('approved', 'attended');

  v_stats := jsonb_build_object(
    'events_hosted', coalesce(v_events_hosted, 0),
    'events_attended', coalesce(v_events_attended, 0),
    'check_ins', coalesce(v_check_ins, 0),
    'peers_met', coalesce(v_peers_met, 0)
  );

  return jsonb_build_object(
    'profile', coalesce(v_profile, '{}'::jsonb),
    'mapped_summary', v_summary,
    'spans', coalesce(v_spans, '[]'::jsonb),
    'claims', coalesce(v_claims, '[]'::jsonb),
    'stats', v_stats
  );
end;
$$;

comment on function public.get_my_profile_dashboard() is
  'Own profile tab: header, Lana mapped_summary/spans, identity claims, participation stats.';

revoke all on function public.get_my_profile_dashboard() from public, anon;
grant execute on function public.get_my_profile_dashboard() to authenticated;

grant execute on function public.get_my_profile() to authenticated;
