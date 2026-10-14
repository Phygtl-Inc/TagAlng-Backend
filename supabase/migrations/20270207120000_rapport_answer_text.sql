-- ============================================================================
-- rapport: keep the answer beside the question
-- ============================================================================
-- A bare answer ("Pausa in San Mateo, CA") means nothing without the question it
-- answered. Before Lana asks anything new, the worker checks it against what the person
-- already told her (rapport_gaps.already_told): their chat lines AND their earlier
-- answers. Without this column only the question survived, so an answer could not
-- count as something they said.
--
-- Nullable, additive. The worker reads and writes it only when present and falls back
-- without it.
-- ============================================================================

alter table public.rapport_gaps
  add column if not exists answer_text text;

comment on column public.rapport_gaps.answer_text is
  'The person''s own words answering this question (whitespace-squashed, <=500 chars). '
  'Read by the already-told check before new questions are opened.';

-- Backfill: the answer to a question is the person's own chat message sent while it was
-- open: the last user line in their sessions between when it was asked and when it was
-- marked answered (1s slack each side for clock rounding). Checked on prod (todiba):
-- 38 of 48 answered gaps pair this way, every pair the right one. The rest were answered
-- by a tap or off chat and stay null.
update public.rapport_gaps g
   set answer_text = left(regexp_replace(a.content, '\s+', ' ', 'g'), 500)
  from (
    select g2.gap_row_id,
           (select lm.content
              from public.lana_messages lm
              join public.lana_sessions s on s.id = lm.session_id
             where s.user_id = g2.user_id
               and lm.role = 'user'
               and lm.created_at
                   between coalesce(g2.asked_at, g2.chat_asked_at, g2.opened_at) - interval '1 second'
                       and g2.answered_at + interval '1 second'
             order by lm.created_at desc
             limit 1) as content
      from public.rapport_gaps g2
     where g2.status = 'answered'
       and g2.answer_text is null
       and g2.answered_at is not null
  ) a
 where g.gap_row_id = a.gap_row_id
   and a.content is not null;

-- ============================================================================
-- ROLLBACK
--   alter table public.rapport_gaps drop column if exists answer_text;
-- ============================================================================
