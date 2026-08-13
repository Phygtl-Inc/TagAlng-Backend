-- ── one community per place, per person ───────────────────────────────────────
-- A community IS a place: public.places.google_place_id is unique, so two communities
-- can never share a place. circle_affiliations is the MEMBERSHIP join — many rows per
-- place is the point (that is what members are). What was never enforced is one row per
-- person per place.
--
-- The active unique index was (user_id, circle_key), so two wordings of one spot both
-- grounded to it: 'st_lukes_church' and 'attends_st_lukes_church' → the same places row.
-- The list then rendered that community twice, and member_count (rows, not people) told
-- one person "2 people". app/circles_flow.ground_affiliation now folds a second grounding
-- into the row already there; this makes that structural.

-- ── 1 · keep the member's words when the survivor row has none ────────────────
with ranked as (
  select id, user_id, place_ref, detail,
         row_number() over (
           partition by user_id, place_ref order by created_at, id
         ) as rn
  from public.circle_affiliations
  where dismissed_at is null and place_ref is not null
)
update public.circle_affiliations a
set detail = d.detail
from (
  select keep.id as keep_id, min(dupe.detail) as detail
  from ranked keep
  join ranked dupe
    on dupe.user_id = keep.user_id
   and dupe.place_ref = keep.place_ref
   and dupe.rn > 1
  where keep.rn = 1 and btrim(coalesce(dupe.detail, '')) <> ''
  group by keep.id
) d
where a.id = d.keep_id and btrim(coalesce(a.detail, '')) = '';

-- ── 2 · soft-dismiss the redundant memberships (same removal path as §G.3) ────
with ranked as (
  select id,
         row_number() over (
           partition by user_id, place_ref order by created_at, id
         ) as rn
  from public.circle_affiliations
  where dismissed_at is null and place_ref is not null
)
update public.circle_affiliations
set dismissed_at = now()
where id in (select id from ranked where rn > 1);

-- ── 3 · and stop it happening again ──────────────────────────────────────────
create unique index if not exists circle_affiliations_user_place_active_idx
  on public.circle_affiliations (user_id, place_ref)
  where dismissed_at is null and place_ref is not null;

comment on index public.circle_affiliations_user_place_active_idx is
  'One membership per person per place. The older (user_id, circle_key) index does not '
  'catch two wordings of the same spot, which is how one community listed twice.';
