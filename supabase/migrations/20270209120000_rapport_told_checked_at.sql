-- ============================================================================
-- rapport: remember which queued questions were checked against what the person said
-- ============================================================================
-- WHY
--   20270206-08 (PR #241) made Lana check every NEW question against what the person
--   already told her. Questions queued before that shipped were never checked: on prod,
--   2026-10-15, 53 queued questions asked what people had already said ("how often do
--   you play tennis?" to someone who plays every other day).
--
-- WHAT THIS DOES
--   told_checked_at: when the already-told check last judged this question. Null means
--   never. The worker (rapport_gaps.revalidate_queue) checks queued questions where it
--   is null, closes the ones already answered, and stamps the rest; new questions are
--   stamped when they open. Nullable, additive; the worker tolerates its absence.
-- ============================================================================

alter table public.rapport_gaps
  add column if not exists told_checked_at timestamptz;

-- The revalidation read: one user's queued, never-checked questions.
create index if not exists rapport_gaps_unchecked_queue_idx
  on public.rapport_gaps (user_id, opened_at)
  where told_checked_at is null and status in ('open', 'asked');

comment on column public.rapport_gaps.told_checked_at is
  'When the already-told check last judged this question; null = never (revalidate_queue picks it up).';

-- ============================================================================
-- ROLLBACK
--   drop index if exists public.rapport_gaps_unchecked_queue_idx;
--   alter table public.rapport_gaps drop column if exists told_checked_at;
-- ============================================================================
