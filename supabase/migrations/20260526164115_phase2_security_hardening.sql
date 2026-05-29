-- 0009 phase2_security_hardening
-- Addresses 5 advisor findings from 2026-05-26 review.
-- Conservative: no breaking changes to existing client contracts.

-- 1. Fix mutable search_path on the two helper functions (advisor: function_search_path_mutable)
alter function public.set_updated_at()                set search_path = pg_catalog, public;
alter function public.validate_cohort_ids(text[])     set search_path = pg_catalog, public;

-- 2. Switch "read my own data" RPCs from SECURITY DEFINER to SECURITY INVOKER.
--    Safe because:
--      - users RLS: users_select_own (id = auth.uid())
--      - user_identity_claims RLS: identity_claims_select_own (user_id = auth.uid())
--      - blocks RLS: blocks_select_public (true) -> join in get_my_profile still works
alter function public.get_my_profile()                security invoker;
alter function public.get_my_identity_claims()        security invoker;

-- 3. Lock down internal helpers from anon EXECUTE.
--    bump_block_waitlist_count is trigger-only; revoking EXECUTE does NOT block trigger firing.
revoke execute on function public.bump_block_waitlist_count() from anon, authenticated, public;
--    assign_home_block is for authenticated users only (raises 'not_authenticated' otherwise).
revoke execute on function public.assign_home_block(text, double precision, double precision) from anon;

-- 4. Tighten analytics_events INSERT (advisor: rls_policy_always_true).
--    Non-breaking: nullable session_id still accepted, but lengths capped and user_id consistency enforced.
--    Real rate-limiting still belongs in an edge function / WAF; this is the floor.
drop policy if exists "analytics_insert" on public.analytics_events;
create policy "analytics_insert_bounded"
  on public.analytics_events
  for insert
  to anon, authenticated
  with check (
    char_length(event_name) between 1 and 80
    and (session_id is null or char_length(session_id) between 1 and 100)
    and (user_id is null or user_id = auth.uid())
    and octet_length(coalesce(properties::text, '{}')) <= 4000
  );

-- 5. Tighten waitlist_signups INSERT (advisor: rls_policy_always_true).
--    Requires recaptcha_verified = true. The canonical insert path is the join_waitlist() RPC
--    which sets recaptcha_verified after server-side verification of the captcha token.
--    Direct anon INSERTs that bypass the RPC are now rejected. 0 rows existed at migration time.
drop policy if exists "waitlist_insert_anon" on public.waitlist_signups;
create policy "waitlist_insert_recaptcha_verified"
  on public.waitlist_signups
  for insert
  to anon, authenticated
  with check (recaptcha_verified = true);

-- Note (deferred, requires Azjit's review):
--   - analytics_events still needs IP-based rate limiting in an edge function / WAF layer.
--   - Auth -> Settings: enable "Leaked Password Protection" (HaveIBeenPwned). Dashboard toggle, not SQL.
--   - assign_home_block / get_atlas_snapshot / resolve_nearest_block_id / join_waitlist
--     remain SECURITY DEFINER + anon-executable. These are intentional for the public landing page.;
