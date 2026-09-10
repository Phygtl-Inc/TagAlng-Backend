-- Recommendations match by MEANING, not by shared letters.
--
-- Until now a tip_seek ask reached neighbours' tips through _signal_match_strength: words of
-- 4+ characters, substring-matched into detail_text, plus one blunt fallback — the same
-- category string scores 0.72. Both halves fail on the same ask:
--
--   "art supplies" vs "Quill & Co on Main — huge paper selection, they cut mat board"
--       "art" is 3 chars (dropped), "supplies" is not in the text   -> 0.0, INVISIBLE
--   "art supplies" vs "Mike the plumber, fixed our water heater same day"
--       both happen to be filed under 'home'                        -> 0.72, TOP ROW
--
-- So the right answer was missing and a plumber led the page. Three changes:
--   1. local_signals.embedding — the app writes it per tip via set_signal_embedding()
--      (scripts/backfill_tip_embeddings.py covers rows posted before today),
--   2. _tip_match_strength() — lexical OR affinity_tags OR cosine, whichever is strongest,
--   3. the category equality branch is GONE from the tip path.
--
-- ponytail: the affinity_tags branch is INERT TODAY. save_local_signal accepts
-- p_affinity_tags but no worker caller has ever sent it, so every tip_share row holds the
-- '{}' default — the column is read (tip_rec_cascade renders it as trait_tags on the card)
-- and never written. The branch is ten lines and costs nothing to carry, and it starts
-- working the moment anything populates the column; until then the meaning match is doing
-- all of the new work. Populating tags at share time is the follow-up.
--
-- Category is not deleted, only demoted: it is still stored, still returned, still rendered
-- by the reason_codes template ('A neighbor shared a tip in {category}'). It simply stops
-- deciding what matches, because it is free text — the sharper and more honest the label
-- gets ("stationery" vs "art supplies"), the less often string equality can ever fire.
--
-- _signal_match_strength IS DELIBERATELY UNTOUCHED. It is shared with the swap matcher,
-- which wants "bicycle != boots" strictness (20260706120000); semantic matching there would
-- re-break precisely what that migration fixed. The write-time matcher inside
-- save_local_signal still uses it for every intent, so match NOTIFICATIONS remain lexical —
-- this migration changes the ask-time read path only.

-- ---------------------------------------------------------------------------
-- 1. The vector, and the index over the only rows that are ever searched.
-- ---------------------------------------------------------------------------
alter table public.local_signals
  add column if not exists embedding extensions.vector(768);

create index if not exists local_signals_embedding_hnsw_idx
  on public.local_signals
  using hnsw (embedding extensions.vector_cosine_ops)
  where embedding is not null and intent = 'tip_share';

comment on column public.local_signals.embedding is
  'text-embedding-005 over tip_embedding_text(); written by set_signal_embedding after the '
  'reco fields land, so it covers the card and not just detail_text. NULL = lexical only.';

-- ---------------------------------------------------------------------------
-- 2. Write path. Separate from save_local_signal on purpose: the embedding is produced by
--    Vertex in the worker, arrives after set_signal_reco has filled the card, and must
--    never be able to fail a post that already succeeded.
-- ---------------------------------------------------------------------------
create or replace function public.set_signal_embedding(
  p_signal_id uuid,
  p_embedding extensions.vector(768)
)
returns boolean
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
as $function$
declare
  v_me uuid := auth.uid();
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  update public.local_signals
     set embedding = p_embedding
   where id = p_signal_id
     and user_id = v_me;

  return found;
end;
$function$;

comment on function public.set_signal_embedding(uuid, extensions.vector) is
  'Attach a 768-d embedding to your OWN signal. Own-rows only; best-effort from the worker.';

revoke all on function public.set_signal_embedding(uuid, extensions.vector) from public, anon;
grant execute on function public.set_signal_embedding(uuid, extensions.vector) to authenticated;

