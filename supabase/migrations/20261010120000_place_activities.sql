-- What people DO at a community, and one emoji for what the place HAS.
--
-- Two gaps the communities surfaces had:
--
--   1. A community was a place + a member count. Nobody could say what they
--      actually do there ("aerobics", "weightlifting"), so the profile could not
--      show "your activities" or what everyone else does at the same spot.
--   2. place_features (has_pool, has_childcare) could only be learned in passing
--      from chat. The panel now lets a member add one directly, and the chips are
--      rendered with an emoji, which had nowhere to live.
--
-- WHY A TABLE AND NOT user_identity_claims.place_ref
--   An activity IS an interest, and adding one still writes the identity claim so
--   it shapes matching (app/place_activities.py). But claims are unique per
--   (user, concept) with a single place_ref, so the same activity at a second
--   community would silently move the tag off the first. The membership edge is
--   many-to-many; this table is that edge, the claim stays the interest.
--
-- DEPLOY ORDER: this migration, then the worker.

create table if not exists public.place_activities (
  id         uuid primary key default gen_random_uuid(),
  place_id   uuid not null references public.places (id) on delete cascade,
  user_id    uuid not null references public.users (id) on delete cascade,
  concept    text not null check (concept ~ '^[a-z][a-z0-9_]{1,63}$'),
  label      text not null check (char_length(label) between 1 and 48),
  created_at timestamptz not null default now(),
  unique (place_id, user_id, concept)
);

comment on table public.place_activities is
  'What one member does at one community ("Aerobics" at OrangeTheory). Written from '
  'the community panel and from the post-grounding "what do you enjoy most at X?" '
  'answer. The matching signal is the mirrored user_identity_claims row — this table '
  'is only the place↔activity edge, which claims cannot hold (one place_ref each).';
comment on column public.place_activities.concept is
  'Slug of the label, shared with the mirrored identity claim so the two line up.';

create index if not exists place_activities_place_idx
  on public.place_activities (place_id);
create index if not exists place_activities_user_idx
  on public.place_activities (user_id);

alter table public.place_activities enable row level security;
-- No policies: reads/writes go through the worker (service role), which re-checks
-- membership on every call, exactly like circle_affiliations.

-- ── one emoji per feature chip ────────────────────────────────────────────────
-- Picked when the feature is written, like circle_affiliations.emoji and
-- events.cover_emoji. Null renders as a plain chip.
alter table public.place_features
  add column if not exists emoji text;

comment on column public.place_features.emoji is
  'One emoji for the "what it has" chip, chosen when the feature is written. Null '
  'on rows learned before this migration — the chip renders without one.';

-- ============================================================================
-- ROLLBACK
--   drop table if exists public.place_activities;
--   alter table public.place_features drop column if exists emoji;
-- ============================================================================
