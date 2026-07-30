-- 20260917140000_lana_rate_limiting.sql
--
-- PR10 · Rate limiting + bot detection for services/lana-worker.
--
-- WHY POSTGRES: there is no Redis in this stack, and Cloud Run is horizontally
-- scaled with no shared process memory, so an in-process counter is worthless
-- (each new instance resets it, and Cloud Run happily runs N instances). These
-- three tables are the durable counter store. Every read and write goes through
-- the SECURITY DEFINER RPCs below, which the worker calls with the service-role
-- key. Clients (anon / authenticated) get no access at all: this is billing and
-- abuse infrastructure, not user data.
--
-- COST: one extra RPC round-trip per turn (~2-4 ms in-region). The turn already
-- makes 3+ Supabase calls (get_session_for_user, insert_message, list_messages),
-- so this is noise next to a 1-3 s LLM call.
--
-- NON-DESTRUCTIVE: three new tables, six new functions, no existing object is
-- altered. Rollback = drop the three tables and six functions (or just leave
-- them and set LANA_RATE_LIMIT=off in the worker — nothing else reads them).

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------------
-- 1. Windowed counters
-- ---------------------------------------------------------------------------
-- One row per (subject, metric, window, bucket). The bucket is derived from the
-- window length so the same table serves a 24 h turn quota, a 1 h session-create
-- quota, and a 1 h places quota without any schema branching.
--
--   bucket_start = to_timestamp(floor(epoch(now()) / window_seconds) * window_seconds)
--
-- For window_seconds = 86400 that is exactly UTC midnight, which is the daily
-- reset the tier table (12 / 60 / 200 turns per day) assumes.

create table if not exists public.lana_rate_counters (
  subject_kind    text        not null
                  check (subject_kind in ('user', 'ip', 'device')),
  subject_id      text        not null
                  check (length(subject_id) between 1 and 128),
  metric          text        not null
                  check (metric in ('turn', 'session_create', 'places_call')),
  window_seconds  integer     not null
                  check (window_seconds between 60 and 604800),
  bucket_start    timestamptz not null,
  hits            integer     not null default 0,
  first_at        timestamptz not null default now(),
  last_at         timestamptz not null default now(),
  primary key (subject_kind, subject_id, metric, window_seconds, bucket_start)
);

comment on table public.lana_rate_counters is
  'Durable request counters for the lana-worker throttle (PR10). One row per '
  '(subject, metric, window, time-bucket). Written only by '
  'public.lana_rate_consume() under the service role. No client access. '
  'Prune with public.lana_rate_prune().';

comment on column public.lana_rate_counters.subject_id is
  'users.id for subject_kind=user; a truncated sha256 of the client IP for '
  'subject_kind=ip; the X-Lana-Device-Id header (or a UA+IP hash fallback) for '
  'subject_kind=device. Raw IPs are never stored.';

-- Prune scan: "everything older than X".
create index if not exists lana_rate_counters_bucket_idx
  on public.lana_rate_counters (bucket_start);

-- Ops: "who is hammering us right now".
create index if not exists lana_rate_counters_hot_idx
  on public.lana_rate_counters (metric, bucket_start desc, hits desc);

alter table public.lana_rate_counters enable row level security;

drop policy if exists "lana_rate_counters_no_client_access" on public.lana_rate_counters;
create policy "lana_rate_counters_no_client_access"
  on public.lana_rate_counters for all
  to authenticated, anon
  using (false)
  with check (false);

