-- An edited recommendation says the same thing everywhere, and a shared one can unfurl.
--
-- §47 (issues #111). The author corrects her own recommendation straight through
-- set_signal_reco (PWA `updateTip`). Two residues on that write path:
--
--   1. detail_text was never re-joined. 20261206120000 already moved get_my_contributions
--      (the Radar) onto reco_name, so the Radar TITLE is not what drifts any more. What
--      still reads detail_text is everything that MATCHES: _tip_match_strength's word
--      overlap (find_neighbor_tips, the block-log matcher), the block-log reason line
--      ("{peer_detail}"), close_local_signal's echo, the legacy rec-cascade row
--      (`tip_text`), and the worker's embedding text. Moving each of those onto the card
--      columns is five changes in five places; re-joining detail_text here is one, and it
--      is the one that keeps every surface — readers AND matchers — on the same words.
--      So: re-joined here, on an edit only (see v_is_edit below for why not on capture).
--
--   2. Nothing re-matched and nothing re-embedded. The vector is produced by Vertex in the
--      worker and cannot be computed in SQL, so this RPC does the two halves it CAN do:
--        * drops the stale vector (embedding := null), so the edited row stops matching on
--          words it no longer says — it is lexical-only until re-embedded, which is honest;
--        * drops the block-log rows this signal produced that nobody has acted on yet
--          (both directions, plus their queued notifications — close_local_signal's
--          pattern), then re-runs _match_local_signal on the new words.
--      The worker route POST /lana/tips/update calls this RPC and then writes the new
--      vector and refreshes matches, so an edit through it is fully re-embedded.
--
-- §35(d). A kind:"place" step's answer row may now carry `google_place_id` (the worker
-- writes it at capture). The PWA's edit path re-sends reco_fields with five keys only, so
-- an edit would silently strip the id: for a place row whose (field, answer) is unchanged
-- and that arrives without an id, the stored id is carried over. A changed answer loses
-- it, which is right — the id belonged to the place she no longer names.
--
-- §47(3) (clearing a value) is NOT here: the blank-means-leave-it contract is unchanged,
-- pending a product ruling on the sentinel shape.
--
-- Body copied from 20261130120000 (the latest definition); validation and the coalesce
-- update are unchanged. Signature unchanged, so create-or-replace keeps the grants; they
-- are restated anyway because revoke-from-public alone is a no-op on Supabase.

