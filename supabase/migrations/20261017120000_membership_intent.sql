-- "I'm a member — I go here" vs "Not yet — just curious" (backend asks §19 / issues #84).
--
-- The join sheet asks that question and the wire could not carry the answer, so a
-- curious tap was written as membership: it inflated member_count, put a stranger in
-- member_preview and made them a match candidate for people they have never met.
--
-- WHY A THIRD status AND NOT A NEW COLUMN
--   Every surface that counts or names members already filters status = 'confirmed'
--   (worker _member_rows / _member_counts / caller_affiliation_at, and the SQL in
--   20261004 discover_communities + 20261011 get_peer_profile.communities). A new
--   `membership` column would mean editing each of those to exclude the new value —
--   a third status value is excluded by all of them as they stand. The row is still
--   grounded and still the user's own, so list_my_circles (no status filter) keeps
--   showing it to her, which is exactly the wanted behaviour: the place stays visible
--   to the curious joiner without her belonging to it.
--
--   'curious' rows always carry place_ref (they can only come from a Join tap), so the
--   20260916 "confirmed implies grounded" constraint needs no change.
--
-- ROLLBACK
--   update public.circle_affiliations set status = 'suggested' where status = 'curious';
--   alter table public.circle_affiliations drop constraint circle_affiliations_status_check;
--   alter table public.circle_affiliations add constraint circle_affiliations_status_check
--     check (status in ('suggested','confirmed'));

alter table public.circle_affiliations
  drop constraint if exists circle_affiliations_status_check;

alter table public.circle_affiliations
  add constraint circle_affiliations_status_check
  check (status in ('suggested', 'confirmed', 'curious'));

comment on column public.circle_affiliations.status is
  'suggested = a place mentioned in chat, not yet pinned (internal candidate). '
  'confirmed = membership: counted, named to other members, used for matching. '
  'curious = joined to watch the place ("not yet, just curious"): visible to the owner '
  'in her own list, deliberately excluded from member counts, rosters and matching by '
  'every reader''s status = ''confirmed'' filter.';
