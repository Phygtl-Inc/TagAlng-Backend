-- Lana · Place handles + owner claim ─────────────────────────────────────────────
-- Phase 2 of places, the half 20260906120000_circles_places_phase_a left as a hook:
-- "places.claimed_by · Phase 2 owner-claim. Invariant for Phase 1: always null, no
-- endpoint writes it." This is that endpoint.
--
-- Two doors into one row, and the reason no duplicate can happen: places.google_place_id
-- is already unique. A member grounding their gym from chat and an operator claiming it
-- from the locations website resolve to the SAME row. The claim upgrades governance on
-- that row — it never creates a second place, and it never touches member activity.
--
-- Source specs: LANA_FOR_LOCATIONS CTO spec (§7 normalization, §7.4 protected, §8
-- reservation atomicity, §9 claim screens, §13 model, §16 state machines) and
-- lana_app_backend_location_context_spec (§1.2 governance separate from existence,
-- §5.0.2 claim transition on the existing canonical row).
--
-- Deliberate deltas from the spec, all additive-friendly:
--   · governance_state on places instead of a parallel `locations` table — Tommaso,
--     LOCATIONS_VALUE_PROP §8 Q1: "same places row via claimed_by ... a parallel
--     business-profile store gives us two sources of truth within a month."
--   · no organizations table. No multi-location claimant exists yet (§9.4 is for
--     Safeway-style chains); add it when one shows up, not before.
--   · verification lives as columns on place_claims rather than its own table. Split it
--     only when someone needs two verification attempts against one claim.
--   · no separate VERIFIED → ACTIVE step (§16.1). A handle resolves iff it is non-null
--     and governance_state = 'operator_verified'; 'suspended' turns it off without
--     freeing the string. One less state to keep consistent.
--   · reservation tokens are generated and hashed by the CALLER, so raw tokens never
--     reach Postgres at all (§8.1 wants only the hash stored; this is stronger).
--
-- Handles must contain a hyphen. That is not cosmetic: public.users.handle is
-- ^[a-z0-9]{3,20}$ with no hyphen allowed, so requiring one here makes a place handle
-- and a user handle structurally unable to collide — no cross-table uniqueness check,
-- no coordination between the two namespaces. Every handle in every spec example
-- already has one (stmarks-orlando, safeway-foster-city, orangetheory-lakenona).

-- ── 1 · places · the handle, and governance as its own axis ─────────────────────
alter table public.places add column if not exists handle text;
alter table public.places add column if not exists governance_state text not null
  default 'community_started';
alter table public.places add column if not exists verified_at timestamptz;
alter table public.places add column if not exists first_action text;

alter table public.places drop constraint if exists places_handle_format;
alter table public.places add constraint places_handle_format
  check (handle is null or (handle ~ '^[a-z0-9]+(-[a-z0-9]+)+$' and length(handle) between 3 and 48));

alter table public.places drop constraint if exists places_governance_state_chk;
alter table public.places add constraint places_governance_state_chk
  check (governance_state in
    ('community_started', 'claim_pending', 'operator_verified', 'suspended'));

