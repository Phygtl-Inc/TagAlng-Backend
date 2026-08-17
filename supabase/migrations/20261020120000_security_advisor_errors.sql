-- Clear the 4 Security Advisor ERRORs on prod.
--
-- 1. investor_metrics / investor_metrics_v2 are SECURITY DEFINER views (the
--    Postgres default): they run with the owner's rights, so any role holding
--    SELECT reads through RLS. Flip to invoker rights and drop the PostgREST
--    grants — these are dashboard/service_role metrics, not client-facing.
--    _v2 was created by hand on prod and has no migration, hence the guard.
-- 2. _pr11_concepts_before / _pr11_links_before are the rollback snapshots taken
--    by 20260918120000. RLS on, no policies = service_role only. Drop them
--    outright when the PR11 backfill is signed off (see that migration's header).

do $$
declare v text;
begin
  foreach v in array array['investor_metrics', 'investor_metrics_v2'] loop
    if to_regclass('public.' || v) is not null then
      execute format('alter view public.%I set (security_invoker = on)', v);
      execute format('revoke all on public.%I from anon, authenticated', v);
    end if;
  end loop;
end $$;

alter table if exists public._pr11_concepts_before enable row level security;
alter table if exists public._pr11_links_before    enable row level security;
