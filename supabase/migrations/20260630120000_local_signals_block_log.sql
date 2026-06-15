-- v0.2 substrate: local_signals, block_log_entries, match_notifications, reason_codes.
-- Matcher runs on save (no batch). block_id is text (H3) per public.blocks.

-- §1 reason_codes (Yunchao can seed more later)
create table if not exists public.reason_codes (
  code text primary key,
  category text not null,
  privacy_tier int not null default 1,
  template text not null,
  template_locale_pt text,
  template_locale_es text
);

insert into public.reason_codes (code, category, privacy_tier, template) values
  ('swap_offer_matches_seek', 'swap', 1, '{peer_detail} matches what you''re looking for'),
  ('swap_seek_matches_offer', 'swap', 1, 'Your offer may help someone seeking {peer_detail}'),
  ('same_block_neighbor', 'vicinity', 1, 'Same block neighbor'),
  ('meet_host_match', 'meet', 1, 'A neighbor wants to meet — you offered to host'),
  ('meet_seek_match', 'meet', 1, 'Someone nearby is looking for a meetup like yours'),
  ('tip_share_match', 'tip', 1, 'A neighbor shared a tip in {category}'),
  ('tip_seek_match', 'tip', 1, 'Someone asked for a rec in {category}')
on conflict (code) do nothing;

alter table public.reason_codes enable row level security;
create policy reason_codes_read_authenticated on public.reason_codes
  for select to authenticated using (true);

-- §2 local_signals
create table if not exists public.local_signals (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users (id) on delete cascade,
  block_id text references public.blocks (id) on delete set null,
  zip text,
  intent text not null check (intent in (
    'swap_seek', 'swap_offer', 'meet_seek', 'host_meet', 'tip_seek', 'tip_share'
  )),
  category text,
  detail_text text not null,
  affinity_tags text[] not null default '{}',
  stage text,
  status text not null default 'listening' check (status in (
    'listening', 'matched', 'published', 'closed', 'expired'
  )),
  source_surface text not null default 'lana',
  contact_permission boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  expires_at timestamptz not null default (now() + interval '14 days')
);

create index if not exists idx_local_signals_block_intent
  on public.local_signals (block_id, intent)
  where status = 'listening';

create index if not exists idx_local_signals_user_active
  on public.local_signals (user_id, created_at desc)
  where status = 'listening';

alter table public.local_signals enable row level security;

create policy local_signals_select_own on public.local_signals
  for select to authenticated
  using (user_id = auth.uid());

-- §3 block_log_entries
create table if not exists public.block_log_entries (
  id uuid primary key default gen_random_uuid(),
  for_user_id uuid not null references public.users (id) on delete cascade,
  match_type text not null check (match_type in (
    'inbound_for_my_seek', 'inbound_for_my_offer',
    'meet_invite_potential', 'meet_attendee_potential',
    'fellow_overlap_high', 'tip_match'
  )),
  my_signal_id uuid references public.local_signals (id) on delete set null,
  peer_signal_id uuid references public.local_signals (id) on delete set null,
  peer_user_id uuid references public.users (id) on delete set null,
  block_id text not null references public.blocks (id) on delete cascade,
  match_strength real not null check (match_strength >= 0 and match_strength <= 1),
  match_reasons text[] not null default '{}',
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  user_acted_at timestamptz,
  action_taken text check (action_taken in ('nudged', 'dismissed', 'saved', 'ignored')),
  notification_sent_to_peer boolean not null default false,
  notification_sent_at timestamptz
);

create index if not exists idx_block_log_user_active
  on public.block_log_entries (for_user_id, created_at desc)
  where action_taken is null and expires_at > now();

create index if not exists idx_block_log_block_recent
  on public.block_log_entries (block_id, created_at desc);

alter table public.block_log_entries enable row level security;

create policy block_log_select_own on public.block_log_entries
  for select to authenticated
  using (for_user_id = auth.uid());

