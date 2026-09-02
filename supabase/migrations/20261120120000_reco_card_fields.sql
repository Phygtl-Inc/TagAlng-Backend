-- The recommendation card, in fields instead of one sentence (C-FIND-RECENT).
--
-- The feed only ever had detail_text: " · "-joined name, category, trait, every answered
-- step and the locality, capped at 500 chars. A card that wants to render
--
--     Dr. Sarah                       <- name
--     Pediatric dentist · Lake Nona   <- category · place
--     "So gentle — quick appointments" <- description
--
-- had to split that string back apart, which is guesswork the moment a name contains " · "
-- or a step answer looks like a locality. The pieces exist at capture time and were simply
-- never stored, so this stores them: reco_name / reco_place / reco_description beside the
-- reco_type + reco_fields that 20261117/20261118 already added.
--
-- Also here, because both change the same feed function:
--   * 👍/👎 — tip_helpful gains a DIRECTION, so "not helpful" is a stored vote instead of
--     the absence of one (absence and disagreement are not the same signal).
--   * the vouch count leaves the feed. Two counters on one card never read as two
--     different questions; helpful/unhelpful is the one product kept. tip_vouches and
--     set_tip_vouch are left standing — no data is destroyed here — they just stop
--     being part of what a card reads.

-- ── 1. The three fields the card head needs ───────────────────────────────────────────
alter table public.local_signals
  add column if not exists reco_name text,
  add column if not exists reco_place text,
  add column if not exists reco_description text;

comment on column public.local_signals.reco_name is
  'The subject as the author wrote it ("Dr. Sarah") — the card''s title. reco_subject is '
  'the lowercased GROUPING key for "others also said" and is not display copy.';
comment on column public.local_signals.reco_place is
  'Neighbourhood/area the recommendation is in, when the author named one.';
comment on column public.local_signals.reco_description is
  'Why it is worth recommending, in the author''s own words — the italic line on the card.';

-- Legacy rows: _detail_text() has always led with the name, so the first segment is it.
-- Description is NOT backfilled — trait and step answers are indistinguishable inside the
-- joined string, and a wrong guess would render as somebody's words.
update public.local_signals
   set reco_name = nullif(btrim(split_part(detail_text, ' · ', 1)), '')
 where intent = 'tip_share'
   and reco_name is null
   and detail_text is not null;

-- ── 2. The writer takes them ──────────────────────────────────────────────────────────
-- 4-arg version DROPPED, not left beside this one: two overloads let PostgREST resolve to
-- whichever matches the keys sent, silently dropping the new fields (same reasoning as
-- 20261118120000).
drop function if exists public.set_signal_reco(uuid, text, jsonb, text);

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
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  if p_reco_type is not null and p_reco_type not in (
    'professional', 'restaurant', 'recipe', 'product', 'location', 'service', 'diy'
  ) then
    raise exception 'invalid_reco_type' using errcode = 'P0001';
  end if;

  if p_reco_fields is not null and jsonb_typeof(p_reco_fields) <> 'array' then
    raise exception 'reco_fields_must_be_array' using errcode = 'P0001';
  end if;

  update public.local_signals
     set reco_type = coalesce(p_reco_type, reco_type),
         reco_fields = coalesce(p_reco_fields, reco_fields),
         reco_subject = coalesce(nullif(btrim(lower(p_reco_subject)), ''), reco_subject),
         reco_name = coalesce(nullif(btrim(p_reco_name), ''), reco_name),
         reco_place = coalesce(nullif(btrim(p_reco_place), ''), reco_place),
         reco_description = coalesce(nullif(btrim(p_reco_description), ''), reco_description),
         updated_at = now()
   where id = p_signal_id
     and user_id = v_me;
end;
$$;

revoke all on function public.set_signal_reco(uuid, text, jsonb, text, text, text, text)
  from public, anon;
grant execute on function public.set_signal_reco(uuid, text, jsonb, text, text, text, text)
  to authenticated;


-- ── 3. 👎 as a stored vote ────────────────────────────────────────────────────────────
-- One row per (signal, reader) still — the PK — so a reader has ONE opinion that flips,
-- never a helpful and an unhelpful at once.
alter table public.tip_helpful
  add column if not exists is_helpful boolean not null default true;