create or replace function public.set_signal_reco(
  p_signal_id        uuid,
  p_reco_type        text default null,
  p_reco_fields      jsonb default null,
  p_reco_subject     text default null,
  p_reco_name        text default null,
  p_reco_place       text default null,
  p_reco_description text default null
)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_old public.local_signals%rowtype;
  v_new public.local_signals%rowtype;
  v_fields jsonb := p_reco_fields;
  v_is_edit boolean;
  v_changed boolean;
  v_parts text[];
  v_detail text;
  v_entries uuid[];
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  if p_reco_type is not null and p_reco_type not in (
    'professional', 'restaurant', 'recipe', 'product', 'location', 'service', 'diy',
    'other'
  ) then
    raise exception 'invalid_reco_type' using errcode = 'P0001';
  end if;

  if p_reco_fields is not null and jsonb_typeof(p_reco_fields) <> 'array' then
    raise exception 'reco_fields_must_be_array' using errcode = 'P0001';
  end if;

  select * into v_old
  from public.local_signals
  where id = p_signal_id and user_id = v_me
  for update;

  if not found then
    return;
  end if;

  -- Keep a place row's Google id across an edit that re-sends the row without it.
  if v_fields is not null and jsonb_typeof(v_old.reco_fields) = 'array' then
    select coalesce(jsonb_agg(
             case
               when jsonb_typeof(f) <> 'object'
                    or f ? 'google_place_id'
                    or coalesce(f ->> 'kind', '') <> 'place'
                 then f
               else coalesce((
                 select f || jsonb_build_object('google_place_id', o ->> 'google_place_id')
                 from jsonb_array_elements(v_old.reco_fields) o
                 where jsonb_typeof(o) = 'object'
                   and o ->> 'field' = f ->> 'field'
                   and btrim(coalesce(o ->> 'answer', '')) = btrim(coalesce(f ->> 'answer', ''))
                   and nullif(btrim(coalesce(o ->> 'google_place_id', '')), '') is not null
                 limit 1
               ), f)
             end
             order by t.ord
           ), '[]'::jsonb)
      into v_fields
    from jsonb_array_elements(v_fields) with ordinality as t(f, ord);
  end if;

  update public.local_signals
     set reco_type = coalesce(p_reco_type, reco_type),
         reco_fields = coalesce(v_fields, reco_fields),
         reco_subject = coalesce(nullif(btrim(lower(p_reco_subject)), ''), reco_subject),
         reco_name = coalesce(nullif(btrim(p_reco_name), ''), reco_name),
         reco_place = coalesce(nullif(btrim(p_reco_place), ''), reco_place),
         reco_description = coalesce(nullif(btrim(p_reco_description), ''), reco_description),
         updated_at = now()
   where id = v_old.id
  returning * into v_new;

  -- An EDIT, not the capture's own stamp. save_local_signal inserts the row with every card
  -- column empty and the worker stamps them one call later; at that moment detail_text is
  -- the worker's own join and is also save_local_signal's dedupe key, so re-joining it here
  -- would make the same recommendation posted twice stop deduping. Any card column already
  -- set (reco_type is floored to 'other' at capture; legacy rows had reco_name backfilled)
  -- means the row has been stamped before, and this call is a correction.
  v_is_edit := v_old.reco_type is not null
    or nullif(btrim(v_old.reco_name), '') is not null
    or nullif(btrim(v_old.reco_description), '') is not null
    or nullif(btrim(v_old.reco_place), '') is not null
    or (jsonb_typeof(v_old.reco_fields) = 'array' and jsonb_array_length(v_old.reco_fields) > 0);

  v_changed := v_new.reco_name is distinct from v_old.reco_name
    or v_new.reco_place is distinct from v_old.reco_place
    or v_new.reco_description is distinct from v_old.reco_description
    or v_new.reco_fields is distinct from v_old.reco_fields;

  if not (v_is_edit and v_changed and v_new.intent = 'tip_share') then
    return;
  end if;

  -- The join the worker's _detail_text writes at capture: name · category · the author's
  -- own words · "Label: answer" per answered step · the area. The closing steps (consent,
  -- agree row), the subject and the community are not the recommendation's words, so they
  -- are left out exactly as the worker leaves them out.
  v_parts := array_remove(array[
      nullif(btrim(v_new.reco_name), ''),
      nullif(btrim(v_new.category), ''),
      nullif(btrim(v_new.reco_description), '')
    ], null)
    || coalesce((
      select array_agg(btrim(f ->> 'label') || ': ' || btrim(f ->> 'answer') order by t.ord)
      from jsonb_array_elements(
             case when jsonb_typeof(v_new.reco_fields) = 'array'
                  then v_new.reco_fields else '[]'::jsonb end
           ) with ordinality as t(f, ord)
      where jsonb_typeof(f) = 'object'
        and nullif(btrim(coalesce(f ->> 'label', '')), '') is not null
        and nullif(btrim(coalesce(f ->> 'answer', '')), '') is not null
        and coalesce(f ->> 'field', '') not in ('ask_ok', 'others_also_said', 'subject', 'community')
    ), '{}'::text[])
    || array_remove(array[nullif(btrim(v_new.reco_place), '')], null);

  v_detail := left(array_to_string(v_parts, ' · '), 500);

  update public.local_signals
     set detail_text = case when length(btrim(coalesce(v_detail, ''))) >= 2
                            then v_detail else detail_text end,
         -- The vector of the words she no longer says. Null = lexical only, until the
         -- worker (POST /lana/tips/update, or the backfill script) writes the new one.
         embedding = null
   where id = v_new.id;

  -- Re-match on the new words. Rows somebody already acted on (nudged, saved, dismissed)
  -- are history and stay; the untouched ones were a claim about the OLD wording.
  select array_agg(e.id) into v_entries
  from public.block_log_entries e
  where (e.my_signal_id = v_new.id or e.peer_signal_id = v_new.id)
    and e.action_taken is null
    and e.user_acted_at is null;

  if v_entries is not null then
    delete from public.match_notifications
    where block_log_entry_id = any (v_entries)
      and status = 'queued';
    delete from public.block_log_entries
    where id = any (v_entries);
  end if;

  if v_new.status = 'listening' then
    perform public._match_local_signal(v_new.id);
  end if;