-- ---------------------------------------------------------------------------
-- 2. Abuse flags
-- ---------------------------------------------------------------------------
-- A flag is an assertion about a subject ("this device posts every 812 ms with
-- the same payload"). blocked=true is the tier-0 hard block; blocked=false is a
-- suspicion recorded in shadow mode so we can measure the false-positive rate
-- before we ever enforce it.

create table if not exists public.lana_abuse_flags (
  id            uuid        primary key default gen_random_uuid(),
  subject_kind  text        not null
                check (subject_kind in ('user', 'ip', 'device')),
  subject_id    text        not null
                check (length(subject_id) between 1 and 128),
  reason        text        not null
                check (reason in (
                  'session_flood',      -- too many POST /lana/sessions per IP/device
                  'metronomic',         -- inter-message gap variance below human floor
                  'templated_payload',  -- same message body over and over
                  'burst',              -- sub-second consecutive turns
                  'manual'              -- set by an operator
                )),
  signal        jsonb       not null default '{}'::jsonb,
  hits          integer     not null default 1,
  blocked       boolean     not null default false,
  expires_at    timestamptz,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  unique (subject_kind, subject_id, reason)
);

comment on table public.lana_abuse_flags is
  'Bot / abuse verdicts per subject (PR10). blocked=true means tier 0: '
  'lana_rate_consume() denies every request for that subject until expires_at. '
  'blocked=false is an observation only (shadow mode). Written by '
  'public.lana_abuse_flag_set(); cleared by public.lana_abuse_flag_clear().';

create index if not exists lana_abuse_flags_subject_idx
  on public.lana_abuse_flags (subject_kind, subject_id)
  where blocked;

create index if not exists lana_abuse_flags_recent_idx
  on public.lana_abuse_flags (updated_at desc);

alter table public.lana_abuse_flags enable row level security;

drop policy if exists "lana_abuse_flags_no_client_access" on public.lana_abuse_flags;
create policy "lana_abuse_flags_no_client_access"
  on public.lana_abuse_flags for all
  to authenticated, anon
  using (false)
  with check (false);

-- ---------------------------------------------------------------------------
-- 3. Turn rhythm samples
-- ---------------------------------------------------------------------------
-- The cheapest behavioural signal we have: humans type irregularly, scripts do
-- not. We keep a short ring buffer of inter-turn gaps and payload hashes per
-- subject. Bounded to p_max_samples entries, so the row never grows.

create table if not exists public.lana_turn_rhythm (
  subject_kind    text        not null
                  check (subject_kind in ('user', 'ip', 'device')),
  subject_id      text        not null
                  check (length(subject_id) between 1 and 128),
  last_turn_at    timestamptz,
  gaps_ms         bigint[]    not null default '{}'::bigint[],
  payload_hashes  text[]      not null default '{}'::text[],
  updated_at      timestamptz not null default now(),
  primary key (subject_kind, subject_id)
);

comment on table public.lana_turn_rhythm is
  'Rolling inter-turn timing + payload-hash ring buffer per subject (PR10). '
  'Feeds the metronomic / templated / burst bot signals. Bounded ring buffer; '
  'no message content is stored, only sha256 prefixes.';

create index if not exists lana_turn_rhythm_updated_idx
  on public.lana_turn_rhythm (updated_at);

alter table public.lana_turn_rhythm enable row level security;

drop policy if exists "lana_turn_rhythm_no_client_access" on public.lana_turn_rhythm;
create policy "lana_turn_rhythm_no_client_access"
  on public.lana_turn_rhythm for all
  to authenticated, anon
  using (false)
  with check (false);

-- ---------------------------------------------------------------------------
-- 4. RPC: lana_rate_consume
-- ---------------------------------------------------------------------------
-- The single hot-path call. Atomic: the INSERT .. ON CONFLICT DO UPDATE both
-- increments and returns the post-increment value under one row lock, so two
-- concurrent Cloud Run instances cannot both see "11 of 12".
--
-- Over-limit requests are still counted. That is deliberate: the count is the
-- abuse signal, and a scripted client that keeps hammering after the wall should
-- be visible in the data.

create or replace function public.lana_rate_consume(
  p_subject_kind   text,
  p_subject_id     text,
  p_metric         text,
  p_limit          integer,
  p_window_seconds integer default 86400,
  p_consume        boolean default true
) returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
as $$
declare
  v_bucket   timestamptz;
  v_hits     integer;
  v_blocked  boolean := false;
  v_reason   text;
begin
  if p_subject_id is null or length(trim(p_subject_id)) = 0 then
    raise exception 'subject_id_required' using errcode = 'P0001';
  end if;
  if p_window_seconds is null or p_window_seconds < 60 then
    raise exception 'window_seconds_too_small' using errcode = 'P0001';
  end if;

  -- Tier 0: an active hard block short-circuits everything. No counter write —
  -- a blocked script must not be able to grow our tables.
  select true, f.reason
    into v_blocked, v_reason
  from public.lana_abuse_flags f
  where f.subject_kind = p_subject_kind
    and f.subject_id   = p_subject_id
    and f.blocked
    and (f.expires_at is null or f.expires_at > now())
  limit 1;

  if coalesce(v_blocked, false) then
    return jsonb_build_object(
      'allowed',        false,
      'blocked',        true,
      'block_reason',   v_reason,
      'used',           null,
      'limit',          p_limit,
      'remaining',      0,
      'window_seconds', p_window_seconds,
      'reset_at',       null
    );
  end if;

  v_bucket := to_timestamp(
    floor(extract(epoch from now()) / p_window_seconds) * p_window_seconds
  );

  if p_consume then
    insert into public.lana_rate_counters as c
      (subject_kind, subject_id, metric, window_seconds, bucket_start, hits)
    values
      (p_subject_kind, p_subject_id, p_metric, p_window_seconds, v_bucket, 1)
    on conflict (subject_kind, subject_id, metric, window_seconds, bucket_start)
    do update set hits = c.hits + 1, last_at = now()
    returning c.hits into v_hits;
  else
    select c.hits into v_hits
    from public.lana_rate_counters c
    where c.subject_kind   = p_subject_kind
      and c.subject_id     = p_subject_id
      and c.metric         = p_metric
      and c.window_seconds = p_window_seconds
      and c.bucket_start   = v_bucket;
    v_hits := coalesce(v_hits, 0);
  end if;

  return jsonb_build_object(
    'allowed',        (p_limit is null or v_hits <= p_limit),
    'blocked',        false,
    'block_reason',   null,
    'used',           v_hits,
    'limit',          p_limit,
    'remaining',      greatest(0, coalesce(p_limit, 0) - v_hits),
    'window_seconds', p_window_seconds,
    'reset_at',       v_bucket + make_interval(secs => p_window_seconds)
  );
end;
$$;

comment on function public.lana_rate_consume(text, text, text, integer, integer, boolean) is
  'Atomically increment and evaluate one rate-limit bucket. Returns '
  '{allowed, blocked, block_reason, used, limit, remaining, window_seconds, reset_at}. '
  'p_limit null = count only, always allowed. p_consume false = peek.';

-- ---------------------------------------------------------------------------
-- 5. RPC: lana_rhythm_observe
-- ---------------------------------------------------------------------------
-- Records one turn and returns the behavioural verdict. Cheapest-first signals:
--   burst        — consecutive turns under _burst_floor ms (nobody types that fast)
--   metronomic   — coefficient of variation of gaps below _cv_floor (a cron job)
--   templated    — the same payload hash over and over
-- The caller decides what to do with the verdict; this function never blocks.

create or replace function public.lana_rhythm_observe(
  p_subject_kind text,
  p_subject_id   text,
  p_payload_hash text default null,
  p_max_samples  integer default 16
) returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
as $$
declare
  v_last        timestamptz;
  v_gaps        bigint[];
  v_hashes      text[];
  v_gap_ms      bigint;
  v_n           integer;
  v_mean        double precision;
  v_sd          double precision;
  v_cv          double precision;
  v_min         bigint;
  v_distinct    integer;
  v_identical   double precision := 0;
  v_burst       boolean := false;
  v_metronomic  boolean := false;
  v_templated   boolean := false;
  -- Tuning floors. Deliberately conservative: these run in shadow mode first
  -- and every threshold here is a false-positive risk against a real person.
  c_burst_floor_ms  constant bigint  := 400;
  c_cv_floor        constant double precision := 0.15;
  c_min_samples     constant integer := 6;
  c_identical_floor constant double precision := 0.60;
begin
  if p_subject_id is null or length(trim(p_subject_id)) = 0 then
    raise exception 'subject_id_required' using errcode = 'P0001';
  end if;

  select r.last_turn_at, r.gaps_ms, r.payload_hashes
    into v_last, v_gaps, v_hashes
  from public.lana_turn_rhythm r
  where r.subject_kind = p_subject_kind
    and r.subject_id   = p_subject_id
  for update;

  v_gaps   := coalesce(v_gaps, '{}'::bigint[]);
  v_hashes := coalesce(v_hashes, '{}'::text[]);

  if v_last is not null then
    v_gap_ms := (extract(epoch from (now() - v_last)) * 1000)::bigint;
    -- A gap longer than 10 minutes is a new sitting, not a typing rhythm.
    if v_gap_ms between 0 and 600000 then
      v_gaps := array_append(v_gaps, v_gap_ms);
    end if;
  end if;

  if p_payload_hash is not null and length(p_payload_hash) > 0 then
    v_hashes := array_append(v_hashes, p_payload_hash);
  end if;

  -- Hard-bound both ring buffers.
  if coalesce(array_length(v_gaps, 1), 0) > p_max_samples then
    v_gaps := v_gaps[(array_length(v_gaps, 1) - p_max_samples + 1):];
  end if;
  if coalesce(array_length(v_hashes, 1), 0) > p_max_samples then
    v_hashes := v_hashes[(array_length(v_hashes, 1) - p_max_samples + 1):];
  end if;

  insert into public.lana_turn_rhythm as r
    (subject_kind, subject_id, last_turn_at, gaps_ms, payload_hashes, updated_at)
  values
    (p_subject_kind, p_subject_id, now(), v_gaps, v_hashes, now())
  on conflict (subject_kind, subject_id) do update
    set last_turn_at   = now(),
        gaps_ms        = excluded.gaps_ms,
        payload_hashes = excluded.payload_hashes,
        updated_at     = now();

  v_n := coalesce(array_length(v_gaps, 1), 0);

  if v_n >= c_min_samples then
    select avg(g), coalesce(stddev_samp(g), 0), min(g)
      into v_mean, v_sd, v_min
    from unnest(v_gaps) as g;

    if v_mean > 0 then
      v_cv := v_sd / v_mean;
      v_metronomic := (v_cv < c_cv_floor);
    end if;
    v_burst := (v_min < c_burst_floor_ms);
  end if;

  if coalesce(array_length(v_hashes, 1), 0) >= c_min_samples - 1 then
    select count(distinct h) into v_distinct from unnest(v_hashes) as h;
    v_identical := 1.0 - (v_distinct::double precision
                          / array_length(v_hashes, 1)::double precision);
    v_templated := (v_identical >= c_identical_floor);
  end if;

  return jsonb_build_object(
    'samples',         v_n,
    'mean_gap_ms',     v_mean,
    'stddev_gap_ms',   v_sd,
    'cv',              v_cv,
    'min_gap_ms',      v_min,
    'identical_ratio', v_identical,
    'burst',           v_burst,
    'metronomic',      v_metronomic,
    'templated',       v_templated,
    'bot_like',        (v_burst or v_metronomic or v_templated)
  );
end;
$$;

comment on function public.lana_rhythm_observe(text, text, text, integer) is
  'Record one turn for a subject and return the behavioural bot verdict '
  '{samples, cv, identical_ratio, burst, metronomic, templated, bot_like}. '
  'Never blocks — the caller decides. Stores no message content.';

-- ---------------------------------------------------------------------------
-- 6. RPC: lana_abuse_flag_set / lana_abuse_flag_clear
-- ---------------------------------------------------------------------------

create or replace function public.lana_abuse_flag_set(
  p_subject_kind text,
  p_subject_id   text,
  p_reason       text,
  p_signal       jsonb   default '{}'::jsonb,
  p_blocked      boolean default false,
  p_ttl          interval default interval '24 hours'
) returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
as $$
declare
  v_row public.lana_abuse_flags;
begin
  insert into public.lana_abuse_flags as f
    (subject_kind, subject_id, reason, signal, blocked, expires_at)
  values
    (p_subject_kind, p_subject_id, p_reason, coalesce(p_signal, '{}'::jsonb),
     coalesce(p_blocked, false),
     case when p_ttl is null then null else now() + p_ttl end)
  on conflict (subject_kind, subject_id, reason) do update
    set signal     = coalesce(excluded.signal, f.signal),
        hits       = f.hits + 1,
        -- A flag can escalate to blocked but never silently de-escalates here;
        -- use lana_abuse_flag_clear() to lift one.
        blocked    = f.blocked or excluded.blocked,
        expires_at = excluded.expires_at,
        updated_at = now()
  returning * into v_row;

  return to_jsonb(v_row);
end;
$$;

comment on function public.lana_abuse_flag_set(text, text, text, jsonb, boolean, interval) is
  'Upsert an abuse flag. Escalates to blocked; never de-escalates (use '
  'lana_abuse_flag_clear). p_ttl null = permanent.';

create or replace function public.lana_abuse_flag_clear(
  p_subject_kind text,
  p_subject_id   text,
  p_reason       text default null
) returns integer
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
as $$
declare
  v_n integer;
begin
  delete from public.lana_abuse_flags f
  where f.subject_kind = p_subject_kind
    and f.subject_id   = p_subject_id
    and (p_reason is null or f.reason = p_reason);
  get diagnostics v_n = row_count;
  return v_n;
end;
$$;

comment on function public.lana_abuse_flag_clear(text, text, text) is
  'Operator escape hatch: lift a false-positive block. p_reason null clears all '
  'flags for the subject. Returns rows deleted.';

-- ---------------------------------------------------------------------------
-- 7. RPC: lana_rate_prune
-- ---------------------------------------------------------------------------
-- No cron at pilot scale (same posture as /lana/area/progress read-repair).
-- The worker calls this opportunistically, roughly once per 500 turns.

create or replace function public.lana_rate_prune(
  p_older_than interval default interval '7 days'
) returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
as $$
declare
  v_counters integer;
  v_rhythm   integer;
  v_flags    integer;
begin
  delete from public.lana_rate_counters
  where bucket_start < now() - p_older_than;
  get diagnostics v_counters = row_count;

  delete from public.lana_turn_rhythm
  where updated_at < now() - p_older_than;
  get diagnostics v_rhythm = row_count;

  delete from public.lana_abuse_flags
  where expires_at is not null and expires_at < now() - interval '1 day';
  get diagnostics v_flags = row_count;

  return jsonb_build_object(
    'counters_deleted', v_counters,
    'rhythm_deleted',   v_rhythm,
    'flags_deleted',    v_flags
  );
end;
$$;

comment on function public.lana_rate_prune(interval) is
  'Opportunistic GC for the throttle tables. Called by the worker roughly once '
  'per 500 turns (no cron at pilot scale).';

-- ---------------------------------------------------------------------------
-- 8. Grants — service role only
-- ---------------------------------------------------------------------------
-- The worker holds SUPABASE_SERVICE_ROLE_KEY (app/auth.py::service_client). No
-- anon / authenticated grant anywhere: a client that could call these could
-- reset its own counter.

revoke all on function public.lana_rate_consume(text, text, text, integer, integer, boolean) from public;
revoke all on function public.lana_rhythm_observe(text, text, text, integer) from public;
revoke all on function public.lana_abuse_flag_set(text, text, text, jsonb, boolean, interval) from public;
revoke all on function public.lana_abuse_flag_clear(text, text, text) from public;
revoke all on function public.lana_rate_prune(interval) from public;

grant execute on function public.lana_rate_consume(text, text, text, integer, integer, boolean) to service_role;
grant execute on function public.lana_rhythm_observe(text, text, text, integer) to service_role;
grant execute on function public.lana_abuse_flag_set(text, text, text, jsonb, boolean, interval) to service_role;
grant execute on function public.lana_abuse_flag_clear(text, text, text) to service_role;
grant execute on function public.lana_rate_prune(interval) to service_role;

grant select, insert, update, delete on public.lana_rate_counters to service_role;
grant select, insert, update, delete on public.lana_abuse_flags   to service_role;
grant select, insert, update, delete on public.lana_turn_rhythm   to service_role;
