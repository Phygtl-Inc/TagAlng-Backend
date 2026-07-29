-- Circles: a community's place is MANDATORY (2026-07-28 product decision).
--
-- A "community" — anything user-visible, matchable, or counted — is a
-- circle_affiliations row with status='confirmed', and confirmation now REQUIRES
-- a canonical place (place_ref). Ungrounded rows (place_ref null) remain legal
-- but are internal candidates only: parked chat mentions / invite self-confirms
-- that exist solely so Lana can ask "which spot is it?" and ground them. They are
-- no longer served as communities anywhere (worker: list_my_circles filters them;
-- /lana/circles/add rejects place-less profile adds with place_required).
--
-- The single writer of status='confirmed' (circles_flow.ground_affiliation) has
-- always set place_ref in the same update, so the repair below is expected to
-- touch zero rows — it exists so the constraint can never fail to apply.

update public.circle_affiliations
   set status = 'suggested'
 where status = 'confirmed'
   and place_ref is null;

alter table public.circle_affiliations
  add constraint circle_affiliations_confirmed_has_place
  check (status <> 'confirmed' or place_ref is not null);

comment on table public.circle_affiliations is
  'A user''s communities. A community REQUIRES a canonical place: status=''confirmed'' '
  'implies place_ref is set (circle_affiliations_confirmed_has_place). Rows with '
  'place_ref null are internal grounding candidates, never user-visible communities. '
  'Only confirmed rows feed matching and member counts.';
