-- Supply engine №1 · partner-sourced events with honest attribution.
--
-- Post-cleanup the 32827 feed holds ~1-2 real events/week (QA 2026-07-08) — marketplace
-- cold-start is the biggest product risk. Recurring anchor events imported from local
-- institutions (library storytime, YMCA swim) keep the feed alive; they are listed with
-- honest attribution ("via Lake Nona Library") so members never mistake an import for a
-- neighbor-hosted meet.

alter table public.events
  add column if not exists source text not null default 'member',
  add column if not exists source_name text;

-- Guard the enum-ish column (add-if-absent so the migration is re-runnable).
do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname = 'events_source_check' and conrelid = 'public.events'::regclass
  ) then
    alter table public.events
      add constraint events_source_check check (source in ('member', 'partner'));
  end if;
end
$$;

comment on column public.events.source is
  'Where the event came from: member (a neighbor created it in-app) or partner '
  '(imported from a local institution via scripts/import_partner_events.py).';
comment on column public.events.source_name is
  'Human name of the sourcing institution for partner events (e.g. "Lake Nona Library"). '
  'Surfaces as "via {source_name}" attribution in previews/feeds. Null for member events.';

-- The importer's idempotency lookup: all partner events for an institution on a block.
create index if not exists events_partner_source_idx
  on public.events (source_name, starts_at)
  where source = 'partner';
