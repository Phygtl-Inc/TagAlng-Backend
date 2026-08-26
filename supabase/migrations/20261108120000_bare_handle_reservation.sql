-- Lana · a brand name may be reserved; only the published handle needs a locality ────────
-- 20261106120000 required a hyphen everywhere, which made the hero a dead end: typing
-- "safeway" returned needs_locality with no way to continue, and the CTO spec's own copy
-- ("Choose the exact location. We'll help complete this handle next.") promises a next step.
--
-- §7.7 enables the submit arrow when the response is available OR location_required, and
-- §9.6 is the step that completes it: reserve "safeway", pick the Foster City branch, and
-- the handle becomes safeway-foster-city. So the hyphen belongs on places.handle — the
-- public, permanent thing — and not on a 20-minute hold over a string.
--
-- places_handle_format is unchanged and still refuses a bare published handle.

-- Locality is now optional, and the caller says whether it is required.
create or replace function public._place_handle_shape_error(
  p_handle           text,
  p_require_locality boolean default true
)
returns text
language sql
immutable
set search_path = pg_catalog, public
as $$
  select case
    when p_handle is null or length(p_handle) < 3 then 'too_short'
    when length(p_handle) > 48 then 'too_long'
    when p_handle !~ '^[a-z0-9]+(-[a-z0-9]+)*$' then 'malformed'
    -- A one-word handle cannot be published: the hyphen is what keeps place handles
    -- disjoint from users.handle (^[a-z0-9]{3,20}$). It can still be held.
    when p_require_locality and p_handle !~ '-' then 'needs_locality'
    when p_handle ~ '^[0-9-]+$' then 'numeric_only'
    when p_handle ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-' then 'uuid_like'
    else null
  end;
$$;

-- check: report a bare-but-free name as available, and flag that a locality is still owed.
-- The hero can then let the person continue, which is the whole point of §7.7.
create or replace function public.check_place_handle(p_in text)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v      text := public.normalize_place_handle(p_in);
  v_err  text := public._place_handle_shape_error(v, false);
  v_why  text;
  v_sugg text[] := '{}';
  v_try  text;
  i      int;
begin
  if v_err is not null then
    return jsonb_build_object(
      'normalizedHandle', v, 'status', 'invalid',
      'reason', v_err, 'needsLocality', false, 'suggestions', to_jsonb(v_sugg));
  end if;

  v_why := public._place_handle_taken(v);
  if v_why is null then
    return jsonb_build_object(
      'normalizedHandle', v, 'status', 'available', 'reason', null,
      -- true means: usable as a hold, but §9.6 must complete it before it can go live.
      'needsLocality', v !~ '-', 'suggestions', to_jsonb(v_sugg));
  end if;

  for i in 2..9 loop
    exit when array_length(v_sugg, 1) >= 3;
    v_try := v || '-' || i::text;
    if length(v_try) <= 48 and public._place_handle_taken(v_try) is null then
      v_sugg := v_sugg || v_try;
    end if;
  end loop;

  return jsonb_build_object(
    'normalizedHandle', v, 'status', 'unavailable',
    'reason', v_why, 'needsLocality', false, 'suggestions', to_jsonb(v_sugg));
end;
$$;

-- reserve: hold a bare name. Nothing publishes from here, so the locality can wait.
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
  v_err text := public._place_handle_shape_error(v, false);
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

  update public.place_handle_reservations
     set status = 'expired'
   where status in ('active', 'bound')
     and expires_at <= now();

  v_why := public._place_handle_taken(v);
  if v_why is not null then
    return jsonb_build_object('status', v_why, 'normalizedHandle', v);
  end if;

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
    'status', 'reserved', 'reservationId', v_id, 'normalizedHandle', v,
    -- The claim flow must complete this before approval; approve_place_claim refuses a
    -- bare handle, so a forgotten locality fails closed rather than publishing one.
    'needsLocality', v !~ '-', 'expiresAt', v_exp);
end;
$$;

comment on function public._place_handle_shape_error(text, boolean) is
  'Shape rules with no database lookup. p_require_locality=false for reservations (a brand '
  'name may be held); the default true is what places.handle must satisfy to be published.';

-- The signature changed, so re-state the grants.
revoke execute on function public._place_handle_shape_error(text, boolean) from public, anon;
revoke execute on function public.check_place_handle(text) from public, anon, authenticated;
revoke execute on function public.reserve_place_handle(text, text, text, text, text)
  from public, anon, authenticated;
drop function if exists public._place_handle_shape_error(text);

-- ── approve: a bare reservation must not dead-end at the finish line either ──────
-- If someone reserved "safeway" and picked the Foster City branch, the handle to publish
-- is safeway-foster-city, not safeway. The previous version fed the reservation string
-- straight to the strict shape check and returned bad_handle, so the whole claim stalled
-- at approval with nothing the reviewer could do about it.
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
  c        public.place_claims;
  v_held   text;
  v        text;
  v_err    text;
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
  if c.requested_by = auth.uid() then
    raise exception 'a claimant cannot approve their own claim';
  end if;

  select normalized_handle into v_held
    from public.place_handle_reservations where id = c.reservation_id;

  -- Reviewer's override wins; then the reserved string, but only if it can be published;
  -- otherwise the locality-derived suggestion (§9.6).
  v := public.normalize_place_handle(coalesce(
         p_handle,
         case when public._place_handle_shape_error(v_held) is null then v_held end,
         public.suggest_place_handle(c.place_id, v_held)));

  v_err := public._place_handle_shape_error(v);
  if v_err is not null then
    return jsonb_build_object('status', 'bad_handle', 'reason', v_err, 'heldHandle', v_held);
  end if;
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

revoke execute on function public.approve_place_claim(uuid, text, text) from public, anon;
grant execute on function public.approve_place_claim(uuid, text, text) to authenticated;

-- ── suggest: do not repeat a locality the name already carries ──────────────────
-- "Orlando Public Library" in Orlando derived orlando-public-library-orlando. These get
-- printed on windows and read aloud, so the stutter matters. Appending is skipped only
-- when the name already contains the locality AND the result still has a hyphen, since
-- the hyphen is what keeps place handles disjoint from users.handle.
create or replace function public.suggest_place_handle(
  p_place_id      uuid,
  p_ignore_handle text default null
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
  v_slug  text;
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
  v_loc  := coalesce(public.normalize_place_handle(v_loc), v_zip);
  v_slug := public.normalize_place_handle(v_name);

  if v_loc is null or v_slug is null then
    return null;
  end if;

  -- Already says where it is, and can stand alone as a handle.
  if ('-' || v_slug || '-') like ('%-' || v_loc || '-%') and v_slug ~ '-' then
    v_base := left(v_slug, 44);
  else
    v_base := left(v_slug || '-' || v_loc, 44);
  end if;
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

revoke execute on function public.suggest_place_handle(uuid, text)
  from public, anon, authenticated;
