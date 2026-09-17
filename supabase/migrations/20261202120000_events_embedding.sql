-- A6 — meets get a vector, and a search path that is not pre-truncated by distance.
-- (Execution contract v2 §A6, sequence item 12. Unblocks C3.)
--
-- Today every meet search lands on get_activities_near_point: radius filter, then
--   order by distance asc limit least(coalesce(p_limit, 20), 50)
-- Distance-ranked and capped at 20 by default. C3's whole job is to admit a topically
-- strong meet that sits further out — and handed that set, the row it exists to admit was
-- already dropped before the rule ran. Raising the cap does not fix it: the ordering is
-- still distance, so at any cap the set is the nearest N, not the most relevant N.
--
-- So this is a second query SHAPE, not a bigger limit: rank by meaning, return distance as
-- data, and let the caller's admission inequality decide. get_activities_near_point is
-- deliberately untouched — browse-by-proximity is a real surface and still wants nearest-first.
--
-- Mirrors the tip path (20261126120000): partial HNSW over exactly the rows that are ever
-- searched, plus a backfill script for everything older (scripts/backfill_event_embeddings.py).
-- No set_event_embedding RPC to match tip's set_signal_embedding — that one exists because the
-- tip write is user-scoped from the request. Every writer here is server-side and service-role
-- (the post-publish thread, the backfill), so a JWT-scoped RPC would have had no caller.
--
-- v1 of the contract indexed `where status = 'published'`. There is no such status — the
-- CHECK is ('open','cancelled','completed') — so the predicate contradicted every query and
-- the index would have sat unused with nothing failing. 'open' is the whole live set.

-- ---------------------------------------------------------------------------
-- 1. The vector, and when it was last computed.
-- ---------------------------------------------------------------------------
alter table public.events
  add column if not exists embedding extensions.vector(768),
  add column if not exists embedding_updated_at timestamptz;

-- embedding_updated_at is the repair hook, and it is what makes edits survivable. Hosts edit
-- a published meet through update_event from the PWA, which the worker never sees — so there
-- is no worker-side hook to re-embed on. Comparing this against events.updated_at is how the
-- backfill finds a meet whose title changed under its vector, and it catches a publish-time
-- embed that failed for free (both read as "older than the row").
comment on column public.events.embedding is
  'text-embedding-005 over event_embedding_text(); written service-side by the worker right '
  'after publish (app/event_publish). NULL = not embedded yet — search_events_semantic still returns those rows, ranked '
  'last, so a meet is never invisible for lacking a vector.';
comment on column public.events.embedding_updated_at is
  'When embedding was last computed. Stale vs events.updated_at (host edited the meet through '
  'update_event) or NULL (publish-time embed failed) => scripts/backfill_event_embeddings.py --stale.';

create index if not exists events_embedding_hnsw
  on public.events
  using hnsw (embedding extensions.vector_cosine_ops)
  where embedding is not null and status = 'open';

-- ponytail: this index is NOT in today's plan, and that is deliberate — say it out loud
-- rather than let someone discover it in an EXPLAIN. The index is partial on
-- `embedding is not null`, and search_events_semantic intentionally also returns meets that
-- have no vector yet (see §2), so the planner cannot prove the predicate and falls back to
-- the GiST radius scan plus an exact sort. At a 40km radius and a 14-day window that set is
-- small and the exact sort is correct and fast, so nothing is lost today.
-- Upgrade path when the live set outgrows an exact sort: split the query into two arms —
-- `where embedding is not null` ordered by `<=>` with the limit (this index applies), union
-- all the handful of unembedded rows — and no migration is needed because the index is
-- already here. Write cost in the meantime is one index entry per published meet.

-- ---------------------------------------------------------------------------
-- 2. The read path C3 runs on. Same row shape as get_activities_near_point so it is a
--    drop-in for the card renderer, plus the two things the old one could not say:
--    how well the meet matches, and how far out it is, as independent numbers.
-- ---------------------------------------------------------------------------
create or replace function public.search_events_semantic(
  p_query_embedding extensions.vector(768),
  p_lat             double precision,
  p_lng             double precision,
  p_radius_meters   double precision default 40000,
  p_window          interval default '14 days'::interval,
  p_locale          text default 'en',
  p_circle_place_id uuid default null,
  p_min_similarity  real default 0,
  p_limit           integer default 50
)
returns table(
  id uuid,
  host_id uuid,
  title text,
  description text,
  starts_at timestamp with time zone,
  has_time boolean,
  ends_at timestamp with time zone,
  duration_minutes integer,
  venue_name text,
  block_id text,
  cohort_tags text[],
  max_attendees integer,
  status text,
  cover_image_url text,
  cover_emoji text,
  distance_meters double precision,
  distance_text text,
  participant_count integer,
  maybe_count integer,
  similarity real
)
language plpgsql
stable
security definer
set search_path to 'pg_catalog', 'public', 'extensions'
as $function$
declare
  v_point extensions.geography;
  -- Same clamp as get_activities_near_point. The radius here is the OUTER bound C3 scores
  -- inside, not the answer — admission still has to earn each metre beyond radius_base.
  v_radius double precision := greatest(100, least(coalesce(p_radius_meters, 40000), 200000));
  v_lang text := lower(coalesce(nullif(split_part(coalesce(p_locale, 'en'), '-', 1), ''), 'en'));
