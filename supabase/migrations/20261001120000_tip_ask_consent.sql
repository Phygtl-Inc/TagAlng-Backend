-- Tip-seek becomes an ANSWER, not a write. Three pieces the consent flow needs:
--
--   1. find_neighbor_tips  — READ-ONLY peek at neighbors' tip_share posts, so Lana can
--      answer "recommend me a doctor" with a real neighbor rec WITHOUT first inserting a
--      tip_seek row. Before this the only way to discover a neighbor tip was to insert the
--      seek and let _match_local_signal run, which is why every question became a posting
--      (and notified neighbors at strength >= 0.80).
--   2. close_local_signal  — Lana's own copy has always offered to "remove your posting";
--      nothing could. Closes the caller's posting and drops the match rows it created in
--      both directions, so the neighbour side stops seeing an ask the user withdrew.
--   3. save_local_signal   — reuse the caller's identical open posting instead of inserting
--      a duplicate. One question that re-entered the tip lane three times produced three
--      live postings; now re-entry is idempotent.

-- ---------------------------------------------------------------------------
-- §1 find_neighbor_tips — read-only, no writes, no peer identity
-- ---------------------------------------------------------------------------
-- security definer: neighbors' local_signals rows are RLS-hidden from the caller (select
-- policy is user_id = auth.uid()), and this is the only read that crosses that line. It
-- returns the tip TEXT plus a coarse label — never the peer's user_id — because nothing is
-- matched yet; identity is earned through the block log after a real match.
--
-- Floor is 0.74, deliberately stricter than the matcher's 0.65: _signal_match_strength
-- returns exactly 0.72 for a bare category match (health == health), which would let ANY
-- health tip answer "know a good doctor?". 0.74 requires real word overlap (0.68 + 0.08
-- per shared word), so a rec is only shown when the neighbor actually wrote about this.
create or replace function public.find_neighbor_tips(
  p_block_id text,
  p_query text,
  p_category text default null,
  p_limit int default 3
)
returns table (
  detail_text text,
  category text,
  match_strength real,
  neighbor_label text,
  created_at timestamptz
)
language plpgsql
security definer
set search_path = pg_catalog, public
stable
as $$
declare
  v_me uuid := auth.uid();
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if p_block_id is null or length(trim(p_block_id)) = 0 then
    return;
  end if;

  return query
  select
    s.detail_text,
    s.category,
    public._signal_match_strength(p_category, p_query, s.category, s.detail_text) as match_strength,
    coalesce(u.nickname, 'A neighbor on your block') as neighbor_label,
    s.created_at
  from public.local_signals s
  left join public.users u on u.id = s.user_id
  where s.block_id = p_block_id
    and s.intent = 'tip_share'
    and s.status = 'listening'
    and s.expires_at > now()
    and s.user_id <> v_me
    and public._signal_match_strength(p_category, p_query, s.category, s.detail_text) >= 0.74
  -- Ordinal, not the alias: `match_strength` is also a RETURNS TABLE variable in scope here,
  -- so naming it in ORDER BY is an ambiguous reference.
  order by 3 desc, s.created_at desc
  limit greatest(1, least(coalesce(p_limit, 3), 10));
end;
$$;

revoke all on function public.find_neighbor_tips(text, text, text, int) from public, anon;
grant execute on function public.find_neighbor_tips(text, text, text, int) to authenticated;

comment on function public.find_neighbor_tips(text, text, text, int) is
  'Read-only neighbor tip_share matches for a query on a block. No writes, no peer ids — '
  'lets Lana answer a recommendation ask without posting a tip_seek signal.';

