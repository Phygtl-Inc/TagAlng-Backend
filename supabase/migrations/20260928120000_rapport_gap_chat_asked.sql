-- Rapport gaps asked in CHAT must stop being offered as candidate goals.
--
-- QA 2026-08-03: the same gap was asked three turns running, reworded each time
-- ("is there a favorite blue thing…"). The policy pursues gaps via
-- candidate_goals, which reads status='open' — and nothing marked a gap as
-- asked when the chat path used it, so it stayed open and was re-offered every
-- single turn. The loop guard could not see it either: that guard compares
-- whole replies for exact equality, and each re-ask was worded differently.
--
-- Why a new column instead of reusing status='asked': that status means "shown
-- on the home tile and awaiting the user", and rapport_ranker._pending_ask
-- re-shows ANY 'asked' row on the tile verbatim. Reusing it would make every
-- question Lana asked in conversation pop up as a home-screen tile too.
--
-- Kept as a timestamp rather than a flag so the gap can come back after a
-- decent interval if the user never engaged — asked-and-ignored is not the same
-- as answered, and 'answered' is still the right status when they do engage
-- (rapport_gaps.mark_answered).

alter table public.rapport_gaps
  add column if not exists chat_asked_at timestamptz;

comment on column public.rapport_gaps.chat_asked_at is
  'Last time this gap was asked in CONVERSATION (not the home tile). '
  'app/policy/goals.py excludes recently chat-asked gaps from candidate_goals so '
  'the policy cannot re-ask the same question turn after turn. Distinct from '
  'asked_at, which means "currently shown on the home tile".';

-- candidate_goals filters on (user_id, status, chat_asked_at) every turn.
create index if not exists rapport_gaps_user_status_chat_asked_idx
  on public.rapport_gaps (user_id, status, chat_asked_at);

-- Any gap already asked in chat before this shipped is unknown to us: leaving it
-- null is correct (it simply becomes askable once, and is stamped from then on).