begin
  if p_lat is null or p_lng is null then
    raise exception 'location_required' using errcode = 'P0001';
  end if;
  if p_lat not between -90 and 90 or p_lng not between -180 and 180 then
    raise exception 'invalid_location' using errcode = 'P0001';
  end if;
  if p_query_embedding is null then
    raise exception 'query_embedding_required' using errcode = 'P0001';
  end if;

  v_point := extensions.st_setsrid(extensions.st_makepoint(p_lng, p_lat), 4326)::extensions.geography;

  return query
  select
    e.id,
    e.host_id,
    coalesce(e.title_translations->>p_locale, e.title_translations->>v_lang, e.title) as title,
    coalesce(
      e.description_translations->>p_locale,
      e.description_translations->>v_lang,
      e.description
    ) as description,
    e.starts_at,
    e.has_time,
    e.ends_at,
    case
      when e.ends_at is null then null
      else greatest(round(extract(epoch from e.ends_at - e.starts_at) / 60)::int, 1)
    end as duration_minutes,
    e.venue_name,
    e.block_id,
    e.cohort_tags,
    e.max_attendees,
    e.status,
    e.cover_image_url,
    e.cover_emoji,
    extensions.st_distance(e.location, v_point)::double precision as distance_meters,
    public.humanize_distance_text(
      extensions.st_distance(e.location, v_point)::double precision, p_locale
    ) as distance_text,
    coalesce((
      select count(*)::int from public.event_requests er
      where er.event_id = e.id
        and er.status in ('approved', 'attended')
        and er.rsvp_status = 'going'
    ), 0) as participant_count,
    coalesce((
      select count(*)::int from public.event_requests er
      where er.event_id = e.id
        and er.status in ('approved', 'attended')
        and er.rsvp_status = 'maybe'
    ), 0) as maybe_count,
    case
      when e.embedding is null then null
      else (1 - (e.embedding <=> p_query_embedding))::real
    end as similarity
  from public.events e
  where e.status = 'open'
    and e.location is not null
    and e.starts_at between now() and now() + p_window
    and extensions.st_dwithin(e.location, v_point, v_radius)
    -- The active community picker, when one is set. Without this the picker silently stops
    -- scoping meets the day anything routes through here.
    and (p_circle_place_id is null or e.circle_place_ref = p_circle_place_id)
    -- A not-yet-embedded meet has an UNKNOWN topic, not a bad one, so no floor can apply to
    -- it. It is ranked last instead of filtered out: a meet published sixty seconds ago has
    -- no vector yet, and "posted too recently to be indexed" is the worst possible reason to
    -- be missing from search.
    and (
      e.embedding is null
      or (1 - (e.embedding <=> p_query_embedding)) >= coalesce(p_min_similarity, 0)
    )
  -- By meaning. NOT by distance — distance is returned as data and judged by the caller.
  -- Unembedded meets sort last for free: `<=>` against NULL is NULL, and ASC is NULLS LAST.
  order by e.embedding <=> p_query_embedding asc, e.starts_at asc
  limit greatest(1, least(coalesce(p_limit, 50), 200));
end;
$function$;

comment on function public.search_events_semantic(extensions.vector, double precision, double precision, double precision, interval, text, uuid, real, integer) is
  'Meets ranked by MEANING inside a radius, with distance returned as data rather than used '
  'as the ranking. The candidate source for C3 distance admission — get_activities_near_point '
  'orders by distance and caps at 20, which discards the far-but-relevant row C3 exists to '
  'admit. Returns similarity=NULL for meets not yet embedded, ranked last, never hidden. '
  'Row count = p_limit means the set was truncated; say so rather than concluding nothing '
  'further out matched (contract D9-A9).';

-- anon for parity with get_activities_near_point, which the signed-out meet preview relies
-- on. No new exposure: RLS already publishes status='open' meets to everyone
-- (events_select_open_anyone), so this reaches nothing an anonymous caller cannot read today.
grant execute on function public.search_events_semantic(extensions.vector, double precision, double precision, double precision, interval, text, uuid, real, integer)
  to anon, authenticated, service_role;

-- ============================================================================
-- ROLLBACK
-- ----------------------------------------------------------------------------
-- Both functions are NEW (no name collisions in public before this migration) and both
-- columns are additive, so nothing existing changes behaviour if this is reverted — meet
-- search keeps running on get_activities_near_point exactly as it does today.
--
--   drop function if exists public.search_events_semantic(
--     extensions.vector, double precision, double precision, double precision,
--     interval, text, uuid, real, integer);
--   drop index if exists public.events_embedding_hnsw;
--   alter table public.events drop column if exists embedding_updated_at;
--   alter table public.events drop column if exists embedding;
--
-- Dropping the columns discards the vectors; the backfill script regenerates them from the
-- rows themselves, so this is a cost-of-Vertex-calls rollback, not a data-loss one.
-- ============================================================================
