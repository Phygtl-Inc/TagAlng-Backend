-- ============================================================================
-- 20260917150000_rls_hardening.sql
--
-- Repo path: supabase/migrations/20260917150000_rls_hardening.sql
-- Target:    Supabase kmetmatfxdkrialwrnzj (tagalng-prod)
-- Follows:   20260916120000_circle_place_mandatory (latest applied)
--
-- WHY
--   Two public-schema tables ship with RLS *disabled*. In Supabase, the default
--   grants give `anon` and `authenticated` full DML on every table in `public`;
--   RLS is the only thing that gates them. With RLS off, both tables are
--   readable AND writable by anyone holding the publishable (anon) API key.
--
--     public.simulations    — holds verbatim user utterances in transcript_json
--                             (persona sims, judge output, SFT messages).
--                             Verified: rls_enabled = false, 0 policies.
--     public.zip_centroids  — ZIP5 -> approximate centroid reference data.
--                             Verified: rls_enabled = false, 0 policies.
--
--   Verified grants before this migration (both tables, identical):
--     anon:          SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
--     authenticated: SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
--
-- SAFETY
--   * Non-destructive: no DROP TABLE, no data touched, no column changed.
--   * Idempotent: ENABLE RLS is a no-op when already on; every policy is
--     DROP ... IF EXISTS then CREATE; REVOKE is idempotent by definition.
--     Verified by running the whole file twice inside one transaction.
--   * service_role and the table owner BYPASS RLS. The lana-worker uses
--     service_client() (service_role) for every read/write of these tables, and
--     all SQL readers of zip_centroids are SECURITY DEFINER functions
--     (get_blocks_near_zip, the event/RSVP geocoders, ...). Neither path is
--     affected by this migration.
--   * No frontend code path reads either table via PostgREST. Searched
--     Phygtl-Inc/tagalng-pwa for `zip_centroids` and `simulations`: 0 hits.
--
-- ROLLBACK: see PR12_rls_hardening.md §6.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. public.simulations — admin-only read, no client writes
-- ----------------------------------------------------------------------------

alter table public.simulations enable row level security;

-- Admin-only SELECT. public.is_tagalng_admin() is SECURITY DEFINER STABLE and
-- resolves auth.uid() against public.admin_allowlist:
--   select exists (select 1 from public.admin_allowlist a where a.user_id = auth.uid());
-- NOTE: admin_allowlist currently has 0 rows in prod, so this policy denies
-- everyone until an admin is enrolled. That is the intended fail-closed default.
drop policy if exists "simulations_admin_read" on public.simulations;
create policy "simulations_admin_read"
  on public.simulations
  for select
  to authenticated
  using (public.is_tagalng_admin());

-- Deliberately NO insert/update/delete policy: simulations are written only by
-- the sim harness via service_role, which bypasses RLS.

-- Defence in depth: strip the default DML grants so a future accidental
-- `alter table ... disable row level security` cannot re-expose write access.
revoke insert, update, delete, truncate on public.simulations from anon, authenticated;

-- anon must never read verbatim utterances, policy or no policy.
revoke select on public.simulations from anon;

comment on table public.simulations is
  'Persona simulation runs incl. verbatim user utterances in transcript_json. '
  'RLS: admin-only SELECT via is_tagalng_admin(); writes are service_role-only.';


-- ----------------------------------------------------------------------------
-- 2. public.zip_centroids — readable reference data, not writable
-- ----------------------------------------------------------------------------

alter table public.zip_centroids enable row level security;

-- Low-sensitivity public reference data (ZIP5 -> approximate centroid; the
-- table comment already states "Not user PII; seed/expand per market").
-- Signed-in clients may read it; nobody but service_role may write it.
drop policy if exists "zip_centroids_read" on public.zip_centroids;
create policy "zip_centroids_read"
  on public.zip_centroids
  for select
  to authenticated
  using (true);

-- Writes stay service_role-only: auto_create_block_for_zip() upserts centroids
-- and runs SECURITY DEFINER, so it is unaffected.
revoke insert, update, delete, truncate on public.zip_centroids from anon, authenticated;

-- anon retains SELECT at the grant level but is NOT covered by any policy, so
-- RLS denies it. Left this way intentionally: if a pre-auth block picker ever
-- needs centroids, add `to anon` to the policy above rather than re-granting DML.

comment on table public.zip_centroids is
  'ZIP5 -> approximate centroid for block picker. Not user PII; seed/expand per market. '
  'RLS: SELECT for authenticated; writes are service_role-only.';


-- ============================================================================
-- POST-CONDITIONS (assert after apply)
--
--   select c.relname, c.relrowsecurity as rls,
--          (select count(*) from pg_policy p where p.polrelid = c.oid) as policies
--   from pg_class c join pg_namespace n on n.oid = c.relnamespace
--   where n.nspname = 'public' and c.relname in ('simulations','zip_centroids');
--
--   Expected:  simulations   | t | 1
--              zip_centroids | t | 1
--
--   select grantee, privilege_type
--   from information_schema.role_table_grants
--   where table_schema = 'public'
--     and table_name in ('simulations','zip_centroids')
--     and grantee in ('anon','authenticated')
--   order by table_name, grantee, privilege_type;
--
--   Expected (verified in a rolled-back PROD transaction):
--     simulations   anon          REFERENCES, TRIGGER
--     simulations   authenticated REFERENCES, SELECT, TRIGGER
--     zip_centroids anon          REFERENCES, SELECT, TRIGGER
--     zip_centroids authenticated REFERENCES, SELECT, TRIGGER
--
--   No INSERT / UPDATE / DELETE / TRUNCATE remains for either client role.
-- ============================================================================
