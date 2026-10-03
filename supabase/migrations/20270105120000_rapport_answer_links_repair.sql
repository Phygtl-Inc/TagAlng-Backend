-- Rapport: repair answer links and legacy topics so answered questions stop coming back
--
-- WHY (2026-10-03, Tommaso: "keeps asking the same questions even if answered")
--
--   His account had been asked about his gym EIGHT times since August, answering every time.
--   Two data faults kept the gym thread looking "unasked" to rapport_uncovered_claims:
--
--   1. answer_claim_id pointed at the wrong claim. The worker linked an answer to the user's
--      NEWEST claim, not to the one the answer wrote. When the answer merged into an existing
--      thread, the newest claim was unrelated: "What kind of workouts do you do at the gym?"
--      was linked to "Likes mortadella pizza", "Which gym do you usually go to?" to "Speaks
--      Brazilian Portuguese". So the gym claim never read as covered, and pizza/Portuguese
--      read as covered without ever being asked. Prod: 16 of 39 linked answers, 7 users.
--      The worker now links the row the answer actually wrote (same PR).
--
--   2. Gaps opened before deepens_concept existed carry NULL there, and coverage only reads
--      that column. Their gap_id still names the concept: 'deepen:<concept>'.
--      Prod: 64 answered gaps across 10 users.
--
-- WHAT THIS DOES
--   · fills deepens_concept from the gap id, where it is NULL and the id says 'deepen:…'
--   · clears answer_claim_id where the linked claim already existed BEFORE the question was
--     asked and is about a different concept — the signature of the newest-claim bug. A claim
--     made before the question existed cannot be what the answer produced, unless it is the
--     very thread the question deepened (a merge), which is kept.
--   Nothing is reopened or deleted. Gap status, timestamps and questions are untouched.

-- 1 · legacy topic
update public.rapport_gaps g
   set deepens_concept = substr(g.gap_id, length('deepen:') + 1),
       updated_at = now()
 where g.deepens_concept is null
   and g.gap_id like 'deepen:%'
   and substr(g.gap_id, length('deepen:') + 1) ~ '^[a-z][a-z0-9_]{1,63}$';

-- 2 · links to a claim the answer cannot have produced
update public.rapport_gaps g
   set answer_claim_id = null,
       updated_at = now()
  from public.user_identity_claims c
 where c.id = g.answer_claim_id
   and c.created_at < coalesce(g.asked_at, g.chat_asked_at, g.opened_at)
   and c.concept is distinct from g.deepens_concept;

-- ============================================================================
-- ROLLBACK
--   Not reversible row-for-row (the cleared links were wrong). To undo step 1:
--     update public.rapport_gaps set deepens_concept = null
--      where gap_id like 'deepen:%' and updated_at >= '<apply time>';
-- ============================================================================
