-- Layer 1 completion: block summary, attr-filtered peers, notification prefs.

alter table public.users
  add column if not exists notification_prefs jsonb not null default '{"sms":"normal"}'::jsonb;

comment on column public.users.notification_prefs is
  'User messaging prefs. sms: normal|quiet|off (Layer 1 settings.notification_prefs).';

-- Block summary for discovery.find_in_block
create or replace function public.get_block_summary()
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
stable
as $$
declare
  v_me uuid := auth.uid();
  v_block_id text;
  v_block_name text;
  v_block_state text;
  v_neighbor_count int;
  v_signal_count int;
  v_match_count int;
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  select u.home_block_id, b.display_name, b.state::text
  into v_block_id, v_block_name, v_block_state
  from public.users u
  left join public.blocks b on b.id = u.home_block_id
  where u.id = v_me;

  select count(*)::int into v_neighbor_count
  from public.users u
  where u.home_block_id = v_block_id
    and u.id <> v_me;

  select count(*)::int into v_signal_count
  from public.local_signals s
  where s.block_id = v_block_id
    and s.status = 'listening'
    and s.expires_at > now();

  select count(*)::int into v_match_count
  from public.block_log_entries e
  where e.for_user_id = v_me
    and e.expires_at > now()
    and e.user_acted_at is null;

  return jsonb_build_object(
    'block_id', v_block_id,
    'block_name', coalesce(v_block_name, 'your block'),
    'block_state', v_block_state,
    'neighbor_count', coalesce(v_neighbor_count, 0),
    'active_signal_count', coalesce(v_signal_count, 0),
    'active_match_count', coalesce(v_match_count, 0)
  );
end;
$$;

comment on function public.get_block_summary() is
  'Layer 1 discovery.find_in_block — block-scoped activity summary.';

revoke all on function public.get_block_summary() from public, anon;
grant execute on function public.get_block_summary() to authenticated;

-- Filtered peer discovery for discovery.find_by_attrs
create or replace function public.find_peers_by_attr_filter(
  p_filter_text text,
  p_limit int default 5
)
returns table (
  peer_user_id uuid,
  nickname text,
  avatar_url text,
  similarity_score real,
  matching_peer_label text,
  matching_peer_concept text,
  has_exact_concept_match boolean
)
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
stable
as $$
declare
  v_caller uuid := auth.uid();
  v_block_id text;
  v_filter text := lower(trim(coalesce(p_filter_text, '')));
begin
  if v_caller is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if char_length(v_filter) < 2 then
    raise exception 'filter_too_short' using errcode = 'P0001';
  end if;

  select u.home_block_id into v_block_id
  from public.users u where u.id = v_caller;

  if v_block_id is null then
    return;
  end if;

  return query
  with matched as (
    select distinct
      c.user_id as peer_id,
      c.label as match_label,
      c.concept as match_concept,
      0.85::real as sim
    from public.user_identity_claims c
    join public.users u on u.id = c.user_id
    where u.home_block_id = v_block_id
      and c.user_id <> v_caller
      and c.dismissed_at is null
      and c.disclosure = 'public'
      and (
        lower(c.label) like '%' || v_filter || '%'
        or lower(c.concept) like '%' || v_filter || '%'
        or exists (
          select 1 from unnest(coalesce(c.synonyms, '{}')) s
          where lower(s) like '%' || v_filter || '%'
        )
      )
  )
  select
    m.peer_id,
    u.nickname,
    u.profile_photo_url,
    m.sim,
    m.match_label,
    m.match_concept,
    true
  from matched m
  join public.users u on u.id = m.peer_id
  order by u.nickname asc nulls last
  limit greatest(1, least(coalesce(p_limit, 5), 20));
end;
$$;

comment on function public.find_peers_by_attr_filter(text, int) is
  'Layer 1 discovery.find_by_attrs — label/concept filter on same block.';

revoke all on function public.find_peers_by_attr_filter(text, int) from public, anon;
grant execute on function public.find_peers_by_attr_filter(text, int) to authenticated;

-- Notification prefs update
create or replace function public.update_user_notification_prefs(p_sms text default 'normal')
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_sms text := lower(trim(coalesce(p_sms, 'normal')));
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if v_sms not in ('normal', 'quiet', 'off') then
    raise exception 'invalid_sms_pref' using errcode = 'P0001';
  end if;

  update public.users
  set notification_prefs = jsonb_build_object('sms', v_sms),
      updated_at = now()
  where id = v_me;

  return jsonb_build_object('sms', v_sms);
end;
$$;

comment on function public.update_user_notification_prefs(text) is
  'Layer 1 settings.notification_prefs — sms frequency.';

revoke all on function public.update_user_notification_prefs(text) from public, anon;
grant execute on function public.update_user_notification_prefs(text) to authenticated;
