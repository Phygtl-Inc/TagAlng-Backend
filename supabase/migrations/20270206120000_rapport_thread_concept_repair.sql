-- ============================================================================
-- rapport: questions point at the claim they deepen, by its real concept
-- ============================================================================
-- WHY
--   Tommaso (todiba) answered a fig-and-gorgonzola pizza question at Pausa on Sep 15 and
--   was asked "which pizzeria do you get your fig and gorgonzola pizza from?" on Oct 12.
--
--   The extractor's per-message follow-up gaps were opened with gap_id
--   'deepen:<slug of the claim LABEL>' and no deepens_concept. 20270105120000 then filled
--   deepens_concept from that gap_id, so it holds the label slug, not the claim's concept:
--     label "Likes fig and gorgonzola pizza" → likes_fig_and_gorgonzola_pizza
--     claim concept                           → fig_gorgonzola_pizza_fan
--   Coverage (rapport_uncovered_claims) matches on the concept, so the answered thread read
--   as never asked. The same migration's step 2 also cleared the answer link, because the
--   claim's concept "differed" from the label slug. That removed the last thing marking the
--   thread covered; 3 hours later the synth opened the repeat question.
--
--   Prod at writing: 96 gaps on 9 users point at a label slug (34 answered), and 6 queued
--   questions re-ask a thread that was already answered.
--
-- WHAT THIS DOES
--   1 · deepens_concept := the claim's real concept, where the stored value matches no
--       claim of that user and their claims with that label slug share exactly ONE concept.
--   2 · answer_claim_id := that claim, on ANSWERED gaps repaired in step 1 that have no
--       link. The question was written to deepen that claim and they answered it.
--   3 · queued (open/asked) questions on a thread that already has an answered question
--       → 'expired'. Nothing is deleted; the rows stay for history.
--   The worker now records deepens_concept on these gaps itself (same PR).
-- ============================================================================

-- Plain temp table, dropped below: `on commit drop` would vanish at once under autocommit.
create temporary table _rapport_concept_fix as
select g.gap_row_id, g.status, g.answer_claim_id, m.claim_id, m.concept
  from public.rapport_gaps g
  join lateral (
    -- One concept, possibly several rows of it (a dismissed copy beside the live one):
    -- link the live row, else the newest.
    select (array_agg(c.id order by (c.dismissed_at is null) desc, c.created_at desc))[1]
             as claim_id,
           min(c.concept) as concept,
           count(distinct c.concept) as n
      from public.user_identity_claims c
     where c.user_id = g.user_id
       -- the slug the worker used: rapport_gaps._slug (lower, non-alnum → '_', strip, 48)
       and left(btrim(regexp_replace(lower(c.label), '[^a-z0-9]+', '_', 'g'), '_'), 48)
           = g.deepens_concept
  ) m on m.n = 1
 where g.deepens_concept is not null
   and not exists (
     select 1 from public.user_identity_claims c2
      where c2.user_id = g.user_id and c2.concept = g.deepens_concept
   );

-- 1 · real concept
update public.rapport_gaps g
   set deepens_concept = f.concept,
       updated_at = now()
  from _rapport_concept_fix f
 where g.gap_row_id = f.gap_row_id;

-- 2 · restore the answer link
update public.rapport_gaps g
   set answer_claim_id = f.claim_id
  from _rapport_concept_fix f
 where g.gap_row_id = f.gap_row_id
   and f.status = 'answered'
   and f.answer_claim_id is null;

-- 3 · queued repeats of an answered thread
update public.rapport_gaps q
   set status = 'expired',
       updated_at = now()
 where q.status in ('open', 'asked')
   and q.deepens_concept is not null
   and exists (
     select 1 from public.rapport_gaps a
      where a.user_id = q.user_id
        and a.deepens_concept = q.deepens_concept
        and a.status = 'answered'
        and a.gap_row_id <> q.gap_row_id
   );

drop table _rapport_concept_fix;

-- ============================================================================
-- ROLLBACK
--   Not reversible row-for-row: the old values were label slugs that match no claim.
--   To reopen step 3's rows:
--     update public.rapport_gaps set status = 'open'
--      where status = 'expired' and updated_at >= '<apply time>' and answered_at is null;
-- ============================================================================
