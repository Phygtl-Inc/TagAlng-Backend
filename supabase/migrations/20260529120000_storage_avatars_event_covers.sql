-- TagAlng v0.1: storage buckets + RLS (buckets may already exist from dashboard)

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values
  ('avatars', 'avatars', true, 2097152, array['image/jpeg', 'image/png', 'image/webp']),
  ('event-covers', 'event-covers', true, 2097152, array['image/jpeg', 'image/png', 'image/webp'])
on conflict (id) do update set
  public = excluded.public,
  file_size_limit = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types;

-- avatars/{user_id}/{filename}
drop policy if exists "avatars_read_public" on storage.objects;
create policy "avatars_read_public"
  on storage.objects for select
  using (bucket_id = 'avatars');

drop policy if exists "avatars_write_owner" on storage.objects;
create policy "avatars_write_owner"
  on storage.objects for insert
  to authenticated
  with check (
    bucket_id = 'avatars'
    and (storage.foldername(name))[1] = auth.uid()::text
  );

drop policy if exists "avatars_update_owner" on storage.objects;
create policy "avatars_update_owner"
  on storage.objects for update
  to authenticated
  using (
    bucket_id = 'avatars'
    and (storage.foldername(name))[1] = auth.uid()::text
  );

-- event-covers/{event_id}/{filename}
drop policy if exists "event_covers_read_public" on storage.objects;
create policy "event_covers_read_public"
  on storage.objects for select
  using (bucket_id = 'event-covers');

drop policy if exists "event_covers_write_host" on storage.objects;
create policy "event_covers_write_host"
  on storage.objects for insert
  to authenticated
  with check (
    bucket_id = 'event-covers'
    and exists (
      select 1
      from public.events e
      where e.id::text = (storage.foldername(name))[1]
        and e.host_id = auth.uid()
    )
  );
