-- Applied directly to PROD by Tommaso on 2026-10-05 21:05 UTC (SJSU events import) and
-- committed here afterwards, verbatim from supabase_migrations.schema_migrations, so the
-- repo's history matches prod's and `supabase db push` stops refusing. Idempotent: prod
-- already has it and will not re-run it; dev and fresh databases get the same columns.

alter table public.events add column if not exists source           text;
alter table public.events add column if not exists source_uid       text;
alter table public.events add column if not exists source_etag      text;
alter table public.events add column if not exists source_rrule     text;
alter table public.events add column if not exists recurrence_label text;

create unique index if not exists events_source_uid_uniq
  on public.events (source, source_uid)
  where source is not null;

comment on column public.events.source           is 'Origin feed id, e.g. sjsu_localist. NULL = created in-app.';
comment on column public.events.source_uid       is 'Stable id from the source feed. Idempotency key with source.';
comment on column public.events.source_etag      is 'Change-detection token from the source for re-sync.';
comment on column public.events.source_rrule     is 'Raw RRULE kept verbatim; recurrence stays NULL for imported rows.';
comment on column public.events.recurrence_label is 'Human-readable recurrence for Lana to speak.';
