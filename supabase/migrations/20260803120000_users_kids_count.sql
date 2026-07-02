-- Kids count: store the NUMBER of children a neighbor states ("I have 2 sons" -> 2).
-- Privacy: count only. We never store a child's name, age, gender, school, or photo.
-- Surfaced in the PWA "Your kids" (privacy-locked, for matching only) section.

alter table public.users
  add column if not exists kids_count smallint
    check (kids_count is null or (kids_count >= 0 and kids_count <= 20));

comment on column public.users.kids_count is
  'Stated number of children (count only, for matching). Never name/age/school/photo.';

-- Extend the profile header RPC so the count flows to the PWA profile dashboard.
create or replace function public.get_my_profile()
returns jsonb
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select jsonb_build_object(
    'id', u.id,
    'full_name', u.full_name,
    'nickname', u.nickname,
    'handle', u.nickname,
    'phone', u.phone,
    'phone_verified_at', u.phone_verified_at,
    'profile_photo_url', u.profile_photo_url,
    'home_block_id', u.home_block_id,
    'home_zip', u.home_zip,
    'block_display_name', b.display_name,
    'block_state', b.state,
    'cluster_id', b.cluster_id,
    'home_location_visibility', u.home_location_visibility::text,
    'locale', u.locale,
    'kids_count', u.kids_count,
    'created_at', u.created_at
  )
  from public.users u
  left join public.blocks b on b.id = u.home_block_id
  where u.id = auth.uid();
$$;

comment on function public.get_my_profile() is
  'Own profile header. handle duplicates nickname for @handle UI. kids_count = stated child count (matching only).';
