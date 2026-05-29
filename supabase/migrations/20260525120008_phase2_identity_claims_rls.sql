-- TagAlng Phase 2: RLS for user_identity_claims

alter table public.user_identity_claims enable row level security;
-- Users read their own claims (worker writes via service_role)
create policy "identity_claims_select_own"
  on public.user_identity_claims for select
  to authenticated
  using (user_id = auth.uid());
-- No direct client insert/update/delete (identity-worker uses service_role)
create policy "identity_claims_no_client_write"
  on public.user_identity_claims for all
  to authenticated
  using (false)
  with check (false);
