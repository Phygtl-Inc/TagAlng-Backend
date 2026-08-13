-- ── events · the community a meet was created for (host setup card 2/5) ───────
-- "For one of your communities?" (C-4-EVENT-P2B v0.2.35). A community IS a canonical
-- place here (circle_affiliations.place_ref), so the tag is that place id.
--
-- Distinct from events.place_ref, which is where the meet HAPPENS: a school-community
-- meet can be held at a park. Null = a plain neighborhood meet, which is the default.
--
-- Written post-publish by the worker's service client after it verifies the host is a
-- confirmed member of that community (app/event_place.stamp_event_community) — the
-- picker's value comes from the client, so membership is never trusted from the payload.
-- That is also why create_event needs no change here.

alter table public.events
  add column if not exists circle_place_ref uuid references public.places (id);

comment on column public.events.circle_place_ref is
  'The community this meet was created for — the canonical place of one of the host''s '
  'confirmed circle_affiliations. Optional; null = a plain neighborhood meet. Not the '
  'venue: that is events.place_ref.';

create index if not exists events_circle_place_ref_idx
  on public.events (circle_place_ref)
  where circle_place_ref is not null;
