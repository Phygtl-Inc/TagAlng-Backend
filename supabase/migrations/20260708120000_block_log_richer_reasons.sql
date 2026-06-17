-- Richer block-log copy: include neighbor ask + your offer from local_signals.detail_text.

update public.reason_codes set template = 'They want to meet: {peer_detail} · You offered: {my_detail}'
where code = 'meet_host_match';

update public.reason_codes set template = 'You want to meet: {my_detail} · They offered to host: {peer_detail}'
where code = 'meet_seek_match';

update public.reason_codes set template = '{peer_detail} matches your ask: {my_detail}'
where code = 'swap_offer_matches_seek';

update public.reason_codes set template = 'A neighbor is offering: {peer_detail} · You want: {my_detail}'
where code = 'swap_seek_matches_offer';

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
  v_my_detail text;
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

    v_peer_detail := coalesce(nullif(trim(v_peer.detail_text), ''), 'something nearby');
    v_my_detail := coalesce(nullif(trim(v_sig.detail_text), ''), 'your post');
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
        replace(
          coalesce((select template from public.reason_codes where code = case
            when v_sig.intent = 'swap_seek' then 'swap_offer_matches_seek'
            when v_sig.intent = 'meet_seek' then 'meet_seek_match'
            else 'tip_seek_match'
          end), 'Neighbor match'),
          '{peer_detail}', v_peer_detail
        ),
        '{my_detail}', v_my_detail
      );
      v_reason_peer := replace(
        replace(
          replace(
            coalesce((select template from public.reason_codes where code = case
              when v_peer.intent = 'swap_offer' then 'swap_seek_matches_offer'
              when v_peer.intent = 'host_meet' then 'meet_host_match'
              else 'tip_share_match'
            end), 'Neighbor match'),
            '{peer_detail}', coalesce(nullif(trim(v_sig.detail_text), ''), 'a neighbor')
          ),
          '{my_detail}', v_peer_detail
        ),
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
        replace(
          replace(
            coalesce((select template from public.reason_codes where code = case
              when v_sig.intent = 'swap_offer' then 'swap_seek_matches_offer'
              when v_sig.intent = 'host_meet' then 'meet_host_match'
              else 'tip_share_match'
            end), 'Neighbor match'),
            '{peer_detail}', v_peer_detail
          ),
          '{my_detail}', v_my_detail
        ),
        '{category}', v_category
      );
      v_reason_peer := replace(
        replace(
          replace(
            coalesce((select template from public.reason_codes where code = case
              when v_peer.intent = 'swap_seek' then 'swap_offer_matches_seek'
              when v_peer.intent = 'meet_seek' then 'meet_seek_match'
              else 'tip_seek_match'
            end), 'Neighbor match'),
            '{peer_detail}', coalesce(nullif(trim(v_sig.detail_text), ''), 'a neighbor')
          ),
          '{my_detail}', v_peer_detail
        ),
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

-- Postgres cannot CREATE OR REPLACE when OUT/table return type changes.
drop function if exists public.get_my_block_log();

create function public.get_my_block_log()
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
  block_name text,
  my_signal_detail text,
  peer_signal_detail text,
  my_signal_intent text,
  peer_signal_intent text
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
    b.display_name as block_name,
    coalesce(my_s.detail_text, '') as my_signal_detail,
    coalesce(peer_s.detail_text, '') as peer_signal_detail,
    my_s.intent as my_signal_intent,
    peer_s.intent as peer_signal_intent
  from public.block_log_entries e
  left join public.users u on u.id = e.peer_user_id
  left join public.blocks b on b.id = e.block_id
  left join public.local_signals my_s on my_s.id = e.my_signal_id
  left join public.local_signals peer_s on peer_s.id = e.peer_signal_id
  where e.for_user_id = auth.uid()
    and e.action_taken is null
    and e.expires_at > now()
  order by e.match_strength desc, e.created_at desc
  limit 20;
end;
$$;

comment on function public.get_my_block_log() is
  'Read-only pending block log rows for caller, with linked signal detail_text for display.';

revoke all on function public.get_my_block_log() from public, anon;
grant execute on function public.get_my_block_log() to authenticated;
