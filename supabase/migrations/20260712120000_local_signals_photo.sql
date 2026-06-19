-- Pass-along items (swap_offer) get an optional photo. Adds local_signals.photo_url,
-- a `signal-photos` storage bucket (owner-write, public-read, path {user_id}/...),
-- and threads p_photo_url through save_local_signal so Lana can persist it on save.

-- ---------------------------------------------------------------------------
-- §1 column
-- ---------------------------------------------------------------------------
alter table public.local_signals
  add column if not exists photo_url text;

-- ---------------------------------------------------------------------------
-- §2 storage bucket + RLS (mirrors avatars: public read, owner write by folder)
-- ---------------------------------------------------------------------------
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values
  ('signal-photos', 'signal-photos', true, 2097152,
   array['image/jpeg', 'image/png', 'image/webp'])
on conflict (id) do update set
  public = excluded.public,
  file_size_limit = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types;

-- signal-photos/{user_id}/{filename}
drop policy if exists "signal_photos_read_public" on storage.objects;
create policy "signal_photos_read_public"
  on storage.objects for select
  using (bucket_id = 'signal-photos');

drop policy if exists "signal_photos_write_owner" on storage.objects;
create policy "signal_photos_write_owner"
  on storage.objects for insert
  to authenticated
  with check (
    bucket_id = 'signal-photos'
    and (storage.foldername(name))[1] = auth.uid()::text
  );

drop policy if exists "signal_photos_update_owner" on storage.objects;
create policy "signal_photos_update_owner"
  on storage.objects for update
  to authenticated
  using (
    bucket_id = 'signal-photos'
    and (storage.foldername(name))[1] = auth.uid()::text
  );

-- ---------------------------------------------------------------------------
-- §3 save_local_signal — add p_photo_url (drop old signature first; adding a
-- parameter changes the signature, so create-or-replace alone would overload it)
-- ---------------------------------------------------------------------------
drop function if exists public.save_local_signal(
  text, text, text, text, text, text[], text
);

create or replace function public.save_local_signal(
  p_intent text,
  p_detail_text text,
  p_category text default null,
  p_block_id text default null,
  p_zip text default null,
  p_affinity_tags text[] default '{}',
  p_stage text default null,
  p_photo_url text default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_block text;
  v_row public.local_signals%rowtype;
  v_matches int;
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  if p_intent not in (
    'swap_seek', 'swap_offer', 'meet_seek', 'host_meet', 'tip_seek', 'tip_share'
  ) then
    raise exception 'invalid_intent' using errcode = 'P0001';
  end if;

  if p_detail_text is null or length(trim(p_detail_text)) < 2 then
    raise exception 'detail_required' using errcode = 'P0001';
  end if;

  v_block := coalesce(
    p_block_id,
    (select home_block_id from public.users where id = v_me)
  );

  if v_block is null then
    raise exception 'block_required' using errcode = 'P0001';
  end if;

  insert into public.local_signals (
    user_id, block_id, zip, intent, category, detail_text,
    affinity_tags, stage, photo_url, status, source_surface
  ) values (
    v_me, v_block, p_zip, p_intent, nullif(trim(p_category), ''),
    left(trim(p_detail_text), 500),
    coalesce(p_affinity_tags, '{}'), nullif(trim(p_stage), ''),
    nullif(trim(p_photo_url), ''),
    'listening', 'lana'
  )
  returning * into v_row;

  v_matches := public._match_local_signal(v_row.id);

  return jsonb_build_object(
    'signal_id', v_row.id,
    'intent', v_row.intent,
    'category', v_row.category,
    'detail_text', v_row.detail_text,
    'block_id', v_row.block_id,
    'photo_url', v_row.photo_url,
    'matches_created', v_matches
  );
end;
$$;