-- No public handle before verification (both specs' hardest non-negotiable), made
-- structural rather than trusted to application code. 'suspended' keeps its handle so
-- the string cannot be recycled onto a different physical place.
alter table public.places drop constraint if exists places_handle_needs_verification;
alter table public.places add constraint places_handle_needs_verification
  check (handle is null or governance_state in ('operator_verified', 'suspended'));

create unique index if not exists places_handle_uniq
  on public.places (handle) where handle is not null;

comment on column public.places.handle is
  'Public location handle: get.lana.help/{handle}. Always contains a hyphen, which is '
  'what keeps it from ever colliding with users.handle (^[a-z0-9]{3,20}$, no hyphens). '
  'Null until an operator claim is verified.';
comment on column public.places.governance_state is
  'Who governs this place, separate from whether it exists (existence is entity/dismissal '
  'state elsewhere). community_started = members made it, no location endorsement implied. '
  'operator_verified never grants access to member conversations, memory, or private data.';
comment on column public.places.first_action is
  'Operator-chosen "what should people do here first?" (CTO spec §9.10). Free text on '
  'purpose — the per-sector catalog is product copy and will churn faster than a check '
  'constraint.';

-- ── 2 · protected_handles · reserved words, in the database ─────────────────────
-- CTO spec §7.4 is explicit that the frontend must not hold the only copy. The list
-- also carries profanity/impersonation blocks, which the spec requires but never
-- enumerates — those need a real source before launch.
create table if not exists public.protected_handles (
  normalized_handle text primary key,
  reason            text not null,
  active            boolean not null default true,
  created_at        timestamptz not null default now()
);
comment on table public.protected_handles is
  'Handles that may never be claimed. System routes seeded here; profanity and '
  'impersonation terms still need a list (CTO spec §7.3 requires the rule, names no source).';

insert into public.protected_handles (normalized_handle, reason)
select h, 'system_route' from unnest(array[
  'admin', 'api', 'app', 'auth', 'billing', 'blog', 'chat', 'claim', 'community',
  'contact', 'dashboard', 'help', 'lana', 'legal', 'login', 'logout', 'new', 'people',
  'privacy', 'safety', 'settings', 'signup', 'support', 'terms', 'verify', 'www'
]) as h
on conflict (normalized_handle) do nothing;

alter table public.protected_handles enable row level security;

-- ── 3 · place_handle_reservations · a short hold, not a claim ───────────────────
-- CTO spec §8.1: "Reservation is not a claim." Availability checks are read-only; a row
-- appears here only when the visitor commits. Bound to an anonymous session before
-- auth, to the user after.
create table if not exists public.place_handle_reservations (
  id                     uuid primary key default gen_random_uuid(),
  normalized_handle      text not null,
  token_hash             text not null unique,
  anonymous_session_hash text,
  user_id                uuid references public.users (id),
  place_id               uuid references public.places (id),
  status                 text not null default 'active'
                           check (status in
                             ('active', 'bound', 'consumed', 'released', 'expired')),
  source                 text,
  requested_place_type   text,
  expires_at             timestamptz not null,
  extended_at            timestamptz,
  created_at             timestamptz not null default now(),
  updated_at             timestamptz not null default now()
);
comment on table public.place_handle_reservations is
  'Temporary hold on a handle string. token_hash is a SHA-256 of a caller-generated '
  'token — the raw token never reaches this database. No client access at all.';

-- This partial index IS the atomicity guarantee. Two visitors both told "available" a
-- millisecond apart both reach the insert; exactly one survives and the other gets a
-- unique_violation, which reserve_place_handle turns into a collision response. Do not
-- rely on the pre-check alone (CTO spec §8.2).
create unique index if not exists place_handle_reservations_live_uniq
  on public.place_handle_reservations (normalized_handle)
  where status in ('active', 'bound');

create index if not exists place_handle_reservations_expiry_idx
  on public.place_handle_reservations (expires_at)
  where status in ('active', 'bound');

drop trigger if exists place_handle_reservations_updated_at
  on public.place_handle_reservations;
create trigger place_handle_reservations_updated_at
before update on public.place_handle_reservations
for each row execute function public.set_updated_at();

alter table public.place_handle_reservations enable row level security;

-- ── 4 · place_claims · who is claiming, and how they proved it ──────────────────
create table if not exists public.place_claims (
  id                        uuid primary key default gen_random_uuid(),
  place_id                  uuid not null references public.places (id) on delete cascade,
  reservation_id            uuid references public.place_handle_reservations (id),
  requested_by              uuid not null references public.users (id),
  role_title                text,
  status                    text not null default 'draft'
                              check (status in ('draft', 'pending_verification',
                                'needs_more_info', 'verified', 'rejected')),
  verification_method       text
                              check (verification_method is null or verification_method in
                                ('domain_email', 'admin_approval', 'manual_review')),
  verification_email_domain text,
  evidence_storage_path     text,
  review_notes              text,
  reviewed_by               uuid references public.users (id),
  submitted_at              timestamptz,
  resolved_at               timestamptz,
  created_at                timestamptz not null default now(),
  updated_at                timestamptz not null default now()
);
comment on table public.place_claims is
  'One operator claim against an existing place. evidence_storage_path points into a '
  'PRIVATE Supabase Storage bucket — never a public one (CTO spec §9.8).';

create unique index if not exists place_claims_one_open_per_place
  on public.place_claims (place_id)
  where status in ('draft', 'pending_verification', 'needs_more_info');

create index if not exists place_claims_review_queue_idx
  on public.place_claims (submitted_at)
  where status in ('pending_verification', 'needs_more_info');

drop trigger if exists place_claims_updated_at on public.place_claims;
create trigger place_claims_updated_at
before update on public.place_claims
for each row execute function public.set_updated_at();

alter table public.place_claims enable row level security;

-- ── 5 · normalization · CTO spec §7.3, one implementation ──────────────────────
-- NFKD first is what makes diacritic stripping free: "café" decomposes to "cafe" plus a
-- combining acute, and the strip-to-[a-z0-9-] step drops the mark and keeps the letter.
create or replace function public.normalize_place_handle(p_in text)
returns text
language plpgsql
immutable
set search_path = pg_catalog, public
as $$
declare
  v text;
begin
  v := lower(normalize(coalesce(p_in, ''), NFKD));
  v := regexp_replace(v, '[[:space:]_]+', '-', 'g');
  v := regexp_replace(v, '[^a-z0-9-]', '', 'g');
  v := regexp_replace(v, '-+', '-', 'g');
  v := btrim(v, '-');
  return nullif(v, '');
end;
$$;

comment on function public.normalize_place_handle(text) is
  'CTO spec §7.3 pipeline. Same function serves the client preview and the server '
  'authority so a handle cannot normalize two ways.';

-- Why a handle is unavailable, or null if it is free.
create or replace function public._place_handle_taken(p_handle text)
returns text
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select case
    when p_handle is null then 'invalid'
    when exists (
      select 1 from public.protected_handles
       where normalized_handle = p_handle and active
    ) then 'protected'
    when exists (
      select 1 from public.places where handle = p_handle
    ) then 'taken'
    when exists (
      select 1 from public.place_handle_reservations
       where normalized_handle = p_handle
         and status in ('active', 'bound')
         and expires_at > now()
    ) then 'held'
    else null
  end;
$$;

-- Shape rules that do not need a database lookup. Returns a reason or null.
create or replace function public._place_handle_shape_error(p_handle text)
returns text
language sql
immutable
set search_path = pg_catalog, public
as $$
  select case
    when p_handle is null or length(p_handle) < 3 then 'too_short'
    when length(p_handle) > 48 then 'too_long'
    -- One-word handles are rejected on purpose: the hyphen is what keeps this namespace
    -- disjoint from users.handle. Every real place gets a locality suffix anyway.
    when p_handle !~ '^[a-z0-9]+(-[a-z0-9]+)+$' then 'needs_locality'
    when p_handle ~ '^[0-9-]+$' then 'numeric_only'
    when p_handle ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-' then 'uuid_like'
    else null
  end;
$$;

-- ── 6 · suggest · slug(name) + slug(locality), numbered on collision ───────────
-- Locality comes out of the Google formatted address, which is
-- "<street>, <city>, <region> <postal>, <country>" — so the city is third from last.
-- Parsing beats storing: no new column, no backfill over the 33 existing rows, and the
-- operator confirms the handle on a dedicated screen anyway (CTO spec §9.6). A wrong
-- guess costs one edit, so this deliberately does not try to be clever.
create or replace function public.suggest_place_handle(
  p_place_id      uuid,
  p_ignore_handle text default null   -- the caller's own live reservation is not a collision
)
returns text
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v_name  text;
  v_addr  text;
  v_zip   text;
  v_parts text[];
  v_loc   text;
  v_base  text;
  v_try   text;
  i       int := 1;
begin
  select p.name, p.address, p.zip
    into v_name, v_addr, v_zip
    from public.places p
   where p.id = p_place_id;

  if v_name is null then
    return null;
  end if;

  v_parts := string_to_array(coalesce(v_addr, ''), ',');
  if array_length(v_parts, 1) >= 3 then
    v_loc := v_parts[array_length(v_parts, 1) - 2];
  end if;
  -- ZIP is the fallback locality; digits still satisfy the hyphen rule (gym-32832).
  v_loc := coalesce(public.normalize_place_handle(v_loc), v_zip);

  if v_loc is null or public.normalize_place_handle(v_name) is null then
    return null;  -- nothing honest to suggest; the operator types it themselves
  end if;

  -- 44, not 48, to leave room for the collision suffix.
  v_base := left(public.normalize_place_handle(v_name) || '-' || v_loc, 44);
  v_base := btrim(regexp_replace(v_base, '-+', '-', 'g'), '-');

  if public._place_handle_shape_error(v_base) is not null then
    return null;
  end if;

  v_try := v_base;
  while v_try is distinct from p_ignore_handle
    and public._place_handle_taken(v_try) is not null loop
    i := i + 1;
    if i > 9 then
      return null;
    end if;
    v_try := v_base || '-' || i::text;
  end loop;

  return v_try;
end;
$$;

comment on function public.suggest_place_handle(uuid, text) is
  'slug(name)-slug(locality), e.g. planet-fitness-lake-nona. Numbered only when two '
  'branches share a locality. Null rather than a bad guess.';

-- ── 7 · check · read-only availability (CTO spec §7.5, §15.1) ─────────────────
-- Returns status and suggestions only. Never who holds the handle (§7.6).
create or replace function public.check_place_handle(p_in text)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v      text := public.normalize_place_handle(p_in);
  v_err  text := public._place_handle_shape_error(v);
  v_why  text;
  v_sugg text[] := '{}';
  v_try  text;
  i      int;
begin
  if v_err is not null then
    return jsonb_build_object(
      'normalizedHandle', v, 'status', 'invalid',
      'reason', v_err, 'suggestions', to_jsonb(v_sugg));
  end if;

  v_why := public._place_handle_taken(v);
  if v_why is null then
    return jsonb_build_object(
      'normalizedHandle', v, 'status', 'available',
      'reason', null, 'suggestions', to_jsonb(v_sugg));
  end if;

  -- Up to three, which the spec promises without saying how. Numbering is the only
  -- honest option here: at the hero we do not know the place yet, so we cannot append a
  -- locality. The real handle gets fixed on the confirm screen once the place is known.
  for i in 2..9 loop
    exit when array_length(v_sugg, 1) >= 3;
    v_try := v || '-' || i::text;
    if length(v_try) <= 48 and public._place_handle_taken(v_try) is null then
      v_sugg := v_sugg || v_try;
    end if;
  end loop;

  return jsonb_build_object(
    'normalizedHandle', v, 'status', 'unavailable',
    'reason', v_why, 'suggestions', to_jsonb(v_sugg));
end;
$$;

-- ── 8 · reserve · atomic, one statement decides (CTO spec §8.2, §15.2) ────────
-- p_token_hash is SHA-256 of a >=256-bit token the caller generated and keeps. Postgres
-- never sees the raw token, so it cannot leak through a log or an error message.
create or replace function public.reserve_place_handle(
  p_in           text,
  p_token_hash   text,
  p_session_hash text default null,
  p_place_type   text default null,
  p_source       text default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v     text := public.normalize_place_handle(p_in);
  v_err text := public._place_handle_shape_error(v);
  v_why text;
  v_id  uuid;
  v_exp timestamptz := now() + interval '20 minutes';
begin
  if v_err is not null then
    return jsonb_build_object('status', 'invalid', 'reason', v_err);
  end if;
  if coalesce(btrim(p_token_hash), '') = '' then
    raise exception 'reserve_place_handle requires a token hash';
  end if;

  -- Free anything that timed out before judging availability.
  update public.place_handle_reservations
     set status = 'expired'
   where status in ('active', 'bound')
     and expires_at <= now();

  v_why := public._place_handle_taken(v);
  if v_why is not null then
    return jsonb_build_object('status', v_why, 'normalizedHandle', v);
  end if;

  -- The pre-check above is a courtesy for a good error message. The partial unique index
  -- is what actually arbitrates when two sessions race here.
  begin
    insert into public.place_handle_reservations (
      normalized_handle, token_hash, anonymous_session_hash,
      requested_place_type, source, expires_at
    ) values (v, p_token_hash, p_session_hash, p_place_type, p_source, v_exp)
    returning id into v_id;
  exception when unique_violation then
    return jsonb_build_object('status', 'collision', 'normalizedHandle', v);
  end;

  return jsonb_build_object(
    'status', 'reserved', 'reservationId', v_id,
    'normalizedHandle', v, 'expiresAt', v_exp);
end;
$$;

-- ── 9 · bind · attach the reservation to whoever just signed in (§15.3) ───────
create or replace function public.bind_place_reservation(p_token_hash text)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  r public.place_handle_reservations;
begin
  if auth.uid() is null then
    raise exception 'bind_place_reservation requires an authenticated caller';
  end if;

  select * into r
    from public.place_handle_reservations
   where token_hash = p_token_hash
     and status in ('active', 'bound')
     and expires_at > now()
   for update;

  if r.id is null then
    return jsonb_build_object('status', 'expired');
  end if;
  -- Single use, and not transferable to a second account.
  if r.user_id is not null and r.user_id <> auth.uid() then
    return jsonb_build_object('status', 'not_yours');
  end if;

  update public.place_handle_reservations
     set user_id     = auth.uid(),
         status      = 'bound',
         expires_at  = greatest(expires_at, now() + interval '24 hours'),
         extended_at = coalesce(extended_at, now())
   where id = r.id;

  return jsonb_build_object(
    'status', 'bound', 'reservationId', r.id, 'normalizedHandle', r.normalized_handle);
end;
$$;

-- ── 10 · save · pick the place, say who you are, submit evidence (§15.5–15.6) ─
-- Idempotent per place: re-calling updates the open claim rather than opening a second.
-- Passing a verification method is what moves it into the review queue.
create or replace function public.save_place_claim(
  p_token_hash    text,
  p_place_id      uuid,
  p_role_title    text default null,
  p_method        text default null,
  p_email_domain  text default null,
  p_evidence_path text default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  r        public.place_handle_reservations;
  v_gov    text;
  v_claim  uuid;
  v_status text;
begin
  if auth.uid() is null then
    raise exception 'save_place_claim requires an authenticated caller';
  end if;

  select * into r
    from public.place_handle_reservations
   where token_hash = p_token_hash
     and user_id = auth.uid()
     and status in ('active', 'bound')
     and expires_at > now()
   for update;

  if r.id is null then
    return jsonb_build_object('status', 'expired');
  end if;

  select governance_state into v_gov
    from public.places where id = p_place_id for update;
  if v_gov is null then
    return jsonb_build_object('status', 'no_such_place');
  end if;
  -- Already governed by someone. This is where a second claimant is told no; it is NOT
  -- where a member-started place gets turned away (that is the whole point of the flow).
  if v_gov = 'operator_verified' then
    return jsonb_build_object('status', 'already_claimed');
  end if;

  v_status := case when p_method is null then 'draft' else 'pending_verification' end;

  insert into public.place_claims (
    place_id, reservation_id, requested_by, role_title,
    status, verification_method, verification_email_domain,
    evidence_storage_path, submitted_at
  ) values (
    p_place_id, r.id, auth.uid(), p_role_title,
    v_status, p_method, p_email_domain, p_evidence_path,
    case when p_method is null then null else now() end
  )
  on conflict (place_id) where status in ('draft', 'pending_verification', 'needs_more_info')
  do update set
    reservation_id            = excluded.reservation_id,
    role_title                = coalesce(excluded.role_title, place_claims.role_title),
    verification_method       = coalesce(excluded.verification_method,
                                         place_claims.verification_method),
    verification_email_domain = coalesce(excluded.verification_email_domain,
                                         place_claims.verification_email_domain),
    evidence_storage_path     = coalesce(excluded.evidence_storage_path,
                                         place_claims.evidence_storage_path),
    -- Submitting evidence advances a draft; it never drags a reviewed claim backwards.
    status                    = case
                                  when place_claims.status = 'draft'
                                   and excluded.status = 'pending_verification'
                                  then 'pending_verification'
                                  else place_claims.status
                                end,
    submitted_at              = coalesce(place_claims.submitted_at, excluded.submitted_at)
  returning id, status into v_claim, v_status;

  update public.place_handle_reservations set place_id = p_place_id where id = r.id;

  -- Governance moves to pending only once evidence is in. A pending claim must not
  -- freeze member Find/Create — nothing here touches memberships, events, or asks.
  if v_status = 'pending_verification' and v_gov = 'community_started' then
    update public.places set governance_state = 'claim_pending' where id = p_place_id;
  end if;

  return jsonb_build_object(
    'status', v_status, 'claimId', v_claim,
    'suggestedHandle', public.suggest_place_handle(p_place_id, r.normalized_handle));
end;
$$;

-- ── 11 · approve / reject · the admin portal's two buttons ────────────────────
-- Approval is the whole §5.0.2 transition and it is one transaction: the claim resolves,
-- the existing place gains governance and its handle, the reservation is consumed.
-- Member activity on the row is untouched by design — there is no delete here.
create or replace function public.approve_place_claim(
  p_claim_id uuid,
  p_handle   text default null,
  p_notes    text default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  c      public.place_claims;
  v      text;
  v_err  text;
  v_why  text;
begin
  if not exists (
    select 1 from public.users u where u.id = auth.uid() and u.founder_role = 'internal'
  ) then
    raise exception 'approve_place_claim is internal-only';
  end if;

  select * into c from public.place_claims where id = p_claim_id for update;
  if c.id is null then
    return jsonb_build_object('status', 'no_such_claim');
  end if;
  if c.status not in ('pending_verification', 'needs_more_info') then
    return jsonb_build_object('status', 'not_open', 'claimStatus', c.status);
  end if;
  -- A reviewer cannot approve their own claim (CTO spec §14.2).
  if c.requested_by = auth.uid() then
    raise exception 'a claimant cannot approve their own claim';
  end if;

  -- Reviewer override, else the handle the claimant reserved, else derive one.
  v := public.normalize_place_handle(
         coalesce(p_handle,
                  (select normalized_handle from public.place_handle_reservations
                    where id = c.reservation_id),
                  public.suggest_place_handle(c.place_id)));  -- no reservation to honour

  v_err := public._place_handle_shape_error(v);
  if v_err is not null then
    return jsonb_build_object('status', 'bad_handle', 'reason', v_err);
  end if;
  -- Ignore the claimant's own reservation when asking whether the string is free.
  if exists (select 1 from public.places where handle = v and id <> c.place_id)
     or exists (select 1 from public.protected_handles
                 where normalized_handle = v and active) then
    return jsonb_build_object('status', 'bad_handle', 'reason', 'taken');
  end if;

  update public.places
     set governance_state = 'operator_verified',
         handle           = v,
         verified_at      = now(),
         claimed_by       = c.requested_by,
         claimed_at       = now(),
         source           = 'owner_claimed'
   where id = c.place_id;

  update public.place_claims
     set status       = 'verified',
         reviewed_by  = auth.uid(),
         review_notes = coalesce(p_notes, review_notes),
         resolved_at  = now()
   where id = c.id;

  update public.place_handle_reservations
     set status = 'consumed' where id = c.reservation_id;

  return jsonb_build_object('status', 'verified', 'handle', v, 'placeId', c.place_id);
end;
$$;

create or replace function public.reject_place_claim(
  p_claim_id     uuid,
  p_notes        text,
  p_needs_more   boolean default false
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  c public.place_claims;
begin
  if not exists (
    select 1 from public.users u where u.id = auth.uid() and u.founder_role = 'internal'
  ) then
    raise exception 'reject_place_claim is internal-only';
  end if;

  select * into c from public.place_claims where id = p_claim_id for update;
  if c.id is null then
    return jsonb_build_object('status', 'no_such_claim');
  end if;
  if c.status not in ('pending_verification', 'needs_more_info') then
    return jsonb_build_object('status', 'not_open', 'claimStatus', c.status);
  end if;

  update public.place_claims
     set status       = case when p_needs_more then 'needs_more_info' else 'rejected' end,
         reviewed_by  = auth.uid(),
         review_notes = p_notes,
         resolved_at  = case when p_needs_more then null else now() end
   where id = c.id;

  -- A rejected claim returns the place to community_started. It never deletes the place
  -- or anything members built on it (app spec §5.0.2).
  if not p_needs_more then
    update public.places
       set governance_state = 'community_started'
     where id = c.place_id and governance_state = 'claim_pending';
    update public.place_handle_reservations
       set status = 'released' where id = c.reservation_id and status in ('active', 'bound');
  end if;

  return jsonb_build_object(
    'status', case when p_needs_more then 'needs_more_info' else 'rejected' end);
end;
$$;

-- ── 12 · resolve · what get.lana.help/{handle} is allowed to read ─────────────
-- Sanitized on purpose (CTO spec §13.11): no address, no claimant, no admin email, no
-- member list. Pending and reserved handles are invisible here, which is what makes
-- their public route a neutral 404 with nothing to leak.
create or replace function public.resolve_place_handle(p_handle text)
returns jsonb
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select jsonb_build_object(
           'placeId',         p.id,
           'handle',          p.handle,
           'displayName',     p.name,
           'placeType',       p.place_type,
           'zip',             p.zip,
           'firstAction',     p.first_action,
           'governanceState', p.governance_state,
           'operatorVerified', true)
    from public.places p
   where p.handle = public.normalize_place_handle(p_handle)
     and p.governance_state = 'operator_verified';
$$;

-- ── 13 · resolve or create · Screen 2, and the same convergence from the other side ──
-- The locations website is a Node app, and place creation currently lives only in Python
-- (circles_flow.ground_affiliation), so an operator whose gym we have never seen had no
-- path at all. This is that path — and it carries the same guarantee as the chat door:
-- on conflict (google_place_id) means a member's gym and an operator's gym are one row,
-- whichever of them arrives first.
--
-- Created as 'operator_submitted', deliberately NOT 'owner_claimed': registering a
-- building is not evidence you run it. approve_place_claim is what promotes the source.

-- places.source predates this migration and its inline check does not know the new
-- value. Drop by definition rather than by guessed constraint name.
do $$
declare c text;
begin
  for c in
    select con.conname
      from pg_constraint con
     where con.conrelid = 'public.places'::regclass
       and con.contype = 'c'
       and pg_get_constraintdef(con.oid) like '%user_grounded%'
  loop
    execute format('alter table public.places drop constraint %I', c);
  end loop;
end $$;

alter table public.places add constraint places_source_chk
  check (source in ('user_grounded', 'operator_submitted', 'owner_claimed', 'import'));

create or replace function public.resolve_or_create_place(
  p_google_place_id text,
  p_name            text,
  p_address         text default null,
  p_lat             double precision default null,
  p_lng             double precision default null,
  p_zip             text default null,
  p_sector          text default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_gpid text := btrim(coalesce(p_google_place_id, ''));
  v_type text;
  v_id   uuid;
  v_new  boolean;
  pl     record;
begin
  -- Screen 2 sits behind the login gate (CTO spec §9.2 precedes §9.3).
  if auth.uid() is null then
    raise exception 'resolve_or_create_place requires an authenticated caller';
  end if;
  if v_gpid = '' then
    raise exception 'resolve_or_create_place requires a google place id';
  end if;
  if btrim(coalesce(p_name, '')) = '' then
    raise exception 'resolve_or_create_place requires a name';
  end if;

  -- The website's six sectors are marketing categories; places.place_type is our own
  -- narrower vocabulary and other code reads it. Map onto it rather than fork the enum
  -- with values the worker has never seen. The operator's real sector intent lands in
  -- places.first_action, which is free text on purpose.
  v_type := case lower(coalesce(p_sector, ''))
              when 'church'          then 'faith'
              when 'faith'           then 'faith'
              when 'gym'             then 'fitness'
              when 'fitness'         then 'fitness'
              when 'school'          then 'school'
              when 'residential'     then 'neighborhood'
              when 'neighborhood'    then 'neighborhood'
              when 'community_space' then 'other'
              when 'supermarket'     then 'other'
              else null
            end;

  -- Insert first and let the unique index decide. do-nothing plus a re-read is what makes
  -- two simultaneous callers converge, instead of one of them getting a 500.
  insert into public.places (
    google_place_id, name, address, lat, lng, zip, place_type, source, created_by
  ) values (
    v_gpid, btrim(p_name), nullif(btrim(coalesce(p_address, '')), ''),
    p_lat, p_lng, nullif(btrim(coalesce(p_zip, '')), ''), v_type,
    'operator_submitted', auth.uid()
  )
  on conflict (google_place_id) do nothing
  returning id into v_id;

  v_new := v_id is not null;

  select p.id, p.name, p.governance_state
    into pl
    from public.places p
   where p.google_place_id = v_gpid;

  -- matchState / nextAction keep the names the locations prototype already branches on,
  -- so wiring it up is a substitution rather than a rewrite.
  return jsonb_build_object(
    'placeId',         pl.id,
    'displayName',     pl.name,
    'governanceState', pl.governance_state,
    'created',         v_new,
    'matchState',      case
                         when v_new then 'created'
                         when pl.governance_state = 'operator_verified'
                           then 'existing_operator_verified'
                         when pl.governance_state = 'claim_pending'
                           then 'existing_claim_pending'
                         else 'existing_community_started'
                       end,
    'nextAction',      case
                         when pl.governance_state = 'operator_verified'
                           then 'already_claimed'
                         when v_new then 'create_claim'
                         else 'claim_existing'
                       end,
    'suggestedHandle', public.suggest_place_handle(pl.id));
end;
$$;

comment on function public.resolve_or_create_place(
  text, text, text, double precision, double precision, text, text) is
  'Screen 2 of the claim flow. Returns the existing canonical place when we already have '
  'it — which is how an operator claim lands on the row a member grounded from chat — and '
  'creates one only when the google place id is genuinely new.';

-- ── 14 · grants · default EXECUTE is granted to PUBLIC, so revoke explicitly ──
-- Website API routes and the admin portal run server-side; only the two functions the
-- signed-in claimant calls directly are reachable as `authenticated`.
revoke execute on function public.normalize_place_handle(text) from public, anon;
revoke execute on function public._place_handle_taken(text) from public, anon, authenticated;
revoke execute on function public._place_handle_shape_error(text) from public, anon;
revoke execute on function public.suggest_place_handle(uuid, text)
  from public, anon, authenticated;
revoke execute on function public.check_place_handle(text) from public, anon, authenticated;
revoke execute on function public.reserve_place_handle(text, text, text, text, text)
  from public, anon, authenticated;
revoke execute on function public.resolve_place_handle(text) from public, anon;

revoke execute on function public.resolve_or_create_place(
  text, text, text, double precision, double precision, text, text) from public, anon;
grant execute on function public.resolve_or_create_place(
  text, text, text, double precision, double precision, text, text) to authenticated;

revoke execute on function public.bind_place_reservation(text) from public, anon;
grant execute on function public.bind_place_reservation(text) to authenticated;

revoke execute on function public.save_place_claim(text, uuid, text, text, text, text)
  from public, anon;
grant execute on function public.save_place_claim(text, uuid, text, text, text, text)
  to authenticated;

-- Internal-only, and gated on founder_role inside as well, so a grant alone is not
-- enough to approve anything.
revoke execute on function public.approve_place_claim(uuid, text, text) from public, anon;
grant execute on function public.approve_place_claim(uuid, text, text) to authenticated;
revoke execute on function public.reject_place_claim(uuid, text, boolean) from public, anon;
grant execute on function public.reject_place_claim(uuid, text, boolean) to authenticated;
