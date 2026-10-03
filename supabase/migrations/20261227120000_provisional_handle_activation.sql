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

-- ── 0 · a missing constraint, found while wiring this ───────────────────────
--
-- external_community_identities has only a PRIMARY KEY on id. Nothing stops the same
-- Instagram account being registered against two different places — which is exactly the
-- impersonation this table exists to prevent. Safe to add now: the table is empty.

create unique index if not exists external_community_identities_account_uniq
  on public.external_community_identities (provider, provider_account_id);

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

revoke all on function public.start_handle_provisional(uuid) from public, anon;
grant execute on function public.start_handle_provisional(uuid) to service_role;

-- ── activate on a verified backlink ─────────────────────────────────────────
--
-- Called by the worker's claim_backlink path once it has actually fetched the creator's
-- public profile and found the link. Never call this from a client: the whole value of
-- the backlink is that only the account owner can edit that bio, and a client-asserted
-- "I did it" throws that away.

create or replace function public.activate_handle_on_backlink(
  p_place_id           uuid,
  p_provider           text,
  p_provider_account_id text,
  p_canonical_url      text,
  p_backlink_url_seen  text
)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public
as $$
declare
  v_claim_id uuid;
  v_operator uuid;
begin
  select m.user_id into v_operator
    from public.place_managers m
   where m.place_id = p_place_id and m.role = 'operator' and m.removed_at is null
   order by m.created_at asc limit 1;

  if v_operator is null then
    raise exception 'no_operator' using hint = 'A place with no operator cannot be activated.';
  end if;

  -- The claim this verification belongs to, so the evidence and the decision stay joined.
  select c.id into v_claim_id
    from public.place_claims c
   where c.place_id = p_place_id
     and c.status in ('pending_verification','draft','needs_more_info')
   order by c.submitted_at desc nulls last
   limit 1;

  insert into public.external_community_identities
    (place_id, provider, provider_account_id, canonical_url,
     backlink_url_seen, ownership_method, ownership_verified_at, last_checked_at, claim_id)
  values (p_place_id, p_provider, p_provider_account_id, p_canonical_url,
          p_backlink_url_seen, 'profile_backlink', now(), now(), v_claim_id)
  on conflict (provider, provider_account_id) do update
    set backlink_url_seen     = excluded.backlink_url_seen,
        ownership_verified_at = now(),
        last_checked_at       = now(),
        updated_at            = now();

  -- Record the method. This is the first profile_backlink row in the system, and the
  -- point of recording it is that the next five verifications are not NULL.
  if v_claim_id is not null then
    update public.place_claims
       set verification_method = 'profile_backlink',
           status = 'verified',
           resolved_at = coalesce(resolved_at, now())
     where id = v_claim_id;
  end if;

  update public.places
     set handle_activated_at = now(),
         handle_provisional_until = null,
         updated_at = now()
   where id = p_place_id;

  return jsonb_build_object('placeId', p_place_id, 'claimId', v_claim_id, 'activated', true);
end;
$$;

revoke all on function public.activate_handle_on_backlink(uuid, text, text, text, text)
  from public, anon, authenticated;
grant execute on function public.activate_handle_on_backlink(uuid, text, text, text, text)
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
       set governance_state = 'suspended', updated_at = now()
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

create or replace view public.handle_activation_status as
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
  'closes, and the view is how we watch it close.';

-- ============================================================================
-- ROLLBACK
--   drop view if exists public.handle_activation_status;
--   drop function if exists public.expire_provisional_handles();
--   drop function if exists public.activate_handle_on_backlink(uuid, text, text, text, text);
--   drop function if exists public.start_handle_provisional(uuid);
--   drop function if exists public.handle_provisional_days();
--   drop table if exists public.handle_policy;
--   drop index if exists public.external_community_identities_account_uniq;
--   No existing place is modified by applying this. Windows only start when
--   start_handle_provisional is called.
-- ============================================================================
