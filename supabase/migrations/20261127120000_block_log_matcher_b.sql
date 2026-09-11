-- Block log onto matcher B.
--
-- The block log has scored on _signal_match_strength since Layer 1 (20260708): count
-- words >=4 chars that appear as a SUBSTRING of the peer's detail, 0.68 + overlap*0.08.
-- One shared word = 0.76 for every pair, so `order by match_strength desc` had nothing
-- to sort on and the card rendered "76%" on unrelated rows (restaurant / park /
-- stationery all identical, prod 2026-09-10).
--
-- The peer surface was moved off raw scores by 20260820120000_truthful_peer_match
-- (badges from proven overlap, no percentage). This does the same for the block log:
--   * tip pairs score through _tip_match_strength (words OR affinity_tags OR cosine),
--     the same scorer find_neighbor_tips uses, so ranking is real;
--   * the worker stops sending match_strength to the client and sends the truthful
--     match_badge instead (app/local_signals.py).
--
-- Unchanged on purpose: the 0.65 admission floor, the 0.75 notify and 0.80
-- notification_sent_to_peer thresholds. _tip_match_strength returns on the same
-- 0.68-0.95 band as _signal_match_strength, so those cutoffs keep their meaning.
--
-- Consequence worth knowing: _tip_match_strength has no category branch, so a tip
-- pair that shared ONLY a category (no words, no tags, no cosine) used to be logged
-- at a flat 0.72 and now scores 0 and is dropped. That is the 20261126 ruling, and
-- it means fewer but truer tip rows in the log.
--
-- Existing rows keep the score they were written with -- the matcher's 24h re-match
-- guard skips pairs it has already logged, and entries expire in 14 days. Only the
-- display changes for them, which is the part that was untrue.

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
    -- Tip pairs score on matcher B (_tip_match_strength, 20261126: best of word
    -- overlap, affinity_tags hit, and cosine). Swap/meet pairs keep
    -- _signal_match_strength -- 20261126 ruled the semantic scorer tip-only, and
    -- nothing writes embeddings for swap/meet signals, so it would score them 0.
    -- 0.50 is the measured cosine floor; keep in sync with local_signals.
    -- _tip_min_similarity(), which carries the prod probe that set it.
    -- ponytail: at INSERT time the new signal has no embedding yet (the worker
    -- calls set_signal_embedding after save_local_signal returns), so the first
    -- pass scores words + peer tags only. refresh_my_signal_matches picks the
    -- semantic pairs up on the next block-log read. Move the embedding write
    -- ahead of the match inside save_local_signal if that lag ever matters.
    if v_sig.intent in ('tip_seek', 'tip_share') then
      v_strength := public._tip_match_strength(
        v_sig.detail_text, v_peer.detail_text, v_peer.affinity_tags,
        v_sig.embedding, v_peer.embedding, 0.50
      );
    else
      v_strength := public._signal_match_strength(
        v_sig.category, v_sig.detail_text, v_peer.category, v_peer.detail_text
      );
    end if;
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

comment on function public._match_local_signal(uuid) is
  'Write block-log entries for a new signal. Tip pairs score on _tip_match_strength '
  '(matcher B); swap/meet pairs keep _signal_match_strength.';
