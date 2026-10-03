-- Provisional handles · the mechanism that shipped and never ran
--
-- STATE TODAY (verified in production 2026-09-29)
--
--   handle_provisional_until : set on 0 places
--   handle_activated_at      : set on 0 places
--   profile_backlink         : allowed by the CHECK, used by 0 of 14 claims
--   external_community_identities : 0 rows
--
--   place_claims.verification_method across all 14: domain_email (5), manual_founder (4),
--   and NULL (5). Five places reached 'verified' with no record of who decided or on what
--   evidence. At this volume that is harmless; as the de facto policy it is untenable,
--   and it is the thing that gets copied.
--
--   So the creator verification model — the one the entire handle scheme rests on — has
--   run zero times. The pilot is its first real test, which is the argument for testing it
--   on our own pilot creator rather than on the tenth one.
--
-- THE RULE
--   The link works the day they claim it. The backlink must verify before the window
--   closes, or the public route goes dark and the handle returns to reservation.
--
--   Gate publication hard, gate reservation loosely. That asymmetry is deliberate: an
--   answer engine that ingests a wrong or impersonated place page cannot be made to
--   un-ingest it. A reservation is reversible; a citation is not.

-- ── 0 · uniqueness is already enforced ──────────────────────────────────────
--
-- 20261228120002 already makes one external account belong to one community, with two
-- partial unique indexes: (provider, provider_account_id) where the id is known (OAuth)
-- and (provider, lower(username)) where it is not (backlink). A plain unique index on
-- (provider, provider_account_id) would add nothing — NULLs are distinct, so it never
-- constrains a backlink row, which is the only kind this migration creates.

-- Marks a suspension THIS mechanism made, so activation can undo exactly that one and
-- never lift a suspension somebody imposed for another reason.
alter table public.places
  add column if not exists handle_expired_at timestamptz;

comment on column public.places.handle_expired_at is
  'Set by expire_provisional_handles when it suspends a place whose provisional window '
  'lapsed. A later verified backlink restores operator_verified only when this is set.';

-- Config, not a constant. Starts permissive and ratchets against observed data — nobody
-- guesses a number and then defends it.
create table if not exists public.handle_policy (
  key   text primary key,
  value jsonb not null,
  updated_at timestamptz not null default now()
);

insert into public.handle_policy (key, value) values
  ('provisional_window', '{"days": 30}'::jsonb)
on conflict (key) do nothing;

alter table public.handle_policy enable row level security;

create or replace function public.handle_provisional_days()
returns int
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select coalesce((select (value ->> 'days')::int from public.handle_policy
                    where key = 'provisional_window'), 30);
$$;

revoke all on function public.handle_provisional_days() from public, anon, authenticated;
grant execute on function public.handle_provisional_days() to service_role;

-- ── start the clock ─────────────────────────────────────────────────────────

create or replace function public.start_handle_provisional(p_place_id uuid)
returns timestamptz
language plpgsql
volatile
security definer
set search_path = pg_catalog, public
as $$
declare v_until timestamptz;
begin
  update public.places p
     set handle_provisional_until =
           coalesce(p.handle_provisional_until,
                    now() + make_interval(days => public.handle_provisional_days())),
         updated_at = now()
   where p.id = p_place_id
     and p.handle is not null
     and p.handle_activated_at is null
  returning p.handle_provisional_until into v_until;

  return v_until;
end;
$$;

-- authenticated too: Supabase grants it EXECUTE by default, and a user who could start
-- the clock on somebody else's community could get it suspended 30 days later.
revoke all on function public.start_handle_provisional(uuid) from public, anon, authenticated;
grant execute on function public.start_handle_provisional(uuid) to service_role;

-- ── activate on a verified backlink ─────────────────────────────────────────
--
-- Called by the worker once claim_backlink._run_check has actually fetched the creator's
-- public profile, found the link, and stamped ownership_verified_at on the identity row
-- it inserted at start_backlink_claim. Takes THAT row, rather than re-asserting the facts
-- as arguments: the evidence (backlink_url_seen, canonical_url) already lives on it, and
-- a second insert path would race the worker's own and trip eci_provider_username_uniq.
--
-- Never call this from a client: the whole value of the backlink is that only the account
-- owner can edit that bio, and a client-asserted "I did it" throws that away.

create or replace function public.activate_handle_on_backlink(p_identity_id uuid)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public
as $$
declare
  v_row      record;
  v_claim_id uuid;
  v_operator uuid;
  v_restored boolean := false;