-- ---------------------------------------------------------------------------
-- 3. The tip matcher. Three independent ways to be a match; the best one wins.
-- ---------------------------------------------------------------------------
create or replace function public._tip_match_strength(
  p_query       text,
  p_peer_detail text,
  p_peer_tags   text[],
  p_query_vec   extensions.vector(768),
  p_peer_vec    extensions.vector(768),
  p_min_sim     real default 0.55
)
returns real
language plpgsql
immutable
as $function$
declare
  v_word text;
  v_overlap  int  := 0;
  v_tag_hits int  := 0;
  v_lexical  real := 0.0;
  v_tags     real := 0.0;
  v_semantic real := 0.0;
  v_sim      real;
  v_min      real := coalesce(p_min_sim, 0.55);
  v_stop text[] := array[
    'looking', 'for', 'have', 'want', 'wanna', 'swap', 'borrow', 'offer',
    'someone', 'good', 'know', 'block', 'neighbor', 'kids', 'kid', 'child',
    'the', 'and', 'with', 'from', 'that', 'this', 'your', 'my', 'our', 'are',
    'you', 'what', 'when', 'size', 'stage', 'also', 'need', 'like', 'just',
    'give', 'away', 'free', 'home', 'help', 'near', 'nearby', 'buy', 'best',
    'any', 'some', 'where', 'can', 'get', 'find', 'recommend', 'suggestion'
  ];
begin
  foreach v_word in array regexp_split_to_array(lower(coalesce(p_query, '')), '[^a-z0-9]+')
  loop
    if length(v_word) < 3 or v_word = any (v_stop) then
      continue;
    end if;

    -- Tags are the sharer's OWN labels for the thing, so three letters counts here:
    -- "art" is the entire ask and could never clear the prose floor below. Inert until
    -- something writes affinity_tags on a tip_share (see the header).
    if p_peer_tags is not null and exists (
      select 1 from unnest(p_peer_tags) t where position(v_word in lower(t)) > 0
    ) then
      v_tag_hits := v_tag_hits + 1;
    end if;

    -- Prose keeps the 4-character floor: substring matching into a whole sentence turns
    -- short words into noise ("art" hits "Bartlett", "cart", "party").
    if length(v_word) >= 4
       and p_peer_detail is not null
       and position(v_word in lower(p_peer_detail)) > 0 then
      v_overlap := v_overlap + 1;
    end if;
  end loop;

  if v_overlap > 0 then
    v_lexical := 0.68 + least(v_overlap * 0.08, 0.27);
  end if;

  if v_tag_hits > 0 then
    v_tags := 0.72 + least(v_tag_hits * 0.06, 0.18);
  end if;

  if p_query_vec is not null and p_peer_vec is not null then
    v_sim := 1.0 - (p_query_vec <=> p_peer_vec);
    if v_sim >= v_min then
      -- Mapped onto the same 0.68-0.95 band the lexical score uses, so one ORDER BY can
      -- rank a word match and a meaning match against each other coherently.
      v_semantic := 0.68 + ((v_sim - v_min) / greatest(1.0 - v_min, 0.01)) * 0.27;
    end if;
  end if;

  return least(greatest(v_lexical, v_tags, v_semantic), 0.95);
end;
$function$;

comment on function public._tip_match_strength(text, text, text[], extensions.vector, extensions.vector, real) is
  'Tip-only match score: best of word overlap, affinity_tags hit, and cosine similarity. '
  'No category branch (see 20261126). Swaps keep _signal_match_strength.';

revoke all on function public._tip_match_strength(text, text, text[], extensions.vector, extensions.vector, real)
  from public, anon;

-- ---------------------------------------------------------------------------
-- 4. find_neighbor_tips v6 — v5 plus p_query_embedding + p_min_similarity, scoring through
--    _tip_match_strength. OUT columns unchanged. p_category is still ACCEPTED (callers send
--    it, and dropping it would 404 every worker that has not redeployed yet) but is no
--    longer scored on.
-- ---------------------------------------------------------------------------
drop function if exists public.find_neighbor_tips(text, text, text, int, text, double precision, uuid);

