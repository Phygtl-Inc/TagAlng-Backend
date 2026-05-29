-- 0010 phase2_security_hardening_part2
-- Cleanup pass for two remaining advisor findings that are NOT intentional:
--   - handle_new_user: trigger-only, should never be callable via RPC
--   - assign_home_block: authenticated-only, was still anon-reachable via PUBLIC default grant

-- handle_new_user: it's a trigger function on auth.users; revoke ALL EXECUTE from API roles.
revoke execute on function public.handle_new_user() from public, anon, authenticated;

-- assign_home_block: explicit grant to authenticated, revoke from public/anon.
revoke execute on function public.assign_home_block(text, double precision, double precision) from public, anon;
grant  execute on function public.assign_home_block(text, double precision, double precision) to authenticated;

-- Also tighten get_my_profile + get_my_identity_claims to authenticated only
-- (they're INVOKER now but anon could still call and get nothing back; let's be explicit).
revoke execute on function public.get_my_profile()          from public, anon;
revoke execute on function public.get_my_identity_claims()  from public, anon;
grant  execute on function public.get_my_profile()          to authenticated;
grant  execute on function public.get_my_identity_claims()  to authenticated;;
