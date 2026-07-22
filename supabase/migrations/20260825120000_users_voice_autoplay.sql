-- Voice autoplay: persist the "read Lana's replies aloud" toggle server-side so it
-- follows the user across devices. TTS itself runs in the browser; the backend only
-- stores the preference (the PWA seeds its lana_voice_autoplay cookie from this).

alter table public.users
  add column if not exists voice_autoplay boolean not null default true;

comment on column public.users.voice_autoplay is
  'Whether Lana''s replies are read aloud in the client (browser TTS). Stored preference only; the worker does not act on it.';

-- Extend the profile header RPC so the preference seeds client settings on bootstrap,
-- same place users.locale is read.
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
    'voice_autoplay', u.voice_autoplay,
    'created_at', u.created_at
  )
  from public.users u
  left join public.blocks b on b.id = u.home_block_id
  where u.id = auth.uid();
$$;

comment on function public.get_my_profile() is
  'Own profile header. handle duplicates nickname for @handle UI. kids_count = stated child count (matching only). voice_autoplay = client TTS preference.';

grant execute on function public.get_my_profile() to authenticated;

-- Setter for the Settings toggle (write-through from the PWA, like setUserLocale).
create or replace function public.set_voice_autoplay(
  p_enabled boolean
)
returns jsonb
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  if p_enabled is null then
    raise exception 'invalid_argument' using errcode = 'P0001';
  end if;

  update public.users
  set voice_autoplay = p_enabled
  where id = auth.uid();

  return jsonb_build_object('voice_autoplay', p_enabled);
end;
$$;

comment on function public.set_voice_autoplay(boolean) is
  'Set the current user''s "read Lana''s replies aloud" preference. RLS (users_update_own) guards the row.';

revoke all on function public.set_voice_autoplay(boolean) from public, anon;
grant execute on function public.set_voice_autoplay(boolean) to authenticated;
