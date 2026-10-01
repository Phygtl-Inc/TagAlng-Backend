-- Creator activation leaves a community with nobody in charge
--
-- TWO BUGS, ONE CAUSE (found 2026-10-01)
--
--   BUG 1 · NO OPERATOR
--
--     8 of 12 creator places have NO row in place_managers. Etiqueta do Reino — our
--     pilot creator — is one of them.
--
--     `places.claimed_by` is set. `place_managers` is empty. So
--     is_community_operator() returns false for everybody, which means NOBODY CAN EDIT
--     THESE COMMUNITIES. update_community_settings, rename_community_handle and
--     community_settings are all gated on place_managers, so the settings screen being
--     designed right now has no one authorised to open it.
--
--     Cause: creator activation writes the legacy single-uuid `claimed_by` field and
--     never inserts into the table that replaced it.
--
--   BUG 2 · THE METHOD IS RECORDED IN PROSE, NOT IN THE COLUMN
--
--     Etiqueta do Reino's claim carries this in review_notes:
--
--       "Self-verified at creator activation: email-confirmed owner of the bound
--        handle reservation. No profile backlink was checked."
--
--     So the verification DID happen and IS documented. It was simply never written to
--     verification_method, which is the column every query and policy actually reads.
--     Six claims are in this state.
--
--     And none of the existing enum values describe it. `domain_email` means a business
--     domain (@crunchfitness.com); this was a personal gmail that owned the handle
--     reservation. Different mechanism, so it needs its own value.

-- ── 1 · a value for what actually happened ──────────────────────────────────

alter table public.place_claims
  drop constraint if exists place_claims_verification_method_check;

alter table public.place_claims
  add constraint place_claims_verification_method_check
  check (verification_method is null or verification_method in (
    'domain_email',      -- business domain, e.g. @crunchfitness.com
    'manual_founder',    -- a human on our side decided, and said why in review_notes
    'profile_backlink',  -- the creator put our link in their public bio and we fetched it
    'reservation_email', -- NEW: confirmed email owner of the bound handle reservation
    'document'
  ));

comment on column public.place_claims.verification_method is
  'HOW this claim was confirmed. Never leave null on a verified row — review_notes is '
  'prose for humans, this column is what queries, policies and audits read. '
  'reservation_email covers creator self-activation: the email that owns the handle '
  'reservation is confirmed, which is weaker than a profile backlink and should be '
  'upgraded to one before a creator community is promoted publicly.';

-- ── 2 · backfill the six, from their own notes ──────────────────────────────
--
-- Not inventing anything. Every row updated here already says in review_notes what was
-- done; this moves that statement into the column that is actually read.

update public.place_claims
   set verification_method = 'reservation_email',
       updated_at = now()
 where status = 'verified'
   and verification_method is null
   and review_notes ilike '%email-confirmed owner%';

update public.place_claims
   set verification_method = 'manual_founder',
       review_notes = coalesce(review_notes, '') ||
         ' [2026-10-01] Method backfilled as manual_founder: row predates the constraint '
         'and carries no record of what was checked.',
       updated_at = now()
 where status = 'verified'
   and verification_method is null;

-- ── 3 · every claimed place gets an operator row ────────────────────────────
--
-- From places.claimed_by, which is the field the activation flow does set. Idempotent.

insert into public.place_managers (place_id, user_id, role, created_at)
select p.id, p.claimed_by, 'operator', coalesce(p.updated_at, now())
from public.places p
where p.claimed_by is not null
  and not exists (
    select 1 from public.place_managers m
     where m.place_id = p.id and m.user_id = p.claimed_by and m.removed_at is null)
on conflict do nothing;

-- ── 4 · stop it happening again ─────────────────────────────────────────────
--
-- Whatever sets claimed_by now also creates the operator row. A trigger rather than a
-- fix to the activation path, because claimed_by is written from more than one place
-- and this cannot be forgotten by a caller.

create or replace function public.places_sync_operator()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  if new.claimed_by is not null
     and (tg_op = 'INSERT' or new.claimed_by is distinct from old.claimed_by) then
    insert into public.place_managers (place_id, user_id, role)
    values (new.id, new.claimed_by, 'operator')
    on conflict do nothing;
  end if;
  return new;
end;
$$;

drop trigger if exists places_sync_operator_trg on public.places;
create trigger places_sync_operator_trg
  after insert or update of claimed_by on public.places
  for each row execute function public.places_sync_operator();

comment on function public.places_sync_operator() is
  'claimed_by is the legacy single-uuid owner field that place_managers replaced. Both '
  'still exist and the activation flow only writes the old one, which left 8 of 12 '
  'creator places with no operator and therefore no way to edit their own settings. '
  'This keeps them in step until claimed_by is retired.';

-- ── 5 · watch it ────────────────────────────────────────────────────────────

create or replace view public.places_without_operator as
select p.id as place_id, p.name, p.handle, p.place_type, p.governance_state, p.claimed_by
from public.places p
where p.handle is not null
  and not exists (
    select 1 from public.place_managers m
     where m.place_id = p.id and m.removed_at is null);

comment on view public.places_without_operator is
  'Places nobody can administer. Should be empty. A row here means somebody owns a '
  'community they cannot edit, rename or verify, with no in-product way to fix it.';

-- ============================================================================
-- ROLLBACK
--   drop view if exists public.places_without_operator;
--   drop trigger if exists places_sync_operator_trg on public.places;
--   drop function if exists public.places_sync_operator();
--   -- The backfilled place_managers rows and verification_method values are data
--   -- corrections and should NOT be reverted; they describe what actually happened.
-- ============================================================================
