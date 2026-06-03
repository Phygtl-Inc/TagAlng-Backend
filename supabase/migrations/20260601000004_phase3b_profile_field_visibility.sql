-- TagAlng Phase 3b (Optional): Profile field visibility control
-- Purpose: Centralized per-user control over which profile fields are visible
-- Allows users to set visibility (public/mutual/private) for fields like work_history, bio, etc.

-- Table: user_profile_field_visibility
-- Schema: { "field_name": "public|mutual|private", ... }
-- Example: { "home_block_id": "public", "work_history": "private", "bio": "mutual" }
create table if not exists public.user_profile_field_visibility (
  user_id uuid primary key references public.users(id) on delete cascade,
  field_visibility jsonb not null default '{}'::jsonb,
  -- jsonb object: { "field_name": "visibility_level" }
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
comment on table public.user_profile_field_visibility is
  'Per-user visibility preferences for profile fields. Schema: { "field_name": "public"|"mutual"|"private" }. Defaults to empty (all fields private).';
-- Enable RLS on visibility table
alter table public.user_profile_field_visibility enable row level security;
-- Policy: Users read/update their own visibility settings
create policy "pv_select_own"
  on public.user_profile_field_visibility for select
  to authenticated
  using (user_id = auth.uid());
create policy "pv_update_own"
  on public.user_profile_field_visibility for update
  to authenticated
  using (user_id = auth.uid())
  with check (user_id = auth.uid());
create policy "pv_insert_own"
  on public.user_profile_field_visibility for insert
  to authenticated
  with check (user_id = auth.uid());
-- Trigger to auto-update updated_at
create trigger pv_updated_at
before update on public.user_profile_field_visibility
for each row execute function public.set_updated_at();
-- RPC: Set visibility for a single field
create or replace function public.set_profile_field_visibility(
  p_field_name text,
  p_visibility text
)
returns jsonb
language plpgsql
security invoker
set search_path = public
as $$
declare
  updated_visibility jsonb;
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  if p_visibility not in ('public', 'mutual', 'private') then
    raise exception 'invalid_visibility' using errcode = 'P0001';
  end if;

  insert into public.user_profile_field_visibility (user_id, field_visibility)
  values (auth.uid(), jsonb_build_object(p_field_name, p_visibility))
  on conflict (user_id) do update
  set field_visibility = excluded.field_visibility || jsonb_build_object(p_field_name, p_visibility),
      updated_at = now()
  returning field_visibility into updated_visibility;

  return updated_visibility;
end;
$$;
comment on function public.set_profile_field_visibility(text, text) is
  'Set visibility for a profile field. Visibility: public (all see), mutual (matched only), private (owner only).';
grant execute on function public.set_profile_field_visibility(text, text) to authenticated;
-- RPC: Get user's own visibility settings
create or replace function public.get_my_profile_field_visibility()
returns jsonb
language sql
stable
security invoker
set search_path = public
as $$
  select coalesce(field_visibility, '{}'::jsonb)
  from public.user_profile_field_visibility
  where user_id = auth.uid();
$$;
comment on function public.get_my_profile_field_visibility() is
  'Retrieve the current user''s field visibility settings.';
grant execute on function public.get_my_profile_field_visibility() to authenticated;
-- Helper: Check if a field is visible to a viewer
-- Returns true if the field visibility allows the viewer to see it
create or replace function public.is_field_visible_to(
  p_user_id uuid,
  p_field_name text,
  p_viewer_id uuid
)
returns boolean
language plpgsql
security definer
set search_path = public
stable
as $$
declare
  field_visibility text;
begin
  -- Get the visibility setting for this field (default 'private' if not set)
  select coalesce((v.field_visibility->>p_field_name)::text, 'private')
  into field_visibility
  from public.user_profile_field_visibility v
  where v.user_id = p_user_id;

  -- Owner always sees their own field
  if p_user_id = p_viewer_id then
    return true;
  end if;

  -- Public: everyone sees it
  if field_visibility = 'public' then
    return true;
  end if;

  -- Mutual: only if matched
  if field_visibility = 'mutual' then
    return public.are_users_matched(p_user_id, p_viewer_id);
  end if;

  -- Private: no one else sees it
  return false;
end;
$$;
comment on function public.is_field_visible_to(uuid, text, uuid) is
  'Check if a field is visible to a viewer based on privacy settings.';
grant execute on function public.is_field_visible_to(uuid, text, uuid) to authenticated;