create function public.find_neighbor_tips(
  p_block_id        text default null,
  p_category        text default null,
  p_query           text default null,
  p_limit           int default 5,
  p_locale          text default 'en',
  p_radius_meters   double precision default null,
  p_circle_place_id uuid default null,
  p_query_embedding extensions.vector(768) default null,
  p_min_similarity  real default 0.55
)
returns table (
  signal_id        uuid,
  detail_text      text,
  category         text,
  match_strength   real,
  neighbor_label   text,
  peer_user_id     uuid,
  avatar_url       text,
  affinity_tags    text[],
  distance_meters  double precision,
  distance_text    text,
  created_at       timestamptz,
  shared_circles   jsonb,
  same_block       boolean,
  vouch_count      int,
  helpful_count    int,
  i_vouched        boolean
)
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
stable
as $function$
declare
  v_me uuid := auth.uid();
  v_origin extensions.geography;
  v_radius double precision;
  v_my_block text;
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  -- Inside a community the roster IS the audience, so neither a block nor a radius is
  -- required to have a read at all — that early return is a geography rule.
  if p_circle_place_id is null
     and p_radius_meters is null
     and (p_block_id is null or length(trim(p_block_id)) = 0) then
    return;
  end if;

  -- Same bar as the roster: a community read is members-only, so this can never be
  -- used to enumerate a place the caller has nothing to do with.
  if p_circle_place_id is not null and not exists (
    select 1 from public.circle_affiliations ca
     where ca.user_id = v_me
       and ca.place_ref = p_circle_place_id
       and ca.status in ('confirmed', 'curious')
  ) then
    return;
  end if;

  select o.origin into v_origin from public.user_origin_point(v_me) o;
  select u.home_block_id into v_my_block from public.users u where u.id = v_me;

  if p_radius_meters is not null then
    v_radius := greatest(100, least(p_radius_meters, 200000));
    if v_origin is null then
      v_radius := null;
      if p_circle_place_id is null
         and (p_block_id is null or length(trim(p_block_id)) = 0) then
        return;
      end if;
    end if;
  end if;

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
  )
  select
    s.id,
    s.detail_text,
    s.category,
    public._tip_match_strength(
      p_query, s.detail_text, s.affinity_tags, p_query_embedding, s.embedding, p_min_similarity
    ) as strength,
    coalesce(u.nickname, 'A neighbor on your block') as neighbor_label,
    s.user_id,
    u.profile_photo_url,
    coalesce(s.affinity_tags, '{}')::text[],
    case
      when v_origin is null or pp.pt is null then null
      else extensions.st_distance(pp.pt, v_origin)::double precision
    end as dist_m,
    case
      when v_origin is null or pp.pt is null then null
      else public.humanize_distance_text(
             extensions.st_distance(pp.pt, v_origin)::double precision,
             coalesce(p_locale, 'en')
           )
    end as dist_text,
    s.created_at,
    coalesce(o.circles, '[]'::jsonb),
    (v_my_block is not null and u.home_block_id = v_my_block),
    (select count(*)::int from public.tip_vouches tv where tv.signal_id = s.id),
    (select count(*)::int from public.tip_helpful th where th.signal_id = s.id),
    exists (select 1 from public.tip_vouches tv where tv.signal_id = s.id and tv.user_id = v_me)
  from public.local_signals s
  join public.users u on u.id = s.user_id
  left join peer_points pp on pp.peer_id = s.user_id
  left join overlap o on o.peer_id = s.user_id
  where s.intent = 'tip_share'
    and s.status = 'listening'
    and s.expires_at > now()
    and s.user_id <> v_me
    and not public.lana_is_blocked(v_me, s.user_id)
    -- Scope, not filter: the community read ignores geography entirely, and the area
    -- read never shows a tip that was meant for a community.
    and (
      case
        when p_circle_place_id is not null then s.circle_place_ref = p_circle_place_id
        else s.circle_place_ref is null
             and case
                   when v_radius is not null
                     then pp.pt is not null and extensions.st_dwithin(pp.pt, v_origin, v_radius)
                   else s.block_id = p_block_id
                 end
      end
    )
    and public._tip_match_strength(
      p_query, s.detail_text, s.affinity_tags, p_query_embedding, s.embedding, p_min_similarity
    ) > 0
  order by strength desc, s.created_at desc
  limit greatest(1, least(coalesce(p_limit, 5), 20));
end;
$function$;

comment on function public.find_neighbor_tips(text, text, text, int, text, double precision, uuid, extensions.vector, real) is
  'Neighbor tips matching an ask. v6 = v5 plus p_query_embedding/p_min_similarity, scored by '
  '_tip_match_strength (words OR tags OR meaning). p_category is accepted for wire '
  'compatibility and is no longer part of the score.';

revoke all on function public.find_neighbor_tips(text, text, text, int, text, double precision, uuid, extensions.vector, real)
  from public, anon;
grant execute on function public.find_neighbor_tips(text, text, text, int, text, double precision, uuid, extensions.vector, real)
  to authenticated;
