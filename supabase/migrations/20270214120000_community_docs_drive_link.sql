-- Community knowledge documents from a Google Drive link.
--
-- An owner pastes a link to a Google Doc / Sheet / Slides deck, or to a PDF / DOCX stored
-- in Drive, shared "anyone with the link can view". The worker downloads it through the
-- public export URL, stores the file exactly like an upload, and re-checks the link when it
-- is more than a day old, so edits made in Drive reach Lana without a re-upload. A link the
-- owner un-shares keeps answering from its last good version and shows a sync error.
--
-- WHAT IS NEW on community_documents
--   source_kind      'upload' (default, every existing row) | 'drive'
--   source_url       the link the owner pasted (drive rows only)
--   content_hash     sha256 of the last fetched bytes: an unchanged file skips re-ingest
--   last_synced_at   when the link was last fetched successfully
--   sync_error       the last refresh failure (link_not_public, link_not_found, …), else null

alter table public.community_documents
  add column if not exists source_kind    text not null default 'upload',
  add column if not exists source_url     text,
  add column if not exists content_hash   text,
  add column if not exists last_synced_at timestamptz,
  add column if not exists sync_error     text;

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'community_documents_source_kind_chk'
  ) then
    alter table public.community_documents
      add constraint community_documents_source_kind_chk
      check (source_kind in ('upload', 'drive'));
  end if;
  if not exists (
    select 1 from pg_constraint where conname = 'community_documents_drive_url_chk'
  ) then
    alter table public.community_documents
      add constraint community_documents_drive_url_chk
      check (source_kind <> 'drive' or (source_url is not null and length(source_url) <= 2048));
  end if;
end $$;

-- The lazy refresh looks for a community's stale drive rows on every list / answer.
create index if not exists community_documents_drive_sync_idx
  on public.community_documents (place_id, last_synced_at)
  where source_kind = 'drive';