begin
  select e.id, e.place_id, e.claim_id, e.ownership_method, e.ownership_verified_at
    into v_row
    from public.external_community_identities e
   where e.id = p_identity_id;

  if v_row.id is null then
    raise exception 'identity_not_found';
  end if;
  if v_row.ownership_verified_at is null or v_row.ownership_method <> 'profile_backlink' then
    raise exception 'backlink_not_verified'
      using hint = 'Activate only after the backlink check has matched.';
  end if;

  select m.user_id into v_operator
    from public.place_managers m
   where m.place_id = v_row.place_id and m.role = 'operator' and m.removed_at is null
   order by m.created_at asc limit 1;

  if v_operator is null then
    raise exception 'no_operator' using hint = 'A place with no operator cannot be activated.';
  end if;

  -- The claim this verification belongs to, so the evidence and the decision stay joined.
  -- The identity row's own claim_id first; else the place's open claim.
  v_claim_id := v_row.claim_id;
  if v_claim_id is null then
    select c.id into v_claim_id
      from public.place_claims c
     where c.place_id = v_row.place_id
       and c.status in ('pending_verification','draft','needs_more_info')
     order by c.submitted_at desc nulls last
     limit 1;
    if v_claim_id is not null then
      update public.external_community_identities
         set claim_id = v_claim_id, updated_at = now()
       where id = v_row.id;
    end if;
  end if;

  -- Record the method. This is the first profile_backlink row in the system, and the
  -- point of recording it is that the next five verifications are not NULL. A claim that
  -- is already verified keeps whatever method it was verified by.
  if v_claim_id is not null then
    update public.place_claims
       set verification_method = 'profile_backlink',
           status = 'verified',
           resolved_at = coalesce(resolved_at, now())
     where id = v_claim_id
       and status <> 'verified';
  end if;

  update public.places p
     set handle_activated_at = coalesce(p.handle_activated_at, now()),
         handle_provisional_until = null,
         -- Undo ONLY the suspension the expiry sweep imposed.
         governance_state = case
                              when p.governance_state = 'suspended'
                                   and p.handle_expired_at is not null
                              then 'operator_verified'
                              else p.governance_state end,
         handle_expired_at = null,
         updated_at = now()
   where p.id = v_row.place_id
  returning (p.governance_state = 'operator_verified') into v_restored;

  return jsonb_build_object(
    'placeId',   v_row.place_id,
    'claimId',   v_claim_id,
    'activated', true,
    'live',      coalesce(v_restored, false));
end;
$$;

revoke all on function public.activate_handle_on_backlink(uuid)
  from public, anon, authenticated;
grant execute on function public.activate_handle_on_backlink(uuid)
  to service_role;

-- ── expire · the route goes dark, the handle is NOT released ────────────────
--
-- Suspended, not released. A creator who missed the window by a week must not find their
-- name taken by whoever was watching — which is a race a legitimate creator loses.

create or replace function public.expire_provisional_handles()
returns int
language sql
volatile
security definer
set search_path = pg_catalog, public
as $$
  with expired as (
    update public.places p
       set governance_state = 'suspended', handle_expired_at = now(), updated_at = now()
     where p.handle_provisional_until is not null
       and p.handle_provisional_until < now()
       and p.handle_activated_at is null
       and p.governance_state = 'operator_verified'
    returning p.id)
  select count(*)::int from expired;
$$;

revoke all on function public.expire_provisional_handles() from public, anon, authenticated;
grant execute on function public.expire_provisional_handles() to service_role;

comment on function public.expire_provisional_handles() is
  'Unverified handles past their window are SUSPENDED, never released. resolve_place_handle '
  'stops answering (publication gate) while the name stays reserved for the person who '
  'picked it. Releasing it would let a watcher take a real creator''s name on a technicality.';

-- ── visibility · what is outstanding ────────────────────────────────────────

-- security_invoker, and no client grants. A plain view runs as its owner and would read
-- `places` past its RLS — publishing every handle, unverified places and each claim's
-- verification method through the public API, which Supabase grants SELECT on by
-- default. This is an operator's console, read with service role.
create or replace view public.handle_activation_status
with (security_invoker = true) as
select p.id as place_id, p.name, p.handle, p.place_type, p.governance_state,
       p.handle_provisional_until, p.handle_activated_at,
       (select count(*) from public.external_community_identities e
         where e.place_id = p.id and e.ownership_verified_at is not null) as verified_identities,
       (select c.verification_method from public.place_claims c
         where c.place_id = p.id and c.status = 'verified'
         order by c.resolved_at desc nulls last limit 1) as claim_method,
       case
         when p.handle_activated_at is not null then 'active'
         when p.handle_provisional_until is null then 'no_window'
         when p.handle_provisional_until < now() then 'expired'
         else 'provisional'
       end as activation_state
from public.places p
where p.handle is not null;

comment on view public.handle_activation_status is
  'Every live handle and where it stands. ''no_window'' is the current state of all nine '
  'verified handles: activated by nothing, expiring never. That is the gap this migration '
  'closes, and the view is how we watch it close. Service role only.';

revoke all on public.handle_activation_status from public, anon, authenticated;
grant select on public.handle_activation_status to service_role;

-- ============================================================================
-- ROLLBACK
--   drop view if exists public.handle_activation_status;
--   drop function if exists public.expire_provisional_handles();
--   drop function if exists public.activate_handle_on_backlink(uuid);
--   drop function if exists public.start_handle_provisional(uuid);
--   drop function if exists public.handle_provisional_days();
--   drop table if exists public.handle_policy;
--   alter table public.places drop column if exists handle_expired_at;
--   No existing place is modified by applying this. Windows only start when
--   start_handle_provisional is called.
-- ============================================================================