-- §4 match_notifications (audit log; Twilio/push wired later)
create table if not exists public.match_notifications (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users (id) on delete cascade,
  block_log_entry_id uuid references public.block_log_entries (id) on delete set null,
  channel text not null check (channel in ('push', 'sms', 'in_app')),
  status text not null default 'queued' check (status in ('queued', 'sent', 'failed', 'skipped')),
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  sent_at timestamptz
);

create index if not exists idx_match_notifications_user_recent
  on public.match_notifications (user_id, created_at desc);

alter table public.match_notifications enable row level security;

create policy match_notifications_select_own on public.match_notifications
  for select to authenticated
  using (user_id = auth.uid());

-- §5 matcher helpers
create or replace function public._signal_match_strength(
  p_my_category text,
  p_my_detail text,
  p_peer_category text,
  p_peer_detail text
)
returns real
language plpgsql
immutable
as $$
declare
  v_strength real := 0.72;
  v_word text;
begin
  if p_my_category is not null and p_peer_category is not null
     and lower(trim(p_my_category)) = lower(trim(p_peer_category)) then
    v_strength := v_strength + 0.08;
  end if;

  if p_my_detail is not null and p_peer_detail is not null then
    foreach v_word in array regexp_split_to_array(lower(p_my_detail), '\s+') loop
      if length(v_word) > 3 and position(v_word in lower(p_peer_detail)) > 0 then
        v_strength := v_strength + 0.1;
        exit;
      end if;
    end loop;
  end if;

  return least(v_strength, 0.95);
end;
$$;

create or replace function public._match_local_signal(p_signal_id uuid)
returns int
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_sig record;
  v_peer record;
  v_strength real;
  v_reasons text[];
  v_match_type_for_me text;
  v_match_type_for_peer text;
  v_inserted int := 0;
  v_peer_detail text;
  v_category text;
  v_entry_me uuid;
  v_entry_peer uuid;
  v_reason_me text;
  v_reason_peer text;
