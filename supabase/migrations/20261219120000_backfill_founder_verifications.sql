-- Every verified community must be able to say why.
--
-- THE FINDING (prod, 2026-09-24): 8 places are governance_state='operator_verified'.
-- Only 4 place_claims rows are status='verified'. So at least four communities are
-- verified with no record of who decided, on what evidence, or when.
--
-- At closed-beta volume that is harmless. As a precedent it is not: the next person to
-- verify a place will do it the way the last four were done, and nobody can say what
-- that was. This backfills the record and then closes the path.
--
-- 'manual_founder' is an honest label, not a euphemism. These verifications happened
-- before any policy existed; recording them as domain_email or admin_approval would
-- assert evidence nobody can point to. review_notes says exactly that, and the rows are
-- distinguishable forever.

insert into public.place_claims (
  place_id, requested_by, role_title, status,
  verification_method, review_notes, reviewed_by, submitted_at, resolved_at
)
select
  p.id,
  p.claimed_by,
  'Owner',
  'verified',
  'manual_founder',
  'Backfilled 2026-09-24. Verified during closed beta, before a claim policy existed; '
  || 'no contemporaneous record of the evidence was kept. Recorded so that the '
  || '"no verified place without a claim" invariant holds and so that these rows are '
  || 'never mistaken for policy-verified ones.',
  p.claimed_by,
  coalesce(p.claimed_at, p.verified_at, p.created_at),
  coalesce(p.verified_at, p.claimed_at, p.created_at)
from public.places p
where p.governance_state = 'operator_verified'
  and not exists (
    select 1 from public.place_claims c
    where c.place_id = p.id and c.status = 'verified'
  );

-- ---------------------------------------------------------------------------
-- Close the path.
--
-- governance_state may only reach 'operator_verified' through a resolved claim. Enforced
-- as a trigger because a CHECK cannot look at another table.
--
-- Deliberately permissive in one direction: 'suspended' and any move DOWN are unaffected.
-- Safety actions must never be blocked by a bookkeeping rule.
-- ---------------------------------------------------------------------------
create or replace function public.places_verified_needs_claim()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  if new.governance_state <> 'operator_verified' then
    return new;
  end if;
  if tg_op = 'UPDATE' and old.governance_state = 'operator_verified' then
    return new;  -- already verified; this update is about something else
  end if;

  if not exists (
    select 1 from public.place_claims c
    where c.place_id = new.id and c.status = 'verified'
  ) then
    raise exception 'operator_verified_requires_verified_claim'
      using errcode = 'P0001',
            hint = 'Resolve a place_claims row to status=''verified'' first. '
                || 'If this was a manual decision, record it — that IS the claim.';
  end if;

  return new;
end;
$$;

drop trigger if exists places_verified_needs_claim_trg on public.places;
create trigger places_verified_needs_claim_trg
  before insert or update of governance_state on public.places
  for each row
  execute function public.places_verified_needs_claim();

comment on function public.places_verified_needs_claim() is
  'A community cannot become operator_verified without a resolved claim explaining why. '
  'Not a policy — an audit trail. Moves to suspended or any downgrade are unaffected, '
  'because a safety action must never be blocked by bookkeeping.';

-- ============================================================================
-- ACCEPTANCE — must return 0
--
--   select count(*) from public.places p
--   where p.governance_state = 'operator_verified'
--     and not exists (select 1 from public.place_claims c
--                     where c.place_id = p.id and c.status = 'verified');
--
-- ORDER: this migration must run AFTER 20261218120000, which adds 'manual_founder' to
-- the verification_method CHECK. Applied out of order, every insert above fails.
--
-- ROLLBACK
--   drop trigger if exists places_verified_needs_claim_trg on public.places;
--   drop function if exists public.places_verified_needs_claim();
--   delete from public.place_claims where verification_method = 'manual_founder';
-- ============================================================================