-- ---------------------------------------------------------------------------
-- §2 close_local_signal — make "remove my posting" real
-- ---------------------------------------------------------------------------
-- p_signal_id null → the caller's most recent open posting (the conversational case:
-- "actually, remove that"). Only ever touches rows the caller owns.
create or replace function public.close_local_signal(
  p_signal_id uuid default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_row public.local_signals%rowtype;
  v_entries uuid[];
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  if p_signal_id is null then
    select * into v_row
    from public.local_signals
    where user_id = v_me and status = 'listening'
    order by created_at desc
    limit 1;
  else
    select * into v_row
    from public.local_signals
    where id = p_signal_id and user_id = v_me
    limit 1;
  end if;

  if not found then
    return jsonb_build_object('closed', false, 'reason', 'not_found');
  end if;
  if v_row.status <> 'listening' then
    return jsonb_build_object(
      'closed', false, 'reason', 'already_closed',
      'signal_id', v_row.id, 'intent', v_row.intent, 'detail_text', v_row.detail_text
    );
  end if;

  update public.local_signals
  set status = 'closed', updated_at = now()
  where id = v_row.id;

  -- Drop the match rows this posting created — BOTH directions. The neighbor's copy has
  -- to go too, otherwise a withdrawn ask keeps showing up in their block log.
  select array_agg(id) into v_entries
  from public.block_log_entries
  where my_signal_id = v_row.id or peer_signal_id = v_row.id;

  if v_entries is not null then
    delete from public.match_notifications
    where block_log_entry_id = any (v_entries)
      and status = 'queued';
    delete from public.block_log_entries
    where id = any (v_entries);
  end if;

  return jsonb_build_object(
    'closed', true,
    'signal_id', v_row.id,
    'intent', v_row.intent,
    'category', v_row.category,
    'detail_text', v_row.detail_text,
    'matches_removed', coalesce(array_length(v_entries, 1), 0)
  );
end;
$$;

revoke all on function public.close_local_signal(uuid) from public, anon;
grant execute on function public.close_local_signal(uuid) to authenticated;

comment on function public.close_local_signal(uuid) is
  'Withdraw the caller''s local signal (default: their most recent open one) and delete the '
  'block-log rows it created in both directions. Backs Lana''s "remove my posting" offer.';

-- ---------------------------------------------------------------------------
-- §3 save_local_signal — idempotent per (user, intent, detail)
-- ---------------------------------------------------------------------------
-- Signature unchanged, so create-or-replace is enough (no drop/regrant needed).
create or replace function public.save_local_signal(
  p_intent text,
  p_detail_text text,
  p_category text default null,
  p_block_id text default null,
  p_zip text default null,
  p_affinity_tags text[] default '{}',
  p_stage text default null,
  p_photo_url text default null
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
  v_detail text;
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

  v_detail := left(trim(p_detail_text), 500);

  v_block := coalesce(
    p_block_id,
    (select home_block_id from public.users where id = v_me)
  );

  if v_block is null then
    raise exception 'block_required' using errcode = 'P0001';
  end if;

  -- Already posted the same thing and it's still open → hand back that row instead of a
  -- second one. A re-entered lane, a re-tapped chip, or a repeated question must not
  -- multiply the user's live postings (or re-notify the same neighbors).
  select * into v_row
  from public.local_signals
  where user_id = v_me
    and intent = p_intent
    and status = 'listening'
    and expires_at > now()
    and block_id = v_block
    and lower(detail_text) = lower(v_detail)
  order by created_at desc
  limit 1;

  if found then
    return jsonb_build_object(
      'signal_id', v_row.id,
      'intent', v_row.intent,
      'category', v_row.category,
      'detail_text', v_row.detail_text,
      'block_id', v_row.block_id,
      'photo_url', v_row.photo_url,
      'matches_created', 0,
      'reused', true
    );
  end if;

  insert into public.local_signals (
    user_id, block_id, zip, intent, category, detail_text,
    affinity_tags, stage, photo_url, status, source_surface
  ) values (
    v_me, v_block, p_zip, p_intent, nullif(trim(p_category), ''),
    v_detail,
    coalesce(p_affinity_tags, '{}'), nullif(trim(p_stage), ''),
    nullif(trim(p_photo_url), ''),
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
    'photo_url', v_row.photo_url,
    'matches_created', v_matches,
    'reused', false
  );
end;
$$;
