-- Two more rateable things on the map: a meet (backend-asks §51, MeetPeekCard) and a
-- community (§55, CommunityPeekCard). Both cards end on "Was this rec useful?" and had
-- nowhere to write the thumb.
--
-- Same contract as message / rapport_question / peer_rec (20260828120000,
-- 20261119120000): one row per (user, target), the other thumb flips it in place, the
-- worker deletes the row on 'clear'. The rated text is snapshotted from the DB into
-- content_snapshot at rating time, and the FKs are `on delete set null`, NOT cascade —
-- the feedback (and its snapshot) outlives the event / place row it was about.
alter table public.lana_feedback
  add column if not exists event_id uuid references public.events(id) on delete set null;

alter table public.lana_feedback
  add column if not exists place_id uuid references public.places(id) on delete set null;

alter table public.lana_feedback
  drop constraint if exists lana_feedback_target_kind_check;
alter table public.lana_feedback
  add constraint lana_feedback_target_kind_check
  check (target_kind in ('message', 'rapport_question', 'peer_rec', 'event', 'place'));

-- Partial, like the other three: exactly one target column is set per row, and once the
-- target is deleted the column goes null and the row drops out of the index.
create unique index if not exists lana_feedback_user_event_uq
  on public.lana_feedback (user_id, event_id) where event_id is not null;
create unique index if not exists lana_feedback_user_place_uq
  on public.lana_feedback (user_id, place_id) where place_id is not null;

comment on table public.lana_feedback is
  'Thumbs up/down a user gave a Lana reply (lana_messages), a rapport tile question (rapport_gaps), a fellows rec line (peer_rec_lines), a surfaced meet (events) or a community (places). One row per user+target; cleared ratings are deleted; content_snapshot survives the target being deleted.';
