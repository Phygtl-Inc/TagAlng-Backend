-- Learned interests: what Lana picks up from what someone DOES, not only what they say
--
-- WHY (Asjid, 2026-10-07): Tommaso searched for AI meets twice and talked about AI, and
-- "find people into AI" still found nobody — claims are only written when someone states
-- something about themselves ("I work in AI"). Most people never do. Decision:
--   * learned interests match in people search automatically, but rank BELOW stated
--     claims, and the card says what they did ("Has been checking out AI meetups"), never
--     what they are — nobody is mislabelled;
--   * every learned interest is listed on the person's own profile and one tap removes
--     it (for good: a removed topic is never re-learned);
--   * no signup question, no per-interest confirmation.
--
-- Kept OUT of user_identity_claims on purpose: the rapport tile, the identity card,
-- community matching and the affinity lines all read claims as things the person SAID.
--
-- WHAT
--   learned_interests              one row per (user, topic); counters + learned_at
--   record_learned_interest(...)   service-role: count one search, learn on a pattern
--   claim_learned_mention(...)     service-role: at most one passing mention a week
--   find_peers_by_learned_interest / _near   people search, the lower tier
--
-- Learned = searched on 2 different days, or 3 times in all. Faded = untouched 90 days.

create table if not exists public.learned_interests (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null references public.users(id) on delete cascade,
  topic            text not null,
  label            text not null,
  event_searches   int  not null default 0,
  people_searches  int  not null default 0,
  search_days      int  not null default 0,
  last_search_day  date,
  first_seen_at    timestamptz not null default now(),
  last_seen_at     timestamptz not null default now(),
  learned_at       timestamptz,
  removed_at       timestamptz,
  mentioned_at     timestamptz,
  constraint learned_interests_topic_len check (char_length(topic) between 2 and 40),
  unique (user_id, topic)
);

create index if not exists learned_interests_learned_idx
  on public.learned_interests (topic)
  where learned_at is not null and removed_at is null;

alter table public.learned_interests enable row level security;

drop policy if exists learned_interests_select_own on public.learned_interests;
create policy learned_interests_select_own on public.learned_interests
  for select using (user_id = auth.uid());

-- Writes go through the worker (service role). Revoked explicitly from each role:
-- revoking from PUBLIC alone leaves the explicit default grants in place.
revoke insert, update, delete on public.learned_interests from anon, authenticated;
revoke all on public.learned_interests from anon;

-- ── record_learned_interest ─────────────────────────────────────────────────────────

create or replace function public.record_learned_interest(
  p_user_id uuid,
  p_label   text,
  p_kind    text  -- 'event_search' | 'people_search'
)
returns table (learned boolean, newly_learned boolean, label text, topic text)
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
#variable_conflict use_column
declare
  v_label text := btrim(regexp_replace(coalesce(p_label, ''), '\s+', ' ', 'g'));
  v_topic text := lower(v_label);
  r public.learned_interests%rowtype;
begin
  if p_user_id is null or p_kind not in ('event_search', 'people_search')
     or char_length(v_topic) not between 2 and 40 then
    return;
  end if;

  insert into public.learned_interests as li (user_id, topic, label)
  values (p_user_id, v_topic, v_label)
  on conflict (user_id, topic) do nothing;

  select * into r from public.learned_interests li
  where li.user_id = p_user_id and li.topic = v_topic
  for update;

  -- Removed by the person: never counted, never re-learned.
  if r.removed_at is not null then
    return query select false, false, r.label, r.topic;
    return;
  end if;

  update public.learned_interests li set
    event_searches  = li.event_searches  + (p_kind = 'event_search')::int,
    people_searches = li.people_searches + (p_kind = 'people_search')::int,
    search_days     = li.search_days + (li.last_search_day is distinct from current_date)::int,
    last_search_day = current_date,
    last_seen_at    = now()
  where li.id = r.id
  returning * into r;

  if r.learned_at is null
     and (r.search_days >= 2 or r.event_searches + r.people_searches >= 3) then
    update public.learned_interests li set learned_at = now()
    where li.id = r.id
    returning * into r;
    return query select true, true, r.label, r.topic;
    return;
  end if;

  return query select r.learned_at is not null, false, r.label, r.topic;
end;
$$;

-- ── claim_learned_mention ───────────────────────────────────────────────────────────
-- True at most once a week per person: the caller may then say ONE line about it.

create or replace function public.claim_learned_mention(p_user_id uuid, p_topic text)
returns boolean
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_ok boolean;
begin
  update public.learned_interests li set mentioned_at = now()
  where li.user_id = p_user_id
    and li.topic = lower(btrim(p_topic))
    and li.learned_at is not null
    and li.removed_at is null
    and li.mentioned_at is null
    and not exists (
      select 1 from public.learned_interests o
      where o.user_id = p_user_id and o.mentioned_at > now() - interval '7 days'
    )
  returning true into v_ok;
  return coalesce(v_ok, false);