end;
$$;

comment on function public.set_signal_reco(uuid, text, jsonb, text, text, text, text) is
  'Write (or correct) the caller''s own recommendation card fields. On a correction it '
  're-joins detail_text from the card, drops the stale embedding (the worker re-embeds via '
  'POST /lana/tips/update) and re-matches; a place row''s google_place_id survives an edit '
  'that leaves its answer unchanged. Blank still means "leave it".';

revoke all on function public.set_signal_reco(uuid, text, jsonb, text, text, text, text)
  from public, anon, authenticated;
grant execute on function public.set_signal_reco(uuid, text, jsonb, text, text, text, text)
  to authenticated, service_role;


-- ----------------------------------------------------------------------------
-- §40 (issues #105). The link preview of a shared recommendation.
--
-- Unfurlers (Slack, WhatsApp, iMessage) fetch /r/<id> with no session, so the page's
-- metadata cannot authenticate. get_event_preview is the precedent: an anon-safe RPC read
-- with the publishable key. This is its twin for a recommendation, and it says strictly
-- LESS than the page: a preview is read by everyone in the chat the link was pasted into.
--
--   subject  — the card's title: reco_name, else the first segment of a legacy detail_text
--              (only the first: later segments of the old join can hold a phone number).
--   reco_type / category — the kind ("professional" / "pediatric dentist").
--   quote    — the author's own one line (reco_description), first line, capped.
--
-- Never the author (no id, nickname, avatar), never her block / area / place, never the
-- answered steps, never counts. Withdrawn, missing, or not a recommendation -> NULL (an
-- empty body), not an error. EXPIRED still previews: expires_at is feed freshness, not the
-- recommendation's life — the same rule /lana/tips/get (tip_feed.tip_by_id) follows.
-- ----------------------------------------------------------------------------
create or replace function public.get_reco_preview(p_signal_id uuid)
returns jsonb
language sql
security definer
set search_path = pg_catalog, public
stable
as $$
  select jsonb_build_object(
           'signal_id', s.id,
           'subject', coalesce(
             nullif(btrim(s.reco_name), ''),
             nullif(btrim(split_part(s.detail_text, ' · ', 1)), '')
           ),
           'reco_type', nullif(btrim(s.reco_type), ''),
           'category', nullif(btrim(s.category), ''),
           'quote', nullif(left(btrim(split_part(coalesce(s.reco_description, ''), E'\n', 1)), 200), '')
         )
  from public.local_signals s
  where s.id = p_signal_id
    and s.intent = 'tip_share'
    and s.status = 'listening'
  limit 1
$$;

comment on function public.get_reco_preview(uuid) is
  'Anon-safe link preview of a shared recommendation (/r/<id>): subject, kind and the '
  'author''s one-line quote only. Never the author, her block, or the answered steps. '
  'NULL when withdrawn or missing.';

revoke all on function public.get_reco_preview(uuid) from public, anon, authenticated;
grant execute on function public.get_reco_preview(uuid) to anon, authenticated, service_role;
