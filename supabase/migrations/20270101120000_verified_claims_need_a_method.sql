-- A verified claim must say how it was verified
--
-- WHY (2026-10-01)
--
--   Six place_claims rows are status='verified' with verification_method null. The
--   count was FIVE on 2026-09-29 and SIX today, so this is not a historical artifact
--   being cleaned up — it is an active practice.
--
--   Five of the six are test rows (Asjid Test, run with asjid, Running with asjid ×2,
--   MrBeast). That is fine and expected.
--
--   The sixth is NOT. `Etiqueta do Reino` — our actual pilot creator — was verified on
--   2026-09-29 with no record of who decided or on what evidence.
--
--   At six rows this is harmless. As the default path it is not, and the default path
--   is what gets copied when volume arrives. A place page is our answer-engine surface;
--   an answer engine that ingests a wrongly-verified place cannot be made to un-ingest
--   it. The asymmetry is the whole argument for gating publication hard.
--
-- WHY NOT VALID
--
--   The six existing rows stay exactly as they are. We do not know what was actually
--   checked for Etiqueta do Reino, and inventing a method retroactively would be worse
--   than leaving the gap visible. NOT VALID enforces the rule on every new and updated
--   row while grandfathering history.
--
--   To adopt the existing rows later, once someone has established what was checked:
--     update public.place_claims set verification_method = '<the real one>' where id = ...;
--     alter table public.place_claims validate constraint place_claims_verified_has_method;

alter table public.place_claims
  add constraint place_claims_verified_has_method
  check (status <> 'verified' or verification_method is not null)
  not valid;

comment on constraint place_claims_verified_has_method on public.place_claims is
  'A claim cannot reach verified without recording how. NOT VALID: six pre-existing rows '
  'are grandfathered, five of them test data and one of them Etiqueta do Reino. Validate '
  'the constraint once those are resolved.';

-- ── visibility · what is outstanding, by name ───────────────────────────────
--
-- A count is ignorable. A list with the pilot creator on it is not.

create or replace view public.claims_missing_method as
select c.id as claim_id,
       p.name  as place_name,
       p.handle,
       p.place_type,
       c.resolved_at,
       -- Test rows are noise; the real ones are the point. Crude on purpose so it is
       -- obvious when it is wrong, rather than quietly filtering something real.
       (p.name ~* '(^|\s)(test|asjid test|mrbeast)' or p.handle ~* 'test') as looks_like_test
from public.place_claims c
join public.places p on p.id = c.place_id
where c.status = 'verified'
  and c.verification_method is null
order by (p.name ~* '(^|\s)(test|asjid test|mrbeast)' or p.handle ~* 'test'), c.resolved_at desc;

comment on view public.claims_missing_method is
  'Verified claims with no recorded method, real ones first. Should trend to zero. If a '
  'non-test row appears here after 2026-10-01 the constraint was bypassed, which means '
  'somebody wrote with a role that skips it.';

-- ============================================================================
-- ROLLBACK
--   drop view if exists public.claims_missing_method;
--   alter table public.place_claims drop constraint if exists place_claims_verified_has_method;
--   No existing row is modified by applying this.
-- ============================================================================