end;
$$;

revoke execute on function public.record_learned_interest(uuid, text, text) from public, anon, authenticated;
revoke execute on function public.claim_learned_mention(uuid, text) from public, anon, authenticated;
grant execute on function public.record_learned_interest(uuid, text, text) to service_role;
grant execute on function public.claim_learned_mention(uuid, text) to service_role;

-- ── people search: the learned tier ─────────────────────────────────────────────────
-- Every term must hit the learned topic (whole word, as _claim_term_hit does for
-- claims). learned_kind tells the card which sentence is true.

create or replace function public.find_peers_by_learned_interest(
  p_terms text[],
  p_limit int default 5
)
returns table (
  peer_user_id uuid,
  nickname text,
  avatar_url text,
  matching_peer_label text,
  learned_kind text
)
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
stable
as $$
declare
  v_caller uuid := auth.uid();
  v_block_id text;
  v_terms text[];
begin
  if v_caller is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  select coalesce(array_agg(distinct lower(btrim(t))), '{}') into v_terms
  from unnest(coalesce(p_terms, '{}')) t where char_length(btrim(t)) >= 2;
  if coalesce(array_length(v_terms, 1), 0) = 0 then
    return;
  end if;
  select u.home_block_id into v_block_id from public.users u where u.id = v_caller;
  if v_block_id is null then
    return;
  end if;

  return query
  select distinct on (li.user_id)
    li.user_id, u.nickname, u.profile_photo_url, li.label,
    case when li.event_searches > 0 then 'events' else 'people' end
  from public.learned_interests li
  join public.users u on u.id = li.user_id
  where u.home_block_id = v_block_id
    and li.user_id <> v_caller
    and not public.lana_is_blocked(v_caller, li.user_id)
    and li.learned_at is not null
    and li.removed_at is null
    and li.last_seen_at > now() - interval '90 days'
    and not exists (
      select 1 from unnest(v_terms) t where not public._claim_term_hit(li.label, t)
    )
  order by li.user_id, li.last_seen_at desc
  limit greatest(1, least(coalesce(p_limit, 5), 20));
end;
$$;

create or replace function public.find_peers_by_learned_interest_near(
  p_terms text[],
  p_radius_meters double precision default 8000,
  p_limit int default 5,
  p_locale text default 'en'
)
returns table (
  peer_user_id uuid,
  nickname text,
  avatar_url text,
  matching_peer_label text,
  learned_kind text,
  distance_meters double precision,
  distance_text text
)
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
stable
as $$
declare
  v_caller uuid := auth.uid();
  v_terms text[];
begin
  if v_caller is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  select coalesce(array_agg(distinct lower(btrim(t))), '{}') into v_terms
  from unnest(coalesce(p_terms, '{}')) t where char_length(btrim(t)) >= 2;
  if coalesce(array_length(v_terms, 1), 0) = 0 then
    return;
  end if;

  return query
  with in_radius as (
    select r.peer_id, r.distance_meters as dist
    from public.peers_within_radius(v_caller, p_radius_meters) r
  ),
  hits as (
    select distinct on (li.user_id) li.user_id, li.label,
           case when li.event_searches > 0 then 'events' else 'people' end as kind
    from public.learned_interests li
    join in_radius ir on ir.peer_id = li.user_id
    where li.user_id <> v_caller
      and li.learned_at is not null
      and li.removed_at is null
      and li.last_seen_at > now() - interval '90 days'
      and not exists (
        select 1 from unnest(v_terms) t where not public._claim_term_hit(li.label, t)
      )
    order by li.user_id, li.last_seen_at desc
  )
  select h.user_id, u.nickname, u.profile_photo_url, h.label, h.kind,
         ir.dist, public.humanize_distance_text(ir.dist, p_locale)
  from hits h
  join public.users u on u.id = h.user_id
  join in_radius ir on ir.peer_id = h.user_id
  order by ir.dist asc, u.nickname asc nulls last
  limit greatest(1, least(coalesce(p_limit, 5), 20));
end;
$$;

revoke execute on function public.find_peers_by_learned_interest(text[], int) from public, anon;
revoke execute on function public.find_peers_by_learned_interest_near(text[], double precision, int, text) from public, anon;
grant execute on function public.find_peers_by_learned_interest(text[], int) to authenticated, service_role;
grant execute on function public.find_peers_by_learned_interest_near(text[], double precision, int, text) to authenticated, service_role;

-- ============================================================================
-- ROLLBACK
--   drop function public.find_peers_by_learned_interest_near(text[], double precision, int, text);
--   drop function public.find_peers_by_learned_interest(text[], int);
--   drop function public.claim_learned_mention(uuid, text);
--   drop function public.record_learned_interest(uuid, text, text);
--   drop table public.learned_interests;
-- ============================================================================
