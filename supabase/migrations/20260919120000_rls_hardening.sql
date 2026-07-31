-- ============================================================================
-- 20260919120000_rls_hardening.sql
--
-- Target:  Supabase kmetmatfxdkrialwrnzj (tagalng-prod) + rjlcyvwogmfmngemhbmn (dev)
-- Follows: 20260918120000_identity_concepts_embedding_repair — verified as the
--          latest row in prod's supabase_migrations.schema_migrations on
--          2026-07-31 via `scripts/db-push.sh prod --list`.
--
-- WHY
--   Two public-schema tables ship with RLS *disabled*. In Supabase the default
--   grants give `anon` and `authenticated` full DML on every table in `public`;
--   RLS is the only thing that gates them. With RLS off, both tables are
--   readable AND writable by anyone holding the publishable (anon) API key,
--   which ships in the client bundle by design.
--
--     public.simulations    — persona-sim transcripts: verbatim user utterances
--                             in transcript_json, plus sft_messages /
--                             judge_summary. Verified: rls off, 0 policies.
--     public.zip_centroids  — ZIP5 -> approximate centroid reference data.
--                             Verified: rls off, 0 policies.
--
--   Grants on BOTH tables before this migration (identical):
--     anon:          SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
--     authenticated: SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
--
--   Exposure confirmed live against PROD on 2026-07-31 with the prod anon key
--   (read-only probes, no writes attempted):
--     GET /rest/v1/simulations?select=id    -> HTTP 200, []   (table is empty)
--     GET /rest/v1/zip_centroids?select=zip5 -> HTTP 200, rows returned
--   simulations is empty today, so no utterance is exposed *yet*; the hole
--   becomes a live data leak the first time the sim harness runs against prod.
--   zip_centroids reads are harmless (non-PII), but the WRITE side is not:
--   get_blocks_near_zip and the event geocoders resolve locations through it,
--   so truncating or poisoning it breaks location resolution product-wide.
--
-- SAFETY
--   * Non-destructive: no DROP TABLE, no data touched, no column changed.
--   * Idempotent: ENABLE RLS is a no-op when already on; policies are
--     DROP ... IF EXISTS then CREATE; REVOKE is idempotent by definition.
--   * service_role and the table owner BYPASS RLS. Verified unaffected callers:
--       - services/lana-worker/app/places.py:37  -> _zip_centroid(service_client(), ...)
--       - services/lana-worker/app/event_location.py:127 -> sb = service_client()
--       - public.get_blocks_near_zip      (security definer)
--       - public.auto_create_block_for_zip (security definer)
--       - the geocoder blocks in 20260829120000_event_has_time / 20260911120000_host_rsvp
--     The admin portal reads via SUPABASE_SERVICE_ROLE_KEY
--     (lana-admin-portal/src/lib/admin-data.ts), so it is unaffected too.
--   * No client reads either table via PostgREST. tagalng-pwa has zero call
--     sites; the only hit is a generated type in database.types.ts.
--
-- ROLLBACK: see docs/prs/PR12_rls_hardening.md §6.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. public.simulations — admin-only read, no client writes
-- ----------------------------------------------------------------------------

alter table public.simulations enable row level security;

-- Admin-only SELECT. public.is_tagalng_admin() is SECURITY DEFINER STABLE and
-- resolves auth.uid() against public.admin_allowlist.
-- admin_allowlist has 0 rows in prod today, so this policy currently denies
-- everyone — the correct fail-closed default. It is NOT a blocker: the portal
-- reads as service_role, and lana-admin-portal/src/lib/ensure-admin.ts upserts
-- any signed-in @phygtl.com account into admin_allowlist on login, so the table
-- self-populates. No manual enrolment step is required.
drop policy if exists "simulations_admin_read" on public.simulations;
create policy "simulations_admin_read"
  on public.simulations
  for select
  to authenticated
  using (public.is_tagalng_admin());

-- Deliberately NO insert/update/delete policy: simulations are written by the
-- nightly sim cron via service_role, which bypasses RLS.
--
-- FUTURE WRITE PATH — read before building /admin/sims.
--   20260730120000_simulations.sql describes the table as written by the cron
--   "and the admin UI" (the HITL review fields: hitl_status, tim_verdict,
--   tim_note, sft_eligible). That UI does not exist yet — lana-admin-portal has
--   no /admin/sims route, so nothing breaks today. When it is built it must
--   write through a server route on the service-role key (the pattern already
--   used by src/lib/admin-data.ts), NOT through the cookie-bound browser client
--   (src/lib/supabase/client.ts), which acts as `authenticated` and would be
--   blocked by the revoke below. If a browser-side write is ever genuinely
--   required, add a narrow policy instead of re-granting broadly:
--     create policy "simulations_admin_review" on public.simulations
--       for update to authenticated
--       using (public.is_tagalng_admin()) with check (public.is_tagalng_admin());
--     grant update on public.simulations to authenticated;

-- Defence in depth: strip the default DML grants so a future accidental
-- `alter table ... disable row level security` cannot re-expose write access.
revoke insert, update, delete, truncate on public.simulations from anon, authenticated;

-- anon must never read verbatim utterances, policy or no policy.
revoke select on public.simulations from anon;

comment on table public.simulations is
  'Persona simulation runs incl. verbatim user utterances in transcript_json. '
  'RLS: admin-only SELECT via is_tagalng_admin(); writes are service_role-only '
  '(see 20260919120000_rls_hardening.sql before adding an admin-UI write path).';

-- ----------------------------------------------------------------------------
-- 2. public.zip_centroids — readable reference data, not writable
-- ----------------------------------------------------------------------------

alter table public.zip_centroids enable row level security;

-- Low-sensitivity public reference data (ZIP5 -> approximate centroid; the
-- existing table comment already states "Not user PII; seed/expand per market").
-- Signed-in clients may read it; nobody but service_role may write it.
drop policy if exists "zip_centroids_read" on public.zip_centroids;
create policy "zip_centroids_read"
  on public.zip_centroids
  for select
  to authenticated
  using (true);

-- Writes stay service_role-only: auto_create_block_for_zip() upserts centroids
-- and is SECURITY DEFINER, so it is unaffected.
revoke insert, update, delete, truncate on public.zip_centroids from anon, authenticated;

-- anon retains its SELECT grant but is covered by no policy, so RLS denies it.
-- Intentional: if a pre-auth block picker ever needs centroids, add `to anon`
-- to the policy above rather than re-granting DML.

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
--   select table_name, grantee, privilege_type
--   from information_schema.role_table_grants
--   where table_schema = 'public'
--     and table_name in ('simulations','zip_centroids')
--     and grantee in ('anon','authenticated')
--   order by table_name, grantee, privilege_type;
--
--   Expected:
--     simulations   anon          REFERENCES, TRIGGER
--     simulations   authenticated REFERENCES, SELECT, TRIGGER
--     zip_centroids anon          REFERENCES, SELECT, TRIGGER
--     zip_centroids authenticated REFERENCES, SELECT, TRIGGER
--
--   No INSERT / UPDATE / DELETE / TRUNCATE remains for either client role.
-- ============================================================================
