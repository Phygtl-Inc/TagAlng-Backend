-- Where does this user stand in rapport building?
--
-- Nothing answered that until now. rapport_gaps holds one row per question and
-- user_identity_claims one row per thing known, but no row anywhere says whether a user
-- is a day-old blank or a fully matchable profile — so "how many users are stuck?" was
-- not a query, and a signup who was shown three questions and answered none looked
-- identical to one who signed up an hour ago.
--
-- A VIEW and not a users.rapport_phase column, deliberately. A column needs a writer on
-- mark_answered / record_skip / mute_gap / reconcile_gaps / open_semantic_gap /
-- open_cold_seed_gaps, on every claim write AND every claim dismissal, on
-- circle_affiliations confirming, on all three home_block_id assignment paths, and in
-- plpgsql as well as Python (increment_skip_and_reopen). Miss one and the column lies —
-- dismiss a language claim and the user reads 'speaking' forever. It also needs a
-- backfill, and a second backfill the next time the thresholds move. Derived, the
-- definition lives in one CASE, is retroactively correct for every user who already
-- exists, and cannot drift. Cost is a scan of three tables per read; if that ever bites,
-- this becomes a materialized view plus a nightly refresh and nothing else changes.
--
-- COVERAGE IS THE UNION of answered gaps and claim buckets, which is the one subtle bit.
-- Counting answered gaps alone undercounts badly: reconcile_gaps only retires a gap whose
-- REAL covers_concept the user states, and semantic gaps (now the main opener) carry a
-- synthetic concept that never matches -- so a user who tells Lana everything in chat but
-- never taps the tile grows claims while `answered` stays flat. Counting claim buckets
-- alone undercounts too: user_identity_claims.bucket is nullable (20260604120000) and
-- legacy rows have none, while rapport_gaps.parent_bucket is not null. Each side covers
-- the other's hole.
--
-- This deliberately does NOT agree with app/rapport_priority.covered_buckets(), which is
-- answered-gaps-only. That is correct THERE: an open question in a bucket has not yet
-- produced its claim, so the bucket is still worth asking into. Scoring the next question
-- and reporting what Lana knows are different jobs. Do not unify them.

create or replace view public.user_rapport_state as
with c as (
  select user_id,
         count(*)::int as claims,
         array_remove(array_agg(distinct bucket), null) as claim_buckets,
         bool_or(concept in ('languages_spoken', 'home_language')) as knows_language
  from public.user_identity_claims
  where dismissed_at is null
  group by user_id
), g as (
  select user_id,
         count(*) filter (where status = 'answered')::int as answered,
         count(*) filter (where status = 'open')::int     as open_gaps,
         count(*) filter (where status = 'expired')::int  as expired,
         -- Lana RENDERED a tile. Not that a human saw it: that is Amplitude's
         -- rapport_gap_shown, and it is the one number this view cannot give you.
         count(*) filter (where asked_at is not null)::int as ever_asked,
         coalesce(sum(skipped_count), 0)::int as skips,
         array_remove(
           array_agg(distinct parent_bucket) filter (where status = 'answered'), null
         ) as answered_buckets,
         max(answered_at) as last_answer
  from public.rapport_gaps
  group by user_id
), p as (
  select user_id, count(*)::int as grounded_places
  from public.circle_affiliations
  where grounded and status = 'confirmed'
  group by user_id
)
select
  u.id         as user_id,
  u.created_at,
  coalesce(c.claims, 0)           as claims,
  coalesce(g.answered, 0)         as answered,
  coalesce(g.open_gaps, 0)        as open_gaps,
  coalesce(g.expired, 0)          as expired,
  coalesce(g.ever_asked, 0)       as ever_asked,
  coalesce(g.skips, 0)            as skips,
  cov.buckets                     as covered_buckets,
  coalesce(array_length(cov.buckets, 1), 0) as buckets_covered,
  coalesce(c.knows_language, false)         as knows_language,
  coalesce(p.grounded_places, 0)  as grounded_places,
  (u.home_block_id is not null)   as has_block,
  g.last_answer,
  -- Shown -> answered. A cold user with ever_asked > 0 and answered = 0 means the tile is
  -- firing and the QUESTIONS are failing; cold with open_gaps = 0 means the seeder itself
  -- failed. Those two need opposite fixes and were previously indistinguishable.
  round(100.0 * coalesce(g.answered, 0) / nullif(g.ever_asked, 0), 1) as answer_rate,
  -- Named by what the user HAS, highest first. The ladder is not monotonic -- a user can
  -- ground a place before ever stating a language -- and first-match-wins means the
  -- higher rung labels them. So `stage` is a headline: build real reports on
  -- knows_language / grounded_places / buckets_covered, which are exposed above for
  -- exactly that reason.
  case
    when coalesce(c.claims, 0) = 0 and coalesce(g.answered, 0) = 0 then 'cold'
    when u.home_block_id is not null
     and coalesce(array_length(cov.buckets, 1), 0) >= 5             then 'deep'
    -- Both of the onion matcher's hard gates cleared (a block, plus enough axes to score
    -- on) -- the first day this user can actually be introduced to someone.
    when u.home_block_id is not null
     and coalesce(array_length(cov.buckets, 1), 0) >= 3             then 'matchable'
    -- A confirmed, grounded place is +3 in score_onion_candidates_for_user: the single
    -- largest signal available, worth more than three interests.
    when coalesce(p.grounded_places, 0) > 0                         then 'placed'
    when coalesce(c.knows_language, false)                          then 'speaking'
    else 'seeded'
  end as stage
from public.users u
left join c on c.user_id = u.id
left join g on g.user_id = u.id
left join p on p.user_id = u.id
cross join lateral (
  select array(
    select distinct b
    from unnest(
      coalesce(g.answered_buckets, '{}'::text[]) || coalesce(c.claim_buckets, '{}'::text[])
    ) as b
  ) as buckets
) cov;

comment on view public.user_rapport_state is
  'Rapport funnel, derived (no stored phase column): one row per user with claim/gap '
  'counts, bucket coverage (answered gaps UNION claim buckets), grounded places, '
  'shown->answered rate, and a stage headline. Threshold 3 buckets = matchable mirrors '
  'COLD_SEED_GAP_IDS. Read-only reporting; scoring uses rapport_priority.covered_buckets.';

-- Reporting and admin only. The underlying tables' RLS already blocks client writes, and
-- nothing on the user-facing path needs this -- so no authenticated grant to reason about.
revoke all on public.user_rapport_state from public, anon, authenticated;
grant select on public.user_rapport_state to service_role;