begin
  select * into v_sig
  from public.local_signals
  where id = p_signal_id and status = 'listening' and block_id is not null;

  if not found then
    return 0;
  end if;

  for v_peer in
    select s.*
    from public.local_signals s
    where s.block_id = v_sig.block_id
      and s.status = 'listening'
      and s.user_id <> v_sig.user_id
      and s.expires_at > now()
      and (
        (v_sig.intent = 'swap_seek' and s.intent = 'swap_offer')
        or (v_sig.intent = 'swap_offer' and s.intent = 'swap_seek')
        or (v_sig.intent = 'meet_seek' and s.intent = 'host_meet')
        or (v_sig.intent = 'host_meet' and s.intent = 'meet_seek')
        or (v_sig.intent = 'tip_seek' and s.intent = 'tip_share')
        or (v_sig.intent = 'tip_share' and s.intent = 'tip_seek')
      )
  loop
    v_strength := public._signal_match_strength(
      v_sig.category, v_sig.detail_text, v_peer.category, v_peer.detail_text
    );
    if v_strength < 0.65 then
      continue;
    end if;

    if exists (
      select 1 from public.block_log_entries e
      where e.for_user_id = v_sig.user_id
        and e.peer_signal_id = v_peer.id
        and e.created_at > now() - interval '24 hours'
    ) then
      continue;
    end if;

    v_peer_detail := coalesce(v_peer.detail_text, 'something nearby');
    v_category := coalesce(v_peer.category, v_sig.category, 'your block');

    if v_sig.intent in ('swap_seek', 'tip_seek', 'meet_seek') then
      v_match_type_for_me := case
        when v_sig.intent = 'swap_seek' then 'inbound_for_my_seek'
        when v_sig.intent = 'meet_seek' then 'meet_attendee_potential'
        else 'tip_match'
      end;
      v_match_type_for_peer := case
        when v_peer.intent = 'swap_offer' then 'inbound_for_my_offer'
        when v_peer.intent = 'host_meet' then 'meet_invite_potential'
        else 'tip_match'
      end;
      v_reason_me := replace(
        coalesce((select template from public.reason_codes where code = case
          when v_sig.intent = 'swap_seek' then 'swap_offer_matches_seek'
          when v_sig.intent = 'meet_seek' then 'meet_seek_match'
          else 'tip_seek_match'
        end), 'Neighbor match'),
        '{peer_detail}', v_peer_detail
      );
      v_reason_peer := replace(
        coalesce((select template from public.reason_codes where code = case
          when v_peer.intent = 'swap_offer' then 'swap_seek_matches_offer'
          when v_peer.intent = 'host_meet' then 'meet_host_match'
          else 'tip_share_match'
        end), 'Neighbor match'),
        '{peer_detail}', coalesce(v_sig.detail_text, 'a neighbor'),
        '{category}', v_category
      );
    else
      v_match_type_for_me := case
        when v_sig.intent = 'swap_offer' then 'inbound_for_my_offer'
        when v_sig.intent = 'host_meet' then 'meet_invite_potential'
        else 'tip_match'
      end;
      v_match_type_for_peer := case
        when v_peer.intent = 'swap_seek' then 'inbound_for_my_seek'
        when v_peer.intent = 'meet_seek' then 'meet_attendee_potential'
        else 'tip_match'
      end;
      v_reason_me := replace(
        coalesce((select template from public.reason_codes where code = case
          when v_sig.intent = 'swap_offer' then 'swap_seek_matches_offer'
          when v_sig.intent = 'host_meet' then 'meet_host_match'
          else 'tip_share_match'
        end), 'Neighbor match'),
        '{peer_detail}', v_peer_detail,
        '{category}', v_category
      );
      v_reason_peer := replace(
        coalesce((select template from public.reason_codes where code = case
          when v_peer.intent = 'swap_seek' then 'swap_offer_matches_seek'
          when v_peer.intent = 'meet_seek' then 'meet_seek_match'
          else 'tip_seek_match'
        end), 'Neighbor match'),
        '{peer_detail}', coalesce(v_sig.detail_text, 'a neighbor'),
        '{category}', v_category
      );
    end if;

    v_reasons := array[v_reason_me, (select template from public.reason_codes where code = 'same_block_neighbor')];

    insert into public.block_log_entries (
      for_user_id, match_type, my_signal_id, peer_signal_id, peer_user_id,
      block_id, match_strength, match_reasons, expires_at
    ) values (
      v_sig.user_id, v_match_type_for_me, v_sig.id, v_peer.id, v_peer.user_id,
      v_sig.block_id, v_strength, v_reasons, now() + interval '14 days'
    )
    returning id into v_entry_me;
    v_inserted := v_inserted + 1;

    insert into public.block_log_entries (
      for_user_id, match_type, my_signal_id, peer_signal_id, peer_user_id,
      block_id, match_strength, match_reasons, expires_at,
      notification_sent_to_peer
    ) values (
      v_peer.user_id, v_match_type_for_peer, v_peer.id, v_sig.id, v_sig.user_id,
      v_sig.block_id, v_strength,
      array[v_reason_peer, (select template from public.reason_codes where code = 'same_block_neighbor')],
      now() + interval '14 days',
      v_strength >= 0.80
    )
    returning id into v_entry_peer;
    v_inserted := v_inserted + 1;

    if v_strength >= 0.75 then
      insert into public.match_notifications (user_id, block_log_entry_id, channel, status, payload)
      values
        (v_sig.user_id, v_entry_me, 'in_app', 'queued',
          jsonb_build_object('match_strength', v_strength, 'peer_user_id', v_peer.user_id)),
        (v_peer.user_id, v_entry_peer, 'in_app', 'queued',
          jsonb_build_object('match_strength', v_strength, 'peer_user_id', v_sig.user_id));
    end if;
  end loop;

  return v_inserted;
end;
$$;

revoke all on function public._signal_match_strength(text, text, text, text) from public, anon;
revoke all on function public._match_local_signal(uuid) from public, anon;

