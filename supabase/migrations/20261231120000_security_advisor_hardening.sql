-- ============================================================================
-- 20261231120000_security_advisor_hardening.sql
--
-- Target:  rjlcyvwogmfmngemhbmn (dev) + kmetmatfxdkrialwrnzj (prod)
-- Follows: 20261230120000_attester_authority_many (dev head, 2026-09-29)
--
-- WHY
--   Supabase's security advisor flagged `rls_disabled_in_public` on dev
--   (2026-09-27). Running the rest of its lints turned up worse:
--
--   1. public.non_verifying_email_domains (20261228120004) has RLS off and
--      the default full-DML grants to anon/authenticated. Anyone holding the
--      publishable key could DELETE 'gmail.com' and make a free-mail address
--      auto-verify a claim to an organisation — the exact thing the table
--      exists to prevent. Its only reader is email_domain_can_verify(),
--      SECURITY DEFINER, so no client needs direct access.
--
--   2. SECURITY DEFINER functions executable by `anon`. The repo idiom
--          revoke all on function f from public;
--          grant execute on function f to service_role;
--      does NOT remove anon/authenticated: Supabase's default privileges grant
--      EXECUTE to those roles *explicitly*, not via PUBLIC. Worst cases,
--      all verified on dev with only the anon key before this migration:
--        cleanup_stale_anonymous_users(interval)  -> 200. Deletes auth.users.
--             rpc(p_older_than => '0s') wipes every unverified guest.
--        peers_within_radius / match_peers_within_radius / resolve_search_origin
--             treat "auth.uid() is null" as the trusted service role — but a
--             keyless anon call also has a null uid, so they answered for any
--             p_user_id.                                    -> 200
--        user_origin_point(p_user_id)  -> any user's home block centroid/ZIP.
--
--   3. Minor lints: user_rapport_state is a SECURITY DEFINER view (no client
--      grants today, so not exposed), and six functions have no pinned
--      search_path.
--
-- WHAT
--   A. RLS on + revoke client grants on non_verifying_email_domains.
--   B. Internal-only functions (called by other SECURITY DEFINER functions
--      or by the worker on service_client()): revoke anon AND authenticated.
--      Note anonymous sign-in makes `authenticated` reachable by anyone, so
--      it is no barrier for these.
--   C. Signed-in-only functions (use auth.uid(), or sit in `to authenticated`
--      policies): revoke anon only.
--   D. Every trigger function: revoke anon/authenticated. EXECUTE on a
--      trigger function is checked at CREATE TRIGGER, never when it fires.
--   E. security_invoker on user_rapport_state; pin search_path on six fns.
--
--   Deliberately left anon-callable (signed-out surfaces): get_event_preview,
--   get_similar_events, get_cluster_events, get_atlas_snapshot,
--   get_nearby_activities, get_activities_near_point, get_profile_summary,
--   get_peer_profile, event_allows_attendee_share, place_claim_card,
--   resolve_place_handle, search_events_semantic, join_waitlist.
--
-- CALLERS CHECKED (git grep across backend, tagalng-pwa, lana-help,
-- lana-admin-portal, excluding tests):
--   discover_communities_near  -> worker service_client()
--   match_peers_within_radius  -> worker service_client()
--   save_local_signal          -> worker call_rpc(user_jwt)   (authenticated)
--   is_tagalng_admin           -> admin UI after sign-in      (authenticated)
--   get_lana_chat_history      -> PWA, guest has an anonymous session
--   everything else in B       -> only other SECURITY DEFINER functions
--   is_event_host / is_tagalng_admin are used in RLS policies scoped
--   `to authenticated`, so authenticated keeps EXECUTE on them.
--
-- SAFETY
--   Grants/RLS only; no data or function body changes. Idempotent. Functions
--   are looked up by name so every overload is covered and a function absent
--   on one environment is skipped rather than failing the migration.
--
-- ROLLBACK
--   alter table public.non_verifying_email_domains disable row level security;
--   grant execute on function public.<fn>(<args>) to anon, authenticated;
--   alter view public.user_rapport_state set (security_invoker = false);
-- ============================================================================

-- A. ------------------------------------------------------------------------
do $$
begin
  if to_regclass('public.non_verifying_email_domains') is not null then
    alter table public.non_verifying_email_domains enable row level security;
    revoke all on table public.non_verifying_email_domains from anon, authenticated;
  end if;
end $$;

-- B, C, D -------------------------------------------------------------------
do $$
declare
  internal_only text[] := array[
    'cleanup_stale_anonymous_users',
    'user_origin_point',
    'get_user_location_label',
    'peers_within_radius',
    'match_peers_within_radius',
    'resolve_search_origin',
    'resolve_nearest_block_id',
    'discover_communities_near',
    '_joint_moment_candidate_card',
    '_users_share_home_block',
    '_require_verified_neighbor_comms',
    '_match_local_signal',
    'are_users_matched',
    'is_field_visible_to',
    '_signal_match_strength',
    '_tip_match_strength',
    '_place_handle_base',
    '_place_handle_shape_error',
    'normalize_place_handle'
  ];
  signed_in_only text[] := array[
    'auth_is_anonymous',
    'auth_is_phone_verified',
    'is_tagalng_admin',
    'is_event_host',
    'admin_get_lana_conversation',
    'admin_list_lana_sessions',
    'get_active_lana_session',
    'get_lana_chat_history',
    'get_lana_session_messages',
    'get_my_identity_claims',
    'get_my_nudges',
    'get_cluster_peers',
    'save_local_signal',
    'generate_handle'
  ];
  r record;
begin
  for r in
    select p.oid::regprocedure as sig, p.proname,
           pg_get_function_result(p.oid) = 'trigger' as is_trigger
    from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public'
      and p.prokind = 'f'
      and not exists (select 1 from pg_depend d where d.objid = p.oid and d.deptype = 'e')
      and (p.proname = any(internal_only)
           or p.proname = any(signed_in_only)
           or pg_get_function_result(p.oid) = 'trigger')
  loop
    if r.proname = any(signed_in_only) then
      execute format('revoke execute on function %s from public, anon', r.sig);
    else
      execute format('revoke execute on function %s from public, anon, authenticated', r.sig);
    end if;
    execute format('grant execute on function %s to service_role', r.sig);
  end loop;
end $$;

-- E. ------------------------------------------------------------------------
do $$
begin
  if to_regclass('public.user_rapport_state') is not null then
    alter view public.user_rapport_state set (security_invoker = true);
  end if;
end $$;

do $$
declare
  r record;
begin
  for r in
    select p.oid::regprocedure as sig
    from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public'
      and p.proname in ('_joint_moment_demo_user_id', '_relationship_pair',
                        '_signal_match_strength', '_tier_max', '_tier_rank',
                        '_tip_match_strength')
  loop
    execute format('alter function %s set search_path = pg_catalog, public, extensions', r.sig);
  end loop;
end $$;