comment on table public.tip_helpful is
  'One reader''s verdict on a recommendation: is_helpful true = 👍, false = 👎. Rates the '
  'ANSWER, not the place. One row per reader per tip, so the vote flips rather than stacks.';

create index if not exists tip_helpful_signal_vote_idx
  on public.tip_helpful (signal_id, is_helpful);

-- int -> jsonb (both counts + the caller's own vote), so a tap does not need a re-read of
-- the feed to render the row it just changed. Return type changes, so drop first.
drop function if exists public.set_tip_helpful(uuid, boolean);

create or replace function public.set_tip_helpful(
  p_signal_id uuid,
  p_on        boolean default true,
  p_helpful   boolean default true
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
as $$
declare
  v_me uuid := auth.uid();
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if not exists (
    select 1 from public.local_signals s where s.id = p_signal_id and s.intent = 'tip_share'
  ) then
    raise exception 'tip_not_found' using errcode = 'P0001';
  end if;

  -- p_on is the desired state, not a toggle: a double tap on a flaky connection must not
  -- invert what the reader chose. p_on false clears the vote whichever way it pointed.
  if coalesce(p_on, true) then
    insert into public.tip_helpful (signal_id, user_id, is_helpful)
    values (p_signal_id, v_me, coalesce(p_helpful, true))
    on conflict (signal_id, user_id)
      do update set is_helpful = excluded.is_helpful, created_at = now();
  else
    delete from public.tip_helpful where signal_id = p_signal_id and user_id = v_me;
  end if;

  return jsonb_build_object(
    'helpful_count',
      (select count(*)::int from public.tip_helpful th
        where th.signal_id = p_signal_id and th.is_helpful),
    'unhelpful_count',
      (select count(*)::int from public.tip_helpful th
        where th.signal_id = p_signal_id and not th.is_helpful),
    'i_marked_helpful',
      exists (select 1 from public.tip_helpful th
               where th.signal_id = p_signal_id and th.user_id = v_me and th.is_helpful),
    'i_marked_unhelpful',
      exists (select 1 from public.tip_helpful th
               where th.signal_id = p_signal_id and th.user_id = v_me and not th.is_helpful)
  );
end;
$$;

revoke all on function public.set_tip_helpful(uuid, boolean, boolean) from public, anon;
grant execute on function public.set_tip_helpful(uuid, boolean, boolean)
  to authenticated, service_role;


-- ── 4. The feed, in fields ────────────────────────────────────────────────────────────
-- Rebuilt rather than replaced: OUT columns change (42P13). Same visibility rules as
-- before — this is still a second VIEW of find_neighbor_tips' rows, not a second source.
-- Gone from the row: vouch_count, i_vouched (see header) and affinity_tags, which fed a
-- trait-chip line the card does not have.
drop function if exists public.recent_neighbor_tips(text, double precision, int, text);

create function public.recent_neighbor_tips(
  p_filter         text default 'recent',
  p_radius_meters  double precision default 25000,
  p_limit          int default 20,
  p_locale         text default 'en'
)
returns table (
  signal_id          uuid,
  reco_name          text,
  category           text,
  reco_type          text,
  reco_place         text,
  reco_description   text,
  reco_fields        jsonb,
  detail_text        text,
  created_at         timestamptz,
  peer_user_id       uuid,
  neighbor_label     text,
  avatar_url         text,
  distance_meters    double precision,
  distance_text      text,
  shared_circles     jsonb,
  same_block         boolean,
  helpful_count      int,
  unhelpful_count    int,
  i_marked_helpful   boolean,
  i_marked_unhelpful boolean
)
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
stable
as $$
declare
  v_me uuid := auth.uid();
  v_origin extensions.geography;
  v_radius double precision := greatest(1000, least(coalesce(p_radius_meters, 25000), 200000));
  v_my_block text;
  v_filter text := lower(coalesce(p_filter, 'recent'));
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  select o.origin into v_origin from public.user_origin_point(v_me) o;
  select u.home_block_id into v_my_block from public.users u where u.id = v_me;

  return query
  with peer_points as (
    select
      u.id as peer_id,
      coalesce(
        b.centroid,
        extensions.st_setsrid(extensions.st_makepoint(z.lng, z.lat), 4326)::extensions.geography
      ) as pt
    from public.users u
    left join public.blocks b on b.id = u.home_block_id and b.centroid is not null
    left join public.zip_centroids z on z.zip5 = public.normalize_zip5(u.home_zip)
    where u.id <> v_me
  ),
  visible as (
    select vm.place_ref, vm.user_id from public.visible_place_members(v_me) vm
  ),
  mine as (
    select v.place_ref from visible v where v.user_id = v_me
  ),
  overlap as (
    select
      v.user_id as peer_id,
      jsonb_agg(
        jsonb_build_object('place_id', p.id, 'name', p.name, 'circle_type', p.place_type)
        order by p.name
      ) as circles
    from visible v
    join mine m on m.place_ref = v.place_ref
    join public.places p on p.id = v.place_ref
    where v.user_id <> v_me
    group by v.user_id
  ),
  rows_out as (
    select
      s.id,
      s.reco_name,
      s.category,
      s.reco_type,
      s.reco_place,
      s.reco_description,
      coalesce(s.reco_fields, '[]'::jsonb) as fields,
      s.detail_text,
      s.created_at,
      s.user_id,
      -- Empty-string nicknames render as a blank byline without the nullif, which is why
      -- every other surface trims before the fallback.
      coalesce(nullif(btrim(u.nickname), ''), 'A neighbor') as label,
      u.profile_photo_url,
      case
        when v_origin is null or pp.pt is null then null
        else extensions.st_distance(pp.pt, v_origin)::double precision
      end as dist_m,
      coalesce(o.circles, '[]'::jsonb) as circles,
      (v_my_block is not null and u.home_block_id = v_my_block) as same_blk,
      (select count(*)::int from public.tip_helpful th
        where th.signal_id = s.id and th.is_helpful) as helpfuls,
      (select count(*)::int from public.tip_helpful th
        where th.signal_id = s.id and not th.is_helpful) as unhelpfuls,
      exists (
        select 1 from public.tip_helpful th
         where th.signal_id = s.id and th.user_id = v_me and th.is_helpful
      ) as did_helpful,
      exists (
        select 1 from public.tip_helpful th
         where th.signal_id = s.id and th.user_id = v_me and not th.is_helpful
      ) as did_unhelpful
    from public.local_signals s
    join public.users u on u.id = s.user_id
    left join peer_points pp on pp.peer_id = s.user_id
    left join overlap o on o.peer_id = s.user_id
    where s.intent = 'tip_share'
      and s.status = 'listening'
      and s.expires_at > now()
      and s.user_id <> v_me
      and not public.lana_is_blocked(v_me, s.user_id)
      -- Unlocatable caller: fall back to the block rather than showing the whole table.
      and (
        case
          when v_origin is null then
            v_my_block is not null and u.home_block_id = v_my_block
          else pp.pt is not null and extensions.st_dwithin(pp.pt, v_origin, v_radius)
        end
      )
      -- "My circles" is a real filter, not a re-sort: a tip from someone she shares no
      -- place with does not belong in that tab at all.
      and (v_filter <> 'circles' or o.circles is not null)
  )
  select
    r.id, r.reco_name, r.category, r.reco_type, r.reco_place, r.reco_description,
    r.fields, r.detail_text, r.created_at,
    r.user_id, r.label, r.profile_photo_url,
    r.dist_m,
    case
      when r.dist_m is null then null
      else public.humanize_distance_text(r.dist_m, coalesce(p_locale, 'en'))
    end,
    r.circles, r.same_blk, r.helpfuls, r.unhelpfuls, r.did_helpful, r.did_unhelpful
  from rows_out r
  order by
    case when v_filter = 'nearest' then coalesce(r.dist_m, 1e9) end asc nulls last,
    r.created_at desc
  limit greatest(1, least(coalesce(p_limit, 20), 50));
end;
$$;

comment on function public.recent_neighbor_tips(text, double precision, int, text) is
  'C-FIND-RECENT: the browse feed behind the Recent-tips pill. v2 = the card in FIELDS '
  '(reco_name / category / reco_place / reco_description / reco_fields) instead of the '
  'joined detail_text, plus the 👎 count; vouch_count and affinity_tags dropped. Same '
  'visibility rules as find_neighbor_tips.';

revoke all on function public.recent_neighbor_tips(text, double precision, int, text) from public, anon;
grant execute on function public.recent_neighbor_tips(text, double precision, int, text)
  to authenticated, service_role;