-- §6 save_local_signal RPC
create or replace function public.save_local_signal(
  p_intent text,
  p_detail_text text,
  p_category text default null,
  p_block_id text default null,
  p_zip text default null,
  p_affinity_tags text[] default '{}',
  p_stage text default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_block text;
  v_row public.local_signals%rowtype;
  v_matches int;
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  if p_intent not in (
    'swap_seek', 'swap_offer', 'meet_seek', 'host_meet', 'tip_seek', 'tip_share'
  ) then
    raise exception 'invalid_intent' using errcode = 'P0001';
  end if;

  if p_detail_text is null or length(trim(p_detail_text)) < 2 then
    raise exception 'detail_required' using errcode = 'P0001';
  end if;

  v_block := coalesce(
    p_block_id,
    (select home_block_id from public.users where id = v_me)
  );

  if v_block is null then
    raise exception 'block_required' using errcode = 'P0001';
  end if;

  insert into public.local_signals (
    user_id, block_id, zip, intent, category, detail_text,
    affinity_tags, stage, status, source_surface
  ) values (
    v_me, v_block, p_zip, p_intent, nullif(trim(p_category), ''),
    left(trim(p_detail_text), 500),
    coalesce(p_affinity_tags, '{}'), nullif(trim(p_stage), ''),
    'listening', 'lana'
  )
  returning * into v_row;

  v_matches := public._match_local_signal(v_row.id);

  return jsonb_build_object(
    'signal_id', v_row.id,
    'intent', v_row.intent,
    'category', v_row.category,
    'detail_text', v_row.detail_text,
    'block_id', v_row.block_id,
    'matches_created', v_matches
  );
end;
$$;

revoke all on function public.save_local_signal(text, text, text, text, text, text[], text) from public, anon;
grant execute on function public.save_local_signal(text, text, text, text, text, text[], text) to authenticated;

-- §7 get_my_block_log RPC
create or replace function public.get_my_block_log()
returns table (
  id uuid,
  match_type text,
  peer_user_id uuid,
  peer_preview_label text,
  match_strength real,
  match_reasons text[],
  created_at timestamptz,
  expires_at timestamptz,
  notification_sent_to_peer boolean,
  block_id text,
  block_name text
)
language plpgsql
security invoker
set search_path = pg_catalog, public
stable
as $$
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  return query
  select
    e.id,
    e.match_type,
    e.peer_user_id,
    coalesce(u.nickname, 'A neighbor on your block') as peer_preview_label,
    e.match_strength,
    e.match_reasons,
    e.created_at,
    e.expires_at,
    e.notification_sent_to_peer,
    e.block_id,
    b.display_name as block_name
  from public.block_log_entries e
  left join public.users u on u.id = e.peer_user_id
  left join public.blocks b on b.id = e.block_id
  where e.for_user_id = auth.uid()
    and e.action_taken is null
    and e.expires_at > now()
  order by e.match_strength desc, e.created_at desc
  limit 20;
end;
$$;

revoke all on function public.get_my_block_log() from public, anon;
grant execute on function public.get_my_block_log() to authenticated;

-- §8 block_log_action RPC
create or replace function public.block_log_action(
  p_entry_id uuid,
  p_action text
)
returns jsonb
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_row public.block_log_entries%rowtype;
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  if p_action not in ('nudged', 'dismissed', 'saved', 'ignored') then
    raise exception 'invalid_action' using errcode = 'P0001';
  end if;

  update public.block_log_entries e
  set action_taken = p_action,
      user_acted_at = now()
  where e.id = p_entry_id
    and e.for_user_id = auth.uid()
  returning * into v_row;

  if not found then
    raise exception 'entry_not_found' using errcode = 'P0001';
  end if;

  return jsonb_build_object('id', v_row.id, 'action_taken', v_row.action_taken);
end;
$$;

revoke all on function public.block_log_action(uuid, text) from public, anon;
grant execute on function public.block_log_action(uuid, text) to authenticated;
