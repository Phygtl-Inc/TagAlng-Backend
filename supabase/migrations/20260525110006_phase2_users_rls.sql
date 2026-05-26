-- TagAlng Phase 2: RLS for public.users

alter table public.users enable row level security;

create policy "users_select_own"
  on public.users for select
  to authenticated
  using (id = auth.uid());

create policy "users_update_own"
  on public.users for update
  to authenticated
  using (id = auth.uid())
  with check (id = auth.uid());

-- Inserts only via handle_new_user / assign_home_block (security definer)
